"""Tests for open_notebook/vector_index/ids.py.

TurboVec identifies a vector by an unsigned 64-bit integer. The app identifies
a record by a string such as ``source_embedding:0zqim8v38amup195oac9``. The
mapping between the two must be deterministic (no counter to persist across
restarts) and collision-free over the ids the library actually holds.
"""

import importlib.util
import random
import string
from pathlib import Path

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "open_notebook"
    / "vector_index"
    / "ids.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("vector_ids_overlay", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def surreal_style_ids(count: int, table: str, seed: int = 7):
    """Ids shaped like SurrealDB's generated ones: 20 lowercase alphanumerics."""
    rng = random.Random(seed)
    alphabet = string.ascii_lowercase + string.digits
    return [f"{table}:{''.join(rng.choices(alphabet, k=20))}" for _ in range(count)]


def test_int_id_is_deterministic_across_calls_and_loads():
    first = load_module()
    second = load_module()
    record = "source_embedding:0zqim8v38amup195oac9"

    assert first.int_id(record) == second.int_id(record)
    assert first.int_id(record) == first.int_id(record)


def test_int_id_fits_an_unsigned_64_bit_integer():
    ids = load_module()

    for record in surreal_style_ids(500, "source_embedding"):
        value = ids.int_id(record)
        assert isinstance(value, int)
        assert 0 <= value < 2**64


def test_int_id_has_no_collisions_over_a_library_sized_set():
    """7,448 chunks is the current library; the space is 2^64, so any collision
    here would mean the hash is broken, not unlucky."""
    ids = load_module()
    records = (
        surreal_style_ids(7448, "source_embedding")
        + surreal_style_ids(204, "source_insight")
        + surreal_style_ids(50, "note")
    )

    values = {ids.int_id(record) for record in records}

    assert len(values) == len(records)


def test_different_tables_with_the_same_key_get_different_ids():
    ids = load_module()

    assert ids.int_id("source_insight:abc") != ids.int_id("note:abc")


def test_kind_is_derived_from_the_table_prefix():
    ids = load_module()

    assert ids.kind_of("source_embedding:abc") == "chunks"
    assert ids.kind_of("source_insight:abc") == "insights"
    assert ids.kind_of("note:abc") == "notes"
    assert ids.kind_of("source:abc") is None
    assert ids.kind_of("garbage") is None


def test_group_by_kind_drops_ids_of_other_tables():
    ids = load_module()

    grouped = ids.group_by_kind(
        ["source_embedding:a", "note:n", "source:s", "source_insight:i", "note:m"]
    )

    assert grouped == {
        "chunks": ["source_embedding:a"],
        "insights": ["source_insight:i"],
        "notes": ["note:n", "note:m"],
    }
