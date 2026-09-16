"""Tests for custom/overlay/api/routers/batch_transformations.py.

The router is a LOCAL addition: it applies one transformation to every source in
a notebook that does not already carry that transformation's insight.

The module reaches the database and the job queue only through ``_query``,
``_submit`` and ``_record_id``, which import their dependencies lazily. The
tests load the real module and replace those three, so the endpoint functions
themselves run unmodified on the host — no container, no database, and nothing
is ever queued for real.
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException


def _ensure_loguru() -> None:
    """
    Let these tests run on a host without loguru.

    The router logs through loguru, which is present in the container but is not
    part of the minimal host test environment. A real install always wins; this
    only fills the gap, and only for logging calls.
    """
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
    / "batch_transformations.py"
)


class FakeDb:
    """Answers the router's three queries from in-memory fixtures."""

    def __init__(
        self,
        *,
        edges: List[str],
        insights: List[Dict[str, Any]],
        transformations: List[Dict[str, Any]],
    ):
        self.edges = edges
        self.insights = insights
        self.transformations = transformations
        self.queries: List[str] = []

    async def query(self, sql: str, variables: Optional[Dict[str, Any]] = None) -> Any:
        self.queries.append(sql)
        if "FROM reference" in sql:
            return list(self.edges)
        if "FROM source_insight" in sql:
            wanted = set(variables["source_ids"]) if variables else set()
            return [row for row in self.insights if row["source"] in wanted]
        if "FROM transformation" in sql:
            return list(self.transformations)
        raise AssertionError(f"unexpected query: {sql}")


class FakeQueue:
    """Records every run_transformation submission instead of queueing one."""

    def __init__(self, fail_for: Optional[set] = None):
        self.submitted: List[tuple] = []
        self.fail_for = fail_for or set()

    def submit(self, source_id: str, transformation_id: str) -> str:
        if source_id in self.fail_for:
            raise RuntimeError("queue is down")
        self.submitted.append((source_id, transformation_id))
        return f"command:{len(self.submitted)}"


