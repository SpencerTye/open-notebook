"""Tests for custom/overlay/open_notebook/vector_index/store.py.

The store owns the three TurboVec indexes (chunks, insights, notes) plus the
map from record ids to index ids, keeps them in files under one folder, and
repairs them against the database on demand. These tests run the real
TurboVec library (the Windows wheel) against an in-memory fake of the
database, so nothing here needs the container.
"""

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
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


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, OVERLAY / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def modules():
    ids = _load("open_notebook.vector_index.ids", "ids.py")
    store = _load("open_notebook.vector_index.store", "store.py")
    return ids, store


DIM = 16


class FakeDb:
    """Holds the records the store would read from SurrealDB.

    Chunks: ``source_embedding:*`` with ``source`` and ``embedding``.
    Insights: ``source_insight:*`` with ``source`` and ``embedding``.
    Notes: ``note:*`` with ``embedding``.
    """

    def __init__(self):
        self.records: Dict[str, Dict[str, Any]] = {}
        self.calls: List[tuple] = []

    def add(self, record_id: str, embedding, source: Optional[str] = None):
        row = {"id": record_id, "embedding": list(map(float, embedding))}
        if source is not None:
            row["source"] = source
        self.records[record_id] = row

    def ids_of(self, table: str) -> List[str]:
        return [r for r in self.records if r.startswith(table + ":")]

    async def query(self, sql: str, variables: Optional[Dict[str, Any]] = None):
        self.calls.append((sql, variables))
        if "FROM $ids" in sql:
            rows = []
            for record_id in variables["ids"]:
                row = self.records.get(record_id)
                if row is not None:
                    rows.append(dict(row))
            return rows
        for table in ("source_embedding", "source_insight", "note"):
            if f"FROM {table}" in sql:
                return [
                    {k: v for k, v in row.items() if k != "embedding"}
                    for row in self.records.values()
                    if row["id"].startswith(table + ":") and row.get("embedding")
                ]
        raise AssertionError(f"unexpected query: {sql}")

    def vector_fetches(self, table: str) -> int:
        """How many by-id vector fetches touched the given table."""
        count = 0
        for sql, variables in self.calls:
            if "FROM $ids" in sql and any(
                i.startswith(table + ":") for i in variables["ids"]
            ):
                count += 1
        return count


def random_vectors(count: int, seed: int, dim: int = DIM) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.standard_normal((count, dim)).astype(np.float32)


def library(seed: int = 1) -> FakeDb:
    """Three sources with ten chunks each, four insights, two notes."""
    db = FakeDb()
    vectors = random_vectors(36, seed)
    n = 0
    for source in ("source:a", "source:b", "source:c"):
        for i in range(10):
            db.add(f"source_embedding:{source[-1]}{i:02d}", vectors[n], source)
            n += 1
    for i, source in enumerate(("source:a", "source:a", "source:b", "source:c")):
        db.add(f"source_insight:i{i}", vectors[n], source)
        n += 1
    for i in range(2):
        db.add(f"note:n{i}", vectors[n])
        n += 1
    return db


def make_store(modules, tmp_path, db: FakeDb):
    _, store_module = modules
    store = store_module.VectorStore(str(tmp_path / "vector_index"))
    store._query = db.query
    store._record_id = lambda value: value
    return store


def run(coro):
    return asyncio.run(coro)


def counts(store) -> Dict[str, int]:
    return store.status()["counts"]


# ---------------------------------------------------------------------------
# building, loading and checking
# ---------------------------------------------------------------------------


def test_first_open_builds_every_kind_from_the_database(modules, tmp_path):
    db = library()
    store = make_store(modules, tmp_path, db)

    run(store.open())
    assert store.is_ready() is False
    report = run(store.check())

    assert counts(store) == {"chunks": 30, "insights": 4, "notes": 2}
    assert store.is_ready() is True
    assert report["chunks"]["added"] == 30
    folder = tmp_path / "vector_index"
    assert (folder / "chunks.tvim").exists()
    assert (folder / "insights.tvim").exists()
    assert (folder / "notes.tvim").exists()
    assert (folder / "map.json").exists()


def test_reopen_loads_the_files_and_does_not_refetch_chunk_vectors(modules, tmp_path):
    db = library()
    first = make_store(modules, tmp_path, db)
    run(first.open())
    run(first.check())
    run(first.close())

    db.calls.clear()
    second = make_store(modules, tmp_path, db)
    run(second.open())

    assert counts(second) == {"chunks": 30, "insights": 4, "notes": 2}

    run(second.check())

    assert db.vector_fetches("source_embedding") == 0
    assert counts(second) == {"chunks": 30, "insights": 4, "notes": 2}
    assert second.is_ready() is True


def test_check_repairs_drift_in_the_chunk_table(modules, tmp_path):
    db = library()
    store = make_store(modules, tmp_path, db)
    run(store.open())
    run(store.check())

    # Three chunks vanish, five new ones appear (a re-embedded source), while
    # the API was not told.
    for gone in (
        "source_embedding:a00",
        "source_embedding:a01",
        "source_embedding:a02",
    ):
        del db.records[gone]
    for vec, i in zip(random_vectors(5, 99), range(5)):
        db.add(f"source_embedding:x{i}", vec, "source:a")

    report = run(store.check())

    assert counts(store)["chunks"] == 32
    assert report["chunks"] == {"added": 5, "removed": 3, "kept": 27}
    assert store.contains("source_embedding:x4")
    assert not store.contains("source_embedding:a00")


