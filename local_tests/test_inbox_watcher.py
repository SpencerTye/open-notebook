"""Tests for the inbox drop-folder watcher (scripts/inbox_watcher.py).

The watcher talks to Open Notebook through a small client object; tests inject
a fake so no server is needed.
"""

import importlib.util
import sys
from pathlib import Path

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "inbox_watcher.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("inbox_watcher", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["inbox_watcher"] = module
    spec.loader.exec_module(module)
    return module


class FakeApi:
    def __init__(self, notebooks=None, fail_upload=False):
        self.notebooks = list(notebooks or [])
        self.uploads = []
        self.created = []
        self.fail_upload = fail_upload

    def list_notebooks(self):
        return list(self.notebooks)

    def create_notebook(self, name):
        nb = {
            "id": f"notebook:{len(self.notebooks) + 1}",
            "name": name,
            "archived": False,
        }
        self.notebooks.append(nb)
        self.created.append(name)
        return nb

    def upload_file(self, path, notebook_id):
        if self.fail_upload:
            raise RuntimeError("HTTP 415: Unsupported file type")
        self.uploads.append((Path(path).name, notebook_id))
        return {"id": "source:1"}


def make_watcher(tmp_path, api):
    m = load_module()
    return m, m.Watcher(m.Config(inbox=tmp_path), api)


NB = {"id": "notebook:1", "name": "Papers", "archived": False}


def test_sanitize_name_makes_windows_safe_folder_names():
    m = load_module()
    assert m.sanitize_name("Dissertation: Content?") == "Dissertation_ Content_"
    assert m.sanitize_name("  trailing dots... ") == "trailing dots"
    assert m.sanitize_name("a/b\\c") == "a_b_c"


def test_sync_folders_creates_one_folder_per_active_notebook(tmp_path):
    api = FakeApi(
        [
            {"id": "notebook:1", "name": "Dissertation Content", "archived": False},
            {"id": "notebook:2", "name": "Old stuff", "archived": True},
        ]
    )
    _, w = make_watcher(tmp_path, api)
    w.sync_folders()
    assert (tmp_path / "Dissertation Content").is_dir()
    assert not (tmp_path / "Old stuff").exists()


def test_new_file_uploads_only_after_it_stops_changing(tmp_path):
    api = FakeApi([NB])
    _, w = make_watcher(tmp_path, api)
    f = tmp_path / "Papers" / "paper.pdf"
    f.parent.mkdir()
    f.write_bytes(b"%PDF-1.4 partial")
    w.tick()
    assert api.uploads == []  # first sighting: could still be copying
    f.write_bytes(b"%PDF-1.4 partial, and more bytes arrived")
    w.tick()
    assert api.uploads == []  # size changed since last look: wait again
    w.tick()
    assert api.uploads == [("paper.pdf", "notebook:1")]
    assert (tmp_path / "_done" / "Papers" / "paper.pdf").is_file()
    assert not f.exists()


def test_failed_upload_moves_file_to_failed_with_an_error_note(tmp_path):
    api = FakeApi([NB], fail_upload=True)
    _, w = make_watcher(tmp_path, api)
    f = tmp_path / "Papers" / "bad.xyz"
    f.parent.mkdir()
    f.write_bytes(b"data")
    w.tick()
    w.tick()
    assert (tmp_path / "_failed" / "Papers" / "bad.xyz").is_file()
    note = (tmp_path / "_failed" / "Papers" / "bad.xyz.error.txt").read_text()
    assert "415" in note
    assert not f.exists()


def test_folder_without_a_matching_notebook_creates_the_notebook(tmp_path):
    api = FakeApi([])
    _, w = make_watcher(tmp_path, api)
    f = tmp_path / "New Topic" / "a.txt"
    f.parent.mkdir()
    f.write_text("hello")
    w.tick()
    w.tick()
    assert api.created == ["New Topic"]
    assert api.uploads == [("a.txt", "notebook:1")]


def test_folder_matching_is_case_insensitive_and_uses_sanitized_names(tmp_path):
    api = FakeApi([{"id": "notebook:7", "name": "Papers: 2026", "archived": False}])
    _, w = make_watcher(tmp_path, api)
    f = tmp_path / "papers_ 2026" / "a.txt"
    f.parent.mkdir()
    f.write_text("x")
    w.tick()
    w.tick()
    assert api.created == []
    assert api.uploads == [("a.txt", "notebook:7")]


def test_temporary_hidden_and_root_files_are_ignored(tmp_path):
    api = FakeApi([NB])
    _, w = make_watcher(tmp_path, api)
    d = tmp_path / "Papers"
    d.mkdir()
    for name in [
        ".hidden.pdf",
        "x.pdf.part",
        "y.crdownload",
        "~$word.docx",
        "Thumbs.db",
        "desktop.ini",
        "z.tmp",
    ]:
        (d / name).write_bytes(b"x")
    (tmp_path / "_done").mkdir()
    (tmp_path / "_done" / "old.pdf").write_bytes(b"x")
    (tmp_path / "README.txt").write_text("files in the inbox root are ignored")
    w.tick()
    w.tick()
    assert api.uploads == []


def test_archived_copy_never_overwrites_an_earlier_one(tmp_path):
    api = FakeApi([NB])
    _, w = make_watcher(tmp_path, api)
    (tmp_path / "_done" / "Papers").mkdir(parents=True)
    (tmp_path / "_done" / "Papers" / "p.pdf").write_bytes(b"old")
    f = tmp_path / "Papers" / "p.pdf"
    f.parent.mkdir()
    f.write_bytes(b"new")
    w.tick()
    w.tick()
    done = sorted(p.name for p in (tmp_path / "_done" / "Papers").iterdir())
    assert len(done) == 2
    assert (tmp_path / "_done" / "Papers" / "p.pdf").read_bytes() == b"old"


def test_api_outage_leaves_the_file_in_place_for_the_next_round(tmp_path):
    class DownApi(FakeApi):
        def list_notebooks(self):
            raise ConnectionError("api down")

    api = DownApi([NB])
    _, w = make_watcher(tmp_path, api)
    f = tmp_path / "Papers" / "p.pdf"
    f.parent.mkdir()
    f.write_bytes(b"x")
    w.tick()
    w.tick()
    assert f.exists()
    assert not (tmp_path / "_failed").exists()
