"""Tests for custom/overlay/api/routers/vector_index.py.

Four endpoints on the API, which owns the index: status, upsert and remove
(what the worker calls after writing or deleting vectors) and rebuild (on
demand, from the database). The router reaches the store only through
``_store``, replaced here by a fake, so the endpoint functions run on the
host unmodified.
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from typing import List

import pytest
from fastapi import HTTPException


def _ensure_loguru() -> None:
    try:
        import loguru  # noqa: F401
    except ModuleNotFoundError:
        stub = types.ModuleType("loguru")
        stub.logger = types.SimpleNamespace(
            debug=lambda *a, **k: None,
            info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            error=lambda *a, **k: None,
            success=lambda *a, **k: None,
        )
        sys.modules["loguru"] = stub


_ensure_loguru()

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "overlay"
    / "api"
    / "routers"
    / "vector_index.py"
)


class FakeStore:
    def __init__(self, state: str = "ready"):
        self.state = state
        self.calls: List[tuple] = []
        self.rebuilt = False

    def status(self):
        return {"state": self.state, "counts": {"chunks": 3, "insights": 1, "notes": 0}}

    async def upsert(self, ids, replace_sources=()):
        self.calls.append(("upsert", list(ids), list(replace_sources)))
        return {"added": len(ids), "removed": len(replace_sources), "missing": 0}

    async def remove(self, ids=(), sources=()):
        self.calls.append(("remove", list(ids), list(sources)))
        return {"removed": len(ids) + len(sources)}

    async def rebuild(self):
        self.state = "rebuilding"
        await asyncio.sleep(0)
        self.rebuilt = True
        self.state = "ready"
        return {"chunks": {"added": 3, "removed": 0, "kept": 0}}


def load_module(store):
    spec = importlib.util.spec_from_file_location(
        "vector_index_router_overlay", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module._store = lambda: store
    return module


def run(coro):
    return asyncio.run(coro)


def test_status_reports_the_store(monkeypatch):
    module = load_module(FakeStore())

    result = run(module.get_status())

    assert result["state"] == "ready"
    assert result["counts"]["chunks"] == 3


def test_upsert_passes_ids_and_replaced_sources_to_the_store():
    store = FakeStore()
    module = load_module(store)

    result = run(
        module.upsert(
            module.UpsertRequest(
                ids=["source_embedding:a"], replace_sources=["source:s"]
            )
        )
    )

    assert store.calls == [("upsert", ["source_embedding:a"], ["source:s"])]
    assert (result.added, result.removed, result.missing) == (1, 1, 0)


def test_remove_passes_ids_and_sources_to_the_store():
    store = FakeStore()
    module = load_module(store)

    result = run(
        module.remove(module.RemoveRequest(ids=["note:n"], sources=["source:s"]))
    )

    assert store.calls == [("remove", ["note:n"], ["source:s"])]
    assert result.removed == 2


def test_rebuild_runs_in_the_background_and_answers_at_once():
    store = FakeStore()
    module = load_module(store)

    async def call():
        response = await module.rebuild()
        assert response.started is True
        assert store.rebuilt is False
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return response

    run(call())

    assert store.rebuilt is True


def test_rebuild_refuses_while_a_check_or_rebuild_is_running():
    module = load_module(FakeStore(state="rebuilding"))

    with pytest.raises(HTTPException) as excinfo:
        run(module.rebuild())

    assert excinfo.value.status_code == 409

    module = load_module(FakeStore(state="checking"))
    with pytest.raises(HTTPException) as excinfo:
        run(module.rebuild())
    assert excinfo.value.status_code == 409


def test_every_endpoint_answers_503_when_the_index_is_not_open():
    module = load_module(None)

    for call in (
        module.get_status(),
        module.upsert(module.UpsertRequest(ids=["note:n"])),
        module.remove(module.RemoveRequest(ids=["note:n"])),
        module.rebuild(),
    ):
        with pytest.raises(HTTPException) as excinfo:
            run(call)
        assert excinfo.value.status_code == 503
