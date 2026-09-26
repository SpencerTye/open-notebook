"""Tests for the null-character fix (open_notebook/...).

SurrealDB refuses to store a string containing the null character (code 0):
"Serialization error: to be serialized string contained a null byte". PDF
extraction emits that character where a glyph in the file's fonts has no
character mapping, so on 2026-09-15 a journal PDF extracted fine and then
failed at the save step, leaving a record that could not be read or deleted.

Two things are covered here:

1. ``open_notebook/utils/text_sanitize.py`` (new): ``strip_null_bytes`` removes
   only that character and leaves everything else exactly as it was.
2. ``open_notebook/graphs/source.py`` (LOCAL change in
   ``save_source``): the extracted content and title go through
   ``strip_null_bytes`` before the record is saved. That module imports the
   whole app, so the wiring is checked by parsing the file, not importing it.
"""

import ast
import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1] / "open_notebook"
SANITIZE_PATH = REPO_ROOT / "utils" / "text_sanitize.py"
SOURCE_GRAPH_PATH = REPO_ROOT / "graphs" / "source.py"


def load_sanitize():
    spec = importlib.util.spec_from_file_location("text_sanitize", SANITIZE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_removes_null_characters_and_nothing_else():
    strip = load_sanitize().strip_null_bytes
    assert strip("ab\x00c") == "abc"
    assert strip("\x00\x00x\x00") == "x"
    assert strip("Fig. 1 – café · \t\n") == "Fig. 1 – café · \t\n"


def test_none_and_empty_pass_through():
    strip = load_sanitize().strip_null_bytes
    assert strip(None) is None
    assert strip("") == ""


def test_clean_text_is_returned_unchanged_object():
    strip = load_sanitize().strip_null_bytes
    text = "no null here"
    assert strip(text) is text


def _calls_in(func_node):
    """Yield (function-name, argument-source) for every call inside a function."""
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            yield node.func.id, [ast.unparse(a) for a in node.args]


def test_save_source_strips_content_and_title_before_saving():
    tree = ast.parse(SOURCE_GRAPH_PATH.read_text(encoding="utf-8"))
    imported = any(
        isinstance(n, ast.ImportFrom)
        and n.module == "open_notebook.utils.text_sanitize"
        and any(a.name == "strip_null_bytes" for a in n.names)
        for n in ast.walk(tree)
    )
    assert imported, "source.py must import strip_null_bytes"

    save_source = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "save_source"
    )
    calls = list(_calls_in(save_source))
    assert ("strip_null_bytes", ["extraction.content"]) in calls
    assert ("strip_null_bytes", ["extraction.title"]) in calls

    # The stripped content must be what is assigned to source.full_text.
    assigns = [
        ast.unparse(n) for n in ast.walk(save_source) if isinstance(n, ast.Assign)
    ]
    assert "source.full_text = strip_null_bytes(extraction.content)" in assigns
    assert "source.title = strip_null_bytes(extraction.title)" in assigns