def test_check_rebuilds_insights_and_notes_wholesale(modules, tmp_path):
    """Insight and note re-embedding keeps the record id (UPDATE ... SET
    embedding), so an id-set comparison could not see a changed vector. Both
    kinds are small; the check re-reads them completely."""
    db = library()
    store = make_store(modules, tmp_path, db)
    run(store.open())
    run(store.check())

    new_vector = random_vectors(1, 5)[0]
    db.records["source_insight:i0"]["embedding"] = list(map(float, new_vector))

    run(store.check())

    hits = store.search(new_vector, 1, ["insights"])["insights"]
    assert hits[0][0] == "source_insight:i0"


def test_a_map_that_disagrees_with_the_index_file_is_rebuilt(modules, tmp_path):
    db = library()
    store = make_store(modules, tmp_path, db)
    run(store.open())
    run(store.check())
    run(store.close())

    map_path = tmp_path / "vector_index" / "map.json"
    data = json.loads(map_path.read_text(encoding="utf-8"))
    del data["kinds"]["chunks"]["source_embedding:b05"]
    map_path.write_text(json.dumps(data), encoding="utf-8")

    store = make_store(modules, tmp_path, db)
    run(store.open())
    run(store.check())

    assert counts(store)["chunks"] == 30
    assert store.contains("source_embedding:b05")


def test_rebuild_starts_from_nothing_and_ends_complete(modules, tmp_path):
    db = library()
    store = make_store(modules, tmp_path, db)
    run(store.open())
    run(store.check())
    run(store.remove(ids=["source_embedding:c09"]))
    assert counts(store)["chunks"] == 29

    report = run(store.rebuild())

    assert counts(store) == {"chunks": 30, "insights": 4, "notes": 2}
    assert report["chunks"]["added"] == 30
    assert store.is_ready() is True


# ---------------------------------------------------------------------------
# searching
# ---------------------------------------------------------------------------


def test_search_returns_the_nearest_records_with_their_sources(modules, tmp_path):
    db = library()
    store = make_store(modules, tmp_path, db)
    run(store.open())
    run(store.check())

    query = np.array(db.records["source_embedding:b03"]["embedding"], dtype=np.float32)
    hits = store.search(query, 5, ["chunks", "insights", "notes"])

    assert hits["chunks"][0][:2] == ("source_embedding:b03", "source:b")
    assert len(hits["chunks"]) == 5
    assert len(hits["insights"]) == 4
    assert len(hits["notes"]) == 2
    assert hits["notes"][0][1] is None


def test_search_candidates_follow_cosine_order_not_vector_length(modules, tmp_path):
    """A long vector pointing elsewhere must not outrank a short one pointing
    at the query: candidates are ranked by direction, like the database's
    cosine, so the exact re-scoring afterwards has the right 50 to look at."""
    db = FakeDb()
    base = random_vectors(2, 3)
    aligned = base[0] / np.linalg.norm(base[0])
    db.add("source_embedding:short", 0.1 * aligned, "source:a")
    db.add(
        "source_embedding:long", 40.0 * base[1] / np.linalg.norm(base[1]), "source:a"
    )
    store = make_store(modules, tmp_path, db)
    run(store.open())
    run(store.check())

    hits = store.search(aligned, 2, ["chunks"])["chunks"]

    assert hits[0][0] == "source_embedding:short"


def test_search_limited_to_sources(modules, tmp_path):
    db = library()
    store = make_store(modules, tmp_path, db)
    run(store.open())
    run(store.check())
    query = random_vectors(1, 11)[0]

    hits = store.search(query, 50, ["chunks", "insights"], allow_sources={"source:b"})

    assert len(hits["chunks"]) == 10
    assert all(source == "source:b" for _, source, _ in hits["chunks"])
    assert [i for i, _, _ in hits["insights"]] == ["source_insight:i2"]


def test_search_limited_to_sources_with_no_entries_is_empty_not_an_error(
    modules, tmp_path
):
    db = library()
    store = make_store(modules, tmp_path, db)
    run(store.open())
    run(store.check())

    hits = store.search(
        random_vectors(1, 12)[0], 5, ["chunks"], allow_sources={"source:zz"}
    )

    assert hits["chunks"] == []


def test_search_limited_to_notes(modules, tmp_path):
    db = library()
    store = make_store(modules, tmp_path, db)
    run(store.open())
    run(store.check())

    hits = store.search(random_vectors(1, 13)[0], 5, ["notes"], allow_notes={"note:n1"})

    assert [i for i, _, _ in hits["notes"]] == ["note:n1"]


def test_search_with_a_query_of_another_dimension_raises(modules, tmp_path):
    db = library()
    store = make_store(modules, tmp_path, db)
    run(store.open())
    run(store.check())

    with pytest.raises(ValueError):
        store.search(np.ones(DIM + 1, dtype=np.float32), 5, ["chunks"])


