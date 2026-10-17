"""Online (real-time) beat, onset and drop tracking.

Necessary because streamed sources (Apple Music web in Firefox and friends) give
us no file to pre-analyse: we only ever have the monitor stream. A tempo
autocorrelation plus a phase-locked loop gives a decent musical grid within a
few seconds of audio.
"""

from __future__ import annotations

import logging
import math
from collections import deque

import numpy as np

log = logging.getLogger("beats")

FPS = 48000 / 1024.0          # ~46.9 analysis frames per second
MIN_BPM, MAX_BPM = 60.0, 200.0
HIST = 384                    # ~8.2 s of onset envelope


class BeatTracker:
    def __init__(self):
        self.env: deque[float] = deque(maxlen=HIST)
        self.env_np = np.zeros(HIST, dtype=np.float32)
        self.flux_hist: deque[float] = deque(maxlen=HIST)
        self.rms_hist: deque[float] = deque(maxlen=HIST)
        self.bpm = 0.0
        self.confidence = 0.0
        self.period = 0.0        # frames per beat
        self.phase = 0.0         # 0..1 within the beat
        self.locked = False
        self._miss = 0
        self._last_beat_t = 0.0
        self._since_analysis = 0
        self._bpm_hist: deque[float] = deque(maxlen=5)
        self._last_onset_frame = -99
        self._frame = 0
        self._drop_cooldown = 0
        self._onset_mean = 0.05

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _autocorr(sig: np.ndarray) -> tuple[float, float]:
        sig = sig - sig.mean()
        if np.allclose(sig, 0):
            return 0.0, 0.0
        ac = np.correlate(sig, sig, mode="full")[sig.size - 1:]
        ac = ac / (ac[0] + 1e-9)
        min_lag = int(round(60.0 / MAX_BPM * FPS))
        max_lag = int(round(60.0 / MIN_BPM * FPS))
        max_lag = min(max_lag, ac.size - 1)
        if max_lag <= min_lag + 1:
            return 0.0, 0.0
        seg = ac[min_lag:max_lag]
        idx = int(np.argmax(seg)) + min_lag
        # parabolic refinement
        lag = float(idx)
        if 0 < idx < ac.size - 1:
            y0, y1, y2 = ac[idx - 1], ac[idx], ac[idx + 1]
            denom = (y0 - 2 * y1 + y2)
            if abs(denom) > 1e-9:
                lag += 0.5 * (y0 - y2) / denom
        if lag <= 0:
            return 0.0, 0.0
        return 60.0 * FPS / lag, float(ac[idx])

    # -- main --------------------------------------------------------------
    def push(self, flux: float, rms: float, now: float) -> dict:
        self._frame += 1
        self.flux_hist.append(flux)
        self.rms_hist.append(rms)

        # adaptive onset threshold from a short trailing window
        tail = list(self.flux_hist)[-24:]
        local_mean = float(np.mean(tail)) if tail else 0.0
        self._onset_mean = 0.98 * self._onset_mean + 0.02 * local_mean
        onset = flux > max(self._onset_mean * 1.8, 1e-4)
        if onset:
            env_val = min(1.0, flux / (self._onset_mean * 4.0 + 1e-9))
            self._last_onset_frame = self._frame
        else:
            env_val = 0.0
        self.env.append(env_val)

        # tempo re-estimate every ~2 s (early estimates use a shorter history so
        # we lock onto the grid in ~3 s instead of waiting 8 s)
        self._since_analysis += 1
        if self._since_analysis >= 94 and len(self.env) >= 128:
            self._since_analysis = 0
            arr = np.asarray(list(self.env)[-HIST:], dtype=np.float32)
            bpm, conf = self._autocorr(arr)
            if conf > 0.20 and MIN_BPM <= bpm <= MAX_BPM:
                self._bpm_hist.append(bpm)
                self.bpm = float(np.median(self._bpm_hist))
                self.confidence = conf
                self.period = 60.0 * FPS / self.bpm
                if not self.locked and conf > 0.30:
                    self.locked = True

        # phase-locked loop
        beat = False
        strength = 0.0
        if self.period > 0:
            self.phase += 1.0 / self.period
            recent_onset = (self._frame - self._last_onset_frame) <= 2
            if self.phase >= 1.0:
                self.phase -= 1.0
                if recent_onset:
                    beat, strength = True, 1.0
                    self._miss = 0
                    self._last_beat_t = now
                elif self.locked and self._miss < 3:
                    beat, strength = True, 0.45
                    self._miss += 1
                else:
                    self._miss += 1
                    if self._miss > 4:
                        self.locked = False
                        self._miss = 0
                        # re-lock by jumping straight onto the last strong onset
                        if self._frame - self._last_onset_frame <= int(self.period):
                            self.phase = (self._frame - self._last_onset_frame) / self.period

        # drop detection: sudden loudness behind a burst of flux
        drop = False
        if self._drop_cooldown > 0:
            self._drop_cooldown -= 1
        else:
            rh = np.fromiter(self.rms_hist, dtype=np.float32, count=len(self.rms_hist))
            if rh.size > 60:
                mu, sd = float(rh.mean()), float(rh.std()) + 1e-9
                z = (rms - mu) / sd
                flux_spike = flux > self._onset_mean * 3.5
                if z > 1.6 and flux_spike and rms > 0.05:
                    drop = True
                    self._drop_cooldown = int(3.0 * FPS)

        return {
            "bpm": round(self.bpm, 1),
            "confidence": round(self.confidence, 2),
            "locked": self.locked,
            "beat": beat,
            "beat_strength": round(strength, 2),
            "drop": drop,
            "phase": round(self.phase, 3),
            "onset": bool(onset),
        }

    def position_in_beat(self) -> float:
        return self.phase


