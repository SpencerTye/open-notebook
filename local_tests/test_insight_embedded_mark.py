"""Tests for the LOCAL embedded mark on insights (task setup.6).

On the Source page's Insights tab each insight shows whether it has a
vector. The information travels in one database query and one field:

- ``Source.get_insights()`` (``open_notebook/domain/notebook.py``)
  projects ``embedded`` with the same expression the Missing rebuild mode
  uses (``embedding != NONE AND array::len(embedding) > 0``) and omits the
  vector itself, so no array crosses the wire.
- ``SourceInsight`` carries ``embedded`` as a read-only attribute that
  ``model_dump()`` never includes, so ``ObjectModel.save()`` (which writes
  ``model_dump()`` minus None) can never write it back to the table.
- ``GET /sources/{id}/insights`` (``api/routers/sources.py``) passes
  it through as ``embedded`` on each ``SourceInsightResponse``.

Both modules are loaded by path with their imports stubbed; the
router runs inside a real FastAPI app so the JSON body is the real one.
"""

import asyncio
import importlib.util
import sys
import types
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK_PATH = REPO_ROOT / "open_notebook" / "domain" / "notebook.py"
SOURCES_ROUTER_PATH = REPO_ROOT / "api" / "routers" / "sources.py"
MODELS_PATH = REPO_ROOT / "api" / "models.py"


