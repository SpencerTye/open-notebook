#!/usr/bin/env python3
"""
Inbox watcher. LOCAL addition to Open Notebook, not part of upstream.

A drop folder for sources. The folder is bind-mounted into the app container
(``open-notebook/inbox`` on the host, ``/app/inbox`` inside):

    inbox/
      README.txt                       written by this script
      <Notebook name>/                 one folder per notebook (created for you)
        paper.pdf                      drop files here
      _done/<Notebook name>/           files that were added successfully
      _failed/<Notebook name>/         files the app refused, plus <file>.error.txt

Every few seconds the watcher looks in each notebook folder. A file is picked
up once its size and timestamp have stopped changing between two looks (so a
file still being copied is left alone), uploaded through the app's own REST
API exactly like the "Add Source" dialog does (extraction and embedding run in
the background worker), and then moved to ``_done``. A folder whose name does
not match an existing notebook gets a new notebook of that name.

Run inside the container by supervisord (see custom/overlay/supervisord.conf).
Unit tests: custom/tests/test_inbox_watcher.py.

Environment:
    OPEN_NOTEBOOK_INBOX_DIR           default /app/inbox
    OPEN_NOTEBOOK_INBOX_API_URL       default http://localhost:5055
    OPEN_NOTEBOOK_INBOX_POLL_SECONDS  default 5
    OPEN_NOTEBOOK_INBOX_EMBED         default true (embed for vector search)
    OPEN_NOTEBOOK_PASSWORD            sent as the bearer token when set
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

LOG = logging.getLogger("inbox")

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SKIP_NAMES = {"thumbs.db", "desktop.ini"}
_SKIP_SUFFIXES = (".part", ".crdownload", ".tmp", ".partial", ".download")
_SKIP_PREFIXES = ("~$", ".")
_RESERVED_FOLDER_PREFIXES = ("_", ".")

README_TEXT = """Open Notebook inbox (drop folder)

Put a file into the folder named after the notebook you want it in, for example:
    Dissertation Reference Library\\some-paper.pdf

Within a few seconds it is added to that notebook, extracted and indexed, and
the file moves to _done\\<notebook>\\. If the app refuses it, it moves to
_failed\\<notebook>\\ next to a .error.txt that says why.

