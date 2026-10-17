"""MPRIS watcher with local clock anchoring.

MPRIS `Position` is coarse (often ~1s, sometimes only updates on seek). We anchor
(reported_position, monotonic_time) on every successful read and interpolate at
frame rate in between, only re-locking when drift exceeds a threshold.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time

log = logging.getLogger("mpris")

try:
    import dbus  # type: ignore
except Exception:  # pragma: no cover
    dbus = None

PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
PLAYER_PATH = "/org/mpris/MediaPlayer2"


def _s(value, default=""):
    try:
        return str(value)
    except Exception:
        return default


class Track:
    __slots__ = ("player", "title", "artist", "album", "length_us", "art_url", "key")

    def __init__(self, player="", title="", artist="", album="", length_us=0, art_url=""):
        self.player = player
        self.title = title
        self.artist = artist
        self.album = album
        self.length_us = int(length_us or 0)
        self.art_url = art_url
        raw = f"{title.strip().lower()}|{artist.strip().lower()}|{self.length_us // 1000_000}"
        self.key = hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:16]

    @property
    def label(self) -> str:
        if self.artist and self.title:
            return f"{self.artist} - {self.title}"
        return self.title or "Unknown track"

    @property
    def duration(self) -> float:
        return self.length_us / 1_000_000.0

    def as_dict(self) -> dict:
        return {
            "key": self.key, "title": self.title, "artist": self.artist,
            "album": self.album, "duration": self.duration, "player": self.player,
            "art": self.art_url,
        }


class MprisWatcher(threading.Thread):
    """Polls the session bus and exposes the active player's state."""

    daemon = True

    def __init__(self, cfg: dict, on_track_change=None):
        super().__init__(name="mpris")
        self.cfg = cfg
        self.on_track_change = on_track_change
        self._lock = threading.Lock()
        self._stop = threading.Event()

        self.track = Track()
        self.status = "Stopped"
        self.player = ""
        self.available = False
        self._anchor_pos = 0.0
        self._anchor_t = time.monotonic()
        self._last_reported = -1.0

    # -- clock -------------------------------------------------------------
    def position(self) -> float:
        """Interpolated playback position in seconds."""
        with self._lock:
            if self.status != "Playing":
                return max(0.0, self._anchor_pos)
            return max(0.0, self._anchor_pos + (time.monotonic() - self._anchor_t))

    def _reanchor(self, reported: float) -> None:
        with self._lock:
            self._anchor_pos = reported
            self._anchor_t = time.monotonic()
            self._last_reported = reported

    @property
    def playing(self) -> bool:
        return self.status == "Playing"

    # -- dbus --------------------------------------------------------------
    def _bus(self):
        addr = __import__("os").environ.get("DBUS_SESSION_BUS_ADDRESS")
        return dbus.bus.BusConnection(addr) if addr else dbus.SessionBus()

    def _candidates(self, bus):
        names = [n for n in bus.list_names() if n.startswith("org.mpris.MediaPlayer2")]
        ignore = [i for i in self.cfg.get("ignore_players", []) if i]
        for name in list(names):
            if name.split(".")[-1] in ignore or any(i in name for i in ignore):
                names.remove(name)
        prefer = (self.cfg.get("prefer_player") or "").lower()
        if prefer:
            names.sort(key=lambda n: 0 if prefer in n.lower() else 1)
        return names

    def _read(self, bus, name):
        obj = bus.get_object(name, PLAYER_PATH)
        props = dbus.Interface(obj, PROPS_IFACE)
        status = _s(props.Get(PLAYER_IFACE, "PlaybackStatus"), "Stopped")
        meta = props.Get(PLAYER_IFACE, "Metadata")
        try:
            length = int(meta.get("mpris:length", 0))
        except Exception:
            length = 0
        artists = meta.get("xesam:artist", [])
        if isinstance(artists, (list, tuple)):
            artist = ", ".join(_s(a) for a in artists)
        else:
            artist = _s(artists)
        track = Track(
            player=name,
            title=_s(meta.get("xesam:title", "")),
            artist=artist,
            album=_s(meta.get("xesam:album", "")),
            length_us=length,
            art_url=_s(meta.get("mpris:artUrl", "")),
        )
        try:
            pos = float(props.Get(PLAYER_IFACE, "Position")) / 1_000_000.0
        except Exception:
            pos = None
        return status, track, pos

    # -- loop --------------------------------------------------------------
    def run(self) -> None:
        if dbus is None:
            log.warning("dbus-python unavailable; MPRIS disabled")
            return
        first = True
        while not self._stop.is_set():
            delay = 0.4
            try:
                bus = self._bus()
                chosen = None
                for name in self._candidates(bus):
                    try:
                        status, track, pos = self._read(bus, name)
                    except Exception:
                        continue
                    if status == "Playing":
                        chosen = (name, status, track, pos)
                        break
                    if chosen is None:
                        chosen = (name, status, track, pos)
                if chosen:
                    name, status, track, pos = chosen
                    self.available = True
                    changed = track.key != self.track.key
                    with self._lock:
                        prev_status = self.status
                        self.status, self.player = status, name
                    if pos is not None:
                        reported = pos
                        if changed or abs(reported - self.position()) * 1000 > self.cfg.get(
                                "position_snap_ms", 900):
                            self._reanchor(reported)
                        else:
                            with self._lock:
                                self._last_reported = reported
                    if changed:
                        self.track = track
                        if self.on_track_change:
                            try:
                                self.on_track_change(track)
                            except Exception:
                                log.exception("track change handler failed")
                    elif prev_status != status and status == "Playing":
                        # resumed: re-anchor so the clock does not run away
                        if pos is not None:
                            self._reanchor(pos)
                else:
                    self.available = False
                    with self._lock:
                        self.status = "Stopped"
                    delay = 1.5
            except Exception:
                if first:
                    log.exception("MPRIS poll failed")
                delay = 1.5
            first = False
            self._stop.wait(delay)

    def stop(self) -> None:
        self._stop.set()

    # -- transport ---------------------------------------------------------
    def _player_iface(self):
        if not self.player:
            return None
        bus = self._bus()
        obj = bus.get_object(self.player, PLAYER_PATH)
        return dbus.Interface(obj, PLAYER_IFACE)

    def seek_relative(self, seconds: float) -> bool:
        """Relative seek - the portable MPRIS call (no TrackId needed)."""
        if abs(seconds) < 0.05:
            return False
        try:
            iface = self._player_iface()
            if iface is None:
                return False
            iface.Seek(dbus.Int64(int(seconds * 1_000_000)))
            with self._lock:
                self._anchor_pos = max(0.0, self._anchor_pos + seconds)
                self._anchor_t = time.monotonic()
            return True
        except Exception as exc:
            log.info("seek failed: %s", exc)
            return False

    def seek_to(self, position: float) -> bool:
        return self.seek_relative(position - self.position())

    def toggle_play_pause(self) -> bool:
        try:
            iface = self._player_iface()
            if iface is None:
                return False
            iface.PlayPause()
            return True
        except Exception as exc:
            log.info("play/pause failed: %s", exc)
            return False

    def next_track(self) -> bool:
        try:
            iface = self._player_iface()
            if iface is None:
                return False
            iface.Next()
            return True
        except Exception:
            return False

    def previous_track(self) -> bool:
        try:
            iface = self._player_iface()
            if iface is None:
                return False
            iface.Previous()
            return True
        except Exception:
            return False


class PlaybackClock:
    """Timeline clock for a (possibly streamed) track.

    When a precomputed timeline exists for the current track we trust the MPRIS
    clock; otherwise we follow the locally advanced anchor and let the online
    beat tracker supply the musical grid.
    """

    def __init__(self, watcher: MprisWatcher, av_offset_ms: int = 0):
        self.watcher = watcher
        self.av_offset_ms = av_offset_ms

    @property
    def offset(self) -> float:
        return self.av_offset_ms / 1000.0

    def now(self) -> float:
        return self.watcher.position() - self.offset
