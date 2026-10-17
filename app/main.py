"""Visualiser by Fiercejojo88862 - application entry point."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from . import config

log = logging.getLogger("main")


def prepare_env(cfg: dict) -> None:
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", cfg.get("chromium_flags", ""))
    os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
    os.environ.setdefault("QT_LOGGING_RULES", "qt.webenginecontext=false")


def run_selftest(cfg: dict) -> int:
    """Verify every moving part without opening a window."""
    from .core.audio import detect_monitor_source, RATE, HOP
    from .core.fetch import ProviderPool
    from .core.mpris import MprisWatcher
    from .core import lyrics as lyrics_mod

    report: dict = {"paths": {
        "app": str(config.APP_DIR), "state": str(config.STATE_DIR),
        "web": str(config.WEB_DIR), "web_exists": config.WEB_DIR.is_dir(),
    }}

    report["tools"] = {name: bool(__import__("shutil").which(name))
                       for name in ("parec", "pactl", "ffmpeg", "ffprobe")}

    source = detect_monitor_source(cfg.get("monitor_source", ""))
    report["audio"] = {"monitor_source": source or "(none found)"}

    try:
        w = MprisWatcher(cfg)
        import dbus  # noqa: F401
        bus = w._bus()
        names = w._candidates(bus)
        report["mpris"] = {"dbus": True, "players": names}
        if names:
            status, track, pos = w._read(bus, names[0])
            report["mpris"].update({"status": status, "track": track.label,
                                    "position": pos, "duration": track.duration})
    except Exception as exc:
        report["mpris"] = {"dbus": False, "error": str(exc)}

    pool = ProviderPool(cfg)
    report["providers"] = pool.status()
    if report["providers"]["providers"] and any(
            p["configured"] for p in report["providers"]["providers"]):
        items = pool.search("neon city night", limit=8)
        report["provider_probe"] = {
            "query": "neon city night", "results": len(items),
            "sample": [c.as_dict() for c in items[:3]],
        }
    else:
        report["provider_probe"] = {"error": "no API key set - add klipy_key to settings.json"}

    try:
        probe_track = type("T", (), {"title": "Blinding Lights", "artist": "The Weeknd",
                                     "album": "After Hours", "duration": 200.0})()
        lyr = lyrics_mod.fetch(probe_track, True)
        report["lyrics"] = {"source": lyr.source, "lines": len(lyr.lines),
                            "sample": [t for _, t in lyr.lines[:3]]}
    except Exception as exc:
        report["lyrics"] = {"error": str(exc)}

    report["analysis"] = {"frame_rate": round(RATE / HOP, 2), "hop": HOP}

    print(json.dumps(report, indent=2))
    ok = report["tools"]["parec"] and report["tools"]["ffmpeg"] and report["paths"]["web_exists"]
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="visualiser")
    parser.add_argument("--selftest", action="store_true",
                        help="check audio/MPRIS/lyrics/provider wiring and exit")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-gui", action="store_true", help="engine only (debug)")
    parser.add_argument("--screenshot", metavar="PATH",
                        help="start normally, save a frame to PATH after --after seconds, exit")
    parser.add_argument("--probe", action="store_true",
                        help="report what the renderer actually drew (pixels, layers, fps)")
    parser.add_argument("--after", type=float, default=12.0,
                        help="seconds to run before the screenshot/probe (default 12)")
    parser.add_argument("--show-settings", action="store_true")
    parser.add_argument("--set", nargs=2, action="append", metavar=("KEY", "VALUE"),
                        help="persist a setting, e.g. --set klipy_key ABC123")
    args = parser.parse_args(argv)

    cfg = config.load()
    if args.set:
        for key, value in args.set:
            if key not in config.DEFAULTS:
                print(f"unknown setting: {key}", file=sys.stderr)
                return 2
            default = config.DEFAULTS[key]
            if isinstance(default, bool):
                value = str(value).lower() in ("1", "true", "yes", "on")
            elif isinstance(default, int):
                value = int(value)
            elif isinstance(default, float):
                value = float(value)
            cfg[key] = value
        config.save(cfg)
        print(f"settings written to {config.SETTINGS_FILE}")
        return 0
    if args.show_settings:
        print(json.dumps(cfg, indent=2))
        return 0
    if args.port:
        cfg["port"] = args.port
    if args.selftest:
        config.setup_logging()
        return run_selftest(cfg)

    config.setup_logging()
    prepare_env(cfg)

    from PyQt6.QtCore import Qt, QUrl, pyqtSignal, pyqtSlot, QObject
    from PyQt6.QtGui import QGuiApplication, QIcon, QKeySequence, QShortcut
    from PyQt6.QtWebChannel import QWebChannel
    from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWidgets import QApplication, QMainWindow

    from . import server
    from .core.engine import Engine

    class Bridge(QObject):
        published = pyqtSignal(str)
        engine: Engine

        @pyqtSlot(str)
        def command(self, payload: str) -> None:
            try:
                self.engine.command(payload)
            except Exception:
                log.exception("command failed: %s", payload)

    app = QApplication(sys.argv[:1])
    app.setApplicationName("visualiser")
    app.setApplicationDisplayName("Visualiser by Fiercejojo88862")
    app.setDesktopFileName("visualiser")
    QGuiApplication.setDesktopFileName("visualiser")

    # the bar / alt-tab / task switcher read this, not the desktop entry
    icon_file = config.APP_DIR / "assets" / "visualiser.png"
    if icon_file.exists():
        app.setWindowIcon(QIcon(str(icon_file)))

    channel_js = ""
    from PyQt6.QtCore import QFile, QIODevice
    fh = QFile(":/qtwebchannel/qwebchannel.js")
    if fh.open(QIODevice.OpenModeFlag.ReadOnly):
        channel_js = bytes(fh.readAll()).decode("utf-8")
        fh.close()
    if not channel_js:
        log.warning("could not load qwebchannel.js from Qt resources")

    httpd = server.start(config.WEB_DIR,
                         {"media": config.CLIP_CACHE, "art": config.ART_CACHE},
                         channel_js, cfg["port"])

    bridge = Bridge()
    engine = Engine(cfg, emit=lambda msg: bridge.published.emit(json.dumps(msg)))
    bridge.engine = engine

    _cleaned = {"done": False}

    def cleanup() -> None:
        """Close the engine and the server exactly once, whatever the exit path."""
        if _cleaned["done"]:
            return
        _cleaned["done"] = True
        log.info("shutting down")
        try:
            engine.stop()
        except Exception:
            log.exception("engine stop failed")
        try:
            httpd.shutdown()
        except Exception:
            pass

    class Page(QWebEnginePage):
        """Surfaces renderer console output (including uncaught errors) in the log."""

        def javaScriptConsoleMessage(self, level, message, line, source):
            try:
                lvl = getattr(level, "name", str(level))
                log.info("renderer js[%s] %s (%s:%s)", lvl, message,
                         (source or "").rsplit("/", 1)[-1], line)
            except Exception:
                pass

    class Window(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("Visualiser by Fiercejojo88862")
            self.resize(int(cfg.get("window_width", 1280)), int(cfg.get("window_height", 720)))
            self.view = QWebEngineView(self)
            self.page = Page(self.view)
            self.view.setPage(self.page)
            self.setCentralWidget(self.view)
            s = self.view.settings()
            s.setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
            s.setAttribute(QWebEngineSettings.WebAttribute.ShowScrollBars, False)
            s.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, True)
            self.channel = QWebChannel(self.page)
            self.channel.registerObject("vis", bridge)
            self.page.setWebChannel(self.channel)
            self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
            self.view.load(QUrl(f"http://127.0.0.1:{cfg['port']}/"))
            # F11/Escape/Ctrl+R only: plain letters must stay usable while typing
            # into the settings panel, and the renderer handles its own hotkeys.
            QShortcut(QKeySequence("F11"), self, self.toggle_full)
            QShortcut(QKeySequence("Escape"), self, self.exit_full)
            QShortcut(QKeySequence("Ctrl+Q"), self, self.close)
            QShortcut(QKeySequence("Ctrl+R"), self, self.view.reload)

        def toggle_full(self):
            self.showNormal() if self.isFullScreen() else self.showFullScreen()

        def exit_full(self):
            if self.isFullScreen():
                self.showNormal()

        def closeEvent(self, event):
            log.info("window closing")
            cleanup()
            super().closeEvent(event)

    # SIGTERM arrives when `docker stop` / `systemctl --user stop` runs. Without a
    # handler the process dies on the signal and the engine never flushes its
    # recording/caches; quitting through Qt keeps the stop clean and instant.
    import signal

    def _on_signal(signum, _frame):
        log.info("received signal %s", signum)
        app.quit()

    for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(_sig, _on_signal)
        except Exception:
            pass
    app.aboutToQuit.connect(cleanup)

    win = Window()
    win.show()
    engine.start()
    log.info("window shown (port %s)", cfg["port"])

    if args.screenshot or args.probe:
        from PyQt6.QtCore import QTimer

        def do_capture() -> None:
            if args.screenshot:
                try:
                    shot = win.view.grab()
                    ok = shot.save(args.screenshot)
                    print(f"screenshot: {args.screenshot} ({'ok' if ok else 'FAILED'}) "
                          f"{shot.width()}x{shot.height()}")
                except Exception as exc:
                    print(f"screenshot failed: {exc}")
            if args.probe:
                probe_js = """
                (() => {
                  const out = {};
                  const sample = (el, name) => {
                    if (!el) return;
                    const c = el.getContext && el.getContext('2d');
                    if (!c || !el.width) return;
                    try {
                      const d = c.getImageData(0, 0, el.width, el.height).data;
                      let sum = 0, lit = 0, max = 0;
                      for (let i = 0; i < d.length; i += 4 * 97) {
                        const v = (d[i] + d[i+1] + d[i+2]) / 3;
                        sum += v; max = Math.max(max, v); if (v > 12) lit++;
                      }
                      const n = Math.ceil(d.length / (4 * 97));
                      out[name] = {mean: +(sum/n).toFixed(1), max: +max.toFixed(0), lit_pct: +(100*lit/n).toFixed(1)};
                    } catch (e) { out[name] = 'error: ' + e.message; }
                  };
                  sample(document.querySelector('#procedural'), 'procedural');
                  sample(document.querySelector('#postfx'), 'postfx');
                  const deck = document.querySelector('.deck.active');
                  const media = deck ? deck.querySelector('video,img') : null;
                  out.deck = !!deck;
                  out.decks = [...document.querySelectorAll('.deck')].map((d) => {
                    const m = d.querySelector('video,img');
                    return {
                      id: d.id, active: d.classList.contains('active'),
                      media: m ? m.tagName + ':' + (m.currentSrc || '').split('/').pop() : null,
                      ready: m ? (m.tagName === 'IMG' ? m.complete : m.readyState) : null,
                      opacity: getComputedStyle(d).opacity,
                    };
                  });
                  out.media = media ? (media.tagName + ':' + (media.currentSrc||'').split('/').pop()) : null;
                  out.media_size = media ? [media.videoWidth||media.naturalWidth, media.videoHeight||media.naturalHeight] : null;
                  out.media_ready = media ? (media.tagName === 'IMG' ? media.complete : media.readyState >= 2) : false;
                  const vinyl = document.querySelector('#vinyl');
                  out.vinyl_visible = !!vinyl && !vinyl.classList.contains('hidden') && vinyl.offsetWidth > 0;
                  const backdrop = document.querySelector('#backdrop');
                  out.backdrop_visible = !!backdrop && backdrop.classList.contains('show')
                     && getComputedStyle(backdrop).backgroundImage !== 'none';
                  out.progress_fill = document.querySelector('#progress-fill').style.width;
                  out.elapsed = document.querySelector('#time-elapsed').textContent;
                  out.remaining = document.querySelector('#time-remaining').textContent;
                  out.title = document.querySelector('#track-title').textContent;
                  out.meta = document.querySelector('#track-meta').textContent.trim();
                  const root = getComputedStyle(document.documentElement);
                  out.vars = {
                    accent: root.getPropertyValue('--accent').trim(),
                    beat: root.getPropertyValue('--beat').trim(),
                    shake_scale: root.getPropertyValue('--shake-scale').trim(),
                    flash: root.getPropertyValue('--flash-alpha').trim(),
                    paused: root.getPropertyValue('--paused').trim(),
                  };
                  out.raf = window.__rafN || 0;
                  const toast = document.querySelector('#toast');
                  out.toast = toast && !toast.classList.contains('hidden') ? toast.textContent : null;
                  if (window.VIS) {
                    const v = window.VIS;
                    out.vis = {
                      activeDeck: v.activeDeck, cuts: v.cuts,
                      cutAttempts: v.cutAttempts || 0, cutLog: v.cutLog || [],
                      buffer: v.buffer.length, playing: v.playing, seenFrame: !!v._seenFrame,
                      lastReq: Math.round(v.lastRequest),
                      deckItems: (v.decks || []).map((d) => ({
                        item: d.item ? (d.item.url || "").split("/").pop() : null,
                        gen: d.gen || 0, ready: d.ready,
                        nodes: d.inner.children.length,
                      })),
                    };
                  }
                  out.attribution_visible = getComputedStyle(document.querySelector('#attribution')).opacity;
                  return JSON.stringify(out);
                })()
                """

                def report(value) -> None:
                    print("probe: " + str(value))

                def report2(value) -> None:
                    print("probe2: " + str(value))

                win.view.page().runJavaScript(probe_js, report)
                QTimer.singleShot(2500, lambda: win.view.page().runJavaScript(probe_js, report2))
            QTimer.singleShot(3200, app.quit)

        QTimer.singleShot(int(args.after * 1000), do_capture)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
