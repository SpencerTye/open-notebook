"""LOCAL: record ids <-> TurboVec ids.

TurboVec identifies a vector by an unsigned 64-bit integer. The app identifies
a record by a string such as ``source_embedding:0zqim8v38amup195oac9``. The
integer is the first 8 bytes of the blake2b hash of that string, so the
mapping is deterministic (nothing to persist, nothing to keep in step across
restarts) and the chance of two of the library's ids colliding is negligible
(the store still refuses a colliding id rather than overwriting).
"""

from __future__ import annotations

import hashlib
from typing import Dict, Iterable, List, Optional

KINDS = ("chunks", "insights", "notes")

TABLE_FOR_KIND: Dict[str, str] = {
    "chunks": "source_embedding",
    "insights": "source_insight",
    "notes": "note",
}

KIND_FOR_TABLE: Dict[str, str] = {table: kind for kind, table in TABLE_FOR_KIND.items()}


def int_id(record_id: str) -> int:
    """The index id for a record id: blake2b, first 8 bytes, little-endian."""
    digest = hashlib.blake2b(str(record_id).encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "little", signed=False)


def kind_of(record_id: str) -> Optional[str]:
    """Which index a record belongs to, from its table prefix; None otherwise."""
    table, separator, key = str(record_id).partition(":")
    if not separator or not key:
        return None
    return KIND_FOR_TABLE.get(table)


def group_by_kind(record_ids: Iterable[str]) -> Dict[str, List[str]]:
    """Split record ids by index kind, dropping ids of other tables."""
    grouped: Dict[str, List[str]] = {kind: [] for kind in KINDS}
    for record_id in record_ids:
        kind = kind_of(record_id)
        if kind is not None:
            grouped[kind].append(str(record_id))
    return grouped
