"""LOCAL: the engine switch and the index search path.

``vector_search`` in ``open_notebook/domain/notebook.py`` embeds the query as
before and then asks ``search_with_index``. That returns rows when the
engine is ``turbovec`` and the index is ready, and ``None`` otherwise, in
which case ``vector_search`` runs the database scan exactly as upstream does.

The rows have the same five fields as ``fn::vector_search`` (migration 9):
``id``, ``parent_id``, ``title``, ``similarity``, ``matches``. For a chunk hit
``id`` is the source, ``title`` the source title, and every chunk of the same
source in the top hits is grouped into one row (``matches`` lists their
texts). For an insight hit ``id`` is the insight, ``parent_id`` its source,
``title`` "insight type - source title". For a note both ids are the note.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
from loguru import logger

DEFAULT_CANDIDATES = 50
FETCH_BATCH = 100


def engine() -> str:
    return (os.getenv("OPEN_NOTEBOOK_VECTOR_ENGINE") or "scan").strip().lower()


def candidate_count(results: int) -> int:
    """How many index candidates per kind to re-score exactly.

    OPEN_NOTEBOOK_VECTOR_CANDIDATES (default 50) is the floor; a request for
    more rows than half of that raises it to twice the rows asked for, so the
    exact re-scoring always has slack beyond the rows it returns.
    """
    raw = (os.getenv("OPEN_NOTEBOOK_VECTOR_CANDIDATES") or "").strip()
    try:
        base = int(raw) if raw else DEFAULT_CANDIDATES
    except ValueError:
        base = DEFAULT_CANDIDATES
    if base <= 0:
        base = DEFAULT_CANDIDATES
    return max(base, 2 * int(results))


def _store():
    from open_notebook.vector_index import store as store_module

    return store_module.get()


async def _query(sql: str, variables: Optional[Dict[str, Any]] = None):
    from open_notebook.database.repository import repo_query

    return await repo_query(sql, variables)


def _record_id(value: str):
    from open_notebook.database.repository import ensure_record_id

    return ensure_record_id(value)


def exact_cosine(
    matrix: Sequence[Sequence[float]], query: Sequence[float]
) -> List[float]:
    """Cosine similarity of every row of ``matrix`` against ``query``, in float64."""
    rows = np.asarray(matrix, dtype=np.float64)
    if rows.size == 0:
        return []
    vector = np.asarray(query, dtype=np.float64)
    norms = np.linalg.norm(rows, axis=1) * float(np.linalg.norm(vector))
    dots = rows @ vector
    with np.errstate(divide="ignore", invalid="ignore"):
        sims = np.where(norms > 0.0, dots / norms, 0.0)
    return [float(value) for value in sims]


def assemble_rows(
    scored: Dict[str, List[Dict[str, Any]]], results: int
) -> List[Dict[str, Any]]:
    """Group re-scored candidates into the rows ``fn::vector_search`` returns.

    ``scored[kind]`` holds dicts with ``id``, ``parent_id``, ``title``,
    ``content`` and ``similarity`` (minimum score already applied). Per kind
    the top ``results`` are kept, then chunks, insights and notes are joined,
    grouped by (id, parent_id, title) with the highest similarity and every
    content as ``matches``, ordered by similarity, cut to ``results``.
    """
    results = int(results)
    ordered: List[Dict[str, Any]] = []
    for kind in ("chunks", "insights", "notes"):
        items = sorted(scored.get(kind, []), key=lambda item: -item["similarity"])
        ordered.extend(items[:results])
    groups: Dict[tuple, Dict[str, Any]] = {}
    for item in ordered:
        key = (item["id"], item["parent_id"], item["title"])
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "id": item["id"],
                "parent_id": item["parent_id"],
                "title": item["title"],
                "similarity": item["similarity"],
                "matches": [],
            }
        if item["similarity"] > group["similarity"]:
            group["similarity"] = item["similarity"]
        group["matches"].append(item["content"])
    rows = sorted(groups.values(), key=lambda group: -group["similarity"])
    return rows[:results]


async def _fetch_records(kind: str, ids: Sequence[str]) -> List[Dict[str, Any]]:
    if not ids:
        return []
    if kind == "chunks":
        sql = "SELECT id, source, source.title AS title, content, embedding FROM $ids"
    elif kind == "insights":
        sql = (
            "SELECT id, source, insight_type, source.title AS source_title, content, embedding "
            "FROM $ids"
        )
    else:
        sql = "SELECT id, title, content, embedding FROM $ids"
    rows: List[Dict[str, Any]] = []
    for start in range(0, len(ids), FETCH_BATCH):
        batch = ids[start : start + FETCH_BATCH]
        fetched = await _query(
            sql, {"ids": [_record_id(record_id) for record_id in batch]}
        )
        rows.extend(fetched or [])
    return rows


def _score_rows(
    kind: str, rows: List[Dict[str, Any]], embed: Sequence[float], minimum_score: float
) -> List[Dict[str, Any]]:
    usable = [
        row
        for row in rows
        if row.get("embedding") and len(row["embedding"]) == len(embed)
    ]
    sims = exact_cosine([row["embedding"] for row in usable], embed)
    scored: List[Dict[str, Any]] = []
    for row, similarity in zip(usable, sims):
        if similarity < minimum_score:
            continue
        if kind == "chunks":
            source = str(row.get("source"))
            item = {
                "id": source,
                "parent_id": source,
                "title": row.get("title"),
            }
        elif kind == "insights":
            item = {
                "id": str(row["id"]),
                "parent_id": str(row.get("source")),
                "title": f"{row.get('insight_type') or ''} - {row.get('source_title') or ''}",
            }
        else:
            item = {
                "id": str(row["id"]),
                "parent_id": str(row["id"]),
                "title": row.get("title"),
            }
        item["content"] = row.get("content")
        item["similarity"] = similarity
        scored.append(item)
    return scored


async def _notebook_sources(notebook_id: str) -> List[str]:
    rows = await _query(
        "SELECT VALUE in FROM reference WHERE out = $notebook",
        {"notebook": _record_id(notebook_id)},
    )
    return [str(value) for value in rows or []]


async def _notebook_notes(notebook_id: str) -> List[str]:
    rows = await _query(
        "SELECT VALUE in FROM artifact WHERE out = $notebook",
        {"notebook": _record_id(notebook_id)},
    )
    return [str(value) for value in rows or []]


async def run_index_search(
    store: Any,
    embed: Sequence[float],
    results: int,
    source: bool,
    note: bool,
    minimum_score: float,
    notebook_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Candidates from the index, records from the database, exact ranking."""
    started = time.perf_counter()
    kinds: List[str] = []
    if source:
        kinds.extend(["chunks", "insights"])
    if note:
        kinds.append("notes")
    if not kinds:
        return []

    allow_sources = None
    allow_notes = None
    if notebook_id:
        allow_sources = set(await _notebook_sources(notebook_id))
        allow_notes = set(await _notebook_notes(notebook_id))

    k = candidate_count(results)
    index_started = time.perf_counter()
    candidates = store.search(
        embed, k, kinds, allow_sources=allow_sources, allow_notes=allow_notes
    )
    index_seconds = time.perf_counter() - index_started

    fetch_seconds = 0.0
    scored: Dict[str, List[Dict[str, Any]]] = {}
    fetched_rows = 0
    for kind in kinds:
        ids = [record_id for record_id, _, _ in candidates.get(kind, [])]
        fetch_started = time.perf_counter()
        rows = await _fetch_records(kind, ids)
        fetch_seconds += time.perf_counter() - fetch_started
        fetched_rows += len(rows)
        scored[kind] = _score_rows(kind, rows, embed, minimum_score)

    rows_out = assemble_rows(scored, results)
    total_seconds = time.perf_counter() - started
    logger.info(
        "vector index search: kinds={} candidates={} fetched={} rows={} "
        "index={:.1f}ms fetch={:.0f}ms total={:.0f}ms{}".format(
            "+".join(kinds),
            k,
            fetched_rows,
            len(rows_out),
            index_seconds * 1000,
            fetch_seconds * 1000,
            total_seconds * 1000,
            f" notebook={notebook_id}" if notebook_id else "",
        )
    )
    return rows_out


async def search_with_index(
    embed: Sequence[float],
    results: int,
    source: bool = True,
    note: bool = True,
    minimum_score: float = 0.2,
    notebook_id: Optional[str] = None,
) -> Optional[List[Dict[str, Any]]]:
    """Rows from the index, or None when the database scan should run instead."""
    if engine() != "turbovec":
        return None
    store = _store()
    if store is None or not store.is_ready():
        state = getattr(store, "state", "unknown") if store is not None else "not opened"
        logger.warning(
            f"vector index not ready (state={state}); using the database scan for this search"
        )
        return None
    try:
        return await run_index_search(
            store, embed, results, source, note, minimum_score, notebook_id
        )
    except Exception as e:
        logger.warning(
            f"vector index search failed ({e}); using the database scan for this search"
        )
        return None
