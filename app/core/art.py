"""Album artwork: fetch, serve, and extract a colour palette from it.

Artwork arrives over MPRIS as `mpris:artUrl`, which is a `file://` path for some
players, an `http(s)://` URL for others, and occasionally a `data:` URI. All three
are normalised into `state/cache/art/` and served from `/art/<name>`.

Colours are pulled out with ffmpeg (always present - it is already required for
clip transcoding) downscaled to a 32x32 raw RGB buffer, then quantised in numpy.
No image library needed, and no colour extraction on the render thread.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np

from ..config import ART_CACHE

log = logging.getLogger("art")

UA = "Visualiser-by-Fiercejojo88862/1.0 (personal use)"


def _sniff_ext(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:2] == b"BM":
        return ".bmp"
    return ".img"


def _read_art(url: str) -> bytes:
    if not url:
        return b""
    if url.startswith("data:"):
        try:
            head, _, payload = url.partition(",")
            if ";base64" in head:
                return base64.b64decode(payload)
            return urllib.parse.unquote_to_bytes(payload)
        except Exception:
            return b""
    if url.startswith("file://"):
        path = Path(urllib.parse.unquote(url[7:]))
        try:
            if path.is_file() and path.stat().st_size < 12 * 1024 * 1024:
                return path.read_bytes()
        except Exception:
            return b""
        log.info("artwork path not readable (outside the container?): %s", path)
        return b""
    if url.startswith(("http://", "https://")):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=8) as resp:
                return resp.read(12 * 1024 * 1024)
        except Exception as exc:
            log.info("art download failed: %s", exc)
    return b""


def _hsv(rgb):
    r, g, b = (c / 255.0 for c in rgb)
    mx, mn = max(r, g, b), min(r, g, b)
    v = mx
    d = mx - mn
    s = 0.0 if mx <= 0 else d / mx
    if d <= 0:
        h = 0.0
    elif mx == r:
        h = (60 * ((g - b) / d)) % 360
    elif mx == g:
        h = 60 * ((b - r) / d) + 120
    else:
        h = 60 * ((r - g) / d) + 240
    return h, s, v


def _punch(rgb, sat_floor=0.55, val=0.92):
    """Push a colour to a usable accent: keep the hue, guarantee saturation."""
    import colorsys
    r, g, b = (c / 255.0 for c in rgb)
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    s = max(s, sat_floor)
    r, g, b = colorsys.hsv_to_rgb(h, s, val)
    return [int(r * 255), int(g * 255), int(b * 255)]


def palette_from_image(path: Path, n: int = 4) -> list[list[int]]:
    """Top colour clusters, most common first."""
    if not path.exists():
        return []
    try:
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path),
             "-vf", "scale=32:32:flags=area", "-f", "rawvideo",
             "-pix_fmt", "rgb24", "-"],
            capture_output=True, timeout=25).stdout
    except Exception:
        return []
    if len(raw) < 32 * 32 * 3:
        return []
    px = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.float32)
    mx, mn = px.max(axis=1), px.min(axis=1)
    sat = (mx - mn) / (mx + 1e-6)
    keep = (mx > 45) & (mn < 232) & (sat > 0.12)
    sel = px[keep] if int(keep.sum()) >= 16 else px
    q = (sel // 32).astype(np.int32)
    codes = q[:, 0] * 64 + q[:, 1] * 8 + q[:, 2]
    uniq, counts = np.unique(codes, return_counts=True)
    order = np.argsort(-counts)[: max(n * 4, 8)]
    out: list[list[int]] = []
    for code in uniq[order]:
        cluster = sel[codes == code]
        if cluster.size == 0:
            continue
        mean = cluster.mean(axis=0)
        cand = [int(mean[0]), int(mean[1]), int(mean[2])]
        if all(sum((a - b) ** 2 for a, b in zip(cand, other)) > 1800 for other in out):
            out.append(cand)
        if len(out) >= n:
            break
    return out


def resolve(track, enabled: bool = True) -> dict:
    """Return {'url', 'palette', 'accent', 'hue'} for a track (empty on failure)."""
    blank = {"url": "", "palette": [], "accent": None, "hue": None}
    if not enabled or not getattr(track, "art_url", ""):
        return blank
    key = hashlib.sha1(f"{track.key}|{track.art_url}".encode()).hexdigest()[:16]
    existing = sorted(ART_CACHE.glob(f"{key}.*"))
    existing = [p for p in existing if p.suffix != ".json"]
    meta_path = ART_CACHE / f"{key}.json"

    if not existing:
        data = _read_art(track.art_url)
        if not data:
            return blank
        ext = _sniff_ext(data)
        path = ART_CACHE / f"{key}{ext}"
        try:
            path.write_bytes(data)
        except Exception:
            return blank
        existing = [path]

    path = existing[0]
    palette: list[list[int]] = []
    if meta_path.exists():
        try:
            palette = json.loads(meta_path.read_text()).get("palette", [])
        except Exception:
            palette = []
    if not palette:
        palette = palette_from_image(path)
        try:
            meta_path.write_text(json.dumps({"palette": palette, "source": track.art_url}))
        except Exception:
            pass

    accent = None
    hue = None
    if palette:
        # the most vivid of the top clusters makes the better accent
        best = max(palette[:3], key=lambda c: _hsv(c)[1])
        accent = _punch(best)
        hue = round(_hsv(accent)[0], 1)

    return {
        "url": f"/art/{urllib.parse.quote(path.name)}",
        "palette": palette,
        "accent": accent,
        "hue": hue,
    }
