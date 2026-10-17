"""Tiny local HTTP server: serves the renderer, the Qt WebChannel shim and cached
media (with byte-range support so Chromium can seek/loop video)."""

from __future__ import annotations

import logging
import mimetypes
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("server")

RANGE_CHUNK = 1024 * 512


class Handler(BaseHTTPRequestHandler):
    server_version = "Visualiser/1.0"
    web_dir: Path = Path(".")
    mounts: dict[str, Path] = {}
    channel_js: bytes = b""

    def log_message(self, fmt, *args):  # keep the log clean
        log.debug("%s - %s", self.address_string(), fmt % args)

    # -- helpers -----------------------------------------------------------
    def _resolve(self, path: str) -> Path | None:
        rel = urllib.parse.unquote(path.split("?", 1)[0]).lstrip("/")
        if rel in ("", "index.html"):
            return self.web_dir / "index.html"
        if rel == "qwebchannel.js":
            return "__channel__"
        for prefix, root in self.mounts.items():
            if rel.startswith(prefix + "/"):
                name = os.path.basename(rel[len(prefix) + 1:])
                candidate = (root / name).resolve()
                if candidate.parent != root.resolve():
                    return None
                return candidate if candidate.exists() else None
        candidate = (self.web_dir / rel).resolve()
        try:
            candidate.relative_to(self.web_dir.resolve())
        except ValueError:
            return None
        return candidate if candidate.exists() else None

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _range(self, path: Path, ctype: str) -> None:
        size = path.stat().st_size
        header = self.headers.get("Range", "")
        start, end = 0, size - 1
        if header.startswith("bytes="):
            spec = header[len("bytes="):].split(",")[0].strip()
            a, _, b = spec.partition("-")
            try:
                if a:
                    start = int(a)
                    end = int(b) if b else size - 1
                elif b:
                    start = max(0, size - int(b))
            except ValueError:
                start, end = 0, size - 1
            end = min(end, size - 1)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as fh:
            fh.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = fh.read(min(RANGE_CHUNK, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        target = self._resolve(self.path)
        if target is None:
            self._send(404, b"not found", "text/plain")
            return
        if target == "__channel__":
            self._send(200, self.channel_js, "application/javascript")
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        try:
            self._range(target, ctype)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        self._send(405, b"nope", "text/plain")


def start(web_dir: Path, mounts: dict[str, Path], channel_js: str, port: int) -> ThreadingHTTPServer:
    Handler.web_dir = web_dir
    Handler.mounts = mounts
    Handler.channel_js = channel_js.encode("utf-8")
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
    log.info("renderer served at http://127.0.0.1:%d (mounts: %s)", port,
             ", ".join(f"/{k}" for k in mounts))
    return httpd
