<p align="center">
  <img src="assets/visualiser.png" alt="Visualiser by Fiercejojo88862" width="220">
</p>

# Visualiser by Fiercejojo88862

A song visualiser that reacts to whatever is playing on this machine and pulls
clips/GIFs that match the track. It watches MPRIS for the current song, listens to
the system audio monitor for the beat, grabs time-synced lyrics, and fetches media
from KLIPY (with GIPHY as a fallback). Everything runs in a Docker container that
starts when you open the app and stops when you close it.

```
Music/Visualiser by Fiercejojo88862/
├── app/                     # Python engine + renderer
│   ├── main.py              # Qt window, WebChannel bridge, entry point
│   ├── server.py            # local HTTP server (renderer + cached media)
│   ├── config.py            # settings + paths
│   ├── core/
│   │   ├── mpris.py         # player tracking + clock anchoring
│   │   ├── audio.py         # PipeWire monitor capture + band analysis
│   │   ├── beats.py         # live beat/onset/drop detection + offline timelines
│   │   ├── lyrics.py        # LRCLIB time-synced lyrics
│   │   ├── fetch.py         # KLIPY / GIPHY providers + rate limiting
│   │   ├── media.py         # download, transcode, cache, evict
│   │   ├── art.py           # album artwork + colour palette extraction
│   │   ├── selector.py      # which clip for which moment
│   │   └── engine.py        # ties it together, pushes frames to the renderer
│   └── web/                 # index.html, style.css, render.js (canvas + decks)
├── bin/
│   ├── visualiser           # launcher / stop / status / logs / selftest
│   ├── run-container.sh     # the docker run the systemd unit executes
│   └── install.sh           # wires launcher, desktop entry, icon, unit
├── docker/                  # Dockerfile + docker-compose.yml
├── desktop/                 # .desktop entry template
├── systemd/                 # user unit
├── assets/                  # icon
└── state/                   # settings, cache, timelines, logs (gitignored)
```

## Quick start

```bash
cd "$HOME/Music/Visualiser by Fiercejojo88862"
bin/install.sh                      # launcher, desktop entry, icon, systemd unit
bin/visualiser                      # start (builds the image on first run)

# recommended: give it a media source
bin/visualiser set klipy_key YOUR_KEY     # https://partner.klipy.com
```

Then the app appears on the Applications menu as **Visualiser by Fiercejojo88862**.

## Adding your own GIFs

No API key needed — point the app at a folder and every clip it can play comes from
there:

```bash
bin/visualiser set local_library "$HOME/Music/Visualiser GIFs"
bin/visualiser stop && bin/visualiser start     # the folder is mounted at startup
```

Then just drop files into that folder (subfolders are searched too). **New files are
picked up while the app is running** — the selector rescans whenever it runs low on
clips, so a GIF copied in shows up within a few seconds without a restart. Only a
*path* change needs the restart above, because the folder is bind-mounted into the
container when it starts.

- Formats: `gif`, `webp`, `apng`, `mp4`, `webm`, `mkv`, `mov`, `png`, `jpg`, `jpeg`.
  GIFs are transcoded to VP9/webm on first use; stills are shown as static clips.
- Up to 16 files are taken per rescan, shuffled, with never-seen files first, so a
  folder bigger than that still gets used across a session. Keep it to a few hundred
  MB — imports are copied into the clip cache and LRU-evicted at `cache_cap_gb` (6 GB).
- Files must be readable by the container (which runs as your uid, so normal home
  folders are fine).
- Change the folder any time in the panel (`S` → *local clip folder*); the path only
  takes effect after a restart. `bin/visualiser set local_library ""` removes it.

Local clips are mixed with provider results when a key is configured, so you can use
both at once.

## What you see

Five layers, bottom to top:

1. **Blurred album-art backdrop** — the current cover, blown up and blurred, tinting
   the whole stage.
2. **Procedural canvas** (single canvas, additive):
   - six band-driven glow orbs that follow the bass envelope in size and brightness;
   - a **particle field bound to live frequencies** — every particle owns a band
     (log-spaced across 32 Hz–16 kHz) and is pushed outward by *that* band, with
     decay trails for motion blur;
   - a **radial frequency ring** around the centre warp origin (64 log-spaced bars)
     plus an **ultra-thin bottom spectrum** bar row;
   - expanding beat rings, and a rotating hue that follows the mood.
3. **The clip itself** on two crossfading decks, transformed by the audio (scale,
   rotate, saturation, contrast, hue shift), composited with `screen`.
4. **Post-processing pass** — bloom of the clip, plus RGB channel-split chromatic
   aberration on transients and drops. It samples the clip at 1/3 resolution into an
   offscreen canvas, so it is cheap; if the frame rate drops below ~42 fps it turns
   itself off rather than stutter.
