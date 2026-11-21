"""beats.py: synthetic click tracks -> BPM tracking, beat events, chorus."""

import numpy as np

from app.core.beats import FPS, BeatTracker


def _clicks(bpm, seconds, nframes=None):
    nframes = nframes or int(seconds * FPS)
    flux = np.zeros(nframes, dtype=np.float32)
    period = int(round(60.0 / bpm * FPS))
    i = 0
    while i + 1 < nframes:
        flux[i] = 1.0
        i += max(1, period)
    return flux


def _feed(tr, flux, rms=0.3):
    out = []
    for i, f in enumerate(flux):
        out.append(tr.push(float(f), rms, i / FPS))
    return out


def test_bpm_tracks_synthetic_clicks():
    tr = BeatTracker()
    _feed(tr, _clicks(128, 12))
    assert 100 <= tr.bpm <= 160, tr.bpm
    assert tr.locked or tr.confidence > 0


def test_beat_events_fire_on_clicks():
    tr = BeatTracker()
    evs = _feed(tr, _clicks(100, 10))
    assert any(e["beat"] for e in evs), "no beats detected on 100 bpm clicks"


def test_chorus_section_on_sustained_loud():
    tr = BeatTracker()
    flux = _clicks(110, 14)
    n = len(flux)
    # rise from soft to loud in the second half -> sustained loud = chorus
    rms = np.empty(n, dtype=np.float32)
    for i in range(n):
        t = i / n
        rms[i] = 0.1 + t * (0.85 - 0.1) + 0.01 * np.sin(i / 30.0)
    evs = []
    for i, f in enumerate(flux):
        evs.append(tr.push(float(f), float(rms[i]), i / FPS))
    assert "chorus" in {e["section"] for e in evs}, "no chorus detected"


def test_verse_on_quiet_descending():
    tr = BeatTracker()
    flux = _clicks(100, 14)
    n = len(flux)
    rms = np.empty(n, dtype=np.float32)
    for i in range(n):
        rms[i] = 0.4 - 0.28 * min(1.0, i / (n / 2.0))
    evs = []
    for i, f in enumerate(flux):
        evs.append(tr.push(float(f), float(rms[i]), i / FPS))
    assert "verse" in {e["section"] for e in evs}, "no verse detected"
