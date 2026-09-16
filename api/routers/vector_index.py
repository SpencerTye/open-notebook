"""LOCAL: endpoints for the TurboVec vector index.

The API process owns the index. ``upsert`` and ``remove`` are what the worker
calls after it writes or deletes vectors (see
``open_notebook/vector_index/client.py``); ``status`` shows the state and the
counts; ``rebuild`` throws the index away and builds it again from the
database, in the background, no model needed.

The router reaches the store only through ``_store`` so it can be unit-tested
on the host with a fake.
"""

import asyncio
from typing import Any, Dict, List, Set

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

router = APIRouter()

_TASKS: Set["asyncio.Task"] = set()


class UpsertRequest(BaseModel):
    ids: List[str] = Field(
        default_factory=list,
        description="Record ids whose vectors were written (source_embedding, source_insight, note)",
    )
    replace_sources: List[str] = Field(
        default_factory=list,
        description="Sources whose previous chunk entries are dropped first (re-embedded sources)",
    )


class RemoveRequest(BaseModel):
    ids: List[str] = Field(default_factory=list, description="Deleted record ids")
    sources: List[str] = Field(
        default_factory=list,
        description="Deleted sources: every chunk and insight entry of theirs is dropped",
    )


class UpsertResponse(BaseModel):
    added: int
    removed: int
    missing: int


class RemoveResponse(BaseModel):
    removed: int


class RebuildResponse(BaseModel):
    started: bool
    state: str


def _store():
    from open_notebook.vector_index import store as store_module

    return store_module.get()


def _require_store():
    store = _store()
    if store is None:
        raise HTTPException(
            status_code=503, detail="The vector index is not open in this process"
        )
    return store


@router.get("/vector-index/status")
async def get_status() -> Dict[str, Any]:
    """State, engine, folder and per-kind counts of the index."""
    return _require_store().status()


@router.post("/vector-index/upsert", response_model=UpsertResponse)
async def upsert(request: UpsertRequest) -> UpsertResponse:
    """Read the vectors of the given records from the database into the index."""
    store = _require_store()
    try:
        result = await store.upsert(
            request.ids, replace_sources=request.replace_sources
        )
    except Exception as e:
        logger.error(f"vector index upsert failed: {e}")
        raise HTTPException(status_code=500, detail=f"vector index upsert failed: {e}")
    return UpsertResponse(**result)


@router.post("/vector-index/remove", response_model=RemoveResponse)
async def remove(request: RemoveRequest) -> RemoveResponse:
    """Drop the given records, and every chunk and insight of the given sources."""
    store = _require_store()
    try:
        result = await store.remove(ids=request.ids, sources=request.sources)
    except Exception as e:
        logger.error(f"vector index remove failed: {e}")
        raise HTTPException(status_code=500, detail=f"vector index remove failed: {e}")
    return RemoveResponse(**result)


@router.post("/vector-index/rebuild", response_model=RebuildResponse)
async def rebuild() -> RebuildResponse:
    """Rebuild the whole index from the database, in the background."""
    store = _require_store()
    if store.state in ("checking", "rebuilding"):
        raise HTTPException(
            status_code=409,
            detail=f"The vector index is busy ({store.state}); try again when it is ready",
        )

    async def run() -> None:
        try:
            await store.rebuild()
        except Exception as e:
            logger.error(f"vector index rebuild failed: {e}")

    task = asyncio.create_task(run())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return RebuildResponse(started=True, state=store.state)
