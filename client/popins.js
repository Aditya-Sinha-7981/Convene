// The mascot now and then pops in at the corner with one light line, then leaves. Decorative only: hidden from
// assistive technology, never over the transcript, and it waits while the page says it is busy (canShow).
(function () {
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const between = ([low, high]) => low + Math.random() * (high - low);

  function start({ lines, first = [45000, 75000], every = [240000, 420000], max = Infinity, canShow = () => true }) {
    const el = document.createElement("div");
    el.className = "popin";
    el.setAttribute("aria-hidden", "true");
    el.innerHTML = '<p class="popin-line"></p><img class="popin-mascot" src="/static/brand/mascot.svg" alt="">';
    document.body.append(el);
    const line = el.querySelector(".popin-line");
    let shown = 0, next = Math.floor(Math.random() * lines.length), hideTimer = null;

    function hide() { clearTimeout(hideTimer); el.classList.remove("is-in"); }
    el.addEventListener("click", hide);

    function show() {
      if (document.hidden || !canShow()) { setTimeout(show, 20000); return; }  // busy: try again shortly
      line.textContent = lines[next++ % lines.length];
      el.classList.toggle("is-still", reduce);
      el.classList.add("is-in");
      hideTimer = setTimeout(hide, 6500);
      if (++shown < max) setTimeout(show, between(every));
    }
    setTimeout(show, between(first));
  }

  window.ConvenePopins = { start };
})();