class OfflineTimeline:
    """Timeline built from a recorded stream, in numpy only.

    This used to call librosa. librosa pulls in numba, and numba cannot build its
    jit cache on this Python (3.14): `RuntimeError: cannot cache function ...
    no locator available`. The import raised, the builder swallowed it and returned
    False, so the whole offline-timeline feature silently never ran.

    The maths is the same shape as BeatTracker's live path - spectral-flux onsets,
    autocorrelated tempo, a phase-locked grid - but it sees the entire track at
    once, so the grid is exact rather than converged.
    """

    ONSET_WIN = 1024
    ONSET_HOP = 512
    BLOCK = 512

    @staticmethod
    def build(wav_path, out_path) -> bool:
        try:
            import json
            import wave

            with wave.open(str(wav_path)) as w:
                sr = w.getframerate() or 48000
                channels = w.getnchannels() or 1
                if w.getsampwidth() != 2:      # the recorder only writes 16-bit
                    return False
                raw = w.readframes(w.getnframes())
            y = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            if channels > 1:
                y = y.reshape(-1, channels).mean(axis=1)
            if y.size < sr * 20:
                return False
            if sr > 24000:                     # plenty for a beat grid, halve the FFTs
                y = y[::2]
                sr //= 2

            win, hop = OfflineTimeline.ONSET_WIN, OfflineTimeline.ONSET_HOP
            if y.size <= win:
                return False
            frames = 1 + (y.size - win) // hop
            view = np.lib.stride_tricks.sliding_window_view(y, win)[::hop][:frames]
            taper = np.hanning(win).astype(np.float32)

            flux = np.zeros(frames, dtype=np.float32)
            rms = np.zeros(frames, dtype=np.float32)
            prev = None
            for start in range(0, frames, OfflineTimeline.BLOCK):
                chunk = view[start:start + OfflineTimeline.BLOCK].astype(np.float32) * taper
                mag = np.log1p(np.abs(np.fft.rfft(chunk, axis=1)) * 12.0)
                diff = np.empty(mag.shape[0], dtype=np.float32)
                diff[0] = 0.0 if prev is None else float(np.maximum(0.0, mag[0] - prev).sum())
                if mag.shape[0] > 1:
                    # half-wave rectified spectral flux
                    diff[1:] = np.maximum(0.0, np.diff(mag, axis=0)).sum(axis=1)
                n = mag.shape[0]
                flux[start:start + n] = diff
                rms[start:start + n] = np.sqrt((chunk ** 2).mean(axis=1))
                prev = mag[-1]

            fps = sr / hop
            onset_env = np.maximum(flux - float(np.median(flux)), 0.0)
            if not onset_env.any():
                return False

            period = OfflineTimeline._tempo_period(onset_env, fps)
            if period <= 0:
                return False
            beats = OfflineTimeline._beat_grid(onset_env, period, frames)
            if beats.size < 8:
                return False

            peak = float(onset_env.max())
            onsets = np.flatnonzero(onset_env > peak * 0.35)
            energy = rms / (float(rms.max()) + 1e-9)
            data = {
                "tempo": round(float(60.0 * fps / period), 2),
                "beats": [round(float(i / fps), 4) for i in beats],
                "onsets": [round(float(i / fps), 4) for i in onsets],
                "energy": [round(float(e), 4) for e in energy],
                "energy_times": [round(float(i / fps), 3) for i in range(frames)],
                "duration": round(float(y.size / sr), 3),
            }
            with open(out_path, "w") as fh:
                json.dump(data, fh)
            return True
        except Exception:
            log.exception("offline timeline build failed")
            return False

    @staticmethod
    def _grid_search(env: np.ndarray, period: float) -> tuple[float, float]:
        """(best phase, score) for a grid of that period over the whole track."""
        period = float(period)
        if period < 2 or env.size < 16:
            return 0.0, -1.0
        count = int(env.size // period)
        if count < 8:
            return 0.0, -1.0
        best_phase, best_score = 0.0, -1.0
        last = env.size - 1
        steps = 40
        for phase in np.linspace(0.0, period, steps, endpoint=False):
            idx = (np.arange(count) * period + phase).astype(np.int64)
            idx = idx[idx < last]
            if idx.size < 8:
                continue
            # a beat rarely lands exactly on a frame: score the local maximum
            vals = np.maximum.reduce([
                env[idx], env[np.minimum(idx + 1, last)], env[np.maximum(idx - 1, 0)]])
            score = float(vals.mean())
            if score > best_score:
                best_phase, best_score = float(phase), score
        return best_phase, best_score

    @staticmethod
    def _tempo_period(env: np.ndarray, fps: float) -> float:
        """Autocorrelation tempo (frames per beat), with half/double checked."""
        x = env - float(env.mean())
        if x.size < 32 or not np.any(x):
            return 0.0
        n = 1 << int(np.ceil(np.log2(x.size)))
        spec = np.abs(np.fft.rfft(x, n)) ** 2
        ac = np.fft.irfft(spec, n)[: x.size]
        if ac[0] <= 0:
            return 0.0
        ac = ac / ac[0]
        lo = max(2, int(round(60.0 / MAX_BPM * fps)))
        hi = min(x.size - 1, int(round(60.0 / MIN_BPM * fps)))
        if hi <= lo:
            return 0.0
        seg = ac[lo:hi]
        best = float(int(np.argmax(seg)) + lo)
        # A click track (or any strictly periodic material) scores identically at a
        # lag and at twice that lag, and the raw autocorrelation peak can land on
        # either. Prefer the faster grid unless the slower one is clearly stronger
        # (that is how genuine 60-80 bpm material still wins).
        scored = []
        for period in (best, best / 2.0, best * 2.0):
            if period < lo or period > hi:
                continue
            phase, score = OfflineTimeline._grid_search(env, period)
            if score > 0:
                scored.append((score, float(period)))
        if not scored:
            return best
        top = max(s for s, _ in scored)
        close = [p for s, p in scored if s >= 0.92 * top]
        return min(close) if close else max(scored)[1]

    @staticmethod
    def _beat_grid(env: np.ndarray, period: float, frames: int) -> np.ndarray:
        phase, score = OfflineTimeline._grid_search(env, period)
        if score <= 0:
            return np.zeros(0)
        idx = (np.arange(int(frames // period) + 1) * float(period) + phase).astype(np.int64)
        return idx[idx < frames - 1]

    def __init__(self, data: dict):
        self.data = data
        self.beats = data.get("beats", [])
        self.drops = self._find_drops(data)

    @staticmethod
    def _find_drops(data: dict) -> list[float]:
        times = data.get("energy_times") or []
        energy = data.get("energy") or []
        if len(times) < 20:
            return []
        arr = np.asarray(energy)
        drops = []
        for i in range(8, len(arr)):
            window = arr[max(0, i - 8):i]
            if arr[i] > window.mean() + 2.0 * (window.std() + 1e-6) and arr[i] > 0.25:
                t = times[i]
                if not drops or t - drops[-1] > 4.0:
                    drops.append(float(t))
        return drops

    def next_beat(self, pos: float) -> tuple[float | None, float]:
        for b in self.beats:
            if b >= pos:
                return float(b), float(b - pos)
        return None, 0.0

    def energy_at(self, pos: float) -> float:
        times = self.data.get("energy_times") or []
        energy = self.data.get("energy") or []
        if not times:
            return 0.0
        i = int(np.searchsorted(times, pos))
        i = max(0, min(i, len(energy) - 1))
        return float(energy[i])

    def drop_soon(self, pos: float, within: float = 1.0) -> bool:
        return any(pos <= d <= pos + within for d in self.drops)
