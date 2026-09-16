"""LOCAL: the TurboVec index store.

Three ``IdMapIndex`` objects (chunks, insights, notes) plus a map from record
ids to index ids, kept in one folder:

    chunks.tvim  insights.tvim  notes.tvim  map.json

Exactly one process may own these files: the API. The worker never opens
them; it tells the API what changed (see ``client.py``) and the API updates
the index. The database is the master copy; the index can always be rebuilt
from it, and ``check()`` repairs any drift on demand (the API runs it in the
background at every startup).

Vectors are L2-normalised before they enter the index, so the index ranks
candidates by direction, the same order as the database's cosine. The final
ranking never relies on the index's 4-bit scores: ``search.py`` re-scores the
candidates with the exact vectors read from the database.

Dimensions follow the database. If a batch of vectors arrives with a different
length than the index holds (a new embedding model), that kind is reset and
starts over with the new vectors; the old ones could never match a new query
anyway (the database scan filters them by length too).
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import numpy as np
from loguru import logger

from open_notebook.vector_index.ids import (
    KINDS,
    TABLE_FOR_KIND,
    group_by_kind,
    int_id,
    kind_of,
)

DEFAULT_DIR = "/app/data/vector_index"
FILES = {"chunks": "chunks.tvim", "insights": "insights.tvim", "notes": "notes.tvim"}
MAP_FILE = "map.json"
MAP_VERSION = 1

BIT_WIDTH = 4
CALIBRATION_SAMPLE = 1024  # rows; the library's guidance is "around 1024"
FETCH_BATCH = 100  # records per by-id read from the database


async def _default_query(sql: str, variables: Optional[Dict[str, Any]] = None):
    from open_notebook.database.repository import repo_query

    return await repo_query(sql, variables)


def _default_record_id(value: str):
    from open_notebook.database.repository import ensure_record_id

    return ensure_record_id(value)


def _index_class():
    from turbovec import IdMapIndex

    return IdMapIndex


def _batches(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _unit_rows(
    rows: List[Dict[str, Any]],
) -> Dict[int, List[Tuple[str, Optional[str], np.ndarray]]]:
    """Normalised float32 vectors from database rows, grouped by dimension.

    Rows without a usable vector (missing, empty, zero-length, non-finite)
    are dropped. Grouping by dimension keeps a mixed batch from failing as a
    whole; in practice every row of a batch has the same length.
    """
    grouped: Dict[int, List[Tuple[str, Optional[str], np.ndarray]]] = {}
    for row in rows:
        embedding = row.get("embedding")
        if not embedding:
            continue
        vector = np.asarray(embedding, dtype=np.float32)
        if vector.ndim != 1 or vector.size == 0 or not np.all(np.isfinite(vector)):
            continue
        norm = float(np.linalg.norm(vector))
        if not norm > 0.0:
            continue
        source = row.get("source")
        grouped.setdefault(int(vector.size), []).append(
            (str(row["id"]), str(source) if source is not None else None, vector / norm)
        )
    return grouped


class _Kind:
    """One index, its record map, and the per-source lookup for allowlists."""

    def __init__(self, name: str):
        self.name = name
        self.index: Any = None  # turbovec.IdMapIndex once something was added
        self.by_record: Dict[str, Tuple[int, Optional[str]]] = {}
        self.by_int: Dict[int, str] = {}
        self.by_source: Dict[str, Set[int]] = {}
        self.needs_full_write = False  # a new index object must be written whole first
        self.needs_rebuild = False  # files and map disagreed at load

    @property
    def count(self) -> int:
        return len(self.by_record)

    @property
    def dim(self) -> Optional[int]:
        if self.index is None:
            return None
        return self.index.dim

    def register(self, record_id: str, index_id: int, source: Optional[str]) -> None:
        self.by_record[record_id] = (index_id, source)
        self.by_int[index_id] = record_id
        if source is not None:
            self.by_source.setdefault(source, set()).add(index_id)

    def unregister(self, record_id: str) -> Optional[int]:
        entry = self.by_record.pop(record_id, None)
        if entry is None:
            return None
        index_id, source = entry
        self.by_int.pop(index_id, None)
        if source is not None:
            ids = self.by_source.get(source)
            if ids is not None:
                ids.discard(index_id)
                if not ids:
                    del self.by_source[source]
        return index_id

    def reset(self) -> None:
        self.index = None
        self.by_record.clear()
        self.by_int.clear()
        self.by_source.clear()
        self.needs_full_write = True
        self.needs_rebuild = False


class VectorStore:
    def __init__(
        self,
        directory: str,
        *,
        bit_width: int = BIT_WIDTH,
        calibration_sample: int = CALIBRATION_SAMPLE,
        fetch_batch: int = FETCH_BATCH,
    ):
        self.directory = directory
        self.bit_width = bit_width
        self.calibration_sample = calibration_sample
        self.fetch_batch = fetch_batch
        self.state = "closed"  # closed | loaded | checking | ready | rebuilding | error
        self.last_error: Optional[str] = None
        self.last_check: Optional[Dict[str, Any]] = None
        self.kinds: Dict[str, _Kind] = {kind: _Kind(kind) for kind in KINDS}
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        # Database access, replaceable in tests.
        self._query = _default_query
        self._record_id = _default_record_id

    # ------------------------------------------------------------------ files

    def _path(self, kind: str) -> str:
        return os.path.join(self.directory, FILES[kind])

    def _map_path(self) -> str:
        return os.path.join(self.directory, MAP_FILE)

    async def open(self) -> None:
        """Load the files if they exist; an absent file is an empty index."""
        os.makedirs(self.directory, exist_ok=True)
        index_class = _index_class()
        map_data: Dict[str, Any] = {}
        map_path = self._map_path()
        if os.path.exists(map_path):
            try:
                with open(map_path, "r", encoding="utf-8") as handle:
                    map_data = json.load(handle)
            except Exception as e:
                logger.warning(
                    f"vector index: map file unreadable ({e}); rebuilding all kinds"
                )
                map_data = {}
        stored_kinds = map_data.get("kinds", {}) if isinstance(map_data, dict) else {}

        for kind_name in KINDS:
            kind = _Kind(kind_name)
            path = self._path(kind_name)
            if os.path.exists(path):
                try:
                    kind.index = index_class.load(path)
                except Exception as e:
                    logger.warning(
                        f"vector index: {path} unreadable ({e}); rebuilding {kind_name}"
                    )
                    kind.index = None
                    kind.needs_rebuild = True
            entries = stored_kinds.get(kind_name, {})
            if not isinstance(entries, dict):
                entries = {}
            if not kind.needs_rebuild and not self._consistent(kind, entries):
                logger.warning(
                    f"vector index: the {kind_name} file and the map disagree; rebuilding {kind_name}"
                )
                kind.needs_rebuild = True
            if kind.needs_rebuild:
                kind.reset()
                kind.needs_rebuild = True
            else:
                for record_id, entry in entries.items():
                    kind.register(str(record_id), int(entry[0]), entry[1])
            self.kinds[kind_name] = kind

        self.state = "loaded"
        logger.info(
            "vector index: opened {} ({})".format(
                self.directory,
                ", ".join(f"{k.count} {name}" for name, k in self.kinds.items()),
            )
        )

    @staticmethod
    def _consistent(kind: _Kind, entries: Dict[str, Any]) -> bool:
        if kind.index is None:
            return len(entries) == 0
        try:
            if len(kind.index) != len(entries):
                return False
            for entry in entries.values():
                if not kind.index.contains(int(entry[0])):
                    return False
        except Exception:
            return False
        return True

    def _persist(self) -> None:
        """Write every changed index and then the map. Caller holds the lock."""
        for kind in self.kinds.values():
            self._persist_kind(kind)
        data = {
            "version": MAP_VERSION,
            "kinds": {
                name: {
                    record_id: [index_id, source]
                    for record_id, (index_id, source) in kind.by_record.items()
                }
                for name, kind in self.kinds.items()
            },
        }
        map_path = self._map_path()
        temp_path = map_path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(temp_path, map_path)

    def _persist_kind(self, kind: _Kind) -> None:
        path = self._path(kind.name)
        if kind.index is None or kind.index.dim is None:
            if os.path.exists(path):
                os.remove(path)
            kind.needs_full_write = False
            return
        if kind.needs_full_write:
            kind.index.write(path)
            kind.needs_full_write = False
        else:
            kind.index.sync(path)

    async def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        if self.state != "closed":
            async with self._lock:
                try:
                    self._persist()
                except Exception as e:
                    logger.warning(f"vector index: final save failed: {e}")
        self.state = "closed"

    # --------------------------------------------------------------- database

    async def _list_ids(self, kind_name: str) -> Dict[str, Optional[str]]:
        """Every record of a kind that has a vector, with its source (None for notes)."""
        table = TABLE_FOR_KIND[kind_name]
        columns = "id" if kind_name == "notes" else "id, source"
        rows = await self._query(
            f"SELECT {columns} FROM {table} WHERE embedding != none AND array::len(embedding) > 0"
        )
        listed: Dict[str, Optional[str]] = {}
        for row in rows or []:
            source = row.get("source")
            listed[str(row["id"])] = str(source) if source is not None else None
        return listed

    async def _fetch_vectors(
        self, kind_name: str, ids: Sequence[str]
    ) -> List[Dict[str, Any]]:
        if not ids:
            return []
        columns = "id, embedding" if kind_name == "notes" else "id, source, embedding"
        rows = await self._query(
            f"SELECT {columns} FROM $ids",
            {"ids": [self._record_id(record_id) for record_id in ids]},
        )
        return list(rows or [])

    # ------------------------------------------------------------ mutation

    def _remove_one(self, kind: _Kind, record_id: str) -> bool:
        index_id = kind.unregister(record_id)
        if index_id is None:
            return False
        if kind.index is not None:
            try:
                kind.index.remove(index_id)
            except Exception as e:
                logger.warning(f"vector index: removing {record_id} failed: {e}")
        return True

    def _add_rows(
        self, kind: _Kind, rows: List[Dict[str, Any]], *, replace: bool
    ) -> int:
        """Add the vectors in ``rows`` to ``kind``. Caller holds the lock."""
        added = 0
        for dim, entries in _unit_rows(rows).items():
            keep: List[Tuple[str, Optional[str], np.ndarray]] = []
            seen: Set[str] = set()
            for record_id, source, vector in entries:
                if record_id in seen:
                    continue
                seen.add(record_id)
                if record_id in kind.by_record:
                    if not replace:
                        continue
                    self._remove_one(kind, record_id)
                index_id = int_id(record_id)
                other = kind.by_int.get(index_id)
                if other is not None and other != record_id:
                    logger.error(
                        f"vector index: id hash collision between {record_id} and {other}; "
                        f"{record_id} left out of the index"
                    )
                    continue
                keep.append((record_id, source, vector))
            if not keep:
                continue

            if kind.index is None:
                kind.index = _index_class()(bit_width=self.bit_width)
                kind.needs_full_write = True
            elif kind.index.dim is not None and kind.index.dim != dim:
                logger.warning(
                    f"vector index: {kind.name} vectors changed from {kind.index.dim} to {dim} "
                    f"numbers; that index starts over with the new vectors "
                    "(run POST /api/vector-index/rebuild once every record is re-embedded)"
                )
                kind.reset()
                kind.index = _index_class()(bit_width=self.bit_width)
                kind.needs_full_write = True

            matrix = np.stack([vector for _, _, vector in keep]).astype(np.float32)
            ids = np.array(
                [int_id(record_id) for record_id, _, _ in keep], dtype=np.uint64
            )
            kind.index.add_with_ids(matrix, ids)
            for (record_id, source, _), index_id in zip(keep, ids.tolist()):
                kind.register(record_id, int(index_id), source)
            added += len(keep)
        return added

    async def _calibrate_if_empty(self, kind: _Kind, ids: Sequence[str]) -> None:
        """Fit the quantiser on a random sample before the first add.

        The library's guidance: a uniform random draw of about 1,024 vectors,
        fitted before adding. A sorted or clustered sample (one source's
        chunks, say) would hurt recall, so the draw is over every id about to
        be added, never the first batch.
        """
        if kind.index is not None and kind.index.dim is not None:
            return
        if not ids:
            return
        sample_ids = list(ids)
        if len(sample_ids) > self.calibration_sample:
            sample_ids = random.Random(0).sample(sample_ids, self.calibration_sample)
        rows = await self._fetch_vectors(kind.name, sample_ids)
        grouped = _unit_rows(rows)
        if not grouped:
            return
        dim, entries = max(grouped.items(), key=lambda item: len(item[1]))
        sample = np.stack([vector for _, _, vector in entries]).astype(np.float32)
        async with self._lock:
            if kind.index is not None and kind.index.dim is not None:
                return
            if kind.index is None:
                kind.index = _index_class()(bit_width=self.bit_width)
                kind.needs_full_write = True
            kind.index.calibrate(sample)
        logger.info(
            f"vector index: {kind.name} calibrated on {sample.shape[0]} vectors of {dim} numbers"
        )

    async def _add_from_db(self, kind: _Kind, ids: Sequence[str]) -> int:
        if not ids:
            return 0
        await self._calibrate_if_empty(kind, ids)
        added = 0
        for batch in _batches(list(ids), self.fetch_batch):
            rows = await self._fetch_vectors(kind.name, batch)
            async with self._lock:
                added += self._add_rows(kind, rows, replace=False)
        return added

    async def _reconcile(self, kind: _Kind) -> Dict[str, int]:
        """Make the index hold exactly the records the database holds, by id."""
        listed = await self._list_ids(kind.name)
        have = set(kind.by_record)
        to_remove = [record_id for record_id in have if record_id not in listed]
        to_add = [record_id for record_id in listed if record_id not in have]
        async with self._lock:
            for record_id in to_remove:
                self._remove_one(kind, record_id)
        added = await self._add_from_db(kind, to_add)
        async with self._lock:
            self._persist()
        return {
            "added": added,
            "removed": len(to_remove),
            "kept": len(have) - len(to_remove),
        }

    async def _rebuild_kind(self, kind: _Kind) -> Dict[str, int]:
        async with self._lock:
            kind.reset()
        return await self._reconcile(kind)

    # ------------------------------------------------------------- public API

    def is_ready(self) -> bool:
        return self.state == "ready"

    def contains(self, record_id: str) -> bool:
        kind_name = kind_of(record_id)
        return (
            kind_name is not None and str(record_id) in self.kinds[kind_name].by_record
        )

    async def check(self, *, rebuilding: bool = False) -> Dict[str, Dict[str, int]]:
        """Repair the index against the database and mark it ready.

        Chunks are compared by id (a re-embedded source gets new chunk ids, so
        the id set tells the whole story, and the table is too large to
        re-read). Insights and notes keep their record id when re-embedded,
        so those two small kinds are re-read completely.
        """
        if self.state == "closed":
            raise RuntimeError("vector index is not open")
        started = time.perf_counter()
        self.state = "rebuilding" if rebuilding else "checking"
        report: Dict[str, Dict[str, int]] = {}
        try:
            chunks = self.kinds["chunks"]
            if chunks.needs_rebuild:
                report["chunks"] = await self._rebuild_kind(chunks)
            else:
                report["chunks"] = await self._reconcile(chunks)
            report["insights"] = await self._rebuild_kind(self.kinds["insights"])
            report["notes"] = await self._rebuild_kind(self.kinds["notes"])
        except Exception as e:
            self.state = "error"
            self.last_error = str(e)
            logger.error(f"vector index: check failed: {e}")
            logger.exception(e)
            raise
        elapsed = time.perf_counter() - started
        self.state = "ready"
        self.last_error = None
        self.last_check = {"seconds": round(elapsed, 2), "report": report}
        logger.info(
            "vector index ready: {} (added {}, removed {}) in {:.1f}s, engine={}, dir={}".format(
                ", ".join(f"{k.count} {name}" for name, k in self.kinds.items()),
                "/".join(str(report[name]["added"]) for name in KINDS),
                "/".join(str(report[name]["removed"]) for name in KINDS),
                elapsed,
                os.getenv("OPEN_NOTEBOOK_VECTOR_ENGINE", "scan"),
                self.directory,
            )
        )
        return report

    async def rebuild(self) -> Dict[str, Dict[str, int]]:
        """Throw the index away and build it again from the database."""
        if self.state == "closed":
            raise RuntimeError("vector index is not open")
        async with self._lock:
            for kind in self.kinds.values():
                kind.reset()
        return await self.check(rebuilding=True)

    def start_check(self) -> "asyncio.Task":
        """Run ``check()`` in the background (API startup)."""

        async def run() -> None:
            try:
                await self.check()
            except Exception:
                pass  # already logged by check()

        self._task = asyncio.create_task(run())
        return self._task

    async def upsert(
        self, ids: Iterable[str], replace_sources: Iterable[str] = ()
    ) -> Dict[str, int]:
        """Add or replace the vectors of ``ids``, read from the database.

        ``replace_sources``: sources whose existing chunk entries go first
        (a re-embedded source gets brand-new chunk ids).
        """
        ids = [str(record_id) for record_id in ids]
        removed = 0
        async with self._lock:
            chunks = self.kinds["chunks"]
            for source in replace_sources:
                for index_id in list(chunks.by_source.get(str(source), ())):
                    record_id = chunks.by_int.get(index_id)
                    if record_id is not None and self._remove_one(chunks, record_id):
                        removed += 1
        added = 0
        missing = 0
        for kind_name, kind_ids in group_by_kind(ids).items():
            kind = self.kinds[kind_name]
            for batch in _batches(kind_ids, self.fetch_batch):
                rows = await self._fetch_vectors(kind_name, batch)
                found = {str(row["id"]) for row in rows if row.get("embedding")}
                missing += sum(1 for record_id in batch if record_id not in found)
                async with self._lock:
                    added += self._add_rows(kind, rows, replace=True)
        async with self._lock:
            self._persist()
        return {"added": added, "removed": removed, "missing": missing}

    async def remove(
        self, ids: Iterable[str] = (), sources: Iterable[str] = ()
    ) -> Dict[str, int]:
        """Drop the given records, and every chunk and insight of the given sources."""
        removed = 0
        async with self._lock:
            for record_id in ids:
                kind_name = kind_of(record_id)
                if kind_name is not None and self._remove_one(
                    self.kinds[kind_name], str(record_id)
                ):
                    removed += 1
            for source in sources:
                for kind_name in ("chunks", "insights"):
                    kind = self.kinds[kind_name]
                    for index_id in list(kind.by_source.get(str(source), ())):
                        record_id = kind.by_int.get(index_id)
                        if record_id is not None and self._remove_one(kind, record_id):
                            removed += 1
            self._persist()
        return {"removed": removed}

    def search(
        self,
        query: Sequence[float],
        k: int,
        kinds: Sequence[str],
        allow_sources: Optional[Iterable[str]] = None,
        allow_notes: Optional[Iterable[str]] = None,
    ) -> Dict[str, List[Tuple[str, Optional[str], float]]]:
        """Top-``k`` candidates per kind: (record id, source id or None, index score).

        ``allow_sources`` restricts chunks and insights to those sources;
        ``allow_notes`` restricts notes to those note ids. An allowlist that
        matches nothing yields no candidates for that kind.
        """
        vector = np.asarray(query, dtype=np.float32).reshape(1, -1)
        norm = float(np.linalg.norm(vector))
        out: Dict[str, List[Tuple[str, Optional[str], float]]] = {
            name: [] for name in kinds
        }
        if not norm > 0.0:
            return out
        vector = vector / norm
        for name in kinds:
            kind = self.kinds[name]
            if kind.index is None or kind.index.dim is None or kind.count == 0:
                continue
            if kind.index.dim != vector.shape[1]:
                raise ValueError(
                    f"the query has {vector.shape[1]} numbers, the {name} index holds "
                    f"vectors of {kind.index.dim}"
                )
            allowlist = None
            if name in ("chunks", "insights") and allow_sources is not None:
                allowed = [
                    index_id
                    for source in allow_sources
                    for index_id in kind.by_source.get(str(source), ())
                ]
                if not allowed:
                    continue
                allowlist = np.array(allowed, dtype=np.uint64)
            elif name == "notes" and allow_notes is not None:
                allowed = [
                    kind.by_record[str(note)][0]
                    for note in allow_notes
                    if str(note) in kind.by_record
                ]
                if not allowed:
                    continue
                allowlist = np.array(allowed, dtype=np.uint64)
            scores, ids = kind.index.search(vector, int(k), allowlist=allowlist)
            hits: List[Tuple[str, Optional[str], float]] = []
            for score, index_id in zip(scores[0].tolist(), ids[0].tolist()):
                record_id = kind.by_int.get(int(index_id))
                if record_id is None:
                    continue
                hits.append((record_id, kind.by_record[record_id][1], float(score)))
            out[name] = hits
        return out

    def status(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "engine": (os.getenv("OPEN_NOTEBOOK_VECTOR_ENGINE") or "scan")
            .strip()
            .lower(),
            "directory": self.directory,
            "counts": {name: kind.count for name, kind in self.kinds.items()},
            "dims": {name: kind.dim for name, kind in self.kinds.items()},
            "last_error": self.last_error,
            "last_check": self.last_check,
        }


# ------------------------------------------------------------ the API's store

_STORE: Optional[VectorStore] = None


def get() -> Optional[VectorStore]:
    return _STORE


def is_open() -> bool:
    return _STORE is not None and _STORE.state != "closed"


async def open_from_env() -> Optional[VectorStore]:
    """Open the store at OPEN_NOTEBOOK_VECTOR_INDEX_DIR and start the background check."""
    global _STORE
    directory = os.getenv("OPEN_NOTEBOOK_VECTOR_INDEX_DIR") or DEFAULT_DIR
    store = VectorStore(directory)
    await store.open()
    _STORE = store
    store.start_check()
    return store


async def close() -> None:
    global _STORE
    if _STORE is not None:
        await _STORE.close()
        _STORE = None