5. **HUD** — title, `artist / mood / BPM`, a metronome ring that pulses the tempo
   around the BPM tag, an **ultra-thin scrubbable progress bar** with elapsed and
   remaining time, transport buttons (MPRIS prev / play-pause / next), and a
   rounded-corner **rotating vinyl thumbnail** of the cover. Everything fades out
   after 12 s of no mouse movement, leaving pure full-screen visuals; `H` hides the
   HUD outright (the KLIPY attribution stays faintly visible unless you do).

Colour is synced from the artwork: the cover is downscaled with `ffmpeg` to a
32×32 raw buffer, quantised in numpy into a 4-colour palette, and the most vivid
cluster is used to tint the orbs, the ring, the progress bar and the glow — falling
back to the mood hue when there is no artwork.

## How the sync actually works

Three layers, all sharing one clock:

1. **Reactive** — the PipeWire monitor of your output sink is captured with
   `parec`, FFT'd at ~47 frames/sec, and split into sub/bass/low-mid/mid/high/air
   envelopes with per-band adaptive floor and ceiling. Bass drives scale, highs
   drive rotation and blur, mids drive saturation, air drives contrast.

2. **Beat grid** — a spectral-flux onset envelope feeds an autocorrelation tempo
   estimate plus a phase-locked loop, so cuts land on beats. Onset bursts plus a
   loudness z-score spike fire **drop** events, which force a cut, a glitch, a
   hue jump and a particle burst. Tempo locks in roughly 3 seconds.

   Streamed sources (Apple Music web in Firefox) give you no file to analyse, so
   live tracking is the primary path. While a track plays once, the engine also
   records the monitor stream, and after the track ends (45 s minimum) it analyses
   it in numpy — spectral flux, autocorrelated tempo, phase-locked grid, drop
   detection — and caches a per-track beat timeline, so the *second* listen is
   tighter still. No librosa: see the note at the end.

3. **Semantic selection** — the track's lyrics (LRCLIB) and an audio-derived mood
   (`frenetic / energetic / euphoric / melancholic / calm / dreamy / gritty`, from
   tempo + loudness + spectral centroid) become search queries. Results are ranked
   by keyword and mood overlap, damped by a recency penalty so the same three
   clips do not loop all night, pre-fetched during the first seconds of playback,
   cached by *query* (so repeat moods and lyrics compound into a warm cache) and
   then cut on beats.

**Fallback chain:** if the provider is unconfigured, rate-limited or offline, the
renderer keeps running on its procedural layer (a canvas of band-driven blobs,
particles and beat rings), so it never goes black.

## Container lifecycle

- `bin/run-container.sh` is `docker run --rm` in the foreground.
- The systemd user unit treats that process as the service: close the window →
  Qt exits → container exits → docker removes it → the unit goes inactive.
- `visualiser stop` sends `systemctl --user stop`, which SIGTERMs the container.
- The unit is **not** enabled at login. It starts when you open the app, exactly
  as requested. To make it come up at login instead, run
  `systemctl --user enable visualiser-by-fiercejojo88862.service`.

## Hotkeys

| Key | Action |
| --- | --- |
| `F11` | fullscreen / windowed |
| `S` | settings panel |
| `H` | hide/show the whole HUD |
| `O` | toggle overlay (track info, lyrics, badge) |
| `N` | cut to the next clip now |
| `Space` | pause/resume the visuals |
| `←` `→` | seek back / forward (`seek_step_seconds`, default 15 s) |
| `Esc` | close the panel |
| `Ctrl+R` | reload the renderer |
| `Ctrl+Q` | quit (stops the container) |

Click or drag the progress bar to scrub (it uses the MPRIS `Seek` call, so it works
on any player that supports relative seeking). The vinyl spins while playing and the
visuals sink into a slow, desaturated drift when you pause.

## Settings

`state/settings.json`, editable live from the panel or with
`bin/visualiser set KEY VALUE`:

