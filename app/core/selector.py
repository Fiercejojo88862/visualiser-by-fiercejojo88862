"""Selection: turns a track + lyrics + audio mood into a queue of ready-to-play clips.

Ranking is keyword/tag based over provider metadata (the provider's own search is
the semantic match), biased by an audio-derived mood profile and damped by a
recency penalty so the same 3 gifs do not loop all night.
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from collections import deque

from . import lyrics as lyrics_mod
from .fetch import ProviderPool
from .media import GIF_EXT, IMG_EXT, VID_EXT, MediaStore, digest

log = logging.getLogger("selector")

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "than", "so", "to", "of", "in",
    "on", "at", "for", "with", "from", "by", "as", "is", "are", "was", "were", "be",
    "been", "am", "do", "does", "did", "have", "has", "had", "i", "me", "my", "you",
    "your", "he", "she", "it", "we", "they", "them", "his", "her", "its", "our",
    "that", "this", "these", "those", "not", "no", "yes", "oh", "yeah", "uh", "i'm",
    "don't", "can't", "won't", "let", "got", "get", "just", "like", "all", "up",
    "out", "down", "when", "what", "who", "how", "why", "there", "here", "now",
    "never", "ever", "still", "will", "would", "could", "should", "one", "two",
}

MOODS = {
    "frenetic": ["neon", "strobe", "fast", "hyper", "chaos", "racing", "spinning"],
    "energetic": ["dance", "party", "energy", "jump", "crowd", "fire", "confetti"],
    "euphoric": ["sunset", "glow", "sparkle", "celebration", "bloom", "shine"],
    "melancholy": ["rain", "sad", "alone", "empty", "grey", "tears", "window", "smoke"],
    "calm": ["ocean", "clouds", "slow", "float", "calm", "breathe", "waves", "space"],
    "dreamy": ["stars", "galaxy", "aurora", "neon", "fog", "soft", "dream", "light"],
    "gritty": ["city", "night", "street", "grime", "industrial", "dark", "smoke"],
}


def mood_for(bpm: float, energy: float, centroid: float, locked: bool) -> tuple[str, list[str]]:
    """Cheap audio-derived mood guess: tempo + loudness + spectral brightness."""
    if not locked or bpm <= 0:
        return ("dreamy" if centroid < 1500 else "euphoric", MOODS["dreamy"])
    if bpm >= 150 and energy > 0.55:
        return "frenetic", MOODS["frenetic"]
    if bpm >= 120 and energy > 0.45:
        return "energetic", MOODS["energetic"]
    if bpm >= 100:
        return ("euphoric" if centroid > 2000 else "gritty",
                MOODS["euphoric"] if centroid > 2000 else MOODS["gritty"])
    if energy < 0.35:
        return "melancholy", MOODS["melancholy"]
    return "calm", MOODS["calm"]


def keywords(text: str) -> list[str]:
    words = re.findall(r"[a-z']{3,}", (text or "").lower())
    return [w for w in words if w not in STOPWORDS][:8]


class Selector:
    def __init__(self, cfg: dict, store: MediaStore, pool: ProviderPool, emit):
        self.cfg = cfg
        self.store = store
        self.pool = pool
        self.emit = emit                     # callable(dict) -> pushed to renderer
        self._lock = threading.Lock()
        self._queue: deque[dict] = deque()
        self._pending_queue: deque[tuple[str, str]] = deque()
        self._pending: set[str] = set()
        self._last_mood_terms: list[str] = []
        self._recent: deque[str] = deque(maxlen=24)
        self._track_key = ""
        self._queries_used = 0
        self._clips_sent = 0
        self._last_pushed: list[dict] = []
        self._local_scan_t = 0.0
        self._scanning = False
        self._stop = threading.Event()
        self._workers: list[threading.Thread] = []
        self._random = random.Random()

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        for i in range(2):
            t = threading.Thread(target=self._work_loop, name=f"selector-{i}", daemon=True)
            t.start()
            self._workers.append(t)

    def stop(self) -> None:
        self._stop.set()

    def on_track(self, track, lyr: lyrics_mod.Lyrics | None = None) -> None:
        with self._lock:
            self._track_key = track.key
            self._queue.clear()
            self._pending.clear()
            self._recent.clear()
            self._pending_queue.clear()
            self._queries_used = 0
            self._clips_sent = 0
            self._last_pushed = []

        queries: list[tuple[str, str]] = []
        seeds: list[str] = []

        if lyr and lyr.lines:
            seeds = lyr.distinctive_queries(8)
            for line in seeds:
                queries.append((line, "gif"))
        if track.title:
            queries.append((lyrics_mod.sanitize(track.title), "gif"))
        if track.artist:
            queries.append((lyrics_mod.sanitize(track.artist), "gif"))

        local = self._local_library_items()
        if local:
            with self._lock:
                self._random.shuffle(local)
                self._queue.extend(local)
            self.emit({"t": "notice", "level": "info",
                       "text": f"{len(local)} clips from local library"})
            self._push_ready()

        for query, kind in queries:
            if query and len(query) >= 3:
                self._enqueue_query(query, kind)

        if not queries and not local:
            self.emit({"t": "notice", "level": "warn",
                       "text": "No media source configured - add a KLIPY key in settings"})

    def mood_refresh(self, mood: str, terms: list[str]) -> None:
        """Called when the audio mood shifts; queues a mood-based query."""
        with self._lock:
            if mood in self._pending:
                return
        phrase = " ".join(terms[:3]) or mood
        self._enqueue_query(phrase, self.cfg.get("clip_kind", "gif"))

    def _enqueue_query(self, query: str, kind: str) -> None:
        if not self.cfg.get("fetch_enabled", True):
            return
        with self._lock:
            if query in self._pending or self._queries_used >= self.cfg.get(
                    "max_queries_per_track", 12):
                return
            self._pending.add(query)
            self._queries_used += 1
            self._pending_queue.append((query, kind))
        self.emit({"t": "status", "queries_used": self._queries_used,
                   "local_files": self.local_count()})

    # -- workers -----------------------------------------------------------
    def _work_loop(self) -> None:
        while not self._stop.is_set():
            job = None
            with self._lock:
                if self._pending_queue:
                    job = self._pending_queue.popleft()
            if job is None:
                self._stop.wait(0.25)
                continue
            query, kind = job
            try:
                self._resolve(query, kind)
            except Exception:
                log.exception("query failed: %s", query)
            finally:
                with self._lock:
                    self._pending.discard(query)

    def _resolve(self, query: str, kind: str) -> None:
        candidates = self.pool.search(query, limit=24, kind=kind)
        if not candidates:
            return
        mood_terms = self._mood_terms
        for c in candidates:
            blob = c.blob()
            score = 0.0
            for word in keywords(query):
                if word in blob:
                    score += 2.0
            for term in mood_terms:
                if term in blob:
                    score += 1.0
            if c.kind == "video":
                score += 0.4
            with self._lock:
                if any(r in blob for r in list(self._recent)[:6]):
                    score -= 1.5
            c.score = score
        candidates.sort(key=lambda c: c.score, reverse=True)
        best = [c for c in candidates if c.score > 0][:6] or candidates[:4]
        self._random.shuffle(best)

        for c in best:
            with self._lock:
                if self._clips_sent >= self.cfg.get("max_clips_per_track", 24):
                    return
            media = self.store.acquire(c.url, transcode_gif=True)
            if not media:
                continue
            item = {
                "url": media["url"], "kind": media["kind"], "title": c.title,
                "tags": c.tags[:6], "provider": c.provider, "query": query,
            }
            with self._lock:
                self._queue.append(item)
                self._recent.append(c.blob()[:60])
                self._clips_sent += 1
            self._push_ready()

    @property
    def _mood_terms(self) -> list[str]:
        return self._last_mood_terms

    def set_mood_terms(self, terms: list[str]) -> None:
        self._last_mood_terms = list(terms)

    # -- delivery ----------------------------------------------------------
    def _push_ready(self) -> None:
        """Push buffered clips to the renderer when it is running low."""
        with self._lock:
            buffered = len(self._queue)
        if buffered == 0:
            return
        want = min(buffered, 3)
        items = []
        with self._lock:
            for _ in range(want):
                if self._queue:
                    items.append(self._queue.popleft())
        if items:
            with self._lock:
                self._last_pushed = list(items)
                self._clips_sent += len(items)
            self.emit({"t": "clips", "items": items})

    def request(self, count: int = 1) -> None:
        self._push_ready()
        self._maybe_scan_local()

    def resend(self) -> None:
        """Re-send the clips the renderer was last handed.

        A renderer that just connected (or reloaded) has an empty buffer, and the
        engine may already have drained its queue into the previous page. Without
        this the fresh page waits for the next provider query to show anything.
        """
        with self._lock:
            items = list(self._last_pushed)
        if items:
            self.emit({"t": "clips", "items": items})

    # -- local folder ------------------------------------------------------
    def _maybe_scan_local(self) -> None:
        """Pick up files dropped into the local folder without restarting.

        The folder is bind-mounted when the container starts, so a *path* change
        needs a restart - but copying new gifs into a folder that is already
        mounted used to require one too, because the library was only read when a
        track was prepared. Now a low queue triggers a rescan (rate limited, and on
        a worker thread because acquiring a new file runs ffmpeg).
        """
        folder = (self.cfg.get("local_library") or "").strip()
        if not folder:
            return
        now = time.monotonic()
        if now - self._local_scan_t < 20.0:
            return
        with self._lock:
            if self._scanning or len(self._queue) > 2:
                return
            self._scanning = True
        self._local_scan_t = now
        threading.Thread(target=self._scan_local_worker, name="local-scan",
                         daemon=True).start()

    def _scan_local_worker(self) -> None:
        try:
            items = self._local_library_items()
            with self._lock:
                known = {i.get("url") for i in self._queue}
                known.update(i.get("url") for i in self._last_pushed)
                fresh = [i for i in items if i.get("url") not in known]
                if fresh:
                    self._queue.extend(fresh)
            if fresh:
                log.info("local library: %d new clip(s) queued", len(fresh))
                self.emit({"t": "notice", "level": "info",
                           "text": f"{len(fresh)} new clip(s) from local library"})
                self._push_ready()
        except Exception:
            log.exception("local library rescan failed")
        finally:
            with self._lock:
                self._scanning = False

    def _local_library_items(self) -> list[dict]:
        folder = (self.cfg.get("local_library") or "").strip()
        if not folder:
            return []
        from pathlib import Path
        root = Path(folder).expanduser()
        if not root.is_dir():
            return []
        exts = GIF_EXT | VID_EXT | IMG_EXT
        files = [p for p in root.rglob("*") if p.suffix.lower() in exts]
        self._random.shuffle(files)
        # Normally only the first 16 are taken, so shuffle first and then let files
        # that have never been imported jump the queue: a library bigger than 16
        # otherwise re-serves the same lucky-but-cached handful forever.
        files.sort(key=lambda p: self.store.local_path(
            f"{digest(str(p))}{p.suffix.lower()}").exists())
        out = []
        for path in files[:16]:
            media = self.store.acquire(f"file://{path}", transcode_gif=True)
            if media:
                out.append({"url": media["url"], "kind": media["kind"],
                            "title": path.stem, "tags": ["local"], "provider": "local",
                            "query": "local"})
        return out

    def local_count(self) -> int:
        """Usable files in the local folder, or -1 if it is not visible in here.

        Cheap on purpose (no acquisition, no transcode): the renderer uses it to
        decide whether to nag about having no media source, and a folder can be
        configured on the host but not mounted into the container.
        """
        folder = (self.cfg.get("local_library") or "").strip()
        if not folder:
            return 0
        from pathlib import Path
        root = Path(folder).expanduser()
        if not root.is_dir():
            return -1
        exts = GIF_EXT | VID_EXT | IMG_EXT
        try:
            return sum(1 for p in root.rglob("*") if p.suffix.lower() in exts)
        except Exception:
            return 0

    def stats(self) -> dict:
        local = self.local_count()          # filesystem I/O: keep it out of the lock
        with self._lock:
            return {"queued": len(self._queue), "sent": self._clips_sent,
                    "queries_used": self._queries_used,
                    "local_files": local}
