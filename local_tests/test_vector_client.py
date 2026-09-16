"""Tests for custom/overlay/open_notebook/vector_index/client.py.

The worker writes vectors to the database and must tell the API, which owns
the index. Inside the API process the call goes straight to the store; from
the worker it is an HTTP call to the API on the container's internal port.
Either way a failure is logged and swallowed: the embedding job already
succeeded, and the API's startup check repairs the index later.
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any, List, Optional

import pytest

OVERLAY = (
    Path(__file__).resolve().parents[1] / "overlay" / "open_notebook" / "vector_index"
)


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


@pytest.fixture()
def client():
    spec = importlib.util.spec_from_file_location(
        "open_notebook.vector_index.client", OVERLAY / "client.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeStore:
    def __init__(self, fail: bool = False):
        self.state = "ready"
        self.calls: List[tuple] = []
        self.fail = fail

    async def upsert(self, ids, replace_sources=()):
        if self.fail:
            raise RuntimeError("index broken")
        self.calls.append(("upsert", list(ids), list(replace_sources)))
        return {"added": len(ids), "removed": 0, "missing": 0}

    async def remove(self, ids=(), sources=()):
        self.calls.append(("remove", list(ids), list(sources)))
        return {"removed": len(ids)}


class FakePoster:
    def __init__(self, fail: bool = False):
        self.posts: List[tuple] = []
        self.fail = fail

    async def __call__(self, path: str, payload: Any) -> Optional[dict]:
        if self.fail:
            raise ConnectionError("api down")
        self.posts.append((path, payload))
        return {}


def run(coro):
    return asyncio.run(coro)


def test_inside_the_api_process_the_store_is_called_directly(client, monkeypatch):
    store = FakeStore()
    poster = FakePoster()
    monkeypatch.setattr(client, "_local_store", lambda: store)
    monkeypatch.setattr(client, "_post", poster)

    assert run(
        client.index_upsert(["source_embedding:a"], replace_sources=["source:s"])
    )
    assert run(client.index_remove(ids=["note:n"], sources=["source:s"]))

    assert store.calls == [
        ("upsert", ["source_embedding:a"], ["source:s"]),
        ("remove", ["note:n"], ["source:s"]),
    ]
    assert poster.posts == []


def test_from_the_worker_the_api_is_called_over_http(client, monkeypatch):
    poster = FakePoster()
    monkeypatch.setattr(client, "_local_store", lambda: None)
    monkeypatch.setattr(client, "_post", poster)

    assert run(client.index_upsert(["source_embedding:a", "source_embedding:b"]))
    assert run(client.index_remove(sources=["source:s"]))

    assert poster.posts == [
        (
            "/api/vector-index/upsert",
            {
                "ids": ["source_embedding:a", "source_embedding:b"],
                "replace_sources": [],
            },
        ),
        ("/api/vector-index/remove", {"ids": [], "sources": ["source:s"]}),
    ]


def test_nothing_to_say_means_no_call(client, monkeypatch):
    poster = FakePoster()
    monkeypatch.setattr(client, "_local_store", lambda: None)
    monkeypatch.setattr(client, "_post", poster)

    assert run(client.index_upsert([])) is True
    assert run(client.index_remove()) is True
    assert poster.posts == []


def test_an_unreachable_api_is_logged_not_raised(client, monkeypatch):
    monkeypatch.setattr(client, "_local_store", lambda: None)
    monkeypatch.setattr(client, "_post", FakePoster(fail=True))

    assert run(client.index_upsert(["source_embedding:a"])) is False
    assert run(client.index_remove(ids=["note:n"])) is False


def test_a_failing_store_is_logged_not_raised(client, monkeypatch):
    monkeypatch.setattr(client, "_local_store", lambda: FakeStore(fail=True))
    monkeypatch.setattr(client, "_post", FakePoster())

    assert run(client.index_upsert(["source_embedding:a"])) is False


def test_api_url_and_password_come_from_the_environment(client, monkeypatch):
    monkeypatch.delenv("OPEN_NOTEBOOK_VECTOR_API_URL", raising=False)
    monkeypatch.delenv("OPEN_NOTEBOOK_PASSWORD", raising=False)
    monkeypatch.delenv("OPEN_NOTEBOOK_PASSWORD_FILE", raising=False)
    assert client.api_base_url() == "http://127.0.0.1:5055"
    assert client.auth_headers() == {}

    monkeypatch.setenv("OPEN_NOTEBOOK_VECTOR_API_URL", "http://api:5055/")
    monkeypatch.setenv("OPEN_NOTEBOOK_PASSWORD", "hunter2")
    assert client.api_base_url() == "http://api:5055"
    assert client.auth_headers() == {"Authorization": "Bearer hunter2"}
