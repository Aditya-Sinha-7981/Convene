// Homepage: create a meeting, plus the page's small amount of motion (hero simulation and scroll reveal).
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

// -- Start a meeting (unchanged behaviour: POST /api/meetings, then open the dashboard) --------------------------
document.querySelector("#start").addEventListener("submit", async event => {
  event.preventDefault();
  const message = document.querySelector("#message");
  const create = document.querySelector("#create");
  create.disabled = true; message.textContent = "";
  try {
    const response = await fetch("/api/meetings", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: document.querySelector("#title").value.trim() || null }),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error ? body.error.message : "could not create the meeting");
    location.href = `/dashboard/${body.meeting.meeting_id}`;
  } catch (error) { message.textContent = String(error); create.disabled = false; }
});

for (const link of document.querySelectorAll("[data-focus-start]")) {
  link.addEventListener("click", event => {
    event.preventDefault();
    const card = document.querySelector("#start");
    card.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "center" });
    document.querySelector("#title").focus({ preventScroll: true });
    card.classList.remove("is-focused"); void card.offsetWidth; card.classList.add("is-focused");
  });
}

const nav = document.querySelector(".nav");
const onScroll = () => nav.classList.toggle("is-scrolled", scrollY > 8);
addEventListener("scroll", onScroll, { passive: true }); onScroll();

// -- Scroll reveal: a quick fade and rise, once per element --------------------------------------------------------
if (!reduceMotion && "IntersectionObserver" in window) {
  document.documentElement.classList.add("js-reveal");
  const observer = new IntersectionObserver(entries => {
    for (const entry of entries) if (entry.isIntersecting) { entry.target.classList.add("in"); observer.unobserve(entry.target); }
  }, { rootMargin: "0px 0px -8% 0px", threshold: .12 });
  for (const el of document.querySelectorAll(".reveal")) observer.observe(el);
}

// -- Hero simulation: phones connect, lines arrive, an uncertain line is corrected, a question is answered ----------
(function simulation() {
  const sim = document.querySelector("#sim");
  const devices = [...sim.querySelectorAll(".sim-devices li")];
  const live = sim.querySelector(".sim-live");
  const lines = sim.querySelector(".sim-lines");
  const question = sim.querySelector(".sim-q");
  const answer = sim.querySelector(".sim-a");
  let visible = true;

  // Waits count only while the widget is on screen (rAF also stops in background tabs), so the story never
  // plays out unseen.
  const wait = ms => new Promise(resolve => {
    let left = ms, last = performance.now();
    const tick = now => { if (visible) left -= now - last; last = now; if (left <= 0) resolve(); else requestAnimationFrame(tick); };
    requestAnimationFrame(tick);
  });
  async function type(el, text, perChar = 24) {
    el.classList.add("caret");
    for (let i = 1; i <= text.length; i++) { el.textContent = text.slice(0, i); if (perChar) await wait(perChar); }
    el.classList.remove("caret");
  }

  function reset() {
    lines.replaceChildren(); question.textContent = ""; answer.replaceChildren();
    live.dataset.on = "false"; live.querySelector("span").textContent = "Waiting";
    devices.forEach(li => { li.classList.remove("on"); li.querySelector("small").textContent = "Waiting"; });
  }
  function connect(i, label = "Connected") {
    devices[i].classList.add("on"); devices[i].querySelector("small").textContent = label;
    live.dataset.on = "true"; live.querySelector("span").textContent = "Live";
  }
  function addLine(who, cls = "") {
    const li = document.createElement("li"); li.className = `sim-line ${cls}`;
    const name = document.createElement("span"); name.className = "who"; name.textContent = who;
    const text = document.createElement("span"); text.className = "what";
    li.append(name, text); lines.append(li); return li;
  }
  function tag(li, text) { const t = document.createElement("span"); t.className = "tag"; t.textContent = text; li.append(t); return t; }
  function showAnswer() {
    answer.replaceChildren(document.createTextNode("Lee said they'll own the QA sign-off, due Wednesday."), document.createElement("br"));
    const cite = document.createElement("cite"); cite.textContent = "Lee · 10:04"; answer.append(cite);
  }

  async function play(speed) {
    const t = ms => wait(ms * speed);
    reset(); await t(500);
    connect(0); await t(420); connect(1); await t(420); connect(2, "Jordan, Lee"); await t(650);
    await type(addLine("Priya").lastChild, "Let's lock the pilot launch date today.", 22 * speed); await t(650);
    await type(addLine("Sam").lastChild, "Friday works if QA signs off by Wednesday.", 22 * speed); await t(650);
    const unsure = addLine("Jordan?", "review");
    await type(unsure.lastChild, "I'll own the QA sign-off.", 22 * speed);
    const badge = tag(unsure, "Needs review"); await t(1500);
    const who = unsure.querySelector(".who");
    who.classList.add("swap"); await t(200); who.textContent = "Lee";
    unsure.classList.replace("review", "fixed"); badge.textContent = "Corrected"; await t(1100);
    unsure.classList.add("settled"); await t(500);
    await type(question, "Who owns the QA sign-off?", 30 * speed); await t(700);
    showAnswer();
  }

  function finalState() {
    reset(); connect(0); connect(1); connect(2, "Jordan, Lee");
    addLine("Priya").lastChild.textContent = "Let's lock the pilot launch date today.";
    addLine("Sam").lastChild.textContent = "Friday works if QA signs off by Wednesday.";
    const fixed = addLine("Lee"); fixed.lastChild.textContent = "I'll own the QA sign-off."; tag(fixed, "Corrected");
    question.textContent = "Who owns the QA sign-off?"; showAnswer();
  }

  if (reduceMotion) { finalState(); return; }
  if ("IntersectionObserver" in window) new IntersectionObserver(([entry]) => { visible = entry.isIntersecting; }).observe(sim);
  (async () => { for (;;) { await play(1); await wait(5200); } })();
})();
