"""Time-synced lyrics from LRCLIB (free, no API key, no Google)."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.parse
import urllib.request

from ..config import LYRIC_CACHE

log = logging.getLogger("lyrics")

BASE = "https://lrclib.net/api"
UA = "Visualiser-by-Fiercejojo88862/1.0 (personal use)"

TS = re.compile(r"\[(\d+):(\d+(?:[.:]\d+)?)\]")


def _get(url: str, timeout: float = 8.0):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def _parse_lrc(text: str) -> list[tuple[float, str]]:
    lines = []
    for raw in (text or "").splitlines():
        stamps = TS.findall(raw)
        if not stamps:
            continue
        body = TS.sub("", raw).strip()
        for m, s in stamps:
            try:
                sec = float(s)
            except ValueError:
                continue
            lines.append((int(m) * 60 + sec, body))
    lines.sort(key=lambda x: x[0])
    return lines


class Lyrics:
    def __init__(self, lines: list[tuple[float, str]], plain: str = "", source: str = ""):
        self.lines = lines
        self.plain = plain
        self.source = source

    def line_at(self, pos: float) -> tuple[str, str]:
        """(current line, next line)"""
        if not self.lines:
            return "", ""
        idx = -1
        for i, (t, _) in enumerate(self.lines):
            if t <= pos + 0.05:
                idx = i
            else:
                break
        cur = self.lines[idx][1] if idx >= 0 else ""
        nxt = self.lines[idx + 1][1] if 0 <= idx + 1 < len(self.lines) else ""
        return cur, nxt

    @property
    def text_blob(self) -> str:
        if self.plain:
            return self.plain
        return "\n".join(line for _, line in self.lines)

    def distinctive_queries(self, n: int = 8) -> list[str]:
        """Phrases from the song, best-first, for media search."""
        seen, out = set(), []
        for _, text in self.lines:
            clean = sanitize(text)
            if len(clean) < 4 or clean.lower() in seen:
                continue
            seen.add(clean.lower())
            out.append(clean)
        # prefer mid-length phrases: short ones are noise, long ones over-fit
        out.sort(key=lambda q: abs(len(q) - 22))
        return out[:n]


def sanitize(text: str) -> str:
    text = re.sub(r"\(.*?\)|\[.*?\]", " ", text or "")
    text = re.sub(r"[^\w\s'\-]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:50]


def _cache_path(key: str):
    return LYRIC_CACHE / f"{key}.json"


def fetch(track, enabled: bool = True) -> Lyrics:
    """Look up synced lyrics. Cached on disk forever (lyrics do not change)."""
    if not enabled or not track.title:
        return Lyrics([])
    key = hashlib.sha1(f"{track.title}|{track.artist}|{track.album}".encode()).hexdigest()[:16]
    path = _cache_path(key)
    if path.exists():
        try:
            data = json.loads(path.read_text())
            return Lyrics([(float(t), l) for t, l in data.get("lines", [])],
                          data.get("plain", ""), data.get("source", "cache"))
        except Exception:
            pass

    queries = []
    params = {"track_name": track.title}
    if track.artist:
        params["artist_name"] = track.artist
    if track.album:
        params["album_name"] = track.album
    if track.duration:
        params["duration"] = int(round(track.duration))
    queries.append(BASE + "/get?" + urllib.parse.urlencode(params))
    queries.append(BASE + "/search?" + urllib.parse.urlencode({"q": f"{track.artist} {track.title}".strip()}))

    result = Lyrics([], source="miss")
    for url in queries:
        try:
            data = _get(url)
        except Exception as exc:
            log.info("lyric lookup failed: %s", exc)
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            synced = item.get("syncedLyrics") or ""
            if synced:
                result = Lyrics(_parse_lrc(synced), item.get("plainLyrics", ""), "lrclib")
                break
            if item.get("plainLyrics") and not result.lines:
                result = Lyrics([], item["plainLyrics"], "lrclib-plain")
        if result.lines:
            break
        time.sleep(0.2)

    # Only cache real hits: a transient 5xx must not poison the track forever.
    if result.lines or result.plain:
        try:
            path.write_text(json.dumps({
                "lines": result.lines, "plain": result.plain, "source": result.source,
            }))
        except Exception:
            pass
    else:
        log.info("no lyrics found for %s", track.label)
    return result
