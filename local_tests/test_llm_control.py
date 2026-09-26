"""Tests for the LLM control router (api/routers/llm_control.py).

The router is loaded straight from its file so these tests need only FastAPI,
httpx and pydantic, not the whole Open Notebook backend. Run from the
workspace root with a venv that has pytest:

    python -m pytest local_tests -q
"""

import importlib.util
import json
import sys
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "api"
    / "routers"
    / "llm_control.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("llm_control", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["llm_control"] = module
    spec.loader.exec_module(module)
    return module


# Trimmed from a real `GET /models` answer of llama-server b10970 in router mode.
ROUTER_MODELS_PAYLOAD = {
    "data": [
        {
            "id": "gemma-4-26b-a4b",
            "object": "model",
            "status": {
                "value": "loaded",
                "args": [
                    "llama-server.exe",
                    "--ctx-size",
                    "65536",
                    "--model",
                    "g.gguf",
                ],
            },
            "architecture": {
                "input_modalities": ["text"],
                "output_modalities": ["text"],
            },
            "meta": {"n_ctx": 65536, "size": 14233222264},
        },
        {
            "id": "qwen3-embedding-4b",
            "object": "model",
            "status": {
                "value": "unloaded",
                "args": ["llama-server.exe", "--embeddings", "--pooling", "last"],
            },
            "architecture": {
                "input_modalities": ["text"],
                "output_modalities": ["text"],
            },
        },
    ],
    "object": "list",
}


def make_client(module, handler, gpu=None):
    app = FastAPI()
    app.include_router(module.router, prefix="/api")

    async def client_override():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://router"
        ) as client:
            yield client

    app.dependency_overrides[module.get_http_client] = client_override
    app.dependency_overrides[module.get_gpu_memory] = lambda: gpu
    return TestClient(app)


def test_parse_models_maps_status_size_context_and_embedding_flag():
    m = load_module()
    models = m.parse_models(ROUTER_MODELS_PAYLOAD)
    assert [x.id for x in models] == ["gemma-4-26b-a4b", "qwen3-embedding-4b"]
    chat, embed = models
    assert chat.status == "loaded"
    assert chat.is_embedding is False
    assert chat.size_bytes == 14233222264
    assert chat.n_ctx == 65536
    assert embed.status == "unloaded"
    assert embed.is_embedding is True
    assert embed.size_bytes is None  # the router only reports meta for loaded models


def test_parse_models_marks_a_failed_load():
    payload = {
        "data": [
            {
                "id": "m",
                "status": {
                    "value": "unloaded",
                    "failed": True,
                    "exit_code": 1,
                    "args": [],
                },
            }
        ]
    }
    (model,) = load_module().parse_models(payload)
    assert model.status == "unloaded"
    assert model.failed is True


def test_parse_nvidia_smi_output():
    m = load_module()
    assert m.parse_nvidia_smi("21569, 24576\n") == m.GpuMemory(
        used_mib=21569, total_mib=24576
    )
    assert m.parse_nvidia_smi("") is None
    assert m.parse_nvidia_smi("[N/A], [N/A]") is None


def test_status_reports_models_and_gpu_memory():
    m = load_module()

    def handler(request):
        assert request.method == "GET" and request.url.path == "/models"
        return httpx.Response(200, json=ROUTER_MODELS_PAYLOAD)

    client = make_client(m, handler, gpu=m.GpuMemory(used_mib=100, total_mib=200))
    r = client.get("/api/llm/status")
    assert r.status_code == 200
    body = r.json()
    assert body["reachable"] is True
    assert body["error"] is None
    assert [x["id"] for x in body["models"]] == [
        "gemma-4-26b-a4b",
        "qwen3-embedding-4b",
    ]
    assert body["gpu"] == {"used_mib": 100, "total_mib": 200}


def test_status_reports_an_unreachable_model_server_instead_of_failing():
    m = load_module()

    def handler(request):
        raise httpx.ConnectError("connection refused")

    client = make_client(m, handler)
    r = client.get("/api/llm/status")
    assert r.status_code == 200
    body = r.json()
    assert body["reachable"] is False
    assert body["models"] == []
    assert "connection refused" in body["error"]


def _stateful_handler(state, calls):
    def handler(request):
        if request.method == "GET" and request.url.path == "/models":
            payload = json.loads(json.dumps(ROUTER_MODELS_PAYLOAD))
            payload["data"][0]["status"]["value"] = state["gemma"]
            return httpx.Response(200, json=payload)
        if request.method == "POST" and request.url.path == "/models/unload":
            calls.append((request.url.path, json.loads(request.content)))
            state["gemma"] = "unloaded"
            return httpx.Response(200, json={"success": True})
        if request.method == "POST" and request.url.path == "/models/load":
            calls.append((request.url.path, json.loads(request.content)))
            state["gemma"] = "loading"
            return httpx.Response(200, json={"success": True})
        return httpx.Response(404, json={"error": "unexpected"})

    return handler


def test_unload_forwards_to_the_router_and_returns_the_new_status():
    m = load_module()
    state, calls = {"gemma": "loaded"}, []
    client = make_client(m, _stateful_handler(state, calls))
    r = client.post("/api/llm/unload", json={"model": "gemma-4-26b-a4b"})
    assert r.status_code == 200
    assert calls == [("/models/unload", {"model": "gemma-4-26b-a4b"})]
    assert r.json() == {
        "success": True,
        "model": "gemma-4-26b-a4b",
        "status": "unloaded",
    }


def test_load_forwards_to_the_router_and_returns_the_new_status():
    m = load_module()
    state, calls = {"gemma": "unloaded"}, []
    client = make_client(m, _stateful_handler(state, calls))
    r = client.post("/api/llm/load", json={"model": "gemma-4-26b-a4b"})
    assert r.status_code == 200
    assert calls == [("/models/load", {"model": "gemma-4-26b-a4b"})]
    assert r.json() == {
        "success": True,
        "model": "gemma-4-26b-a4b",
        "status": "loading",
    }


def test_unknown_model_is_rejected_without_touching_the_router():
    m = load_module()
    state, calls = {"gemma": "loaded"}, []
    client = make_client(m, _stateful_handler(state, calls))
    r = client.post("/api/llm/unload", json={"model": "not-a-model"})
    assert r.status_code == 404
    assert calls == []


def test_router_failure_is_reported_as_bad_gateway():
    m = load_module()

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=ROUTER_MODELS_PAYLOAD)
        return httpx.Response(500, json={"error": {"message": "boom"}})

    client = make_client(m, handler)
    r = client.post("/api/llm/load", json={"model": "qwen3-embedding-4b"})
    assert r.status_code == 502
    assert "boom" in r.json()["detail"]
