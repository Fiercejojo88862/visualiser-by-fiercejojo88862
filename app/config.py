"""Configuration and path resolution for Visualiser by Fiercejojo88862.

State always lives inside the app folder so the whole thing is portable and
self-contained (the container mounts the repo at /app and writes into /app/state).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

APP_DIR = Path(os.environ.get("VISUALISER_APP_DIR") or Path(__file__).resolve().parent.parent)
STATE_DIR = Path(os.environ.get("VISUALISER_STATE_DIR") or (APP_DIR / "state"))
WEB_DIR = Path(__file__).resolve().parent / "web"

CACHE_DIR = STATE_DIR / "cache"
CLIP_CACHE = CACHE_DIR / "clips"
QUERY_CACHE = CACHE_DIR / "queries"
LYRIC_CACHE = CACHE_DIR / "lyrics"
REC_CACHE = CACHE_DIR / "recordings"
TIMELINE_CACHE = CACHE_DIR / "timelines"
ART_CACHE = CACHE_DIR / "art"
LOG_DIR = STATE_DIR / "logs"

SETTINGS_FILE = STATE_DIR / "settings.json"

DEFAULTS: dict = {
    # ---- display ---------------------------------------------------------
    "window_width": 1280,
    "window_height": 720,
    "show_overlay": True,
    "show_track_info": True,
    "show_lyrics": True,
    # ---- look ------------------------------------------------------------
    "effect_intensity": 1.0,
    "reactive_glow": True,
    "flash_on_beat": True,
    "min_beats_between_cuts": 2,
    "cut_on_drop": True,
    # ---- spectrum / reactive layers --------------------------------------
    "spectrum_bands": 64,
    "radial_spectrum": True,
    "bottom_spectrum": True,
    "bass_shake": True,
    "particle_trails": True,
    "particle_count": 260,
    # ---- post processing -------------------------------------------------
    "postfx_bloom": True,
    "postfx_chroma": True,
    # ---- metadata overlay ------------------------------------------------
    "show_progress": True,
    "show_album_art": True,
    "art_backdrop": True,
    "palette_sync": True,
    "show_bpm_ring": True,
    "hud_autohide_seconds": 12,
    # ---- transport -------------------------------------------------------
    "seek_step_seconds": 15,
    "seek_enabled": True,
    # ---- sync ------------------------------------------------------------
    "av_offset_ms": 0,
    "position_snap_ms": 900,
    # ---- music source ----------------------------------------------------
    "prefer_player": "",           # e.g. "firefox" - empty means auto
    "ignore_players": ["playerctld"],
    "monitor_source": "",          # pulse monitor device, empty = auto-detect
    # ---- lyrics ----------------------------------------------------------
    "lyrics_enabled": True,
    # ---- gif / clip provider --------------------------------------------
    "fetch_enabled": True,
    "provider": "klipy",           # "klipy" (primary) or "giphy" (fallback)
    "klipy_key": "",
    "giphy_key": "",
    "content_filter": "medium",    # klipy: off | low | medium | high
    "allow_ads": False,
    "max_queries_per_track": 12,
    "max_clips_per_track": 24,
    "queries_per_hour": 100,       # tester key ceiling, production key = 0 (unlimited)
    "clip_kind": "gif",            # gif | clip | meme
    # ---- local fallback library -----------------------------------------
    "local_library": "",           # folder of gifs/videos used before/without the API
    # ---- cache -----------------------------------------------------------
    "cache_cap_gb": 6.0,
    "query_cache_days": 30,
    # ---- analysis --------------------------------------------------------
    "record_streams": True,
    "offline_analysis": True,
    # ---- server ----------------------------------------------------------
    "port": 8733,
    # ---- container/gfx ---------------------------------------------------
    "chromium_flags": (
        "--no-sandbox --disable-gpu-sandbox --ozone-platform-hint=auto "
        "--enable-features=VaapiVideoDecoder --ignore-gpu-blocklist"
    ),
}


def _ensure_dirs() -> None:
    for d in (STATE_DIR, CACHE_DIR, CLIP_CACHE, QUERY_CACHE, LYRIC_CACHE,
              REC_CACHE, TIMELINE_CACHE, ART_CACHE, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load() -> dict:
    _ensure_dirs()
    cfg = dict(DEFAULTS)
    fresh = not SETTINGS_FILE.exists()
    if not fresh:
        try:
            cfg.update(json.loads(SETTINGS_FILE.read_text()))
        except Exception:
            fresh = True
    env_key = os.environ.get("KLIPY_KEY")
    if env_key and not cfg.get("klipy_key"):
        cfg["klipy_key"] = env_key
    if fresh:
        # write the defaults out on first run so the file is there to edit
        try:
            save(cfg)
        except Exception:
            pass
    return cfg


def save(cfg: dict) -> None:
    _ensure_dirs()
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, sort_keys=True))
    tmp.replace(SETTINGS_FILE)


def update(**kw) -> dict:
    cfg = load()
    cfg.update(kw)
    save(cfg)
    return cfg


def setup_logging() -> Path:
    import logging
    _ensure_dirs()
    path = LOG_DIR / "visualiser.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[logging.FileHandler(path), logging.StreamHandler()],
    )
    return path
