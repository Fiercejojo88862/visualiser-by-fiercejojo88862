"""Media providers (KLIPY primary, GIPHY fallback) + rate limiting.

Tenor's public API was shut down by Google on 2026-06-30, so Tenor is not an
option. KLIPY (built by ex-Tenor engineers) has a free production key and a
near-identical API surface; GIPHY works too but production access is paid.

Response shapes are parsed tolerantly: we walk the JSON and accept any dict that
looks like a media item, preferring video renditions over tiny GIF thumbs. That
keeps the app working if a provider tweaks its schema.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ..config import QUERY_CACHE, STATE_DIR

log = logging.getLogger("fetch")

UA = "Visualiser-by-Fiercejojo88862/1.0 (personal use)"
MEDIA_RE = re.compile(r"\.(gif|mp4|webm|webp)(\?|$)", re.I)
VIDEO_RE = re.compile(r"\.(mp4|webm)(\?|$)", re.I)
TINY_RE = re.compile(r"(tiny|thumb|preview|placeholder|_xs|_sm)", re.I)
TITLE_KEYS = ("title", "content_description", "slug", "name", "description", "caption")
TAG_KEYS = ("tags", "keywords", "categories", "labels")
ID_KEYS = ("id", "slug", "itemurl", "url")
AD_KEYS = ("is_ad", "ad", "ads", "sponsored", "advertiser", "sponsored_by")


class RateLimiter:
    """File-backed token bucket so an app restart cannot refill the hour."""

    def __init__(self, per_hour: int):
        self.per_hour = int(per_hour or 0)
        self.path = STATE_DIR / "rate_limit.json"
        self._state = self._read()

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            if time.time() - data.get("start", 0) > 3600:
                raise ValueError
            return data
        except Exception:
            return {"start": time.time(), "count": 0}

    def _write(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._state))
        except Exception:
            pass

    @property
    def remaining(self) -> float:
        if self.per_hour <= 0:
            return float("inf")
        if time.time() - self._state.get("start", 0) > 3600:
            self._state = {"start": time.time(), "count": 0}
        return max(0, self.per_hour - self._state.get("count", 0))

    def allow(self, n: int = 1) -> bool:
        if self.per_hour <= 0:
            return True
        if self.remaining < n:
            return False
        self._state["count"] = self._state.get("count", 0) + n
        self._write()
        return True

    def snapshot(self) -> dict:
        return {"limit": self.per_hour, "used": self._state.get("count", 0),
                "remaining": self.remaining}


class Candidate:
    __slots__ = ("url", "kind", "title", "tags", "provider", "score")

    def __init__(self, url, kind, title="", tags=None, provider=""):
        self.url = url
        self.kind = kind
        self.title = title or ""
        self.tags = list(tags or [])
        self.provider = provider
        self.score = 0.0

    def blob(self) -> str:
        return f"{self.title} {' '.join(self.tags)}".lower()

    def as_dict(self) -> dict:
        return {"url": self.url, "kind": self.kind, "title": self.title,
                "tags": self.tags, "provider": self.provider, "score": round(self.score, 3)}


# -- tolerant parsing ------------------------------------------------------
def _looks_ad(d: dict) -> bool:
    for k, v in d.items():
        if k.lower() in AD_KEYS and v not in (False, None, 0, "", "0"):
            return True
    return False


def _strings(value) -> list[str]:
    out = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            out.extend(_strings(v))
    elif isinstance(value, (list, tuple)):
        for v in value:
            out.extend(_strings(v))
    return out


def _best_url(item: dict) -> str:
    urls = []
    for key, value in item.items():
        for s in _strings(value):
            if not isinstance(s, str) or not s.startswith("http"):
                continue
            if MEDIA_RE.search(urllib.parse.urlparse(s).path or s):
                urls.append((key.lower(), s))
    if not urls:
        return ""
    def rank(pair):
        key, url = pair
        video = 1 if VIDEO_RE.search(urllib.parse.urlparse(url).path or url) else 0
        tiny = -1 if TINY_RE.search(url) else 0
        hd = 1 if ("hd" in key or "medium" in key or "original" in key) else 0
        return (video, tiny, hd)
    urls.sort(key=rank, reverse=True)
    return urls[0][1]


def parse_candidates(payload, provider: str, limit: int) -> list[Candidate]:
    found: list[dict] = []

    def walk(node, depth=0):
        if depth > 8:
            return
        if isinstance(node, dict):
            url = _best_url(node)
            if url and (any(k in node for k in ID_KEYS) or any(k in node for k in TITLE_KEYS)
                        or isinstance(node.get("file"), dict)):
                found.append(node)
            for v in node.values():
                walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth + 1)

    walk(payload)

    out: list[Candidate] = []
    seen = set()
    for item in found:
        if _looks_ad(item):
            continue
        url = _best_url(item)
        if not url or url in seen:
            continue
        seen.add(url)
        title = ""
        for k in TITLE_KEYS:
            v = item.get(k)
            if isinstance(v, str) and v:
                title = v
                break
        tags: list[str] = []
        for k in TAG_KEYS:
            v = item.get(k)
            tags.extend(t for t in _strings(v) if isinstance(t, str) and len(t) < 40)
        out.append(Candidate(url, "video" if VIDEO_RE.search(url) else "gif",
                             title, tags, provider))
        if len(out) >= limit:
            break
    return out


class Provider:
    name = "base"
    base = ""

    def __init__(self, cfg: dict, limiter: RateLimiter):
        self.cfg = cfg
        self.limiter = limiter
        self.last_error = ""

    def configured(self) -> bool:
        raise NotImplementedError

    def search_url(self, query: str, limit: int, kind: str) -> str:
        raise NotImplementedError

    def search(self, query: str, limit: int = 20, kind: str = "gif") -> list[Candidate]:
        if not self.configured():
            self.last_error = "no API key configured"
            return []
        cached = self._cached(query, limit, kind)
        if cached is not None:
            return cached
        if not self.limiter.allow():
            self.last_error = "rate limit reached"
            log.warning("rate limit reached, skipping query %r", query)
            return []
        url = self.search_url(query, limit, kind)
        payload = None
        for attempt in range(2):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                           "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=12) as resp:
                    payload = json.loads(resp.read().decode("utf-8", "ignore"))
                break
            except urllib.error.HTTPError as exc:
                self.last_error = f"HTTP {exc.code}"
                if exc.code == 400 and attempt == 0:
                    # some providers reject unfamiliar query params: retry minimal
                    url = self.search_url(query, limit, kind, minimal=True)
                    continue
                if exc.code == 429:
                    self.limiter._state["count"] = self.limiter.per_hour  # noqa: SLF001
                    self.limiter._write()  # noqa: SLF001
                log.info("%s search failed: %s", self.name, exc)
                return []
            except Exception as exc:
                self.last_error = str(exc)
                log.info("%s search failed: %s", self.name, exc)
                return []
        if payload is None:
            return []
        items = parse_candidates(payload, self.name, limit)
        if items:
            self._store(query, limit, kind, items)
        return items

    # query cache: keyed on the *query*, so repeat moods/lyrics compound
    def _cache_key(self, query: str, limit: int, kind: str) -> str:
        import hashlib
        raw = f"{self.name}|{kind}|{limit}|{query.strip().lower()}"
        return hashlib.sha1(raw.encode()).hexdigest()[:20]

    def _cache_path(self, key: str) -> Path:
        return QUERY_CACHE / f"{self.name}-{key}.json"

    def _cached(self, query: str, limit: int, kind: str):
        path = self._cache_path(self._cache_key(query, limit, kind))
        if not path.exists():
            return None
        age_days = (time.time() - path.stat().st_mtime) / 86400
        if age_days > self.cfg.get("query_cache_days", 30):
            return None
        try:
            data = json.loads(path.read_text())
        except Exception:
            return None
        return [Candidate(c["url"], c["kind"], c["title"], c["tags"], c["provider"])
                for c in data]

    def _store(self, query: str, limit: int, kind: str, items: list[Candidate]) -> None:
        try:
            path = self._cache_path(self._cache_key(query, limit, kind))
            path.write_text(json.dumps([c.as_dict() for c in items]))
        except Exception:
            pass


class KlipyProvider(Provider):
    name = "klipy"
    base = "https://api.klipy.com/api/v1"

    def configured(self) -> bool:
        return bool(self.cfg.get("klipy_key"))

    def search_url(self, query: str, limit: int, kind: str = "gif", minimal: bool = False) -> str:
        key = urllib.parse.quote(self.cfg.get("klipy_key", ""), safe="")
        segment = {"gif": "gifs", "clip": "clips", "meme": "memes"}.get(kind, "gifs")
        params = {
            "q": query,
            "page": 1,
            "per_page": max(8, min(50, limit)),
            "customer_id": "fiercejojo88862",
        }
        if not minimal:
            filter_level = (self.cfg.get("content_filter") or "").strip()
            if filter_level:
                params["content_filter"] = filter_level
        return f"{self.base}/{key}/{segment}/search?" + urllib.parse.urlencode(params)


class GiphyProvider(Provider):
    name = "giphy"
    base = "https://api.giphy.com/v1"

    def configured(self) -> bool:
        return bool(self.cfg.get("giphy_key"))

    def search_url(self, query: str, limit: int, kind: str = "gif", minimal: bool = False) -> str:
        segment = "stickers" if kind == "meme" else "gifs"
        params = {
            "api_key": self.cfg.get("giphy_key", ""),
            "q": query,
            "limit": max(1, min(50, limit)),
            "offset": 0,
            "lang": "en",
        }
        rating = {"off": "r", "low": "pg-13", "medium": "pg", "high": "g"}.get(
            (self.cfg.get("content_filter") or "").lower(), "pg")
        params["rating"] = rating
        return f"{self.base}/{segment}/search?" + urllib.parse.urlencode(params)


def build_providers(cfg: dict) -> list[Provider]:
    limiter = RateLimiter(cfg.get("queries_per_hour", 100))
    order = [(cfg.get("provider") or "klipy").lower(), "giphy", "klipy"]
    providers: list[Provider] = []
    seen = set()
    for name in order:
        if name in seen:
            continue
        seen.add(name)
        if name == "klipy":
            providers.append(KlipyProvider(cfg, limiter))
        elif name == "giphy":
            providers.append(GiphyProvider(cfg, limiter))
    return providers


class ProviderPool:
    """Tries the preferred provider, falls back to the other one."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.providers = build_providers(cfg)

    @property
    def limiter(self) -> RateLimiter:
        return self.providers[0].limiter if self.providers else RateLimiter(0)

    def search(self, query: str, limit: int = 20, kind: str = "gif") -> list[Candidate]:
        for p in self.providers:
            if not p.configured():
                continue
            items = p.search(query, limit, kind)
            if items:
                return items
        return []

    def status(self) -> dict:
        return {
            "providers": [
                {"name": p.name, "configured": p.configured(), "error": p.last_error}
                for p in self.providers
            ],
            "rate": self.limiter.snapshot(),
        }
