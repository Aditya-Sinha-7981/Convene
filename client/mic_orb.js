// The live microphone orb on the phone: a circle in the participant's colour that swells and ripples with the level
// of the microphone stream the page is already sending. It only reads the level (an AnalyserNode that is never
// connected to the speakers), so it cannot change what is transmitted.
(function () {
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  let audio = null;

  // Mobile browsers only start audio from a tap, so the Join button's tap unlocks it before the stream exists.
  function unlock() {
    const Context = window.AudioContext || window.webkitAudioContext;
    if (!Context) return;
    try { audio = audio || new Context(); if (audio.state === "suspended") audio.resume(); } catch { audio = null; }
  }

  function start(canvas, stream, color) {
    unlock();
    if (!audio || !stream?.getAudioTracks?.().length) return null;
    const source = audio.createMediaStreamSource(stream);
    const analyser = audio.createAnalyser();
    analyser.fftSize = 1024;
    source.connect(analyser);
    const samples = new Float32Array(analyser.fftSize);
    const ctx = canvas.getContext("2d");
    let level = 0, active = true, frame = 0, t = 0;

    function measure() {
      analyser.getFloatTimeDomainData(samples);
      let sum = 0;
      for (const s of samples) sum += s * s;
      const db = 20 * Math.log10(Math.sqrt(sum / samples.length) + 1e-9);
      const target = active ? Math.min(1, Math.max(0, (db + 58) / 40)) : 0;  // about -58 dBFS (quiet) to -18 (loud)
      level = target > level ? level + (target - level) * .5 : level * .92;   // quick to rise, slow to settle
    }

    function draw() {
      const size = canvas.clientWidth, dpr = window.devicePixelRatio || 1;
      if (canvas.width !== Math.round(size * dpr)) { canvas.width = canvas.height = Math.round(size * dpr); }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, size, size);
      const c = size / 2, base = size * .3;
      ctx.fillStyle = color;
      // Three soft layers; each ripples at its own speed, more strongly the louder the room.
      for (let layer = 2; layer >= 0; layer--) {
        const grow = base * (1 + .1 * (layer + 1) + .3 * level * (layer + 1) / 3);
        const wobble = reduce ? 0 : size * (.004 + .045 * level) * (1 + layer * .4);
        ctx.globalAlpha = (active ? .1 : .05) + (2 - layer) * .05;
        ctx.beginPath();
        for (let i = 0; i <= 96; i++) {
          const a = (i / 96) * Math.PI * 2;
          const r = grow + wobble * (Math.sin(a * 3 + t * (1.1 + layer * .5) + layer) + .6 * Math.sin(a * 5 - t * (.8 + layer * .3)));
          const x = c + r * Math.cos(a), y = c + r * Math.sin(a);
          if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y);
        }
        ctx.fill();
      }
      ctx.globalAlpha = 1;
      ctx.fillStyle = "#fff";
      ctx.beginPath(); ctx.arc(c, c, base, 0, Math.PI * 2); ctx.fill();
    }

    function tick() {
      measure();
      t += reduce ? 0 : .045 + level * .06;
      draw();
      frame = requestAnimationFrame(tick);
    }
    tick();

    return {
      get level() { return level; },
      setActive(value) { active = value; },
      stop() { cancelAnimationFrame(frame); try { source.disconnect(); } catch { /* already gone */ } },
    };
  }

  window.ConveneOrb = { unlock, start };
})();
