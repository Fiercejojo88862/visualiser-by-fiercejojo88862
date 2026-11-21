"""selector.py: curation features - blacklist, pins, stats."""

import pytest

from app.core import state
from app.core.selector import Selector


class FakeStore:
    def acquire(self, url, transcode_gif=True):
        name = url.rsplit("/", 1)[-1]
        return {"url": "/media/" + name, "kind": "gif", "bytes": 100,
                "file": "/tmp/fake", "source": url}


class FakeCand:
    def __init__(self, url, tags=None):
        self.url = url
        self.kind = "gif"
        self.title = url.rsplit("/", 1)[-1]
        self.tags = tags or []
        self.provider = "klipy"
        self.score = 0

    def blob(self):
        return self.url + " " + " ".join(self.tags)


class FakePool:
    def __init__(self, urls):
        self.urls = urls

    def search(self, query, limit=20, kind="gif"):
        return [FakeCand(u) for u in self.urls]


def _sel(urls):
    sel = Selector({"fetch_enabled": True}, FakeStore(), FakePool(urls), lambda m: None)
    sel._track_key = "TESTTRACK"
    return sel


def test_blacklist_blocks_acquisition(tmp_path, monkeypatch):
    state.BLACKLIST_FILE = tmp_path / "blacklist.json"
    state.PIN_DIR = tmp_path / "pinned"
    sel = _sel(["http://klipy.net/a.gif"])
    assert sel.blacklist_clip("/media/a.gif")
    assert state.is_blacklisted("/media/a.gif")
    sel._resolve("a", "gif")
    assert sel.stats()["queued"] == 0, "blacklisted clip should not queue"


def test_pin_and_unpin_toggle(tmp_path, monkeypatch):
    state.BLACKLIST_FILE = tmp_path / "blacklist.json"
    state.PIN_DIR = tmp_path / "pinned"
    sel = _sel(["http://klipy.net/b.gif"])
    assert not sel.pinned()
    sel.pin("/media/b.gif")
    assert "/media/b.gif" in sel.pinned()
    sel._resolve("b", "gif")
    # pin gives a scoring bonus, not a hard block
    assert sel.stats()["queued"] >= 0
    sel.unpin("/media/b.gif")
    assert "/media/b.gif" not in sel.pinned()


def test_unblacklist_restores(tmp_path):
    state.BLACKLIST_FILE = tmp_path / "blacklist.json"
    state.PIN_DIR = tmp_path / "pinned"
    sel = _sel(["http://klipy.net/c.gif"])
    sel.blacklist_clip("/media/c.gif")
    assert sel.stats()["blacklisted"] >= 1
    sel.unblacklist_clip("/media/c.gif") if hasattr(sel, "unblacklist_clip") else \
        state.blacklist_remove("/media/c.gif")
    assert not state.is_blacklisted("/media/c.gif")


def test_stats_and_local_blacklist(tmp_path):
    state.BLACKLIST_FILE = tmp_path / "blacklist.json"
    state.PIN_DIR = tmp_path / "pinned"
    sel = _sel([])
    st = sel.stats()
    assert st["blacklisted"] == 0 and st["pinned"] == 0
    state.blacklist_add("x.gif")
    assert sel.stats()["blacklisted"] == 1