A folder that does not match an existing notebook becomes a new notebook with
that name. Files placed directly in this top folder are ignored.
Accepted file types are the app's own: PDF, Word, PowerPoint, Excel, CSV, EPUB,
text, Markdown, HTML, images (OCR). Audio and video need a speech-to-text model.
"""


def sanitize_name(name: str) -> str:
    """Make a notebook name safe as a folder name on Windows and Linux."""
    cleaned = _INVALID_CHARS.sub("_", name).strip()
    return cleaned.rstrip(". ").strip()


def _skip_file(name: str) -> bool:
    lower = name.lower()
    return (
        lower in _SKIP_NAMES
        or lower.endswith(_SKIP_SUFFIXES)
        or name.startswith(_SKIP_PREFIXES)
    )


def _unique_path(target: Path) -> Path:
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for n in range(1, 10_000):
        candidate = target.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not find a free name for {target}")


@dataclass
class Config:
    inbox: Path
    embed: bool = True
    poll_seconds: float = 5.0
    done_dir: str = "_done"
    failed_dir: str = "_failed"


class ApiClient:
    """The few Open Notebook REST calls the watcher needs (requests-based)."""

    def __init__(
        self, base_url: str, password: Optional[str] = None, timeout: float = 600.0
    ):
        import requests  # imported here so the unit tests do not need it

        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        if password:
            self.session.headers["Authorization"] = f"Bearer {password}"

    def health(self) -> bool:
        try:
            response = self.session.get(f"{self.base_url}/health", timeout=5)
            return response.ok
        except Exception:
            return False

    def list_notebooks(self) -> List[dict]:
        response = self.session.get(f"{self.base_url}/api/notebooks", timeout=30)
        response.raise_for_status()
        return response.json()

    def create_notebook(self, name: str) -> dict:
        response = self.session.post(
            f"{self.base_url}/api/notebooks",
            json={"name": name, "description": "Created by the inbox drop folder"},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def upload_file(self, path: Path, notebook_id: str, embed: bool = True) -> dict:
        data = {
            "type": "upload",
            "notebooks": json.dumps([notebook_id]),
            "embed": "true" if embed else "false",
            "async_processing": "true",
            "delete_source": "false",
        }
        with open(path, "rb") as handle:
            response = self.session.post(
                f"{self.base_url}/api/sources",
                data=data,
                files={"file": (path.name, handle)},
                timeout=self.timeout,
            )
        if not response.ok:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
        return response.json()


class Watcher:
    def __init__(self, config: Config, api) -> None:
        self.config = config
        self.api = api
        self._seen: Dict[Path, Tuple[int, int]] = {}

    # -- notebooks -----------------------------------------------------------

    def _active_notebooks(self) -> List[dict]:
        return [nb for nb in self.api.list_notebooks() if not nb.get("archived")]

    def sync_folders(self) -> None:
        """Create one drop folder per notebook so the operator never types a name."""
        for notebook in self._active_notebooks():
            folder = sanitize_name(notebook.get("name") or "")
            if folder:
                (self.config.inbox / folder).mkdir(parents=True, exist_ok=True)

    def _resolve_notebook(self, folder: str) -> dict:
        key = sanitize_name(folder).casefold()
        for notebook in self._active_notebooks():
            if sanitize_name(notebook.get("name") or "").casefold() == key:
                return notebook
        LOG.info("no notebook named %r; creating it", folder)
        return self.api.create_notebook(folder)

    # -- files ---------------------------------------------------------------

    def candidate_files(self) -> List[Tuple[Path, str]]:
        found: List[Tuple[Path, str]] = []
        if not self.config.inbox.is_dir():
            return found
        for folder in sorted(self.config.inbox.iterdir()):
            if not folder.is_dir() or folder.name.startswith(_RESERVED_FOLDER_PREFIXES):
                continue
            for path in sorted(folder.iterdir()):
                if path.is_file() and not _skip_file(path.name):
                    found.append((path, folder.name))
        return found

    def tick(self) -> None:
        """One look at the inbox. Uploads files that have been stable since the last look."""
        candidates = self.candidate_files()
        current = {path for path, _ in candidates}
        for stale in [p for p in self._seen if p not in current]:
            del self._seen[stale]

        for path, folder in candidates:
            try:
                stat = path.stat()
            except OSError:
                continue
            signature = (stat.st_size, stat.st_mtime_ns)
            if self._seen.get(path) != signature:
                self._seen[path] = signature  # first sighting or still changing: wait
                continue
            self._process(path, folder)

    def _process(self, path: Path, folder: str) -> None:
        try:
            notebook = self._resolve_notebook(folder)
        except Exception as exc:  # API down or refusing: try again next round
            LOG.warning("cannot resolve notebook for %s (%s); will retry", path, exc)
            return

        try:
            result = self.api.upload_file(path, notebook["id"])
        except Exception as exc:
            target = self._move(path, self.config.failed_dir, folder)
            target.with_name(target.name + ".error.txt").write_text(
                f"{time.strftime('%Y-%m-%d %H:%M:%S')}  upload of {path.name} "
                f"to notebook {notebook.get('name')!r} failed:\n{exc}\n",
                encoding="utf-8",
            )
            LOG.error("FAILED %s -> %s: %s", path.name, notebook.get("name"), exc)
        else:
            self._move(path, self.config.done_dir, folder)
            LOG.info(
                "added %s to %r (source %s)",
                path.name,
                notebook.get("name"),
                result.get("id"),
            )
        finally:
            self._seen.pop(path, None)

    def _move(self, path: Path, bucket: str, folder: str) -> Path:
        target_dir = self.config.inbox / bucket / folder
        target_dir.mkdir(parents=True, exist_ok=True)
        target = _unique_path(target_dir / path.name)
        shutil.move(str(path), str(target))
        return target


# -- entry point ---------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="[inbox] %(message)s", stream=sys.stdout
    )
    config = Config(
        inbox=Path(os.environ.get("OPEN_NOTEBOOK_INBOX_DIR", "/app/inbox")),
        embed=_env_bool("OPEN_NOTEBOOK_INBOX_EMBED", True),
        poll_seconds=float(os.environ.get("OPEN_NOTEBOOK_INBOX_POLL_SECONDS", "5")),
    )
    api = ApiClient(
        os.environ.get("OPEN_NOTEBOOK_INBOX_API_URL", "http://localhost:5055"),
        password=os.environ.get("OPEN_NOTEBOOK_PASSWORD") or None,
    )

    config.inbox.mkdir(parents=True, exist_ok=True)
    readme = config.inbox / "README.txt"
    if not readme.exists():
        readme.write_text(README_TEXT, encoding="utf-8")

    LOG.info(
        "watching %s every %.0f s (API %s)",
        config.inbox,
        config.poll_seconds,
        api.base_url,
    )
    while not api.health():
        LOG.info("waiting for the API to answer at %s ...", api.base_url)
        time.sleep(5)

    watcher = Watcher(config, api)
    last_sync = 0.0
    while True:
        now = time.monotonic()
        if now - last_sync > 60:
            try:
                watcher.sync_folders()
                last_sync = now
            except Exception as exc:
                LOG.warning("could not sync notebook folders: %s", exc)
        try:
            watcher.tick()
        except Exception:
            LOG.exception("inbox round failed")
        time.sleep(config.poll_seconds)


if __name__ == "__main__":
    sys.exit(main())
