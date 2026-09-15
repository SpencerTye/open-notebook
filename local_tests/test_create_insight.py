"""Tests for the create_insight command in custom/overlay/commands/embedding_commands.py.

The overlay copy differs from upstream v1.14.0 in one place, marked LOCAL:
after an insight is written, no embed_insight job is queued. Insights are
embedded only when the operator runs Manage -> Advanced -> Rebuild Embeddings
with the embedder loaded.

The module imports the app's database, model and job-queue layers. The tests
replace those with small fakes so the real command function runs on the host.
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "overlay"
    / "commands"
    / "embedding_commands.py"
)


class FakeQueue:
    """Stands in for surreal_commands.submit_command and records every call."""

    def __init__(self):
        self.submitted = []

    def submit_command(self, app, name, args, *extra, **kwargs):
        self.submitted.append((app, name, args))
        return f"command:{len(self.submitted)}"


def _stub(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def load_module(queue: FakeQueue, rows):
    """Load the overlay file with fakes for the queue, the database and the app."""

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
        submit_command=queue.submit_command,
    )

    async def repo_query(query, params=None):
        return rows

    async def repo_insert(table, records):
        return records

    class ConfigurationError(Exception):
        pass

    class ContentType:
        pass

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
        "embedding_commands_overlay", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_creating_an_insight_queues_no_embedding_job():
    queue = FakeQueue()
    module = load_module(queue, rows=[{"id": "source_insight:abc123"}])

    result = asyncio.run(
        module.create_insight_command(
            module.CreateInsightInput(
                source_id="source:s1",
                insight_type="Dense Summary",
                content="The summary text.",
            )
        )
    )

    assert result.success is True
    assert result.insight_id == "source_insight:abc123"
    assert queue.submitted == []


def test_embed_insight_command_still_exists_for_rebuild_embeddings():
    module = load_module(FakeQueue(), rows=[])

    assert module.embed_insight_command.command_name == "embed_insight"
