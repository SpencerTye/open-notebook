"""Tests for open_notebook/vector_index/search.py.

The search path takes candidates from the index, re-scores them with the exact
vectors read from the database, and returns rows in the shape the database
function ``fn::vector_search`` returns today (``id``, ``parent_id``, ``title``,
``similarity``, ``matches``), so the Ask graph and the Search page need no
change. A plain numpy transcription of that SurrealQL function is the
reference here.
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pytest

REPO_ROOT = (
    Path(__file__).resolve().parents[1] / "open_notebook" / "vector_index"
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


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def search():
    _load("open_notebook.vector_index.ids", "ids.py")
    _load("open_notebook.vector_index.store", "store.py")
    return _load("open_notebook.vector_index.search", "search.py")


DIM = 12


def cosine(a, b) -> float:
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))


def reference_vector_search(
    chunks: List[Dict[str, Any]],
    insights: List[Dict[str, Any]],
    notes: List[Dict[str, Any]],
    query,
    match_count: int,
    sources: bool,
    show_notes: bool,
    min_similarity: float,
) -> List[Dict[str, Any]]:
    """fn::vector_search (migration 9) transcribed literally."""

    def top(rows, make):
        scored = []
        for row in rows:
            if len(row["embedding"]) != len(query):
                continue
            sim = cosine(row["embedding"], query)
            if sim >= min_similarity:
                scored.append((sim, make(row)))
        scored.sort(key=lambda item: -item[0])
        return [dict(item[1], similarity=item[0]) for item in scored[:match_count]]

    chunk_rows = (
        top(
            chunks,
            lambda r: {
                "id": r["source"],
                "title": r["source_title"],
                "content": r["content"],
                "parent_id": r["source"],
            },
        )
        if sources
        else []
    )
    insight_rows = (
        top(
            insights,
            lambda r: {
                "id": r["id"],
                "title": r["insight_type"] + " - " + (r["source_title"] or ""),
                "content": r["content"],
                "parent_id": r["source"],
            },
        )
        if sources
        else []
    )
    note_rows = (
        top(
            notes,
            lambda r: {
                "id": r["id"],
                "title": r["title"],
                "content": r["content"],
                "parent_id": r["id"],
            },
        )
        if show_notes
        else []
    )

    groups: Dict[tuple, Dict[str, Any]] = {}
    for row in chunk_rows + insight_rows + note_rows:
        key = (row["id"], row["parent_id"], row["title"])
        group = groups.setdefault(
            key,
            {
                "id": row["id"],
                "parent_id": row["parent_id"],
                "title": row["title"],
                "similarity": row["similarity"],
                "matches": [],
            },
        )
        group["similarity"] = max(group["similarity"], row["similarity"])
        group["matches"].append(row["content"])
    ordered = sorted(groups.values(), key=lambda g: -g["similarity"])
    return ordered[:match_count]


def synthetic_library(seed: int = 3):
    rng = np.random.default_rng(seed)
    sources = {f"source:s{i}": f"Paper {i}" for i in range(4)}
    chunks = []
    for source in sources:
        for j in range(6):
            chunks.append(
                {
                    "id": f"source_embedding:{source[-2:]}{j}",
                    "source": source,
                    "source_title": sources[source],
                    "content": f"chunk {j} of {source}",
                    "embedding": rng.standard_normal(DIM).tolist(),
                }
            )
    insights = [
        {
            "id": f"source_insight:i{i}",
            "source": f"source:s{i % 4}",
            "source_title": sources[f"source:s{i % 4}"],
            "insight_type": "Dense Summary",
            "content": f"summary {i}",
            "embedding": rng.standard_normal(DIM).tolist(),
        }
        for i in range(5)
    ]
    notes = [
        {
            "id": f"note:n{i}",
            "title": f"Note {i}",
            "content": f"note text {i}",
            "embedding": rng.standard_normal(DIM).tolist(),
        }
        for i in range(3)
    ]
    query = rng.standard_normal(DIM).tolist()
    return chunks, insights, notes, query


def scored_from(rows, query, min_score, make):
    """What search.py's re-scoring yields for a kind: every candidate with
    its exact similarity, min-score applied."""
    out = []
    for row in rows:
        sim = cosine(row["embedding"], query)
        if sim >= min_score:
            out.append(dict(make(row), similarity=sim))
    return out


CHUNK = lambda r: {  # noqa: E731
    "id": r["source"],
    "parent_id": r["source"],
    "title": r["source_title"],
    "content": r["content"],
}
INSIGHT = lambda r: {  # noqa: E731
    "id": r["id"],
    "parent_id": r["source"],
    "title": r["insight_type"] + " - " + (r["source_title"] or ""),
    "content": r["content"],
}
NOTE = lambda r: {  # noqa: E731
    "id": r["id"],
    "parent_id": r["id"],
    "title": r["title"],
    "content": r["content"],
}


def assert_same_rows(actual, expected):
    assert [r["id"] for r in actual] == [r["id"] for r in expected]
    for a, e in zip(actual, expected):
        assert a["parent_id"] == e["parent_id"]
        assert a["title"] == e["title"]
        assert a["matches"] == e["matches"]
        assert a["similarity"] == pytest.approx(e["similarity"])
        assert set(a) == {"id", "parent_id", "title", "similarity", "matches"}


@pytest.mark.parametrize("match_count", [1, 3, 10, 100])
@pytest.mark.parametrize("min_score", [-1.0, 0.0, 0.2])
def test_rows_match_the_database_function(search, match_count, min_score):
    chunks, insights, notes, query = synthetic_library()

    actual = search.assemble_rows(
        {
            "chunks": scored_from(chunks, query, min_score, CHUNK),
            "insights": scored_from(insights, query, min_score, INSIGHT),
            "notes": scored_from(notes, query, min_score, NOTE),
        },
        match_count,
    )
    expected = reference_vector_search(
        chunks, insights, notes, query, match_count, True, True, min_score
    )

    assert_same_rows(actual, expected)


def test_rows_group_every_chunk_of_a_source_into_one_row(search):
    chunks, insights, notes, query = synthetic_library(seed=8)
    query = chunks[7]["embedding"]  # a chunk of source:s1 is the exact match

    rows = search.assemble_rows(
        {
            "chunks": scored_from(chunks, query, -1.0, CHUNK),
            "insights": [],
            "notes": [],
        },
        100,
    )

    assert rows[0]["id"] == "source:s1"
    assert rows[0]["similarity"] == pytest.approx(1.0)
    assert rows[0]["matches"][0] == chunks[7]["content"]
    assert len(rows[0]["matches"]) == 6
    assert len(rows) == 4


def test_exact_cosine_matches_numpy_reference(search):
    rng = np.random.default_rng(0)
    matrix = rng.standard_normal((5, DIM))
    query = rng.standard_normal(DIM)

    sims = search.exact_cosine(matrix.tolist(), query.tolist())

    for row, sim in zip(matrix, sims):
        assert sim == pytest.approx(cosine(row, query))


def test_candidate_count_is_at_least_the_default_and_twice_the_ask(search, monkeypatch):
    monkeypatch.delenv("OPEN_NOTEBOOK_VECTOR_CANDIDATES", raising=False)
    assert search.candidate_count(10) == 50
    assert search.candidate_count(100) == 200
    monkeypatch.setenv("OPEN_NOTEBOOK_VECTOR_CANDIDATES", "80")
    assert search.candidate_count(10) == 80
    monkeypatch.setenv("OPEN_NOTEBOOK_VECTOR_CANDIDATES", "nonsense")
    assert search.candidate_count(10) == 50


# ---------------------------------------------------------------------------
# the engine switch
# ---------------------------------------------------------------------------


class FakeStore:
    def __init__(self, ready=True):
        self.ready = ready
        self.searched = []

    def is_ready(self):
        return self.ready

    def search(self, query, k, kinds, allow_sources=None, allow_notes=None):
        self.searched.append((k, tuple(kinds), allow_sources, allow_notes))
        return {kind: [] for kind in kinds}


def run(coro):
    return asyncio.run(coro)


def test_switch_unset_means_the_database_scan(search, monkeypatch):
    monkeypatch.delenv("OPEN_NOTEBOOK_VECTOR_ENGINE", raising=False)
    monkeypatch.setattr(search, "_store", lambda: FakeStore())

    assert run(search.search_with_index([0.1] * DIM, 10, True, True, 0.2)) is None


def test_switch_set_to_scan_means_the_database_scan(search, monkeypatch):
    monkeypatch.setenv("OPEN_NOTEBOOK_VECTOR_ENGINE", "scan")
    monkeypatch.setattr(search, "_store", lambda: FakeStore())

    assert run(search.search_with_index([0.1] * DIM, 10, True, True, 0.2)) is None


def test_switch_on_but_index_not_ready_means_the_database_scan(search, monkeypatch):
    monkeypatch.setenv("OPEN_NOTEBOOK_VECTOR_ENGINE", "turbovec")
    monkeypatch.setattr(search, "_store", lambda: FakeStore(ready=False))

    assert run(search.search_with_index([0.1] * DIM, 10, True, True, 0.2)) is None

    monkeypatch.setattr(search, "_store", lambda: None)
    assert run(search.search_with_index([0.1] * DIM, 10, True, True, 0.2)) is None


def test_switch_on_and_ready_uses_the_index(search, monkeypatch):
    monkeypatch.setenv("OPEN_NOTEBOOK_VECTOR_ENGINE", "TurboVec")
    store = FakeStore()
    monkeypatch.setattr(search, "_store", lambda: store)

    async def no_rows(sql, variables=None):
        return []

    monkeypatch.setattr(search, "_query", no_rows)

    rows = run(search.search_with_index([0.1] * DIM, 10, True, False, 0.2))

    assert rows == []
    assert store.searched == [(50, ("chunks", "insights"), None, None)]


def test_an_index_failure_falls_back_to_the_database_scan(search, monkeypatch):
    monkeypatch.setenv("OPEN_NOTEBOOK_VECTOR_ENGINE", "turbovec")

    class Broken(FakeStore):
        def search(self, *a, **k):
            raise ValueError("dimension mismatch")

    monkeypatch.setattr(search, "_store", lambda: Broken())

    assert run(search.search_with_index([0.1] * DIM, 10, True, True, 0.2)) is None


def test_full_path_rescored_from_the_database(search, monkeypatch):
    """Candidates from the index, records from the database, exact ranking."""
    monkeypatch.setenv("OPEN_NOTEBOOK_VECTOR_ENGINE", "turbovec")
    chunks, insights, notes, query = synthetic_library(seed=5)
    by_id = {r["id"]: r for r in chunks + insights + notes}

    class Store(FakeStore):
        def search(self, q, k, kinds, allow_sources=None, allow_notes=None):
            # The index hands back candidates in a deliberately wrong order;
            # the re-scoring must fix it.
            out = {}
            if "chunks" in kinds:
                out["chunks"] = [(r["id"], r["source"], 0.0) for r in chunks[:k]]
            if "insights" in kinds:
                out["insights"] = [(r["id"], r["source"], 0.0) for r in insights[:k]]
            if "notes" in kinds:
                out["notes"] = [(r["id"], None, 0.0) for r in notes[:k]]
            return out

    async def fetch(sql, variables=None):
        rows = []
        for record_id in variables["ids"]:
            r = by_id[record_id]
            if record_id.startswith("source_embedding:"):
                rows.append(
                    {
                        "id": r["id"],
                        "source": r["source"],
                        "title": r["source_title"],
                        "content": r["content"],
                        "embedding": r["embedding"],
                    }
                )
            elif record_id.startswith("source_insight:"):
                rows.append(
                    {
                        "id": r["id"],
                        "source": r["source"],
                        "insight_type": r["insight_type"],
                        "source_title": r["source_title"],
                        "content": r["content"],
                        "embedding": r["embedding"],
                    }
                )
            else:
                rows.append(
                    {
                        "id": r["id"],
                        "title": r["title"],
                        "content": r["content"],
                        "embedding": r["embedding"],
                    }
                )
        return rows

    monkeypatch.setattr(search, "_store", lambda: Store())
    monkeypatch.setattr(search, "_query", fetch)
    monkeypatch.setattr(search, "_record_id", lambda value: value)

    rows = run(search.search_with_index(query, 5, True, True, 0.0))
    expected = reference_vector_search(
        chunks, insights, notes, query, 5, True, True, 0.0
    )

    assert_same_rows(rows, expected)


def test_notebook_restriction_is_turned_into_allowlists(search, monkeypatch):
    monkeypatch.setenv("OPEN_NOTEBOOK_VECTOR_ENGINE", "turbovec")
    store = FakeStore()
    monkeypatch.setattr(search, "_store", lambda: store)

    async def query(sql, variables=None):
        if "FROM reference" in sql:
            return ["source:a", "source:b"]
        if "FROM artifact" in sql:
            return ["note:n1"]
        return []

    monkeypatch.setattr(search, "_query", query)
    monkeypatch.setattr(search, "_record_id", lambda value: value)

    run(search.search_with_index([0.1] * DIM, 10, True, True, 0.2, "notebook:x"))

    k, kinds, allow_sources, allow_notes = store.searched[0]
    assert allow_sources == {"source:a", "source:b"}
    assert allow_notes == {"note:n1"}
