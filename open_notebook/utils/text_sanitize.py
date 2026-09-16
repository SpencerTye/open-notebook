"""LOCAL addition (custom build, 2026-09-15): text clean-up before database writes.

SurrealDB refuses to store a string that contains the null character (code 0)
and answers with "Serialization error: to be serialized string contained a
null byte". PDF text extraction emits that character wherever a glyph in the
file's fonts has no character mapping, so a paper with a single such glyph was
extracted in full and then failed at the save step, retried 15 times, and left
a record that could neither be read back nor deleted from the app.

The character carries no content. Removing it, and only it, before the save is
the whole fix. See open_notebook/graphs/source.py (save_source) for the call.
"""

from typing import Optional

NULL_CHARACTER = "\x00"


def strip_null_bytes(text: Optional[str]) -> Optional[str]:
    """Return ``text`` without any null characters; None and clean text pass through."""
    if text is None or NULL_CHARACTER not in text:
        return text
    return text.replace(NULL_CHARACTER, "")
