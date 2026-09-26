// Theme preference is local to this browser. "auto" follows the operating system until the
// visitor chooses a side; this keeps the meeting UI usable without any server-side state.
(function () {
  const key = "convene:theme";
  const query = matchMedia("(prefers-color-scheme: dark)");
  const icon = '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="M12 3v2m0 14v2M3 12h2m14 0h2M5.6 5.6 7 7m10 10 1.4 1.4M18.4 5.6 17 7M7 17l-1.4 1.4"/><circle cx="12" cy="12" r="4"/></svg>';
  const stored = () => { try { return localStorage.getItem(key); } catch { return null; } };
  const active = () => stored() || (query.matches ? "dark" : "light");
  const apply = (value = active()) => {
    document.documentElement.dataset.theme = value;
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", value === "dark" ? "#10121b" : "#f7f5f0");
    document.querySelectorAll("[data-theme-toggle]").forEach(button => {
      const next = value === "dark" ? "light" : "dark";
      button.setAttribute("aria-label", `Switch to ${next} mode`);
      button.setAttribute("title", `Switch to ${next} mode`);
      button.setAttribute("aria-pressed", String(value === "dark"));
    });
  };
  apply();
  addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-theme-toggle]").forEach(button => {
      button.innerHTML = icon;
      button.addEventListener("click", () => {
        const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
        try { localStorage.setItem(key, next); } catch { /* preference remains for this page */ }
        apply(next);
      });
    });
    apply();
    if (!matchMedia("(prefers-reduced-motion: reduce)").matches && matchMedia("(hover: hover) and (pointer: fine)").matches) {
      let targetX = -300, targetY = -300, currentX = -300, currentY = -300, frame = null;
      const glide = () => {
        // Ease toward the latest pointer position once per frame rather than snapping on every event.
        currentX += (targetX - currentX) * .16;
        currentY += (targetY - currentY) * .16;
        document.documentElement.style.setProperty("--cursor-glow-x", `${currentX}px`);
        document.documentElement.style.setProperty("--cursor-glow-y", `${currentY}px`);
        if (Math.abs(targetX - currentX) > .2 || Math.abs(targetY - currentY) > .2) frame = requestAnimationFrame(glide);
        else frame = null;
      };
      addEventListener("pointermove", event => {
        targetX = event.clientX; targetY = event.clientY;
        if (!frame) frame = requestAnimationFrame(glide);
      }, { passive: true });
    }
  });
  query.addEventListener?.("change", () => { if (!stored()) apply(); });
})();
