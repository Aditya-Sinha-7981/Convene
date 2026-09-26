// A small microphone character for waiting and empty states. Purely presentational: it never reads meeting data.
// Markup: <figure class="mascot" data-mascot data-mood="waiting"><div class="mascot-art"></div>
//         <figcaption class="mascot-line">…</figcaption></figure>
(function () {
  const SVG = `<svg viewBox="0 0 64 64" aria-hidden="true" focusable="false">
  <path class="m-waves" d="M7 24q-3.5 7 0 14M1.5 20q-5 11 0 22M57 24q3.5 7 0 14M62.5 20q5 11 0 22"/>
  <path class="m-stand" d="M13 30a19 19 0 0 0 38 0M32 49v7M23 58h18"/>
  <rect class="m-body" x="18" y="5" width="28" height="39" rx="14"/>
  <g class="m-eyes-open"><ellipse class="m-eye" cx="26.5" cy="21" rx="3.6" ry="4.3"/><ellipse class="m-eye" cx="37.5" cy="21" rx="3.6" ry="4.3"/>
    <circle class="m-pupil" cx="27" cy="21.8" r="1.9"/><circle class="m-pupil" cx="38" cy="21.8" r="1.9"/></g>
  <path class="m-face m-eyes-happy" d="M23.5 22.5q3-3.6 6 0M34.5 22.5q3-3.6 6 0"/>
  <path class="m-face m-eyes-closed" d="M23.5 22h6M34.5 22h6"/>
  <path class="m-face m-brows" d="M23 14.5l5.5 1.8M41 14.5l-5.5 1.8"/>
  <path class="m-face m-mouth-smile" d="M28 31q4 3.6 8 0"/>
  <path class="m-face m-mouth-flat" d="M29 32h6"/>
  <ellipse class="m-eye m-mouth-o" cx="32" cy="32.5" rx="2.2" ry="2.6"/>
</svg>`;

  function art(figure) { return figure.querySelector(".mascot-art"); }

  function set(figure, mood, line) {
    if (!figure) return;
    const changed = figure.dataset.mood !== mood;
    figure.dataset.mood = mood;
    const caption = figure.querySelector(".mascot-line");
    if (caption && line != null && caption.textContent !== line) caption.textContent = line;
    if (changed) {
      const el = art(figure);
      el.classList.remove("m-bump"); void el.offsetWidth; el.classList.add("m-bump");
    }
  }

  function mountAll() {
    for (const figure of document.querySelectorAll("[data-mascot]")) {
      const el = art(figure);
      if (el && !el.firstChild) el.innerHTML = SVG;
    }
  }

  window.ConveneMascot = { set, mountAll };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mountAll); else mountAll();
})();
