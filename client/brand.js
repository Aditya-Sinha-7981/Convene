// Convene brand pieces drawn by script: the recolourable participant avatar and the walking mascot.
// Colours are palette keys (ADR-25) resolved to hex in theme.css through [data-color].
(function () {
  const PALETTE = ["lime", "green", "teal", "cyan", "sky", "azure", "violet", "plum", "magenta", "pink", "slate", "charcoal"];
  const HEAD = '<svg viewBox="0 0 120 120" aria-hidden="true" focusable="false">'
    + '<g fill="none" stroke="currentColor" stroke-width="10" stroke-linecap="round" stroke-linejoin="round">'
    + '<path d="M78 20H48C28 20 16 34 16 60s12 40 32 40h30"/><path d="M86 31H51C36 31 28 41 28 60s8 29 23 29h35"/>'
    + '<path d="M91 44H56C47 44 42 50 42 60s5 16 14 16h35"/></g>'
    + '<rect x="38" y="48" width="57" height="31" rx="15.5" fill="#fff"/>'
    + '<g fill="currentColor"><circle cx="57" cy="63" r="3.8"/><circle cx="76" cy="63" r="3.8"/></g></svg>';

  // Rows from before participant colours existed have none: derive a stable one from the id.
  function colorFor(key, id) {
    if (key) return key;
    if (!id) return "unknown";
    let hash = 0;
    for (const ch of id) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
    return PALETTE[hash % PALETTE.length];
  }

  function avatar(key, id, label) {
    const el = document.createElement("span");
    el.className = "avatar";
    el.dataset.color = colorFor(key, id);
    el.innerHTML = HEAD;
    if (label) { el.setAttribute("role", "img"); el.setAttribute("aria-label", label); } else el.setAttribute("aria-hidden", "true");
    return el;
  }

  function walker() {
    const track = document.createElement("div");
    track.className = "walk-track";
    track.setAttribute("aria-hidden", "true");
    track.innerHTML = '<div class="walker"><div class="walker-frames"><img src="/static/brand/walk.svg" alt=""></div></div>';
    return track;
  }

  window.ConveneBrand = { PALETTE, HEAD, colorFor, avatar, walker };
})();