def test_search_on_an_empty_kind_returns_nothing(modules, tmp_path):
    db = library()
    for note in db.ids_of("note"):
        del db.records[note]
    store = make_store(modules, tmp_path, db)
    run(store.open())
    run(store.check())

    hits = store.search(random_vectors(1, 14)[0], 5, ["notes"])

    assert hits["notes"] == []
    assert counts(store)["notes"] == 0


# ---------------------------------------------------------------------------
# maintenance from the worker
# ---------------------------------------------------------------------------


def test_upsert_replaces_a_sources_chunks(modules, tmp_path):
    db = library()
    store = make_store(modules, tmp_path, db)
    run(store.open())
    run(store.check())

    for old in db.ids_of("source_embedding"):
        if db.records[old]["source"] == "source:a":
            del db.records[old]
    new_ids = [f"source_embedding:new{i}" for i in range(4)]
    for record_id, vec in zip(new_ids, random_vectors(4, 21)):
        db.add(record_id, vec, "source:a")

    result = run(store.upsert(new_ids, replace_sources=["source:a"]))

    assert result == {"added": 4, "removed": 10, "missing": 0}
    assert counts(store)["chunks"] == 24
    assert all(store.contains(i) for i in new_ids)
    assert not store.contains("source_embedding:a00")
    assert (tmp_path / "vector_index" / "map.json").exists()


def test_upsert_of_an_existing_id_replaces_its_vector(modules, tmp_path):
    db = library()
    store = make_store(modules, tmp_path, db)
    run(store.open())
    run(store.check())
    new_vector = random_vectors(1, 31)[0]
    db.records["note:n0"]["embedding"] = list(map(float, new_vector))

    result = run(store.upsert(["note:n0"]))

    assert result["added"] == 1
    assert counts(store)["notes"] == 2
    assert store.search(new_vector, 1, ["notes"])["notes"][0][0] == "note:n0"


def test_upsert_of_an_id_missing_from_the_database_is_counted_not_fatal(
    modules, tmp_path
):
    db = library()
    store = make_store(modules, tmp_path, db)
    run(store.open())
    run(store.check())

    result = run(store.upsert(["source_embedding:ghost", "source:notavector"]))

    assert result["added"] == 0
    assert result["missing"] == 1
    assert counts(store)["chunks"] == 30


def test_remove_by_source_drops_its_chunks_and_insights(modules, tmp_path):
    db = library()
    store = make_store(modules, tmp_path, db)
    run(store.open())
    run(store.check())

    result = run(store.remove(sources=["source:a"]))

    assert result == {"removed": 12}
    assert counts(store) == {"chunks": 20, "insights": 2, "notes": 2}
    hits = store.search(random_vectors(1, 41)[0], 50, ["chunks", "insights"])
    assert all(source != "source:a" for _, source, _ in hits["chunks"])
    assert all(source != "source:a" for _, source, _ in hits["insights"])


def test_remove_by_id_and_unknown_ids_are_harmless(modules, tmp_path):
    db = library()
    store = make_store(modules, tmp_path, db)
    run(store.open())
    run(store.check())

    result = run(store.remove(ids=["note:n1", "note:never", "source:x"]))

    assert result == {"removed": 1}
    assert counts(store)["notes"] == 1


def test_a_vector_of_another_dimension_resets_that_kind(modules, tmp_path):
    """A new embedding model changes the dimension. Old vectors can never
    match a new query (the database scan filters them by length too), so the
    kind restarts with the new vectors instead of refusing them."""
    db = library()
    store = make_store(modules, tmp_path, db)
    run(store.open())
    run(store.check())
    wide = random_vectors(3, 51, dim=DIM + 8)
    new_ids = [f"note:wide{i}" for i in range(3)]
    for record_id, vec in zip(new_ids, wide):
        db.add(record_id, vec)

    result = run(store.upsert(new_ids))

    assert result["added"] == 3
    assert counts(store)["notes"] == 3
    assert store.status()["dims"]["notes"] == DIM + 8
    assert counts(store)["chunks"] == 30


def test_changes_survive_a_reopen(modules, tmp_path):
    db = library()
    store = make_store(modules, tmp_path, db)
    run(store.open())
    run(store.check())
    run(store.remove(sources=["source:c"]))
    run(store.close())

    again = make_store(modules, tmp_path, db)
    run(again.open())

    assert counts(again) == {"chunks": 20, "insights": 3, "notes": 2}
    assert not again.contains("source_embedding:c00")


def test_status_reports_state_counts_and_folder(modules, tmp_path):
    db = library()
    store = make_store(modules, tmp_path, db)

    assert store.status()["state"] == "closed"
    run(store.open())
    assert store.status()["state"] == "loaded"
    run(store.check())

    status = store.status()
    assert status["state"] == "ready"
    assert status["counts"] == {"chunks": 30, "insights": 4, "notes": 2}
    assert status["dims"] == {"chunks": DIM, "insights": DIM, "notes": DIM}
    assert status["directory"].endswith("vector_index")
