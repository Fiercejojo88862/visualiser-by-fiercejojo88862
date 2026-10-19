/* Visualiser by Fiercejojo88862 - renderer.
   Talks to the Python engine over the Qt WebChannel. Every message is JSON.
   Falls back to a self-driving demo when opened outside the app (plain browser).

   Layers, bottom to top:
     #backdrop   blurred album art
     #procedural canvas: palette-tinted glow orbs, particle field (frequency bound),
                 radial spectrum ring around the warp origin, bottom spectrum bars,
                 beat rings
     .deck x2    the clip/GIF itself, crossfaded on beats
     #postfx     chromatic aberration + bloom, sampled from the deck
     HUD         title / artist / mood / metronome BPM ring / progress scrubber
*/
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const lerp = (a, b, t) => a + (b - a) * t;
  const rng = (a = 0, b = 1) => a + Math.random() * (b - a);

  const MOOD_HUE = {
    frenetic: 320, energetic: 28, euphoric: 190, melancholy: 215,
    calm: 165, dreamy: 265, gritty: 12,
  };

  const state = {
    settings: {},
    target: { sub: 0, bass: 0, lowmid: 0, mid: 0, high: 0, air: 0, rms: 0 },
    smooth: { sub: 0, bass: 0, lowmid: 0, mid: 0, high: 0, air: 0, rms: 0 },
    spec: new Float32Array(64),
    specTarget: new Float32Array(64),
    bpm: 0, mood: "dreamy", locked: false, playing: false,
    pos: 0, duration: 0, phase: 0,
    beats: 0, cuts: 0, lastCutBeat: -99, panelOpen: false,
    buffer: [], history: [], activeDeck: 0, lastCut: -1e9, lastRequest: 0,
    flash: 0, hue: 190, hueTarget: 190, pulse: 0, transient: 0,
    lyric: "", track: {}, providersReady: false, localFiles: null,
    connected: false, demo: false, lastStatus: {}, fps: 0, frames: 0,
    art: null, artUrl: "", palette: [], accent: null,
    shakeX: 0, shakeY: 0, shakeScale: 1,
    scrub: null, dragging: false, lastActivity: performance.now(),
    hudHover: false, postfxOn: true, lowFpsSince: 0, paused: 0, speed: 1,
    postfxFrames: 0, chromaFrames: 0,
  };

  const pulseRings = [];
  let particles = [];

  function buildParticles(count) {
    particles = [];
    for (let i = 0; i < count; i++) {
      particles.push({
        x: rng(), y: rng(), px: rng(), py: rng(), z: rng(0.2, 1),
        a: rng(0.1, 0.5), band: 0,
      });
    }
    assignParticleBands();
  }

  function assignParticleBands() {
    const n = state.spec.length || 64;
    for (const p of particles) p.band = Math.floor(rng(0, n)) % n;
  }

  /* ---------- palette helpers ------------------------------------------- */
  function specHue(i, n) {
    // blends the artwork palette with the mood hue across the spectrum
    if (state.palette.length && state.settings.palette_sync !== false) {
      const c = state.palette[i % state.palette.length];
      return c[0] * 0.299 + c[1] * 0.587 + c[2] * 0.114 > 128
        ? rgbHue(c) : (state.hue + (i / n) * 70) % 360;
    }
    return (state.hue + (i / n) * 70) % 360;
  }

  function rgbHue(c) {
    const r = c[0] / 255, g = c[1] / 255, b = c[2] / 255;
    const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
    if (d === 0) return state.hue;
    let h;
    if (mx === r) h = 60 * (((g - b) / d) % 6);
    else if (mx === g) h = 60 * ((b - r) / d + 2);
    else h = 60 * ((r - g) / d + 4);
    return (h + 360) % 360;
  }

  /* ---------- procedural canvas ----------------------------------------- */
  const canvas = $("#procedural");
  const ctx = canvas.getContext("2d", { alpha: true });
  let W = 0, H = 0, dpr = 1;

  const postfx = $("#postfx");
  const pctx = postfx.getContext("2d", { alpha: true });
  const fx = document.createElement("canvas");
  const fxc = fx.getContext("2d", { alpha: false });
  const bl = document.createElement("canvas");
  const blc = bl.getContext("2d", { alpha: false });
  const chan = ["#ff0000", "#00ff00", "#0000ff"].map((color) => {
    const cv = document.createElement("canvas");
    return { cv, c: cv.getContext("2d", { alpha: false }), color };
  });

  function resize() {
    dpr = Math.min(2, window.devicePixelRatio || 1);
    W = canvas.clientWidth;
    H = canvas.clientHeight;
    canvas.width = Math.max(1, Math.floor(W * dpr));
    canvas.height = Math.max(1, Math.floor(H * dpr));
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    postfx.width = canvas.width;
    postfx.height = canvas.height;
    pctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const fw = Math.max(160, Math.round(W / 3));
    const fh = Math.max(90, Math.round(H / 3));
    fx.width = bl.width = fw;
    fx.height = bl.height = fh;
    for (const ch of chan) { ch.cv.width = fw; ch.cv.height = fh; }
    ctx.fillStyle = "#05060a";
    ctx.fillRect(0, 0, W, H);
  }
  window.addEventListener("resize", resize);

  function drawProcedural(t) {
    const s = state.smooth;
    const intensity = Number(state.settings.effect_intensity ?? 1);
    const trails = state.settings.particle_trails !== false;
    const energy = s.rms * (0.6 + 0.8 * intensity);
    const speed = state.speed;

    // persistence fade = motion blur / trails
    ctx.globalCompositeOperation = "source-over";
    ctx.fillStyle = trails
      ? `rgba(5,6,10,${0.20 + 0.16 * (1 - clamp(energy, 0, 1))})`
      : "rgba(5,6,10,0.55)";
    ctx.fillRect(0, 0, W, H);

    const bands = [s.sub, s.bass, s.lowmid, s.mid, s.high, s.air];
    const bass = clamp(s.sub * 0.7 + s.bass * 0.3, 0, 1);
    const cx = W / 2, cy = H / 2;

    // ---- glow orbs (bass drives radial scale + brightness) ----
    ctx.globalCompositeOperation = "lighter";
    for (let i = 0; i < bands.length; i++) {
      const v = clamp(bands[i], 0, 1);
      const ox = W * (0.5 + 0.34 * Math.sin(t * speed * (0.05 + i * 0.013) + i * 1.7));
      const oy = H * (0.5 + 0.3 * Math.cos(t * speed * (0.042 + i * 0.011) + i * 2.3));
      const puff = 1 + bass * 0.35 * intensity;
      const r = (0.12 + v * 0.5 + i * 0.02) * Math.min(W, H) * puff;
      const g = ctx.createRadialGradient(ox, oy, 0, ox, oy, r);
      const hue = state.palette.length && state.settings.palette_sync !== false
        ? rgbHue(state.palette[i % state.palette.length]) : (state.hue + i * 26) % 360;
      const alpha = (0.012 + v * 0.075) * (1 + bass * 0.35);
      g.addColorStop(0, `hsla(${hue}, 92%, ${52 + v * 26 + bass * 8}%, ${alpha})`);
      g.addColorStop(1, "hsla(0, 0%, 0%, 0)");
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.arc(ox, oy, r, 0, Math.PI * 2);
      ctx.fill();
    }

    // ---- particle field: dispersion bound to live frequencies ----
    const spec = state.spec;
    const nspec = spec.length || 64;
    const baseSpeed = 0.00012 + energy * 0.0018;
    for (const p of particles) {
      const bandVal = spec[p.band] || 0;
      const dx = p.x - 0.5, dy = p.y - 0.5;
      const d = Math.hypot(dx, dy) + 1e-4;
      const push = state.pulse * 0.004 * p.z + bandVal * 0.0022 * (0.4 + p.z);
      const v = (baseSpeed + push) * p.z * speed;
      p.px = p.x; p.py = p.y;
      p.x += (dx / d) * v * 26;
      p.y += (dy / d) * v * 26;
      if (d > 0.68) { p.x = 0.5 + rng(-0.05, 0.05); p.y = 0.5 + rng(-0.05, 0.05); p.px = p.x; p.py = p.y; }
      const hue = specHue(p.band, nspec);
      const alpha = p.a * (0.10 + energy * 0.28 + bandVal * 0.16);
      const size = 1.4 + p.z * 2.2 + bandVal * 2.5;
      if (trails) {
        ctx.strokeStyle = `hsla(${hue}, 85%, ${68 - 18 * p.z}%, ${alpha * 0.4})`;
        ctx.lineWidth = size * 0.7;
        ctx.beginPath();
        ctx.moveTo(p.px * W, p.py * H);
        ctx.lineTo(p.x * W, p.y * H);
        ctx.stroke();
      }
      ctx.fillStyle = `hsla(${hue}, 80%, ${70 - 20 * p.z}%, ${alpha})`;
      ctx.fillRect(p.x * W, p.y * H, size, size);
    }

    // ---- beat rings ----
    for (let i = pulseRings.length - 1; i >= 0; i--) {
      const ring = pulseRings[i];
      ring.age += 1 / 60;
      ring.r += (ring.speed + energy * 0.4) * 12 * speed;
      const life = 1 - ring.age / ring.life;
      if (life <= 0) { pulseRings.splice(i, 1); continue; }
      ctx.strokeStyle = `hsla(${(state.hue + ring.hue) % 360}, 95%, 68%, ${0.18 * life * ring.strength})`;
      ctx.lineWidth = 1 + 5 * life * ring.strength;
      ctx.beginPath();
      ctx.arc(cx, cy, ring.r * Math.min(W, H), 0, Math.PI * 2);
      ctx.stroke();
    }

    // ---- radial frequency ring around the warp origin ----
    if (state.settings.radial_spectrum !== false && nspec > 1) {
      const base = Math.min(W, H) * (0.26 + bass * 0.02);
      const maxLen = Math.min(W, H) * 0.15 * (1 + bass * 0.6) * intensity;
      const rot = t * 0.06 * speed;
      ctx.lineWidth = Math.max(1.1, Math.min(W, H) * 0.0021);
      for (let i = 0; i < nspec; i++) {
        const v = spec[i] || 0;
        const ang = rot + (i / nspec) * Math.PI * 2;
        const r0 = base;
        const r1 = base + Math.max(1.2, v * maxLen);
        const ca = Math.cos(ang), sa = Math.sin(ang);
        ctx.strokeStyle = `hsla(${specHue(i, nspec)}, 95%, ${56 + v * 24}%, ${(0.05 + v * 0.26) * 0.7})`;
        ctx.beginPath();
        ctx.moveTo(cx + ca * r0, cy + sa * r0);
        ctx.lineTo(cx + ca * r1, cy + sa * r1);
        ctx.stroke();
      }
      ctx.strokeStyle = `hsla(${state.hue}, 90%, 72%, ${0.03 + bass * 0.06})`;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.arc(cx, cy, base, t * 0.05, t * 0.05 + Math.PI * 1.85);
      ctx.stroke();
    }

    // ---- bottom spectrum bars (ultra thin, semi transparent) ----
    if (state.settings.bottom_spectrum !== false && nspec > 1) {
      const maxH = H * 0.085 * (0.5 + intensity * 0.7);
      const bw = W / nspec;
      for (let i = 0; i < nspec; i++) {
        const v = spec[i] || 0;
        const h = Math.max(1, v * maxH);
        const x = i * bw;
        const hue = specHue(i, nspec);
        ctx.fillStyle = `hsla(${hue}, 92%, ${54 + v * 26}%, ${0.04 + v * 0.20})`;
        ctx.fillRect(x + bw * 0.18, H - h, bw * 0.64, h);
        ctx.fillStyle = `hsla(${hue}, 92%, 62%, ${0.015 + v * 0.06})`;
        ctx.fillRect(x + bw * 0.18, H - h * 1.5, bw * 0.64, h * 0.5);
      }
    }

    ctx.globalCompositeOperation = "source-over";
  }

  /* ---------- post-processing: chromatic aberration + bloom ------------- */
  function activeMediaNode() {
    return decks[state.activeDeck].inner.querySelector("video,img");
  }

  function drawCover(g, node, w, h) {
    const nw = node.videoWidth || node.naturalWidth || 16;
    const nh = node.videoHeight || node.naturalHeight || 9;
    const scale = Math.max(w / nw, h / nh);
    const dw = nw * scale, dh = nh * scale;
    g.drawImage(node, (w - dw) / 2, (h - dh) / 2, dw, dh);
  }

  function drawPostFX() {
    const bloomWanted = state.settings.postfx_bloom !== false && state.postfxOn;
    const chromaWanted = state.settings.postfx_chroma !== false && state.postfxOn;
    if (!bloomWanted && !chromaWanted) {
      if (state._postfxDirty) { pctx.clearRect(0, 0, W, H); state._postfxDirty = false; }
      return;
    }
    const node = activeMediaNode();
    if (!node) {
      if (state._postfxDirty) { pctx.clearRect(0, 0, W, H); state._postfxDirty = false; }
      return;
    }
    try {
      fxc.globalCompositeOperation = "source-over";
      fxc.clearRect(0, 0, fx.width, fx.height);
      drawCover(fxc, node, fx.width, fx.height);
    } catch (e) {
      return;
    }
    const intensity = Number(state.settings.effect_intensity ?? 1);
    const t = state.transient;
    const energy = clamp(state.smooth.rms, 0, 1);

    pctx.clearRect(0, 0, W, H);
    state._postfxDirty = true;
    pctx.globalCompositeOperation = "lighter";

    if (bloomWanted) {
      blc.globalCompositeOperation = "source-over";
      blc.clearRect(0, 0, bl.width, bl.height);
      blc.filter = "blur(10px) brightness(1.4) saturate(1.3)";
      blc.drawImage(fx, 0, 0, bl.width, bl.height);
      blc.filter = "none";
      pctx.globalAlpha = clamp(0.10 + energy * 0.3 + t * 0.5, 0, 0.8) * intensity;
      pctx.drawImage(bl, 0, 0, W, H);
      state.postfxFrames += 1;
    }

    // RGB split only during transients (beats with punch, drops)
    if (chromaWanted && t > 0.02) {
      for (const ch of chan) {
        ch.c.globalCompositeOperation = "source-over";
        ch.c.clearRect(0, 0, ch.cv.width, ch.cv.height);
        ch.c.drawImage(fx, 0, 0, ch.cv.width, ch.cv.height);
        ch.c.globalCompositeOperation = "multiply";
        ch.c.fillStyle = ch.color;
        ch.c.fillRect(0, 0, ch.cv.width, ch.cv.height);
      }
      const d = 1.5 + t * 9;
      pctx.globalAlpha = clamp(t * 0.75, 0, 0.8) * intensity;
      pctx.drawImage(chan[0].cv, -d, 0, W, H);
      pctx.drawImage(chan[2].cv, d, 0, W, H);
      pctx.globalAlpha = clamp(t * 0.30, 0, 0.5);
      pctx.drawImage(chan[1].cv, 0, 0, W, H);
      state.chromaFrames += 1;
    }

    pctx.globalAlpha = 1;
    pctx.globalCompositeOperation = "source-over";
  }

  /* ---------- decks ----------------------------------------------------- */
  const decks = [
    { el: $("#deckA"), inner: $("#deckA .inner"), item: null, ready: false },
    { el: $("#deckB"), inner: $("#deckB .inner"), item: null, ready: false },
  ];

  function mountMedia(deck, item) {
    const inner = deck.inner;
    inner.innerHTML = "";
    if (!item) return null;
    let node;
    if (item.kind === "video") {
      node = document.createElement("video");
      node.muted = true;
      node.defaultMuted = true;
      node.loop = true;
      node.playsInline = true;
      node.autoplay = true;
      node.preload = "auto";
      node.disablePictureInPicture = true;
    } else {
      node = document.createElement("img");
      node.decoding = "async";
    }
    node.src = item.url;
    inner.appendChild(node);
    deck.item = item;
    deck.ready = false;
    deck.gen = (deck.gen || 0) + 1;
    node.addEventListener("loadeddata", () => { deck.ready = true; }, { once: true });
    node.addEventListener("loadedmetadata", () => { deck.ready = true; }, { once: true });
    node.addEventListener("error", () => {
      deck.ready = false;
      send({ cmd: "log", text: "media load failed: " + item.url });
    }, { once: true });
    if (node.play) node.play().catch(() => {});
    return node;
  }

  const inactive = () => 1 - state.activeDeck;

  // A swap-out fade must not wipe a deck that has already been refilled since.
  // Every mount bumps a generation; the delayed clear only fires for its own.
  function clearDeckSoon(deck) {
    const gen = deck.gen;
    setTimeout(() => {
      if (deck.gen === gen && deck !== decks[state.activeDeck]) {
        deck.inner.innerHTML = "";
        deck.item = null;
        deck.ready = false;
      }
    }, 320);
  }

  function hasMedia(deck) {
    return !!deck.inner.querySelector("video,img");
  }

  function preloadNext() {
    const deck = decks[inactive()];
    if (deck.item && deck.ready && hasMedia(deck)) return;
    const item = state.buffer.shift();
    if (!item) return;
    mountMedia(deck, item);
  }

  function performCut(reason, strength) {
    const now = performance.now();
    state.cutAttempts = (state.cutAttempts || 0) + 1;
    state.cutLog = (state.cutLog || []);
    state.cutLog.push(`${reason}:${Math.round(now)}`);
    if (state.cutLog.length > 10) state.cutLog.shift();
    if (now - state.lastCut < 260) return;
    const next = decks[inactive()];
    if (!next.item || !hasMedia(next)) {
      if (!next.item) preloadNext();
      // an item whose element went missing (raced clear) is re-mounted in place
      else if (!hasMedia(next)) mountMedia(next, next.item);
      if (!next.item || !hasMedia(next)) { state.pulse = Math.max(state.pulse, 0.5); return; }
    }
    const prev = decks[state.activeDeck];
    const node = next.inner.querySelector("video,img");
    if (node && node.tagName === "VIDEO") {
      try { node.currentTime = 0; } catch (e) { /* not seekable yet */ }
      node.play().catch(() => {});
    }
    next.el.classList.add("active");
    prev.el.classList.remove("active");
    if (reason === "drop" || (strength || 0) > 0.85) {
      next.el.classList.add("glitch");
      setTimeout(() => next.el.classList.remove("glitch"), 460);
    }
    if (prev.item) state.history.push(prev.item);
    if (state.history.length > 12) state.history.shift();
    prev.item = null;
    prev.ready = false;
    clearDeckSoon(prev);
    state.activeDeck = inactive();
    state.lastCut = now;
    state.lastCutBeat = state.beats;
    state.cuts += 1;
    updateClipsLine();
    requestClips();
  }

  /* ---------- engine link ---------------------------------------------- */
  function send(obj) {
    if (state.connected && window.vis && window.vis.command) {
      window.vis.command(JSON.stringify(obj));
    }
  }

  function requestClips(force) {
    const now = performance.now();
    if (!force && now - state.lastRequest < 1500) return;
    state.lastRequest = now;
    const want = clamp(4 - state.buffer.length - (decks[inactive()].item ? 1 : 0), 0, 4);
    if (want > 0) send({ cmd: "need_clips", count: want });
  }

  function onMessage(raw) {
    let msg;
    try { msg = JSON.parse(raw); } catch (e) { return; }

    switch (msg.t) {
      case "frame": {
        Object.assign(state.target, msg.bands || {});
        state.target.rms = msg.rms ?? 0;
        if (msg.spec && msg.spec.length) {
          if (state.specTarget.length !== msg.spec.length) {
            state.specTarget = new Float32Array(msg.spec.length);
            state.spec = new Float32Array(msg.spec.length);
            assignParticleBands();
          }
          for (let i = 0; i < msg.spec.length; i++) state.specTarget[i] = msg.spec[i];
        }
        state.bpm = msg.bpm || 0;
        state.locked = !!msg.locked;
        state.playing = !!msg.playing;
        state.pos = msg.pos || 0;
        state.duration = msg.duration || 0;
        state.phase = msg.phase || 0;
        if (msg.mood && msg.mood !== state.mood) {
          state.mood = msg.mood;
          if (!(state.palette.length && state.settings.palette_sync !== false)) {
            state.hueTarget = MOOD_HUE[msg.mood] ?? 200;
          }
        }
        if (msg.art) applyArt(msg.art, false);
        if (msg.beat) onBeat(msg.beat_strength || 0.5);
        if (msg.drop) onDrop();
        if (state._seenFrame === undefined) {
          state._seenFrame = true;
          setPlaying(!!msg.playing, true);
        }
        updateLyric(msg.lyric || {});
        updateHudMeta();
        break;
      }
      case "clips": {
        for (const item of msg.items || []) {
          if (!state.buffer.some((b) => b.url === item.url)) state.buffer.push(item);
        }
        if (!decks[state.activeDeck].item && !decks[inactive()].item) performCut("initial", 0.5);
        else preloadNext();
        updateClipsLine();
        break;
      }
      case "track": {
        const key = (msg.track && msg.track.key) || "";
        const sameTrack = key && key === state.track.key;
        state.track = msg.track || {};
        if (!sameTrack) {
          // a real track change: drop the old queue, keep whatever is on screen
          state.buffer = [];
          state.history = [];
          state.lastCutBeat = state.beats;
        }
        $("#track-title").textContent = state.track.title || "Unknown track";
        $("#track-artist").textContent = state.track.artist || "";
        const badge = $("#badge");
        badge.classList.toggle("hidden", !msg.timeline);
        if (msg.palette) applyArt(msg.palette, true);
        updateClipsLine();
        requestClips(true);
        break;
      }
      case "settings": {
        state.settings = msg.settings || {};
        applySettingsToPanel();
        checkSourceHint();
        requestAnimationFrame(() => { state.hueTarget = resolveHue(); });
        break;
      }
      case "status_full": {
        state.lastStatus = msg;
        const sel = msg.selector || {};
        if (typeof sel.local_files === "number") state.localFiles = sel.local_files;
        checkSourceHint();
        renderStatusBox();
        break;
      }
      case "transport": {
        setPlaying(!!msg.playing);
        break;
      }
      case "seek": {
        if (msg.ok) {
          state.pos = msg.pos || state.pos;
          state.scrub = null;
        } else toast("this player will not seek", "warn");
        break;
      }
      case "notice": toast(msg.text, msg.level); break;
      case "status": {
        state.lastStatus.selector = { queries_used: msg.queries_used };
        if (typeof msg.local_files === "number") {
          state.localFiles = msg.local_files;
          checkSourceHint();
        }
        break;
      }
      case "diag": {
        const node = activeMediaNode();
        send({
          cmd: "diag",
          data: {
            active_deck_loaded: !!decks[state.activeDeck].item,
            media_ready: !!(node && (node.tagName === "IMG" ? node.complete : node.readyState >= 2)),
            staged: !!decks[inactive()].item,
            buffer: state.buffer.length,
            fps: Math.round(state.fps),
            spec_bands: state.spec.length,
            bass: Number(state.smooth.bass.toFixed(2)),
            rms: Number(state.smooth.rms.toFixed(2)),
            bpm: state.bpm,
            locked: state.locked,
            beats: state.beats,
            cuts: state.cuts,
            mood: state.mood,
            lyric: state.lyric,
            hue: Math.round(state.hue),
            art: state.artUrl ? true : false,
            palette: state.palette.length,
            postfx: state.postfxOn,
            postfx_frames: state.postfxFrames,
            chroma_frames: state.chromaFrames,
            hud_idle: document.body.classList.contains("hud-idle"),
            pose: { x: Math.round(state.pos), dur: Math.round(state.duration) },
            providers_ready: state.providersReady,
            local_files: state.localFiles,
            demo: state.demo,
          },
        });
        break;
      }
    }
  }

  function connect() {
    if (typeof qt === "undefined" || !qt.webChannelTransport) {
      startDemoMode();
      return;
    }
    new QWebChannel(qt.webChannelTransport, (channel) => {
      window.vis = channel.objects.vis;
      state.connected = true;
      vis.published.connect(onMessage);
      send({ cmd: "hello" });
      setInterval(() => send({ cmd: "status" }), 15000);
      setInterval(() => requestClips(false), 4000);
    });
  }

  /* ---------- demo mode (plain browser, no engine) ---------------------- */
  function startDemoMode() {
    state.demo = true;
    state.connected = false;
    state.settings = {
      effect_intensity: 1, min_beats_between_cuts: 4, show_overlay: true,
      show_lyrics: true, radial_spectrum: true, bottom_spectrum: true,
      particle_trails: true, postfx_bloom: true, postfx_chroma: true,
      show_progress: true, show_album_art: true, show_bpm_ring: true,
      hud_autohide_seconds: 0, seek_step_seconds: 15,
    };
    $("#track-title").textContent = "Demo mode (no engine attached)";
    $("#track-artist").textContent = "open through the app for real audio";
    $("#progress-wrap").classList.remove("hidden");
    $("#transport").classList.remove("hidden");
    state.duration = 214;
    let beat = 0;
    setInterval(() => {
      const t = performance.now() / 1000;
      const bass = Math.pow(Math.max(0, Math.sin(t * Math.PI * 2)), 3);
      Object.assign(state.target, {
        sub: bass * 0.9, bass: bass, lowmid: bass * 0.6,
        mid: 0.3 + 0.3 * Math.random(), high: Math.random() * 0.4, air: Math.random() * 0.25,
        rms: 0.35 + bass * 0.4,
      });
      const n = state.specTarget.length || 64;
      for (let i = 0; i < n; i++) {
        const tilt = 1 - i / n;
        state.specTarget[i] = clamp(
          (0.35 + 0.5 * Math.sin(t * (2 + i * 0.15) + i * 0.7)) * tilt + bass * 0.35, 0, 1);
      }
      state.bpm = 128; state.locked = true; state.playing = true;
      state.pos = t % 214; state.duration = 214; state.phase = (t * 128 / 60) % 1;
      if (++beat % 2 === 0) onBeat(0.8);
      if (beat % 64 === 0) onDrop();
      state.mood = "euphoric";
      state.hueTarget = MOOD_HUE.euphoric;
    }, 60);
  }

  /* ---------- events ---------------------------------------------------- */
  function onBeat(strength) {
    state.beats += 1;
    const s = Number(state.settings.effect_intensity ?? 1);
    state.flash = Math.max(state.flash, clamp(0.1 + strength * 0.28 * s, 0, 0.55));
    state.pulse = Math.max(state.pulse, strength);
    state.transient = Math.max(state.transient, clamp(0.25 + strength * 0.75, 0, 1));
    pulseRings.push({
      r: 0.06, age: 0, life: 1.15, speed: 0.5 + strength,
      strength, hue: rng(-30, 30),
    });
    if (pulseRings.length > 14) pulseRings.shift();
    const need = Number(state.settings.min_beats_between_cuts ?? 2);
    if (state.beats - state.lastCutBeat >= Math.max(1, need)) performCut("beat", strength);
  }

  function onDrop() {
    state.pulse = 1.5;
    state.flash = Math.max(state.flash, 0.8);
    state.transient = 1;
    state.hueTarget = (resolveHue() + rng(80, 200)) % 360;
    performCut("drop", 1);
  }

  function setPlaying(playing, first) {
    state.playing = playing;
    document.body.classList.toggle("paused-visuals", !playing);
    $("#vinyl").classList.toggle("show", playing && !!state.artUrl);
    const btn = $('#transport button[data-act="playpause"]');
    if (btn) btn.innerHTML = playing ? "&#10074;&#10074;" : "&#9654;";
    if (playing && !first) {
      // resuming: gentle punch + a fresh clip so the transition reads as intentional
      state.flash = Math.max(state.flash, 0.35);
      state.transient = Math.max(state.transient, 0.6);
      preloadNext();
      performCut("resume", 0.6);
    }
  }

  function updateLyric(lyric) {
    const el = $("#lyric");
    const text = (lyric.cur || "").trim();
    if (state.settings.show_lyrics === false) { el.classList.remove("show"); return; }
    if (text && text !== state.lyric) {
      state.lyric = text;
      $("#lyric-text").textContent = text;
    }
    el.classList.toggle("show", !!text);
  }

  const fmt = (s) => {
    s = Math.max(0, Math.floor(s || 0));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  };

  function updateHudMeta() {
    $("#mood-line").textContent = state.mood;
    const bpm = state.bpm > 0
      ? `BPM ${state.bpm.toFixed(0)}${state.locked ? "" : "?"}` : "BPM –";
    $("#tempo-line").textContent = bpm;
    updateProgressUI(state.scrub != null ? state.scrub : state.pos);
  }

  function updateProgressUI(pos) {
    const dur = state.duration || 0;
    if (dur > 0) pos = clamp(pos, 0, dur);
    const frac = dur > 0 ? clamp(pos / dur, 0, 1) : 0;
    $("#progress-fill").style.width = (frac * 100).toFixed(2) + "%";
    $("#progress-knob").style.left = (frac * 100).toFixed(2) + "%";
    $("#time-elapsed").textContent = fmt(pos);
    $("#time-remaining").textContent = dur > 0 ? "-" + fmt(Math.max(0, dur - pos)) : "-:--";
  }

  function updateClipsLine() {
    $("#clips-line").textContent =
      `${state.buffer.length + (decks[inactive()].item ? 1 : 0)} clips · ${state.cuts} cuts`;
  }

  function applyArt(art, force) {
    if (!art) return;
    const url = art.url || "";
    if (!force && url === state.artUrl) return;
    state.art = art;
    state.artUrl = url;
    state.palette = art.palette || [];
    state.accent = art.accent || null;
    const showArt = state.settings.show_album_art !== false;
    const backdrop = $("#backdrop");
    const vinyl = $("#vinyl");
    if (url && showArt) {
      $("#vinyl-img").src = url;
      vinyl.classList.remove("hidden");
      vinyl.classList.toggle("show", state.playing);
      if (state.settings.art_backdrop !== false) {
        backdrop.style.backgroundImage = `url("${url}")`;
        backdrop.classList.add("show");
      } else backdrop.classList.remove("show");
    } else {
      vinyl.classList.add("hidden");
      backdrop.classList.remove("show");
    }
    state.hueTarget = resolveHue();
  }

  function resolveHue() {
    if (state.palette.length && state.settings.palette_sync !== false) {
      if (state.art && state.art.hue != null) return state.art.hue;
      return rgbHue(state.palette[0]);
    }
    return MOOD_HUE[state.mood] ?? 200;
  }

  function toast(text, level) {
    if (!text) return;
    const el = $("#toast");
    el.textContent = text;
    el.classList.remove("hidden");
    el.classList.toggle("warn", level === "warn" || level === "error");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => el.classList.add("hidden"), 4200);
  }

  function checkSourceHint() {
    const hasKey = !!(state.settings.klipy_key || state.settings.giphy_key);
    const folder = (state.settings.local_library || "").trim();
    // null = the engine has not reported the folder contents yet: do not cry wolf
    const files = state.localFiles;
    const usableLocal = !!folder && (files === null || files === undefined || files > 0);
    state.providersReady = hasKey || usableLocal;
    if (state.providersReady || state.settings.fetch_enabled === false) return;
    if (folder) {
      toast("Local folder has no clips - drop gifs in, or add a KLIPY key", "warn");
    } else {
      toast("No media source yet - press S and paste your KLIPY key", "warn");
    }
  }

  /* ---------- HUD controls ---------------------------------------------- */
  function noteActivity() {
    state.lastActivity = performance.now();
    if (document.body.classList.contains("hud-idle")) {
      document.body.classList.remove("hud-idle");
    }
  }

  function toggleHud() {
    document.body.classList.toggle("hud-off");
    state.lastActivity = performance.now();
  }

  function wireTransport() {
    $("#transport").addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      const act = btn.dataset.act;
      if (act === "playpause") send({ cmd: "playpause" });
      else if (act === "next") { send({ cmd: "next" }); preloadNext(); performCut("manual", 1); }
      else if (act === "prev") { send({ cmd: "prev" }); preloadNext(); performCut("manual", 1); }
    });

    const bar = $("#progress");
    const fracFrom = (e) => {
      const r = bar.getBoundingClientRect();
      return clamp((e.clientX - r.left) / Math.max(1, r.width), 0, 1);
    };
    bar.addEventListener("pointerdown", (e) => {
      if (!state.duration) return;
      state.dragging = true;
      bar.classList.add("dragging");
      try { bar.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
      state.scrub = fracFrom(e) * state.duration;
      updateProgressUI(state.scrub);
    });
    bar.addEventListener("pointermove", (e) => {
      const f = fracFrom(e);
      const hover = $("#progress-hover");
      hover.style.left = (f * 100).toFixed(2) + "%";
      hover.textContent = fmt(f * (state.duration || 0));
      if (state.dragging) {
        state.scrub = f * state.duration;
        updateProgressUI(state.scrub);
      }
    });
    const finish = (e) => {
      if (!state.dragging) return;
      state.dragging = false;
      bar.classList.remove("dragging");
      send({ cmd: "seek", to: state.scrub || 0 });
    };
    bar.addEventListener("pointerup", finish);
    bar.addEventListener("pointercancel", finish);

    $("#hud").addEventListener("pointerenter", () => { state.hudHover = true; noteActivity(); });
    $("#hud").addEventListener("pointerleave", () => { state.hudHover = false; });
    window.addEventListener("mousemove", noteActivity);
    window.addEventListener("mousedown", noteActivity);
    window.addEventListener("wheel", noteActivity, { passive: true });
    window.addEventListener("keydown", noteActivity);
  }

  /* ---------- settings panel -------------------------------------------- */
  function togglePanel(force) {
    state.panelOpen = force !== undefined ? force : !state.panelOpen;
    $("#panel").classList.toggle("hidden", !state.panelOpen);
    document.body.classList.toggle("panel-open", state.panelOpen);
    noteActivity();
    if (state.panelOpen) send({ cmd: "status" });
  }

  function toggleOverlay() {
    const on = state.settings.show_overlay === false;
    state.settings.show_overlay = on;
    document.body.classList.toggle("no-overlay", !on);
    send({ cmd: "set", key: "show_overlay", value: on });
    const box = document.querySelector('[data-key="show_overlay"]');
    if (box) box.checked = on;
    noteActivity();
  }

  function applySettingsToPanel() {
    const s = state.settings;
    document.querySelectorAll("#panel [data-key]").forEach((el) => {
      const key = el.dataset.key;
      if (document.activeElement === el) return;
      const value = s[key];
      if (el.type === "checkbox") el.checked = !!value;
      else if (value !== undefined && value !== null) el.value = value;
      if (el.tagName === "INPUT" && el.type === "range") {
        const out = el.parentElement.querySelector("output");
        if (out) out.textContent = el.value;
      }
    });
    document.body.classList.toggle("no-overlay", s.show_overlay === false);
    document.body.classList.toggle("hud-off", false);
    const showArt = s.show_album_art !== false;
    if (!showArt) {
      $("#vinyl").classList.add("hidden");
      $("#backdrop").classList.remove("show");
    } else if (state.artUrl) {
      $("#vinyl").classList.remove("hidden");
      $("#backdrop").classList.toggle("show", s.art_backdrop !== false);
    }
    $("#progress-wrap").classList.toggle("hidden", s.show_progress === false);
    $("#transport").classList.toggle("hidden", s.show_progress === false);
    $("#bpm-ring").style.display = s.show_bpm_ring === false ? "none" : "";
  }

  function wirePanel() {
    document.querySelectorAll("#panel [data-key]").forEach((el) => {
      const handler = () => {
        let value;
        if (el.type === "checkbox") value = el.checked;
        else if (el.type === "range") value = Number(el.value);
        else value = el.value;
        state.settings[el.dataset.key] = value;
        if (el.type === "range") {
          const out = el.parentElement.querySelector("output");
          if (out) out.textContent = el.value;
        }
        send({ cmd: "set", key: el.dataset.key, value });
        if (el.dataset.key === "local_library" || el.dataset.key === "klipy_key") {
          setTimeout(() => requestClips(true), 400);
        }
        if (el.dataset.key === "particle_count") buildParticles(Number(el.value) || 0);
        if (el.dataset.key === "show_progress" || el.dataset.key === "show_album_art"
            || el.dataset.key === "art_backdrop" || el.dataset.key === "show_bpm_ring") {
          applySettingsToPanel();
        }
        noteActivity();
      };
      el.addEventListener("change", handler);
      if (el.type === "range") el.addEventListener("input", handler);
    });
    $("#gear").addEventListener("click", () => togglePanel());

    document.addEventListener("keydown", (e) => {
      const typing = /^(INPUT|SELECT|TEXTAREA)$/.test((e.target && e.target.tagName) || "");
      if (typing) return;
      switch (e.key) {
        case "s": case "S": togglePanel(); break;
        case "h": case "H": toggleHud(); break;
        case "o": case "O": toggleOverlay(); break;
        case "n": case "N": preloadNext(); performCut("manual", 1); break;
        case "Escape": togglePanel(false); break;
        case "ArrowLeft": send({ cmd: "seek", delta: -(Number(state.settings.seek_step_seconds) || 15) }); break;
        case "ArrowRight": send({ cmd: "seek", delta: Number(state.settings.seek_step_seconds) || 15 }); break;
      }
    });
  }

  function renderStatusBox() {
    const s = state.lastStatus || {};
    const lines = [];
    const mp = s.mpris || {};
    lines.push(`player  : ${mp.player || "-"} (${mp.status || "unknown"})`);
    lines.push(`track   : ${(mp.track && mp.track.title) || "-"} - ${(mp.track && mp.track.artist) || "-"}`);
    const au = s.audio || {};
    lines.push(`audio   : ${au.streaming ? "live" : "down"} via ${au.source || "-"}`);
    if (au.error) lines.push(`audio err: ${au.error}`);
    const b = s.beats || {};
    lines.push(`beats   : ${b.bpm || 0} bpm ${b.timeline ? "(cached timeline)" : "(live)"} locked=${!!b.locked}`);
    const ly = s.lyrics || {};
    lines.push(`lyrics  : ${ly.source || "-"} (${ly.lines || 0} lines)`);
    const sel = s.selector || {};
    lines.push(`clips   : queued=${sel.queued ?? 0} sent=${sel.sent ?? 0} queries=${sel.queries_used ?? 0}`);
    const prov = (s.providers && s.providers.providers) || [];
    for (const p of prov) {
      lines.push(`provider: ${p.name} ${p.configured ? "keyed" : "no key"}${p.error ? " - " + p.error : ""}`);
    }
    const rate = (s.providers && s.providers.rate) || {};
    if (rate.limit) lines.push(`rate    : ${rate.used}/${rate.limit} per hour`);
    const art = state.art || {};
    lines.push(`artwork : ${state.artUrl || "-"}${art.accent ? " accent rgb(" + art.accent.join(",") + ")" : ""}`);
    lines.push(`spectrum: ${state.spec.length} bands · postfx ${state.postfxOn ? "on" : "off"}`);
    if (s.cache_bytes) lines.push(`cache   : ${(s.cache_bytes / 1024 ** 3).toFixed(2)} GB`);
    lines.push(`render  : ${Math.round(state.fps)} fps · cuts ${state.cuts} · frames ${s.frames_sent ?? 0}`);
    $("#status-box").textContent = lines.join("\n");
  }

  /* ---------- main loop ------------------------------------------------- */
  let last = performance.now();
  function frame(now) {
    window.__rafN = (window.__rafN || 0) + 1;
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    const t = now / 1000;

    state.frames += 1;
    if (!frame._t0) { frame._t0 = now; frame._n = 0; }
    frame._n += 1;
    if (now - frame._t0 > 1000) {
      state.fps = (frame._n * 1000) / (now - frame._t0);
      frame._t0 = now;
      frame._n = 0;
      // auto-fallback if the post pass costs too much on this GPU
      if (state.postfxOn && state.fps < 42 && state.settings.postfx_bloom !== false) {
        if (!state.lowFpsSince) state.lowFpsSince = now;
        else if (now - state.lowFpsSince > 4000) {
          state.postfxOn = false;
          toast("post-processing disabled (low fps underneath)", "warn");
        }
      } else state.lowFpsSince = 0;
    }

    const intensity = Number(state.settings.effect_intensity ?? 1);
    const pausedTarget = state.playing ? 0 : 1;
    state.paused = lerp(state.paused, pausedTarget, clamp(dt * 1.6, 0, 1));
    state.speed = 1 - state.paused * 0.75;

    for (const k of Object.keys(state.smooth)) {
      const target = k === "rms" ? (state.target.rms || 0) : (state.target[k] || 0);
      state.smooth[k] = lerp(state.smooth[k], target, clamp(dt * 14, 0, 1));
    }
    for (let i = 0; i < state.spec.length; i++) {
      state.spec[i] = lerp(state.spec[i], state.specTarget[i], clamp(dt * 16, 0, 1));
    }

    state.hue = lerp(state.hue, state.hueTarget, dt * 0.6);
    state.flash = Math.max(0, state.flash - dt * 2.4);
    state.pulse = Math.max(0, state.pulse - dt * 1.8);
    state.transient = Math.max(0, state.transient - dt * 1.5);

    const s = state.smooth;
    const root = document.documentElement.style;
    root.setProperty("--accent", state.hue.toFixed(0));
    root.setProperty("--flash-alpha", (state.flash * intensity).toFixed(3));
    root.setProperty("--paused", state.paused.toFixed(3));

    // metronome ring: 1 right on the beat, decaying across the beat
    let beatPulse = 0;
    if (state.locked && state.bpm > 0) {
      beatPulse = Math.pow(1 - clamp(state.phase, 0, 1), 2.4);
    } else {
      beatPulse = state.pulse * 0.6;
    }
    const beatPulse2 = clamp(beatPulse, 0, 1).toFixed(3);
    if (beatPulse2 !== state._beatVar) {
      root.setProperty("--beat", beatPulse2);
      state._beatVar = beatPulse2;
    }

    // bass-driven camera shake + radial pulse, applied to the world layer
    const bass = clamp(s.sub * 0.7 + s.bass * 0.3, 0, 1);
    if (state.settings.bass_shake !== false && state.paused < 0.9) {
      const amt = Math.pow(bass, 1.7) * 11 * intensity;
      state.shakeX = lerp(state.shakeX, rng(-amt, amt), 0.35);
      state.shakeY = lerp(state.shakeY, rng(-amt, amt), 0.35);
      state.shakeScale = lerp(state.shakeScale, 1 + Math.pow(bass, 2) * 0.035 * intensity, 0.25);
    } else {
      state.shakeX = lerp(state.shakeX, 0, 0.2);
      state.shakeY = lerp(state.shakeY, 0, 0.2);
      state.shakeScale = lerp(state.shakeScale, 1, 0.15);
    }
    root.setProperty("--shake-x", state.shakeX.toFixed(2) + "px");
    root.setProperty("--shake-y", state.shakeY.toFixed(2) + "px");
    root.setProperty("--shake-scale", state.shakeScale.toFixed(4));

    // deck motion
    const active = decks[state.activeDeck];
    if (active && active.inner) {
      const stillness = 1 - state.paused * 0.8;
      const scale = (1 + s.bass * 0.06 * intensity + s.sub * 0.03 * intensity)
        * (1 - state.paused * 0.08);
      const rot = ((s.high - 0.5) * 1.6 * intensity + Math.sin(t * 0.07 * state.speed) * 0.35)
        * stillness;
      const dx = (s.mid - 0.5) * 1.2 * intensity * stillness;
      const dy = (s.lowmid - 0.5) * 1.2 * intensity * stillness;
      active.inner.style.transform =
        `translate3d(${dx.toFixed(2)}%, ${dy.toFixed(2)}%, 0) rotate(${rot.toFixed(2)}deg) scale(${scale.toFixed(3)})`;
      const sat = (1 + s.mid * 0.9 * intensity) * (1 - state.paused * 0.35);
      const bright = (0.82 + s.rms * 0.5) * (1 - state.paused * 0.28);
      const contrast = 1 + s.air * 0.6 * intensity;
      const hueShift = (s.high - 0.5) * 30 * intensity;
      const blur = s.air > 0.75 ? (s.air - 0.75) * 6 * intensity : 0;
      active.inner.style.filter =
        `saturate(${sat.toFixed(2)}) brightness(${bright.toFixed(2)}) ` +
        `contrast(${contrast.toFixed(2)}) hue-rotate(${hueShift.toFixed(1)}deg)` +
        (blur > 0.05 ? ` blur(${blur.toFixed(2)}px)` : "");
    }

    drawProcedural(t);
    drawPostFX();

    // watchdog: never sit on a blank stage while we hold media to show
    if (!frame._watch || now - frame._watch > 1200) {
      frame._watch = now;
      if (!document.querySelector(".deck.active")) {
        const cur = decks[state.activeDeck];
        if (cur.item && hasMedia(cur)) cur.el.classList.add("active");
        else performCut("heal", 0.6);
      }
    }

    // HUD auto-hide after inactivity (mouse hovering the HUD keeps it up)
    const hide = Number(state.settings.hud_autohide_seconds ?? 0);
    const idle = hide > 0 && !state.panelOpen && !state.hudHover && !state.dragging
      && (now - state.lastActivity) > hide * 1000;
    document.body.classList.toggle("hud-idle", idle);
    document.body.classList.toggle("controls-open", state.hudHover || state.dragging);

    requestAnimationFrame(frame);
  }

  /* ---------- boot ------------------------------------------------------ */
  window.VIS = state;   // handy for debugging from the devtools console
  window.VIS.decks = decks;
  window.addEventListener("error", (e) => send({
    cmd: "log",
    text: `js error: ${e.message} @${((e.filename || "").split("/").pop())}:${e.lineno}`,
  }));
  window.addEventListener("unhandledrejection", (e) => send({
    cmd: "log",
    text: "js rejection: " + ((e.reason && e.reason.message) || e.reason || "unknown"),
  }));
  resize();
  buildParticles(260);
  wirePanel();
  wireTransport();
  applySettingsToPanel();
  requestAnimationFrame(frame);
  connect();
  setTimeout(() => requestClips(true), 800);

  setTimeout(() => {
    if (state.connected && !decks[state.activeDeck].item && !state.buffer.length) {
      toast("No clips yet - add a KLIPY key (press S) or set a local clip folder", "warn");
    }
  }, 6000);
})();
