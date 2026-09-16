"""LOCAL: how a writer tells the API's TurboVec index that vectors changed.

The API process owns the index (see ``store.py``). Inside that process the
call goes straight to the store. From any other process (the worker, which
runs the embedding jobs) it is an HTTP call to the API on the container's
internal port, with the app password when one is set.

Both calls report success or failure and never raise: the vectors are already
in the database, and the API's startup check repairs the index if a message
was lost.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, Optional

from loguru import logger

DEFAULT_API_URL = "http://127.0.0.1:5055"
TIMEOUT_SECONDS = 60.0

UPSERT_PATH = "/api/vector-index/upsert"
REMOVE_PATH = "/api/vector-index/remove"


def api_base_url() -> str:
    return (
        (os.getenv("OPEN_NOTEBOOK_VECTOR_API_URL") or DEFAULT_API_URL)
        .strip()
        .rstrip("/")
    )


def _password() -> Optional[str]:
    try:
        from open_notebook.utils.encryption import get_secret_from_env

        return get_secret_from_env("OPEN_NOTEBOOK_PASSWORD")
    except Exception:
        path = os.getenv("OPEN_NOTEBOOK_PASSWORD_FILE")
        if path:
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    value = handle.read().strip()
                if value:
                    return value
            except OSError:
                pass
        return os.getenv("OPEN_NOTEBOOK_PASSWORD") or None


def auth_headers() -> Dict[str, str]:
    password = _password()
    return {"Authorization": f"Bearer {password}"} if password else {}


def _local_store():
    """The store when this process is the API and has it open; else None."""
    try:
        from open_notebook.vector_index import store as store_module
    except Exception:
        return None
    store = store_module.get()
    if store is None or getattr(store, "state", "closed") == "closed":
        return None
    return store


async def _post(path: str, payload: Any) -> Optional[dict]:
    import httpx

    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        response = await client.post(
            api_base_url() + path, json=payload, headers=auth_headers()
        )
        response.raise_for_status()
        try:
            return response.json()
        except ValueError:
            return None


async def index_upsert(ids: Iterable[str], replace_sources: Iterable[str] = ()) -> bool:
    """Record that the vectors of ``ids`` were written (added or replaced).

    ``replace_sources``: sources whose previous chunk entries must go first,
    because re-embedding a source writes brand-new chunk records.
    """
    ids = [str(record_id) for record_id in ids]
    replace_sources = [str(source) for source in replace_sources]
    if not ids and not replace_sources:
        return True
    try:
        store = _local_store()
        if store is not None:
            result = await store.upsert(ids, replace_sources=replace_sources)
        else:
            result = await _post(
                UPSERT_PATH, {"ids": ids, "replace_sources": replace_sources}
            )
        logger.debug(f"vector index: recorded {len(ids)} changed vector(s): {result}")
        return True
    except Exception as e:
        logger.warning(
            f"vector index: could not record {len(ids)} changed vector(s) ({e}); "
            "the API's next startup check repairs the index"
        )
        return False


async def index_remove(ids: Iterable[str] = (), sources: Iterable[str] = ()) -> bool:
    """Record that ``ids`` were deleted, and every chunk and insight of ``sources``."""
    ids = [str(record_id) for record_id in ids]
    sources = [str(source) for source in sources]
    if not ids and not sources:
        return True
    try:
        store = _local_store()
        if store is not None:
            result = await store.remove(ids=ids, sources=sources)
        else:
            result = await _post(REMOVE_PATH, {"ids": ids, "sources": sources})
        logger.debug(f"vector index: recorded removal: {result}")
        return True
    except Exception as e:
        logger.warning(
            f"vector index: could not record a removal ({e}); "
            "the API's next startup check repairs the index"
        )
        return False