def _stub(name: str, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class DatabaseOperationError(Exception):
    pass


class InvalidInputError(Exception):
    pass


class NotFoundError(Exception):
    pass


class OpenNotebookError(Exception):
    pass


class UnsupportedTypeException(Exception):
    pass


# --------------------------------------------------------------------------
# Domain: open_notebook/domain/notebook.py
# --------------------------------------------------------------------------


class FakeRepo:
    """Records every query and answers get_insights from a fixed row set."""

    def __init__(self, rows: List[Dict[str, Any]]):
        self.rows = rows
        self.queries: List[tuple] = []

    async def query(self, sql: str, params: Optional[Dict[str, Any]] = None):
        self.queries.append((" ".join(sql.split()), params))
        return [dict(row) for row in self.rows]


def load_domain(repo: FakeRepo):
    class ObjectModel(BaseModel):
        """The parts of upstream's ObjectModel that SourceInsight relies on."""

        id: Optional[str] = None
        table_name: ClassVar[str] = ""
        nullable_fields: ClassVar[set] = set()
        created: Optional[datetime] = None
        updated: Optional[datetime] = None

        async def delete(self) -> bool:
            return True

    class RecordID:
        def __init__(self, table: str = "", identifier: str = ""):
            self.table_name = table
            self.id = identifier

    _stub("surreal_commands", submit_command=lambda *a, **k: "command:1")
    _stub("surrealdb", RecordID=RecordID)
    _stub("open_notebook")
    _stub("open_notebook.database")
    _stub(
        "open_notebook.database.repository",
        ensure_record_id=lambda value: value,
        repo_query=repo.query,
    )
    _stub("open_notebook.domain")
    _stub("open_notebook.domain.base", ObjectModel=ObjectModel)
    _stub(
        "open_notebook.exceptions",
        DatabaseOperationError=DatabaseOperationError,
        InvalidInputError=InvalidInputError,
    )
    return _load("notebook_overlay_embedded_mark", NOTEBOOK_PATH)


def insight_rows() -> List[Dict[str, Any]]:
    return [
        {
            "id": "source_insight:with_vector",
            "source": "source:s",
            "insight_type": "Paper Analysis",
            "content": "one",
            "embedded": True,
        },
        {
            "id": "source_insight:without_vector",
            "source": "source:s",
            "insight_type": "Dense Summary",
            "content": "two",
            "embedded": False,
        },
    ]


def test_get_insights_asks_the_database_whether_each_insight_has_a_vector():
    repo = FakeRepo(insight_rows())
    domain = load_domain(repo)

    insights = asyncio.run(domain.Source(id="source:s").get_insights())

    sql, params = repo.queries[0]
    assert "FROM source_insight WHERE source=$id" in sql
    assert params == {"id": "source:s"}
    # The same "has a vector" test as the Missing rebuild mode, computed in
    # the database and returned as one boolean per row.
    assert "(embedding != NONE AND array::len(embedding) > 0) AS embedded" in sql
    # The vector itself never leaves the database for this query.
    assert "OMIT embedding" in sql
    assert [(i.id, i.embedded) for i in insights] == [
        ("source_insight:with_vector", True),
        ("source_insight:without_vector", False),
    ]


def test_an_insight_read_without_the_projection_does_not_claim_either_way():
    domain = load_domain(FakeRepo([]))

    insight = domain.SourceInsight(
        id="source_insight:x", insight_type="Paper Analysis", content="one"
    )

    assert insight.embedded is None


def test_the_mark_is_never_written_back_to_the_table():
    domain = load_domain(FakeRepo([]))

    insight = domain.SourceInsight(
        id="source_insight:x",
        insight_type="Paper Analysis",
        content="one",
        embedded=True,
    )

    # ObjectModel.save() writes model_dump() (minus None values); the mark
    # must not be in it, or every save would add an ``embedded`` column.
    assert "embedded" not in insight.model_dump()
    assert insight.embedded is True


# --------------------------------------------------------------------------
# API: GET /sources/{source_id}/insights in api/routers/sources.py
# --------------------------------------------------------------------------


def load_sources_router(insights: List[Any]):
    class Source:
        def __init__(self, id: str):
            self.id = id

        @classmethod
        async def get(cls, source_id: str):
            return cls(source_id)

        async def get_insights(self):
            return list(insights)

    class Placeholder:
        pass

    _stub("content_core", check_file_support=None)
    _stub(
        "surreal_commands",
        execute_command_sync=None,
        submit_command=lambda *a, **k: "command:1",
    )
    _stub("api")
    _stub("api.command_service", CommandService=Placeholder)
    _stub("api.credentials_service", validate_url=None)
    _load("api.models", MODELS_PATH)
    _stub("commands")
    _stub("commands.source_commands", SourceProcessingInput=Placeholder)
    _stub("open_notebook")
    _stub("open_notebook.config", UPLOADS_FOLDER="/tmp/uploads")
    _stub("open_notebook.database")
    _stub(
        "open_notebook.database.repository",
        ensure_record_id=lambda value: value,
        repo_query=None,
    )
    _stub("open_notebook.domain")
    _stub(
        "open_notebook.domain.notebook",
        Asset=Placeholder,
        Notebook=Placeholder,
        Source=Source,
    )
    _stub("open_notebook.domain.transformation", Transformation=Placeholder)
    _stub(
        "open_notebook.exceptions",
        InvalidInputError=InvalidInputError,
        NotFoundError=NotFoundError,
        OpenNotebookError=OpenNotebookError,
        UnsupportedTypeException=UnsupportedTypeException,
    )
    module = _load("sources_router_overlay_embedded_mark", SOURCES_ROUTER_PATH)
    app = FastAPI()
    app.include_router(module.router)
    return TestClient(app)


def stored_insight(id: str, content: str, embedded: Optional[bool]):
    return types.SimpleNamespace(
        id=id,
        insight_type="Paper Analysis",
        content=content,
        created=datetime(2026, 9, 16, 12, 0, 0),
        updated=None,
        embedded=embedded,
    )


def test_the_insights_list_carries_the_mark_for_each_insight():
    client = load_sources_router(
        [
            stored_insight("source_insight:with_vector", "one", True),
            stored_insight("source_insight:without_vector", "two", False),
        ]
    )

    response = client.get("/sources/source:s/insights")

    assert response.status_code == 200
    body = response.json()
    assert [(row["id"], row["embedded"]) for row in body] == [
        ("source_insight:with_vector", True),
        ("source_insight:without_vector", False),
    ]
    # Everything upstream returned is still there, unchanged.
    assert body[0]["source_id"] == "source:s"
    assert body[0]["insight_type"] == "Paper Analysis"
    assert body[0]["content"] == "one"
    assert body[0]["created"] == "2026-09-16T12:00:00"
    assert body[0]["updated"] is None
    # No vector in the response.
    assert "embedding" not in body[0]