def load_module(db: FakeDb, queue: FakeQueue):
    spec = importlib.util.spec_from_file_location(
        "batch_transformations_overlay", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    # Register before executing: the module uses `from __future__ import
    # annotations`, so pydantic resolves its field types by looking the module up
    # in sys.modules. In the container it is imported normally and this is
    # automatic; loading by path is what needs it spelled out.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    module._query = db.query
    module._submit = queue.submit
    module._record_id = lambda value: value
    return module


DENSE = {"id": "transformation:dense", "name": "dense", "title": "Dense Summary"}
TOC = {"id": "transformation:toc", "name": "toc", "title": "Table of Contents"}


def three_sources() -> List[str]:
    return ["source:a", "source:b", "source:c"]


def status_for(module, notebook="notebook:n1"):
    return asyncio.run(module.get_batch_status(notebook))


def apply_for(module, transformation_id, notebook="notebook:n1"):
    return asyncio.run(module.apply_batch_transformation(notebook, transformation_id))


def test_status_counts_applied_and_missing_per_transformation():
    db = FakeDb(
        edges=three_sources(),
        insights=[
            {"source": "source:a", "insight_type": "Dense Summary"},
            {"source": "source:b", "insight_type": "Table of Contents"},
        ],
        transformations=[DENSE, TOC],
    )
    module = load_module(db, FakeQueue())

    result = status_for(module)

    assert result.total_sources == 3
    by_title = {item.title: item for item in result.transformations}
    assert (by_title["Dense Summary"].applied, by_title["Dense Summary"].missing) == (
        1,
        2,
    )
    assert (
        by_title["Table of Contents"].applied,
        by_title["Table of Contents"].missing,
    ) == (1, 2)


def test_status_ignores_edges_that_are_not_sources():
    """A non-source edge would otherwise be a phantom that never gets an insight."""
    db = FakeDb(
        edges=["source:a", "note:n9"],
        insights=[{"source": "source:a", "insight_type": "Dense Summary"}],
        transformations=[DENSE],
    )
    module = load_module(db, FakeQueue())

    result = status_for(module)

    assert result.total_sources == 1
    assert result.transformations[0].missing == 0


def test_status_skips_a_transformation_with_no_title():
    db = FakeDb(
        edges=["source:a"],
        insights=[],
        transformations=[DENSE, {"id": "transformation:blank", "title": ""}],
    )
    module = load_module(db, FakeQueue())

    result = status_for(module)

    assert [item.id for item in result.transformations] == ["transformation:dense"]


def test_status_on_an_empty_notebook_reports_zero_rather_than_failing():
    db = FakeDb(edges=[], insights=[], transformations=[DENSE])
    module = load_module(db, FakeQueue())

    result = status_for(module)

    assert result.total_sources == 0
    assert result.transformations[0].missing == 0
    assert result.transformations[0].applied == 0


def test_apply_queues_only_the_sources_missing_that_insight():
    db = FakeDb(
        edges=three_sources(),
        insights=[
            {"source": "source:a", "insight_type": "Dense Summary"},
            # source:b has a different transformation's insight, so it still needs
            # this one - the check is per transformation, not "has any insight".
            {"source": "source:b", "insight_type": "Table of Contents"},
        ],
        transformations=[DENSE, TOC],
    )
    queue = FakeQueue()
    module = load_module(db, queue)

    result = apply_for(module, "transformation:dense")

    assert [source for source, _ in queue.submitted] == ["source:b", "source:c"]
    assert all(t == "transformation:dense" for _, t in queue.submitted)
    assert (result.queued, result.skipped, result.failed) == (2, 1, 0)
    assert result.total_sources == 3
    assert result.title == "Dense Summary"


def test_apply_is_a_no_op_once_every_source_has_the_insight():
    db = FakeDb(
        edges=three_sources(),
        insights=[
            {"source": source, "insight_type": "Dense Summary"}
            for source in three_sources()
        ],
        transformations=[DENSE],
    )
    queue = FakeQueue()
    module = load_module(db, queue)

    result = apply_for(module, "transformation:dense")

    assert queue.submitted == []
    assert (result.queued, result.skipped) == (0, 3)


def test_apply_continues_past_a_submission_failure():
    """One bad submission must not abandon the rest of the batch."""
    db = FakeDb(edges=three_sources(), insights=[], transformations=[DENSE])
    queue = FakeQueue(fail_for={"source:b"})
    module = load_module(db, queue)

    result = apply_for(module, "transformation:dense")

    assert [source for source, _ in queue.submitted] == ["source:a", "source:c"]
    assert (result.queued, result.failed) == (2, 1)


def test_apply_rejects_an_unknown_transformation():
    db = FakeDb(edges=three_sources(), insights=[], transformations=[DENSE])
    module = load_module(db, FakeQueue())

    with pytest.raises(HTTPException) as excinfo:
        apply_for(module, "transformation:nope")

    assert excinfo.value.status_code == 404


def test_apply_rejects_a_transformation_with_no_title():
    """Without a title there is no insight_type, so nothing could be deduplicated."""
    blank = {"id": "transformation:blank", "title": ""}
    db = FakeDb(edges=three_sources(), insights=[], transformations=[blank])
    module = load_module(db, FakeQueue())

    with pytest.raises(HTTPException) as excinfo:
        apply_for(module, "transformation:blank")

    assert excinfo.value.status_code == 422


def test_apply_rejects_a_notebook_with_no_sources():
    db = FakeDb(edges=[], insights=[], transformations=[DENSE])
    queue = FakeQueue()
    module = load_module(db, queue)

    with pytest.raises(HTTPException) as excinfo:
        apply_for(module, "transformation:dense")

    assert excinfo.value.status_code == 404
    assert queue.submitted == []


def test_insight_lookup_is_scoped_to_this_notebooks_sources():
    """An insight on a source in another notebook must not mark ours as done."""
    db = FakeDb(
        edges=["source:a"],
        insights=[
            {"source": "source:elsewhere", "insight_type": "Dense Summary"},
        ],
        transformations=[DENSE],
    )
    queue = FakeQueue()
    module = load_module(db, queue)

    result = apply_for(module, "transformation:dense")

    assert [source for source, _ in queue.submitted] == ["source:a"]
    assert result.queued == 1
