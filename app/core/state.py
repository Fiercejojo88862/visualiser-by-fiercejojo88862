"""Persistent user curation state.

Two JSON stores in the state dir (never auto-cleaned aggressively - the
blacklist is exactly what the user asked for, the pins are per-track timelines):

- blacklist.json : clip urls (and their content hashes) the user has X-ed away.
                   They never resurface for later plays of that song or mood.
- pinned/<key>.json : clips pinned to a track's timeline by the user (F key).
                      The selector prioritises them on choruses/drops.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from ..config import STATE_DIR

log = logging.getLogger("state")

BLACKLIST_FILE = STATE_DIR / "blacklist.json"
PIN_DIR = STATE_DIR / "pinned"


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(path)
    except Exception:
        log.exception("state save failed: %s", path)


def blacklist() -> dict:
    return _load(BLACKLIST_FILE)


def _digest_in(data: dict, url: str) -> bool:
    if not url:
        return False
    from .media import digest
    try:
        d = digest(url)
    except Exception:
        return False
    return d in data.get("hashes", [])


def is_blacklisted(url: str) -> bool:
    if not url:
        return False
    data = blacklist()
    if url in data.get("urls", []):
        return True
    return _digest_in(data, url)


def blacklist_add(url: str) -> int:
    if not url or is_blacklisted(url):
        return len(blacklist().get("urls", []))
    data = blacklist()
    data.setdefault("urls", []).append(url)
    # Also key by content hash: the deck url /media/<digest><ext> and the
    # provider url both point at the same clip, so users can blacklist either.
    from .media import digest
    try:
        h = digest(url)
        data.setdefault("hashes", []).append(h)
    except Exception:
        pass
    data["updated"] = time.time()
    _save(BLACKLIST_FILE, data)
    return len(data.get("urls", []))


def blacklist_remove(url: str) -> bool:
    data = blacklist()
    urls = [u for u in data.get("urls", []) if u != url]
    if len(urls) == len(data.get("urls", [])):
        return False
    data["urls"] = urls
    _save(BLACKLIST_FILE, data)
    return True


def blacklist_all() -> list[str]:
    return list(blacklist().get("urls", []))


def pins_for(track_key: str) -> set[str]:
    return set(_load(PIN_DIR / f"{track_key}.json").get("pins", []))


def pin_add(track_key: str, url: str) -> bool:
    if not track_key or not url:
        return False
    path = PIN_DIR / f"{track_key}.json"
    data = _load(path)
    pins = set(data.get("pins", []))
    if url in pins:
        return False
    pins.add(url)
    data["pins"] = sorted(pins)
    data["updated"] = time.time()
    _save(path, data)
    return True


def pin_remove(track_key: str, url: str) -> bool:
    path = PIN_DIR / f"{track_key}.json"
    data = _load(path)
    pins = set(data.get("pins", []))
    if url not in pins:
        return False
    pins.discard(url)
    data["pins"] = sorted(pins)
    _save(path, data)
    return True


def all_pins() -> dict[str, set[str]]:
    out = {}
    for f in PIN_DIR.glob("*.json"):
        data = _load(f)
        if data.get("pins"):
            out[f.stem] = set(data["pins"])
    return out


def stats() -> dict:
    data = blacklist()
    return {
        "blacklisted": len(data.get("urls", [])),
        "pinned_total": sum(len(p) for p in all_pins().values()),
        "tracks_with_pins": len(all_pins()),
    }