| Key | Meaning |
| --- | --- |
| `klipy_key` / `giphy_key` | provider API keys |
| `provider` | `klipy` (default) or `giphy` |
| `content_filter` | `off` / `low` / `medium` / `high` |
| `clip_kind` | `gif`, `clip` (10s video clips) or `meme` |
| `queries_per_hour` | tester-key ceiling; `0` = unlimited (production key) |
| `max_queries_per_track` | search budget per song |
| `effect_intensity` | overall reactive strength |
| `min_beats_between_cuts` | cut cadence |
| `av_offset_ms` | A/V offset — raise it if cuts feel late through Bluetooth |
| `spectrum_bands` | fine spectrum resolution for the ring/bottom bars (restart) |
| `radial_spectrum` / `bottom_spectrum` | the two spectrum overlays |
| `bass_shake` | sub-bass camera shake + radial pulse |
| `particle_trails` / `particle_count` | trail motion blur, particle density |
| `postfx_bloom` / `postfx_chroma` | post-processing passes |
| `show_progress` | progress scrubber + transport buttons |
| `show_album_art` / `art_backdrop` / `palette_sync` | artwork, blurred backdrop, colour sync |
| `show_bpm_ring` | metronome ring around the BPM tag |
| `hud_autohide_seconds` | HUD fade-out delay, `0` = never |
| `seek_step_seconds` | `←`/`→` seek distance |
| `seek_enabled` | allow the app to seek your player |
| `lyrics_enabled`, `show_lyrics` | lyric syncing |
| `local_library` | folder of local gifs/videos used alongside (or instead of) the API — new files are picked up live, a path change needs a restart |
| `cache_cap_gb` | clip cache cap (LRU-evicted) |
| `monitor_source` | override the PulseAudio monitor, empty = auto |
| `prefer_player` | e.g. `firefox`, empty = auto (ignores `playerctld`) |

## Provider notes

- **Tenor is dead** — Google shut down its public API on 2026-06-30, so it is not
  an option. KLIPY is the ex-Tenor-engineers replacement with a free production
  key; GIPHY works but production access is paid.
- Both providers forbid re-hosting their media. `state/cache/` is a local playback
  cache for personal use (capped, evictable) — do not redistribute it, and do not
  publish recordings that contain provider media.
- Attribution: the renderer shows "Powered by KLIPY". Keep it if you use the key.
- Offline beat analysis records the system audio monitor to `state/cache/recordings`,
  analyses it, then deletes the wav. That is personal-use only; turn it off with
  `record_streams` if you would rather not write audio to disk at all.

## Troubleshooting

```bash
bin/visualiser status          # unit, container, cpu/mem, last log lines
bin/visualiser logs            # follow the engine log
bin/visualiser selftest        # checks audio device, MPRIS, lyrics, provider
docker logs visualiser-by-fiercejojo88862 | tail -50

# what the renderer is actually drawing (pixels, layers, fps, art, audio state):
bin/run-container.sh --probe --after 12
# save a frame to state/probe.png (works when grim cannot, e.g. docked/clamshell):
bin/run-container.sh --probe --screenshot /app/state/probe.png --after 10
```

Renderer console output — including uncaught JS errors — is written to the log as
`renderer js[...]`, and unrecoverable page errors also come back as `js error:`.

| Symptom | Fix |
| --- | --- |
| Window opens, nothing moves | `selftest` — if the monitor source is wrong, set `monitor_source` manually to `<sink>.monitor` |
| "No media source yet" toast | set `klipy_key`, or set `local_library` to a folder of gifs |
| "Local folder has no clips" toast | `local_library` is set but the folder is empty (or not mounted into the container) — drop gifs in, or check the path |
| Cuts feel late/early | adjust `av_offset_ms` (Bluetooth adds 100–250 ms) |
| No track info | the player must expose MPRIS; `playerctld` is ignored on purpose |
| Album art missing | the cover is a local file — for Firefox, `~/.config/mozilla/firefox/firefox-mpris` is mounted read-only into the container; other players can be added with `VISUALISER_EXTRA_MOUNTS="/path:/path:ro"` |
| Black window | try `--set chromium_flags "--no-sandbox --disable-gpu"` to force software rendering |
| "post-processing disabled" toast | the bloom/chroma pass could not hold 60 fps on this GPU — lower `effect_intensity` or turn the passes off |
| Rate limited | 100 req/hr on a tester key — request the free production key, or rely on the query cache |

## Notes on the container

The container needs the host's Wayland socket, PipeWire socket, session bus and
render node, so it is not a sandbox — it is a reproducibility wrapper. The image is
Arch-based to match the host's mesa/Qt/ffmpeg ABI exactly. ~3.2 GB, numpy only: no
librosa, no numba, no scipy.

```bash
bin/visualiser rebuild        # rebuild after changing docker/Dockerfile
```

**Why no librosa:** offline beat timelines originally used `librosa`. On this
Python (3.14) the import raises inside the image — numba's jit cache cannot find a
locator for `librosa/core/notation.py` (`RuntimeError: cannot cache function
'__o_fold': no locator available`) — and because the builder caught bare exceptions
it silently returned "no beats found" forever. It is now the same numpy maths as the
live tracker, verified at 72/100/128 bpm on synthetic material. Worth remembering:
a bare `except: return False` hid a total feature failure behind a plausible one.
