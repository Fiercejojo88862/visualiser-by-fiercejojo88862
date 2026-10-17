"""System audio capture + spectral band analysis.

Captures the PipeWire/PulseAudio *monitor* of the default sink via `parec`
(never the microphone). Produces normalised band envelopes at the analysis frame
rate plus a spectral-flux onset signal used by the beat tracker.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time

import numpy as np

log = logging.getLogger("audio")

RATE = 48000
HOP = 1024                  # analysis frame size -> ~46.9 fps
CHANNELS = 2
BYTES_PER_FRAME = HOP * CHANNELS * 2

# (label, low_hz, high_hz)
BANDS = [
    ("sub", 20, 60),
    ("bass", 60, 250),
    ("lowmid", 250, 500),
    ("mid", 500, 2000),
    ("high", 2000, 6000),
    ("air", 6000, 15000),
]


def detect_monitor_source(preferred: str = "") -> str:
    """Pick the pulse source that mirrors what is playing."""
    if preferred:
        return preferred
    if not shutil.which("pactl"):
        return ""
    try:
        out = subprocess.run(["pactl", "list", "short", "sources"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return ""
    monitors, running = [], []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name = parts[1].strip()
        if not name.endswith(".monitor"):
            continue
        monitors.append(name)
        if len(parts) >= 5 and "RUNNING" in parts[4]:
            running.append(name)
    # easyeffects/sink monitor first: that is the bus every app plays into
    for name in running + monitors:
        if "easyeffects" in name:
            return name
    if running:
        return running[0]
    if monitors:
        return monitors[0]
    return "@DEFAULT_MONITOR@"


class AudioCapture(threading.Thread):
    """Reads the monitor stream and keeps the latest analysis frame."""

    daemon = True

    def __init__(self, cfg: dict, on_frame=None):
        super().__init__(name="audio")
        self.cfg = cfg
        self.on_frame = on_frame
        self._stop = threading.Event()
        self.source = ""
        self.alive_stream = False
        self.error = ""

        self._lock = threading.Lock()
        self.bands: dict[str, float] = {b[0]: 0.0 for b in BANDS}
        self.rms = 0.0
        self.flux = 0.0
        self.centroid = 0.0
        self.frames = 0

        # per-band adaptive floor/ceiling in dB
        self._floor = {b[0]: -78.0 for b in BANDS}
        self._ceil = {b[0]: -30.0 for b in BANDS}
        self._smooth = {b[0]: 0.0 for b in BANDS}
        self._window = np.hanning(HOP).astype(np.float32)
        n = HOP // 2 + 1
        self._bin_hz = np.linspace(0, RATE / 2, n)
        self._band_idx = []
        for _, lo, hi in BANDS:
            self._band_idx.append((self._bin_hz >= lo) & (self._bin_hz < hi))
        self._prev_mag = np.zeros(n, dtype=np.float32)
        self._rms_hist: list[float] = []

        # fine spectrum for the radial ring / bottom bars: log-spaced bands
        self.spectrum_bands = int(cfg.get("spectrum_bands", 64) or 64)
        edges = np.logspace(np.log10(32.0), np.log10(16000.0), self.spectrum_bands + 1)
        self._spec_lo = edges[:-1]
        self._spec_hi = edges[1:]
        self._spec_idx = [(self._bin_hz >= lo) & (self._bin_hz < hi)
                          for lo, hi in zip(self._spec_lo, self._spec_hi)]
        self._spec_floor = np.full(self.spectrum_bands, -80.0, dtype=np.float32)
        self._spec_ceil = np.full(self.spectrum_bands, -34.0, dtype=np.float32)
        self._spec_smooth = np.zeros(self.spectrum_bands, dtype=np.float32)
        self._spec = np.zeros(self.spectrum_bands, dtype=np.float32)

        self.recorder = None  # optional StreamRecorder

    def latest(self) -> dict:
        with self._lock:
            d = dict(self.bands)
            d["rms"] = self.rms
            d["flux"] = self.flux
            d["centroid"] = self.centroid
        return d

    def spectrum(self) -> list[float]:
        with self._lock:
            return [round(float(v), 3) for v in self._spec]

    # -- capture -----------------------------------------------------------
    def _spawn(self):
        source = detect_monitor_source(self.cfg.get("monitor_source", ""))
        self.source = source
        cmd = ["parec", "--device=" + source, "--raw", "--format=s16le",
               f"--rate={RATE}", f"--channels={CHANNELS}", "--latency-msec=20"]
        if not shutil.which("parec"):
            raise RuntimeError("parec not found (install libpulse)")
        log.info("capturing: %s", " ".join(cmd))
        return subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, bufsize=0)

    def _read_exact(self, stream, n: int) -> bytes | None:
        buf = b""
        while len(buf) < n:
            chunk = stream.read(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def run(self) -> None:
        proc = None
        backoff = 1.0
        while not self._stop.is_set():
            try:
                if proc is None:
                    proc = self._spawn()
                    self.alive_stream = True
                    backoff = 1.0
                raw = self._read_exact(proc.stdout, BYTES_PER_FRAME)
                if raw is None:
                    raise RuntimeError("monitor stream ended")
                self._analyse(raw)
            except Exception as exc:
                self.error = str(exc)
                self.alive_stream = False
                if proc:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    proc = None
                log.warning("audio capture error: %s (retry in %.1fs)", exc, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 10.0)
        if proc:
            try:
                proc.kill()
            except Exception:
                pass

    # -- analysis ----------------------------------------------------------
    def _analyse(self, raw: bytes) -> None:
        samples = np.frombuffer(raw, dtype="<i2")
        if samples.size >= CHANNELS:
            samples = samples.reshape(-1, CHANNELS).mean(axis=1)
        x = samples.astype(np.float32) / 32768.0
        if self.recorder is not None:
            try:
                self.recorder.feed(x)
            except Exception:
                pass

        w = x * self._window
        mag = np.abs(np.fft.rfft(w)).astype(np.float32)
        db = 20.0 * np.log10(mag + 1e-7)

        flux = float(np.maximum(0.0, mag - self._prev_mag).sum() / mag.size)
        self._prev_mag = mag
        rms = float(np.sqrt(np.mean(x * x) + 1e-12))
        total = float(mag.sum()) + 1e-9
        centroid = float((self._bin_hz * mag).sum() / total)

        with self._lock:
            for (name, _lo, _hi), mask in zip(BANDS, self._band_idx):
                band_db = float(np.mean(db[mask])) if mask.any() else -100.0
                # slow-tracking noise floor, fast-tracking ceiling
                self._floor[name] = min(band_db, self._floor[name] * 0.999 + band_db * 0.001)
                self._ceil[name] = max(band_db, self._ceil[name] * 0.995 + band_db * 0.005)
                span = max(6.0, self._ceil[name] - self._floor[name])
                norm = (band_db - (self._ceil[name] - span)) / span
                norm = float(min(1.0, max(0.0, norm)))
                # attack fast / release slow => punchy but not jittery
                prev = self._smooth[name]
                self._smooth[name] = norm if norm > prev else prev * 0.82 + norm * 0.18
                self.bands[name] = self._smooth[name]
            self.rms = rms
            self.flux = flux
            self.centroid = centroid
            self.frames += 1
            self._rms_hist.append(rms)
            if len(self._rms_hist) > 4000:
                del self._rms_hist[:2000]

            # fine log-spaced spectrum (same adaptive floor/ceiling idea, vectorised)
            spec_db = np.empty(self.spectrum_bands, dtype=np.float32)
            for i, mask in enumerate(self._spec_idx):
                spec_db[i] = float(np.mean(db[mask])) if mask.any() else -100.0
            self._spec_floor = np.minimum(spec_db, self._spec_floor * 0.999 + spec_db * 0.001)
            self._spec_ceil = np.maximum(spec_db, self._spec_ceil * 0.995 + spec_db * 0.005)
            span = np.maximum(6.0, self._spec_ceil - self._spec_floor)
            norm = np.clip((spec_db - (self._spec_ceil - span)) / span, 0.0, 1.0)
            self._spec_smooth = np.where(norm > self._spec_smooth, norm,
                                         self._spec_smooth * 0.80 + norm * 0.20)
            self._spec = self._spec_smooth

        if self.on_frame:
            try:
                self.on_frame(w, raw)
            except Exception:
                pass

    def energy_level(self) -> float:
        """0..1 loudness relative to the last ~60 s of listening."""
        with self._lock:
            hist = list(self._rms_hist[-2800:])
            rms = self.rms
        if len(hist) < 40:
            return 0.0
        arr = np.asarray(hist)
        return float((arr < rms).mean())

    def stop(self) -> None:
        self._stop.set()


class StreamRecorder:
    """Buffers the captured monitor stream to a wav so a track can be analysed
    offline (see OfflineTimeline) once and cached for future plays."""

    def __init__(self, path):
        import wave
        self.wav = wave.open(str(path), "wb")
        self.wav.setnchannels(1)
        self.wav.setsampwidth(2)
        self.wav.setframerate(RATE)
        self.samples = 0
        self.path = path

    def feed(self, float_mono: np.ndarray) -> None:
        pcm = np.clip(float_mono * 32767.0, -32768, 32767).astype("<i2")
        self.wav.writeframes(pcm.tobytes())
        self.samples += pcm.size

    @property
    def seconds(self) -> float:
        return self.samples / RATE

    def close(self) -> None:
        try:
            self.wav.close()
        except Exception:
            pass


def silent_fallback(capture: AudioCapture) -> None:
    """Keeps the visuals alive (procedurally) if audio capture is unavailable."""
    t0 = time.monotonic()
    while not capture.alive_stream:
        with capture._lock:  # noqa: SLF001 - intentional internal poke
            t = time.monotonic() - t0
            for i, (name, _lo, _hi) in enumerate(BANDS):
                capture.bands[name] = 0.35 + 0.3 * np.sin(t * (0.7 + i * 0.27)) ** 2
            for i in range(capture.spectrum_bands):
                capture._spec[i] = 0.25 + 0.35 * (  # noqa: SLF001
                    0.5 + 0.5 * np.sin(t * (0.6 + i * 0.09) + i * 0.4))
            capture.rms = 0.2
            capture.flux = 0.1
        time.sleep(1 / 30)
