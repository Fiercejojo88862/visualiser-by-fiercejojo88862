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
import re
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


def probe_size(path) -> tuple[int, int]:
    """Intrinsic video dimensions, or (0, 0) if ffprobe cannot tell."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=20).stdout.strip()
        w, h = out.split(",")[:2]
        return int(w), int(h)
    except Exception:
        return 0, 0


def detect_content_rect(src: Path, samples: int = 20) -> tuple[int, int, int, int] | None:
    """Content rectangle (w, h, x, y) of a video, ignoring baked-in bars.

    Provider memes are often padded into a square or 4:3 frame (a 640x480 file
    holding 640x360 of picture, or a 498x230 file pillarboxed to 410x230). CSS
    `object-fit: cover` cannot remove that: the bars are *inside* the file, so
    the stage still shows them. The most common cropdetect rect across a handful
    of frames is the real picture area; constant padding makes the mode reliable.

    Returns None when the frame is already full-bleed, or when the detection is
    suspicious (a dark scene can collapse to an 8x8 box - cropping to that would
    destroy the clip, so anything under 45% of the frame is rejected).
    """
    if not shutil.which("ffmpeg"):
        return None
    try:
        log_text = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", str(src),
             "-vf", "cropdetect=limit=24:round=2:reset=0",
             "-frames:v", str(max(1, samples)), "-f", "null", "-"],
            capture_output=True, text=True, timeout=60).stderr or ""
    except Exception:
        return None
    counts: dict[tuple[int, int, int, int], int] = {}
    for m in re.finditer(r"crop=(\d+):(\d+):(\d+):(\d+)", log_text):
        rect = tuple(int(v) for v in m.groups())
        counts[rect] = counts.get(rect, 0) + 1
    if not counts:
        return None
    rect = max(counts, key=counts.get)
    fw, fh = probe_size(src)
    if not fw or not fh:
        return None
    w, h, x, y = rect
    area = (w * h) / float(fw * fh)
    if area < 0.45 or area >= 0.97:
        return None
    w -= w % 2
    h -= h % 2
    x = max(0, min(x, fw - w))
    y = max(0, min(y, fh - h))
    if w < 16 or h < 16:
        return None
    return (w, h, x, y)


def transcode(src: Path, dst: Path, max_px: int = 1280,
              crop: tuple[int, int, int, int] | None = None) -> bool:
    """GIF -> looping VP9 webm. Much cheaper for Chromium to composite."""
    if not shutil.which("ffmpeg"):
        return False
    scale = (f"scale='if(gt(iw,ih),min({max_px},iw),-2)':'if(gt(iw,ih),-2,min({max_px},ih))'"
             f",fps=30")
    filters = ([f"crop={crop[0]}:{crop[1]}:{crop[2]}:{crop[3]}"] if crop else []) + [scale]
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
        "-vf", ",".join(filters), "-an", "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "36",
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

    def _crop_for(self, raw: Path, key: str) -> tuple[int, int, int, int] | None:
        """Memoised detect_content_rect, stored beside the clip.

        The marker keeps repeated acquisitions cheap (ffmpeg decode + cropdetect
        is not free) and survives as long as the cache entry does.
        """
        marker = self.local_path(f"{key}.crop")
        if marker.exists():
            try:
                text = marker.read_text(errors="ignore").strip()
                if text == "none":
                    return None
                w, h, x, y = (int(v) for v in text.split(":"))
                return (w, h, x, y)
            except Exception:
                pass
        rect = detect_content_rect(raw)
        try:
            marker.write_text("none" if rect is None else ":".join(str(v) for v in rect))
        except Exception:
            log.exception("could not write crop marker")
        if rect:
            log.info("baked-in bars removed: %s content=%dx%d+%d+%d",
                     raw.name, rect[0], rect[1], rect[2], rect[3])
        return rect

    def _register(self, raw: Path, key: str, kind: str, transcode_gif: bool,
                  source_url: str) -> dict:
        play = raw
        crop = self._crop_for(raw, key) if kind in ("gif", "video") else None

        if transcode_gif and kind == "gif":
            # cropped output gets its own name so an older, uncropped transcode
            # from a previous version of the app cannot mask the fix
            webm = self.local_path(f"{key}.fit.webm" if crop else f"{key}.webm")
            if not webm.exists() or webm.stat().st_size < 2048:
                if not transcode(raw, webm, crop=crop):
                    webm.unlink(missing_ok=True)
            if webm.exists():
                play = webm
                kind = "video"
        elif kind == "video" and crop:
            # A provider mp4/webm with letterboxing baked into the frame: crop it
            # once and play the cropped copy, so the stage is genuinely filled.
            fit = self.local_path(f"{key}.fit.webm")
            if not fit.exists() or fit.stat().st_size < 2048:
                if not transcode(raw, fit, crop=crop):
                    fit.unlink(missing_ok=True)
            if fit.exists():
                play = fit
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
