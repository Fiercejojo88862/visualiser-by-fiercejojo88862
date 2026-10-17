"""Media store: download, transcode, serve and evict cached clips.

GIFs are heavy to decode full-screen, so anything animated is transcoded once to
a tightly packed webm loop and reused. Everything is content-addressed by URL.

Note: KLIPY and GIPHY both forbid re-hosting their media. This cache is a local
playback cache for personal use only, capped and evictable - do not ship it, and
do not publish recordings that contain provider media.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

from ..config import CLIP_CACHE

log = logging.getLogger("media")

UA = "Visualiser-by-Fiercejojo88862/1.0 (personal use)"
GIF_EXT = {".gif", ".webp", ".apng"}
VID_EXT = {".mp4", ".webm", ".mkv", ".mov"}
IMG_EXT = {".jpg", ".jpeg", ".png"}


def digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:20]


def kind_for_url(url: str) -> str:
    ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if ext in GIF_EXT:
        return "gif"
    if ext in VID_EXT:
        return "video"
    if ext in IMG_EXT:
        return "image"
    return "gif"


def _download(url: str, dest: Path, timeout: float = 25.0) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as fh:
            shutil.copyfileobj(resp, fh)
        return dest.stat().st_size > 1024
    except Exception as exc:
        log.info("download failed %s: %s", url[:80], exc)
        dest.unlink(missing_ok=True)
        return False


def transcode(src: Path, dst: Path, max_px: int = 1280) -> bool:
    """GIF -> looping VP9 webm. Much cheaper for Chromium to composite."""
    if not shutil.which("ffmpeg"):
        return False
    scale = (f"scale='if(gt(iw,ih),min({max_px},iw),-2)':'if(gt(iw,ih),-2,min({max_px},ih))'"
             f",fps=30")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
        "-vf", scale, "-an", "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "36",
        "-deadline", "realtime", "-cpu-used", "6", "-row-mt", "1",
        "-pix_fmt", "yuv420p", "-loop", "0", str(dst),
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=120, check=False)
        return dst.exists() and dst.stat().st_size > 2048
    except Exception as exc:
        log.info("transcode failed: %s", exc)
        return False


class MediaStore:
    def __init__(self, cache_cap_gb: float = 6.0):
        self.root = CLIP_CACHE
        self.root.mkdir(parents=True, exist_ok=True)
        self.cap_bytes = int(cache_cap_gb * 1024 ** 3)

    def local_path(self, name: str) -> Path:
        return self.root / name

    def url_for(self, name: str) -> str:
        return f"/media/{urllib.parse.quote(name)}"

    def acquire(self, url: str, transcode_gif: bool = True) -> dict | None:
        """Fetch a remote clip (or copy a local file) into the cache.

        Returns {'file','url','kind','bytes'} or None.
        """
        if not url:
            return None
        if url.startswith("file://"):
            return self._import_local(Path(url[7:]), transcode_gif)
        if not url.startswith(("http://", "https://")):
            return self._import_local(Path(url), transcode_gif)

        key = digest(url)
        kind = kind_for_url(url)
        ext = Path(urllib.parse.urlparse(url).path).suffix.lower() or ".bin"
        raw = self.local_path(f"{key}{ext}")
        if not raw.exists():
            if not _download(url, raw):
                return None
        return self._register(raw, key, kind, transcode_gif, source_url=url)

    def _import_local(self, path: Path, transcode_gif: bool) -> dict | None:
        if not path.exists():
            return None
        key = digest(str(path))
        raw = self.local_path(f"{key}{path.suffix.lower()}")
        if not raw.exists():
            try:
                shutil.copy2(path, raw)
            except Exception:
                return None
        ext = path.suffix.lower()
        kind = "gif" if ext in GIF_EXT else ("video" if ext in VID_EXT else
                                            ("image" if ext in IMG_EXT else "gif"))
        return self._register(raw, key, kind, transcode_gif, source_url=f"file://{path}")

    def _register(self, raw: Path, key: str, kind: str, transcode_gif: bool,
                  source_url: str) -> dict:
        play = raw
        if transcode_gif and kind == "gif":
            webm = self.local_path(f"{key}.webm")
            if not webm.exists() or webm.stat().st_size < 2048:
                if not transcode(raw, webm):
                    webm.unlink(missing_ok=True)
            if webm.exists():
                play = webm
                kind = "video"
        os.utime(play, None)
        return {
            "file": str(play),
            "url": self.url_for(play.name),
            "kind": kind,
            "bytes": play.stat().st_size,
            "source": source_url,
        }

    def total_bytes(self) -> int:
        return sum(f.stat().st_size for f in self.root.glob("*") if f.is_file())

    def enforce_cap(self) -> None:
        try:
            total = self.total_bytes()
            if total <= self.cap_bytes:
                return
            files = sorted((f for f in self.root.glob("*") if f.is_file()),
                           key=lambda f: f.stat().st_mtime)
            for f in files:
                if total <= self.cap_bytes * 0.85:
                    break
                size = f.stat().st_size
                f.unlink(missing_ok=True)
                total -= size
            log.info("cache trimmed to %.2f GB", total / 1024 ** 3)
        except Exception:
            log.exception("cache trim failed")


def probe_duration(path) -> float:
    if not shutil.which("ffprobe"):
        return 0.0
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=20).stdout.strip()
        return float(out)
    except Exception:
        return 0.0
