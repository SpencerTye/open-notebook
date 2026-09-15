"""
LLM control router. LOCAL addition to Open Notebook, not part of upstream.

Lets the web app see which models the llama.cpp server (``llama-server`` in
router mode) currently holds in GPU memory, and load or unload them on demand.
The router talks to llama-server's own management API, documented in
llama.cpp ``tools/server/README.md`` under "Router mode":

- ``GET  /models``          list models with ``status.value`` of
                            loaded | loading | unloaded | sleeping | downloading
- ``POST /models/load``     ``{"model": "<id>"}``
- ``POST /models/unload``   ``{"model": "<id>"}``

GPU memory comes from ``nvidia-smi`` when the container has the GPU (it does
in this workspace: see the ``deploy`` block in docker-compose.yml).

Endpoints (all under ``/api``):

- ``GET  /llm/status``  models with their load state, plus GPU memory in use
- ``POST /llm/load``    ``{"model": id}``
- ``POST /llm/unload``  ``{"model": id}``

Kept self-contained (no ``open_notebook`` imports) so it can be unit-tested
outside the container: see ``custom/tests/test_llm_control.py``.

Configuration: ``LLAMA_SERVER_URL`` (default ``http://host.docker.internal:8080``).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from typing import AsyncIterator, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

router = APIRouter(prefix="/llm", tags=["llm-control"])

DEFAULT_SERVER_URL = "http://host.docker.internal:8080"
# A load request may block until the weights are in memory (about 30 s for a
# 14 GB model from a warm disk cache); give it room.
ROUTER_TIMEOUT_SECONDS = 180.0
NVIDIA_SMI_TIMEOUT_SECONDS = 5.0


class ModelState(BaseModel):
    id: str
    status: str = Field(
        description="loaded | loading | unloaded | sleeping | downloading | unknown"
    )
    failed: bool = Field(False, description="The last load attempt failed")
    is_embedding: bool = Field(False, description="Started with --embeddings")
    size_bytes: Optional[int] = Field(
        None, description="Weights size, only while loaded"
    )
    n_ctx: Optional[int] = Field(None, description="Context size, only while loaded")
    input_modalities: List[str] = Field(default_factory=list)


class GpuMemory(BaseModel):
    used_mib: int
    total_mib: int


class LlmStatusResponse(BaseModel):
    server_url: str
    reachable: bool
    error: Optional[str] = None
    models: List[ModelState] = Field(default_factory=list)
    gpu: Optional[GpuMemory] = None


class ModelActionRequest(BaseModel):
    model: str = Field(
        ..., min_length=1, description="Model id as listed by GET /llm/status"
    )


class ModelActionResponse(BaseModel):
    success: bool
    model: str
    status: str = Field(
        description="Status reported by the model server right after the action"
    )


def get_server_url() -> str:
    return os.environ.get("LLAMA_SERVER_URL", DEFAULT_SERVER_URL).rstrip("/")


async def get_http_client() -> AsyncIterator[httpx.AsyncClient]:
    """One client per request, pointed at the model server. Overridden in tests."""
    async with httpx.AsyncClient(
        base_url=get_server_url(), timeout=ROUTER_TIMEOUT_SECONDS
    ) as client:
        yield client


def parse_models(payload: dict) -> List[ModelState]:
    """Turn llama-server's ``GET /models`` answer into ``ModelState`` rows."""
    models: List[ModelState] = []
    for item in payload.get("data") or []:
        status = item.get("status") or {}
        args = status.get("args") or []
        meta = item.get("meta") or {}
        architecture = item.get("architecture") or {}
        models.append(
            ModelState(
                id=str(item.get("id", "")),
                status=str(status.get("value") or "unknown"),
                failed=bool(status.get("failed", False)),
                is_embedding="--embeddings" in args,
                size_bytes=meta.get("size"),
                n_ctx=meta.get("n_ctx"),
                input_modalities=list(architecture.get("input_modalities") or []),
            )
        )
    return models


def parse_nvidia_smi(output: str) -> Optional[GpuMemory]:
    """Parse ``nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits``."""
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        return None
    parts = [part.strip() for part in lines[0].split(",")]
    if len(parts) < 2:
        return None
    try:
        return GpuMemory(used_mib=int(parts[0]), total_mib=int(parts[1]))
    except ValueError:
        return None


def _run_nvidia_smi() -> Optional[str]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [
                exe,
                "--query-gpu=memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=NVIDIA_SMI_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug(f"nvidia-smi failed: {exc}")
        return None
    if proc.returncode != 0:
        logger.debug(f"nvidia-smi exit {proc.returncode}: {proc.stderr.strip()}")
        return None
    return proc.stdout


async def get_gpu_memory() -> Optional[GpuMemory]:
    """GPU memory in use, or None when nvidia-smi is unavailable. Overridden in tests."""
    output = await asyncio.to_thread(_run_nvidia_smi)
    return parse_nvidia_smi(output) if output else None


async def _fetch_models(client: httpx.AsyncClient) -> List[ModelState]:
    response = await client.get("/models")
    response.raise_for_status()
    return parse_models(response.json())


@router.get("/status", response_model=LlmStatusResponse)
async def get_llm_status(
    client: httpx.AsyncClient = Depends(get_http_client),
    gpu: Optional[GpuMemory] = Depends(get_gpu_memory),
) -> LlmStatusResponse:
    """What the model server holds right now, plus GPU memory in use."""
    server_url = get_server_url()
    try:
        models = await _fetch_models(client)
    except httpx.HTTPError as exc:
        message = str(exc) or exc.__class__.__name__
        logger.warning(f"Model server at {server_url} not reachable: {message}")
        return LlmStatusResponse(
            server_url=server_url, reachable=False, error=message, gpu=gpu
        )
    return LlmStatusResponse(
        server_url=server_url, reachable=True, models=models, gpu=gpu
    )


async def _perform(
    action: str, model: str, client: httpx.AsyncClient
) -> ModelActionResponse:
    server_url = get_server_url()
    try:
        known = {m.id for m in await _fetch_models(client)}
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"Model server unreachable at {server_url}: {exc}"
        ) from exc
    if model not in known:
        raise HTTPException(
            status_code=404,
            detail=f"Model '{model}' is not known to the model server (known: {sorted(known)})",
        )

    logger.info(f"LLM control: {action} {model} via {server_url}")
    try:
        response = await client.post(f"/models/{action}", json={"model": model})
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"Model server request failed: {exc}"
        ) from exc
    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Model server refused to {action} '{model}': "
                f"HTTP {response.status_code} {response.text[:300]}"
            ),
        )

    # Report what the server says now. A load usually shows as "loading" for a
    # while; the UI keeps polling GET /llm/status until it settles.
    try:
        after = {m.id: m for m in await _fetch_models(client)}
        status = after[model].status if model in after else "unknown"
    except httpx.HTTPError:
        status = "unknown"
    return ModelActionResponse(success=True, model=model, status=status)


@router.post("/load", response_model=ModelActionResponse)
async def load_model(
    request: ModelActionRequest,
    client: httpx.AsyncClient = Depends(get_http_client),
) -> ModelActionResponse:
    """Ask the model server to load a model into GPU memory."""
    return await _perform("load", request.model, client)


@router.post("/unload", response_model=ModelActionResponse)
async def unload_model(
    request: ModelActionRequest,
    client: httpx.AsyncClient = Depends(get_http_client),
) -> ModelActionResponse:
    """Ask the model server to unload a model and free its GPU memory."""
    return await _perform("unload", request.model, client)
