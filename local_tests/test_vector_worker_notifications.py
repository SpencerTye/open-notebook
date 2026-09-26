"""Tests for the LOCAL index notifications in
commands/embedding_commands.py.

The worker writes vectors to the database (chunks on embed_source, one vector
on embed_note and embed_insight). After each write it tells the API, which
owns the TurboVec index, which record ids changed. A source's old chunks are
replaced by the new ones. A failed notification never fails the job: the
vectors are in the database and the API's startup check repairs the index.

The module imports the app's database, model and job-queue layers; the tests
replace those with fakes so the real command functions run on the host.
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any, List, Optional

import pytest
from pydantic import BaseModel

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


class FakeRecord:
    def __init__(self, id: str, content: str = "text"):
        self.id = id
        self.content = content
        self.full_text = content
        self.asset = None


class Notifier:
    """Stands in for open_notebook.vector_index.client and records every call."""

    def __init__(self, fail: bool = False):
        self.calls: List[tuple] = []
        self.fail = fail

    async def index_upsert(self, ids, replace_sources=()):
        if self.fail:
            raise RuntimeError("notification failed")
        self.calls.append((list(ids), list(replace_sources)))
        return True

    async def index_remove(self, ids=(), sources=()):
        self.calls.append(("remove", list(ids), list(sources)))
        return True


def load_module(notifier: Notifier, *, inserted_with_ids: bool = True):
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

    queries: List[tuple] = []

    async def repo_query(query, params=None):
        queries.append((query, params))
        if "SELECT VALUE id FROM source_embedding" in query:
            return ["source_embedding:q1", "source_embedding:q2"]
        return []

    async def repo_insert(table, records):
        if inserted_with_ids:
            return [
                dict(r, id=f"source_embedding:{i + 1}") for i, r in enumerate(records)
            ]
        return list(records)

    class ConfigurationError(Exception):
        pass

    class ContentType:
        MARKDOWN = "markdown"
        value = "text"

    async def get_record(record_id):
        return FakeRecord(record_id, "some markdown content")

    class Note:
        get = staticmethod(get_record)

    class SourceInsight:
        get = staticmethod(get_record)

    class Source:
        @staticmethod
        async def get(record_id):
            return FakeRecord(record_id, "Full text of the paper.")

    async def generate_embedding(text, content_type=None, command_id=None):
        return [0.1, 0.2, 0.3]

    async def generate_embeddings(chunks, command_id=None):
        return [[0.1, 0.2, 0.3] for _ in chunks]

    _stub("open_notebook")
    _stub("open_notebook.ai")
    _stub("open_notebook.ai.models", model_manager=object())
    _stub("open_notebook.database")
    _stub(
        "open_notebook.database.repository",
        ensure_record_id=lambda value: value,
        repo_insert=repo_insert,
        repo_query=repo_query,
    )
    _stub("open_notebook.domain")
    _stub(
        "open_notebook.domain.notebook",
        Note=Note,
        Source=Source,
        SourceInsight=SourceInsight,
    )
    _stub("open_notebook.exceptions", ConfigurationError=ConfigurationError)
    _stub("open_notebook.utils")
    _stub(
        "open_notebook.utils.chunking",
        ContentType=ContentType,
        chunk_text=lambda text, content_type=None: ["one", "two", "three"],
        detect_content_type=lambda text, path=None: ContentType(),
    )
    _stub(
        "open_notebook.utils.embedding",
        generate_embedding=generate_embedding,
        generate_embeddings=generate_embeddings,
    )
    _stub("open_notebook.vector_index")
    _stub(
        "open_notebook.vector_index.client",
        index_upsert=notifier.index_upsert,
        index_remove=notifier.index_remove,
    )

    spec = importlib.util.spec_from_file_location(
        "embedding_commands_overlay_vi", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._test_queries = queries
    return module


def run(coro):
    return asyncio.run(coro)


def test_embed_source_replaces_the_sources_chunks_in_the_index():
    notifier = Notifier()
    module = load_module(notifier)

    result = run(
        module.embed_source_command(module.EmbedSourceInput(source_id="source:s1"))
    )

    assert result.success is True
    assert result.chunks_created == 3
    assert notifier.calls == [
        (
            ["source_embedding:1", "source_embedding:2", "source_embedding:3"],
            ["source:s1"],
        )
    ]


def test_embed_source_looks_the_ids_up_when_the_insert_returns_none():
    notifier = Notifier()
    module = load_module(notifier, inserted_with_ids=False)

    result = run(
        module.embed_source_command(module.EmbedSourceInput(source_id="source:s1"))
    )

    assert result.success is True
    assert notifier.calls == [
        (["source_embedding:q1", "source_embedding:q2"], ["source:s1"])
    ]


def test_embed_note_and_embed_insight_send_their_record_id():
    notifier = Notifier()
    module = load_module(notifier)

    note = run(module.embed_note_command(module.EmbedNoteInput(note_id="note:n1")))
    insight = run(
        module.embed_insight_command(
            module.EmbedInsightInput(insight_id="source_insight:i1")
        )
    )

    assert note.success is True
    assert insight.success is True
    assert notifier.calls == [(["note:n1"], []), (["source_insight:i1"], [])]


def test_a_failed_notification_does_not_fail_the_job():
    notifier = Notifier(fail=True)
    module = load_module(notifier)

    source = run(
        module.embed_source_command(module.EmbedSourceInput(source_id="source:s1"))
    )
    note = run(module.embed_note_command(module.EmbedNoteInput(note_id="note:n1")))

    assert source.success is True
    assert source.chunks_created == 3
    assert note.success is True


def test_the_notification_comes_after_the_database_write():
    """The API reads the vectors back from the database; they must be there."""
    order: List[str] = []
    notifier = Notifier()

    async def index_upsert(ids, replace_sources=()):
        order.append("notify")
        return True

    notifier.index_upsert = index_upsert
    module = load_module(notifier)
    original_insert = sys.modules["open_notebook.database.repository"].repo_insert

    async def repo_insert(table, records):
        order.append("insert")
        return await original_insert(table, records)

    module.repo_insert = repo_insert

    run(module.embed_source_command(module.EmbedSourceInput(source_id="source:s1")))

    assert order == ["insert", "notify"]


def test_create_insight_still_queues_no_embedding_job():
    """The earlier LOCAL change stays: an insight is written, nothing is queued
    and, having no vector yet, nothing is sent to the index."""
    notifier = Notifier()
    module = load_module(notifier)

    async def repo_query(query, params=None):
        return [{"id": "source_insight:new"}]

    module.repo_query = repo_query

    result = run(
        module.create_insight_command(
            module.CreateInsightInput(
                source_id="source:s1", insight_type="Dense Summary", content="Summary."
            )
        )
    )

    assert result.success is True
    assert notifier.calls == []


@pytest.mark.parametrize(
    "name", ["embed_note", "embed_insight", "embed_source", "rebuild_embeddings"]
)
def test_commands_keep_their_names(name):
    module = load_module(Notifier())
    functions = {
        "embed_note": module.embed_note_command,
        "embed_insight": module.embed_insight_command,
        "embed_source": module.embed_source_command,
        "rebuild_embeddings": module.rebuild_embeddings_command,
    }
    assert functions[name].command_name == name
