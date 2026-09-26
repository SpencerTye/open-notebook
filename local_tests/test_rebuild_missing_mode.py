"""Tests for the LOCAL "missing" rebuild mode in
commands/embedding_commands.py.

Upstream's Rebuild Embeddings knows two modes: "existing" (records that
already carry a vector) and "all" (every record with content). Neither
selects only the records that have no vector yet, which is the case that
matters in this build, where insights are not embedded when they are
created. The "missing" mode selects exactly those records.
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from pydantic import BaseModel, ValidationError

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "commands"
    / "embedding_commands.py"
)


def _stub(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class FakeDb:
    """Answers the collect queries from three small tables.

    Each record: id, content, and either ``embedding`` (a vector) or
    ``chunks`` (for sources: how many source_embedding rows exist).
    """

    def __init__(self):
        self.sources: List[Dict[str, Any]] = []
        self.notes: List[Dict[str, Any]] = []
        self.insights: List[Dict[str, Any]] = []
        self.queries: List[str] = []

    async def query(self, sql: str, params: Optional[Dict[str, Any]] = None):
        self.queries.append(sql)
        text = " ".join(sql.split())
        if "FROM source_embedding" in text and "array::distinct" in text:
            return [s["id"] for s in self.sources if s.get("chunks", 0) > 0]
        if "FROM source " in text or text.endswith("FROM source"):
            rows = [s for s in self.sources if s.get("content")]
            if "source_embedding" in text:  # LOCAL missing mode: no chunk rows yet
                rows = [s for s in rows if s.get("chunks", 0) == 0]
            return [{"id": s["id"]} for s in rows]
        if "FROM note" in text:
            return self._pick(self.notes, text)
        if "FROM source_insight" in text:
            return self._pick(self.insights, text)
        raise AssertionError(f"unexpected query: {sql}")

    @staticmethod
    def _pick(rows, text):
        if "embedding != none" in text:
            return [{"id": r["id"]} for r in rows if r.get("embedding")]
        if "embedding = none" in text or "embedding IS NONE" in text.upper():
            return [
                {"id": r["id"]}
                for r in rows
                if r.get("content") and not r.get("embedding")
            ]
        return [{"id": r["id"]} for r in rows if r.get("content")]


def load_module(db: FakeDb):
    class CommandInput(BaseModel):
        execution_context: Optional[Any] = None

    class CommandOutput(BaseModel):
        pass

    def command(name, app=None, retry=None):
        def decorate(fn):
            fn.command_name = name
            return fn

        return decorate

    _stub(
        "surreal_commands",
        CommandInput=CommandInput,
        CommandOutput=CommandOutput,
        command=command,
        submit_command=lambda *a, **k: "command:1",
    )

    async def repo_insert(table, records):
        return records

    class ConfigurationError(Exception):
        pass

    class ContentType:
        MARKDOWN = "markdown"
        value = "text"

    _stub("open_notebook")
    _stub("open_notebook.ai")
    _stub("open_notebook.ai.models", model_manager=object())
    _stub("open_notebook.database")
    _stub(
        "open_notebook.database.repository",
        ensure_record_id=lambda value: value,
        repo_insert=repo_insert,
        repo_query=db.query,
    )
    _stub("open_notebook.domain")
    _stub(
        "open_notebook.domain.notebook",
        Note=object,
        Source=object,
        SourceInsight=object,
    )
    _stub("open_notebook.exceptions", ConfigurationError=ConfigurationError)
    _stub("open_notebook.utils")
    _stub(
        "open_notebook.utils.chunking",
        ContentType=ContentType,
        chunk_text=lambda text, content_type=None: [text],
        detect_content_type=lambda text, path=None: ContentType(),
    )
    _stub(
        "open_notebook.utils.embedding",
        generate_embedding=None,
        generate_embeddings=None,
    )

    spec = importlib.util.spec_from_file_location(
        "embedding_commands_overlay_missing", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def library() -> FakeDb:
    db = FakeDb()
    db.sources = [
        {"id": "source:embedded", "content": "text", "chunks": 76},
        {"id": "source:bare", "content": "text", "chunks": 0},
        {"id": "source:empty", "content": "", "chunks": 0},
    ]
    db.notes = [
        {"id": "note:embedded", "content": "text", "embedding": [0.1]},
        {"id": "note:bare", "content": "text"},
    ]
    db.insights = [
        {"id": "source_insight:embedded", "content": "text", "embedding": [0.1]},
        {"id": "source_insight:bare1", "content": "text"},
        {"id": "source_insight:bare2", "content": "text", "embedding": []},
        {"id": "source_insight:empty", "content": ""},
    ]
    return db


def collect(module, mode, sources=True, notes=True, insights=True):
    return asyncio.run(module.collect_items_for_rebuild(mode, sources, notes, insights))


def test_missing_mode_selects_only_records_without_a_vector():
    db = library()
    module = load_module(db)

    items = collect(module, "missing")

    assert items["insights"] == ["source_insight:bare1", "source_insight:bare2"]
    assert items["notes"] == ["note:bare"]
    assert items["sources"] == ["source:bare"]


def test_missing_mode_respects_the_include_switches():
    db = library()
    module = load_module(db)

    items = collect(module, "missing", sources=False, notes=False, insights=True)

    assert items == {
        "sources": [],
        "notes": [],
        "insights": ["source_insight:bare1", "source_insight:bare2"],
    }
    assert not any("FROM source " in " ".join(q.split()) for q in db.queries)
    assert not any("FROM note" in q for q in db.queries)


def test_existing_and_all_modes_are_unchanged():
    db = library()
    module = load_module(db)

    existing = collect(module, "existing")
    everything = collect(module, "all")

    assert existing["insights"] == ["source_insight:embedded"]
    assert existing["notes"] == ["note:embedded"]
    assert existing["sources"] == ["source:embedded"]
    assert everything["insights"] == [
        "source_insight:embedded",
        "source_insight:bare1",
        "source_insight:bare2",
    ]
    assert everything["notes"] == ["note:embedded", "note:bare"]
    assert everything["sources"] == ["source:embedded", "source:bare"]


def test_the_command_input_accepts_the_missing_mode():
    module = load_module(library())

    accepted = module.RebuildEmbeddingsInput(mode="missing")
    assert accepted.mode == "missing"

    with pytest.raises(ValidationError):
        module.RebuildEmbeddingsInput(mode="nonsense")


def test_the_rebuild_command_submits_one_job_per_missing_record():
    db = library()
    module = load_module(db)
    submitted = []
    module.submit_command = lambda app, name, args: (
        submitted.append((name, args)) or "command:x"
    )

    async def embedding_model():
        return "embedder"

    module.model_manager = types.SimpleNamespace(get_embedding_model=embedding_model)

    result = asyncio.run(
        module.rebuild_embeddings_command(
            module.RebuildEmbeddingsInput(
                mode="missing",
                include_sources=False,
                include_notes=False,
                include_insights=True,
            )
        )
    )

    assert result.success is True
    assert result.total_items == 2
    assert result.insights_submitted == 2
    assert [args["insight_id"] for _, args in submitted] == [
        "source_insight:bare1",
        "source_insight:bare2",
    ]
