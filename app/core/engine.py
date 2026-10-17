"""Engine: pulls MPRIS + audio + lyrics + provider clips together and pushes
frame/event messages to the renderer over the Qt WebChannel."""

from __future__ import annotations

import bisect
import json
import logging
import threading
import time

from .. import config
from . import art as art_mod
from . import lyrics as lyrics_mod
from .audio import AudioCapture, StreamRecorder
from .beats import BeatTracker, OfflineTimeline
from .fetch import ProviderPool
from .media import MediaStore
from .mpris import MprisWatcher
from .selector import Selector, mood_for

log = logging.getLogger("engine")

TARGET_FPS = 30


class Engine:
    def __init__(self, cfg: dict, emit):
        self.cfg = cfg
        self.emit = emit                       # callable(dict) -> renderer
        self._stop = threading.Event()

        self.store = MediaStore(cfg.get("cache_cap_gb", 6.0))
        self.pool = ProviderPool(cfg)
        self.tracker = BeatTracker()
        self.lyrics = lyrics_mod.Lyrics([])
        self.timeline: OfflineTimeline | None = None
        self.selector = Selector(cfg, self.store, self.pool, self.emit)

        self.watcher = MprisWatcher(cfg, on_track_change=self._on_track_change)
        self.capture = AudioCapture(cfg)
        self.clock = None

        self._mood = "dreamy"
        self._mood_terms: list[str] = []
        self._last_mood_push = 0.0
        self._last_track_key = ""
        self._last_prepared_key = ""
        self._last_prepared_t = 0.0
        self.palette: dict = {"url": "", "palette": [], "accent": None, "hue": None}
        self._transient = 0.0
        self._last_play_state: bool | None = None
        self._recorder: StreamRecorder | None = None
        self._recorder_key = ""
        self._timeline_beats: list[float] = []
        self._timeline_drops: list[float] = []
        self._beat_cursor = 0
        self._drop_cursor = 0
        self._lyric_cache: dict[str, lyrics_mod.Lyrics] = {}
        self._last_pos = 0.0
        self._hello = False
        self._frames_sent = 0
        self._last_stats = 0.0

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        from .mpris import PlaybackClock
        self.clock = PlaybackClock(self.watcher, self.cfg.get("av_offset_ms", 0))
        self.selector.start()
        self.watcher.start()
        self.capture.start()
        threading.Thread(target=self._loop, name="engine", daemon=True).start()
        log.info("engine started (provider=%s, monitor=%s)",
                 self.cfg.get("provider"), self.capture.source or "pending")

    def stop(self) -> None:
        self._stop.set()
        self.watcher.stop()
        self.capture.stop()
        self.selector.stop()
        self._finish_recording(force=False)

    # -- renderer commands -------------------------------------------------
    def command(self, raw: str) -> None:
        try:
            msg = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except Exception:
            log.info("bad command: %r", raw)
            return
        cmd = msg.get("cmd")
        if cmd == "hello":
            self._hello = True
            log.info("renderer connected")
            self._send_bootstrap()
            threading.Timer(5.0, lambda: self.emit({"t": "diag"})).start()
        elif cmd == "need_clips":
            self.selector.request(int(msg.get("count", 2)))
        elif cmd == "set":
            self._apply_setting(msg.get("key", ""), msg.get("value"))
        elif cmd == "status":
            self.emit({"t": "status_full", **self.status()})
        elif cmd == "seek":
            self._seek(msg)
        elif cmd == "playpause":
            self.watcher.toggle_play_pause()
        elif cmd == "next":
            self.watcher.next_track()
        elif cmd == "prev":
            self.watcher.previous_track()
        elif cmd == "skip":
            self.selector.request(3)
        elif cmd == "log":
            log.info("renderer: %s", msg.get("text", ""))
        elif cmd == "diag":
            log.info("renderer diag: %s", json.dumps(msg.get("data", {}), sort_keys=True))

    def _seek(self, msg: dict) -> None:
        if not self.cfg.get("seek_enabled", True):
            return
        if "to" in msg:
            ok = self.watcher.seek_to(float(msg["to"]))
        elif "delta" in msg:
            ok = self.watcher.seek_relative(float(msg["delta"]))
        else:
            ok = False
        self.emit({"t": "seek", "ok": bool(ok),
                   "pos": round(self.clock.now() if self.clock else 0.0, 3)})

    def _apply_setting(self, key: str, value) -> None:
        if not key:
            return
        allowed = {k for k in config.DEFAULTS}
        if key not in allowed:
            log.info("ignoring unknown setting %r", key)
            return
        self.cfg[key] = value
        config.save(self.cfg)
        if key == "av_offset_ms" and self.clock:
            self.clock.av_offset_ms = int(value)
        if key in ("queries_per_hour", "provider", "klipy_key", "giphy_key",
                   "content_filter", "query_cache_days", "clip_kind", "fetch_enabled"):
            self.pool = ProviderPool(self.cfg)
            self.selector.pool = self.pool
        self.emit({"t": "settings", "settings": self.cfg})

    # -- track handling ----------------------------------------------------
    def _on_track_change(self, track) -> None:
        log.info("track: %s", track.label)
        threading.Thread(target=self._prepare_track, args=(track,), daemon=True).start()

    def _prepare_track(self, track) -> None:
        """Prepare everything a track needs, once per track (see _send_bootstrap).

        A renderer that connects mid-track re-runs this. Lyrics and artwork are
        cached, so redoing them is merely wasteful - but it would also kill the
        stream recording that offline beat analysis feeds on and spend provider
        queries twice. So a rapid repeat only re-announces the track and re-sends
        its clips; anything older is treated as a fresh listen.
        """
        now = time.monotonic()
        repeat = (bool(track.key) and track.key == self._last_prepared_key
                  and now - self._last_prepared_t < 20.0)

        lyr = self._lyric_cache.get(track.key)
        if lyr is None and not repeat:
            lyr = lyrics_mod.fetch(track, self.cfg.get("lyrics_enabled", True))
            self._lyric_cache[track.key] = lyr
        if lyr is None:
            lyr = lyrics_mod.Lyrics([])
        self.lyrics = lyr

        if not repeat:
            self._finish_recording(force=True)
            self._last_prepared_key = track.key
            self._last_prepared_t = now

            # album art + palette: drives the backdrop, the vinyl thumb, colour sync
            try:
                self.palette = art_mod.resolve(track,
                                               enabled=self.cfg.get("show_album_art", True))
            except Exception:
                log.exception("artwork resolution failed")
                self.palette = {"url": "", "palette": [], "accent": None, "hue": None}
            if self.palette.get("url"):
                log.info("artwork: %s palette=%s", self.palette["url"],
                         self.palette.get("accent"))

        # offline timeline (from a previous listen) is far tighter than live tracking
        self.timeline = None
        self._timeline_beats, self._timeline_drops = [], []
        path = config.TIMELINE_CACHE / f"{track.key}.json"
        if path.exists():
            try:
                self.timeline = OfflineTimeline(json.loads(path.read_text()))
                self._timeline_beats = self.timeline.beats
                self._timeline_drops = self.timeline.drops
                log.info("timeline loaded: %.1f bpm, %d beats, %d drops",
                         self.timeline.data.get("tempo", 0), len(self._timeline_beats),
                         len(self._timeline_drops))
            except Exception:
                self.timeline = None
        self._beat_cursor = 0
        self._drop_cursor = 0

        # Announce the track *before* queueing clips: the renderer clears its
        # clip buffer on a new track, so clips must follow the announcement.
        self.emit({
            "t": "track", "track": track.as_dict(),
            "lyrics": {"count": len(lyr.lines), "source": lyr.source,
                       "preview": lyr.distinctive_queries(3)},
            "timeline": bool(self.timeline),
            "palette": self.palette,
        })

        if repeat:
            self.selector.resend()
            return

        self.selector.on_track(track, lyr)
        self._start_recording(track)

    def _start_recording(self, track) -> None:
        if not self.cfg.get("record_streams", True):
            return
        if (config.TIMELINE_CACHE / f"{track.key}.json").exists():
            return
        try:
            path = config.REC_CACHE / f"{track.key}.wav"
            self._recorder = StreamRecorder(path)
            self._recorder_key = track.key
            self.capture.recorder = self._recorder
        except Exception:
            log.exception("could not start stream recording")
            self._recorder = None

    def _finish_recording(self, force: bool) -> None:
        rec, key = self._recorder, self._recorder_key
        self._recorder, self._recorder_key = None, ""
        self.capture.recorder = None
        if rec is None:
            return
        try:
            seconds = rec.seconds
            rec.close()
        except Exception:
            return
        wav = config.REC_CACHE / f"{key}.wav"
        if not force or seconds < 45 or not self.cfg.get("offline_analysis", True):
            wav.unlink(missing_ok=True)
            return
        threading.Thread(target=self._analyse_recording, args=(wav, key), daemon=True).start()

    def _analyse_recording(self, wav, key: str) -> None:
        out = config.TIMELINE_CACHE / f"{key}.json"
        ok = OfflineTimeline.build(wav, out)
        log.info("offline analysis %s for %s", "ok" if ok else "skipped/failed", key)
        if ok:
            self.emit({"t": "notice", "level": "info",
                       "text": "Beat timeline cached for next play"})
        wav.unlink(missing_ok=True)

    # -- main loop ---------------------------------------------------------
    def _loop(self) -> None:
        interval = 1.0 / TARGET_FPS
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self._tick()
            except Exception:
                log.exception("engine tick failed")
            dt = time.monotonic() - t0
            self._stop.wait(max(0.0, interval - dt))

    def _tick(self) -> None:
        bands = self.capture.latest()
        energy = self.capture.energy_level()
        now = time.monotonic()

        ev = self.tracker.push(bands["flux"], bands["rms"], now)
        mood, terms = mood_for(ev["bpm"], energy, bands["centroid"], ev["locked"])
        self._mood, self._mood_terms = mood, terms
        self.selector.set_mood_terms(terms)

        pos = self.clock.now() if self.clock else 0.0

        # offline timeline takes precedence when we have one
        beat, beat_strength, drop = ev["beat"], ev["beat_strength"], ev["drop"]
        if self.timeline:
            # a seek/resume makes the clock jump: resync the grid cursors
            if abs(pos - self._last_pos) > 2.5:
                self._beat_cursor = bisect.bisect_right(self._timeline_beats, pos)
                self._drop_cursor = bisect.bisect_right(self._timeline_drops, pos)
            while (self._beat_cursor < len(self._timeline_beats)
                   and self._timeline_beats[self._beat_cursor] <= pos + 0.02):
                beat, beat_strength = True, 0.9
                self._beat_cursor += 1
            while (self._drop_cursor < len(self._timeline_drops)
                   and self._timeline_drops[self._drop_cursor] <= pos + 0.02):
                drop = True
                self._drop_cursor += 1
        self._last_pos = pos

        cur_line, next_line = self.lyrics.line_at(pos) if self.cfg.get(
            "show_lyrics", True) else ("", "")

        track = self.watcher.track
        msg = {
            "t": "frame",
            "bands": {k: round(float(bands[k]), 4) for k in
                      ("sub", "bass", "lowmid", "mid", "high", "air")},
            "spec": self.capture.spectrum(),
            "rms": round(float(bands["rms"]), 4),
            "centroid": round(float(bands["centroid"]), 1),
            "energy": round(float(energy), 3),
            "bpm": ev["bpm"],
            "confidence": ev["confidence"],
            "locked": bool(ev["locked"]),
            "beat": bool(beat),
            "beat_strength": beat_strength,
            "drop": bool(drop),
            "phase": ev["phase"],
            "pos": round(pos, 3),
            "duration": round(track.duration, 1),
            "playing": self.watcher.playing,
            "mpris": self.watcher.available,
            "mood": mood,
            "lyric": {"cur": cur_line, "next": next_line},
            "track": {"title": track.title, "artist": track.artist,
                      "key": track.key, "player": track.player},
            "art": self.palette,
        }
        self.emit(msg)
        self._frames_sent += 1

        # play/pause transitions drive the renderer's fade states
        playing = self.watcher.playing
        if playing != self._last_play_state:
            if self._last_play_state is not None:
                self.emit({"t": "transport", "playing": playing})
            self._last_play_state = playing

        if now - self._last_mood_push > 25 and mood != getattr(self, "_queued_mood", ""):
            self._queued_mood = mood
            self.selector.mood_refresh(mood, terms)
            self._last_mood_push = now

        if now - self._last_stats > 20:
            self._last_stats = now
            self.store.enforce_cap()
            if self._hello:
                self.emit({"t": "diag"})

    def _send_bootstrap(self) -> None:
        self.emit({"t": "settings", "settings": self.cfg})
        self.emit({"t": "status_full", **self.status()})
        track = self.watcher.track
        if track.key and track.title:
            # never do network I/O on the Qt event loop
            threading.Thread(target=self._prepare_track, args=(track,), daemon=True).start()

    def status(self) -> dict:
        return {
            "mpris": {"available": self.watcher.available, "player": self.watcher.player,
                      "status": self.watcher.status,
                      "track": self.watcher.track.as_dict()},
            "audio": {"source": self.capture.source, "streaming": self.capture.alive_stream,
                      "error": self.capture.error, "frames": self.capture.frames},
            "beats": {"bpm": self.tracker.bpm, "locked": self.tracker.locked,
                      "confidence": self.tracker.confidence,
                      "timeline": bool(self.timeline),
                      "beats": len(self._timeline_beats)},
            "lyrics": {"source": self.lyrics.source, "lines": len(self.lyrics.lines)},
            "selector": self.selector.stats(),
            "providers": self.pool.status(),
            "cache_bytes": self.store.total_bytes(),
            "frames_sent": self._frames_sent,
        }
