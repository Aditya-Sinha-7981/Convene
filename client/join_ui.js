// Visual layer for the join page: the colour picker, and the guide (mascot + line) that mirrors the connection
// status app.js writes into #status. app.js keeps all connection logic; it only reads the picked colour and tells
// this layer about the registered device (window.conveneRegistered).
(function () {
  const meetingId = decodeURIComponent(location.pathname.split("/").pop());
  const status = document.querySelector("#status");
  const pill = document.querySelector(".join-status");
  const guide = document.querySelector("#guide");
  const guideArt = document.querySelector("#guideArt");
  const guideLine = document.querySelector("#guideLine");
  const guideWalk = document.querySelector("#guideWalk");
  const swatches = document.querySelector("#swatches");
  const picker = document.querySelector("#colorPicker");
  const hint = document.querySelector("#colorHint");
  const Brand = window.ConveneBrand;

  const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } },
  };
  const MINE = `convene:${meetingId}:color`;   // the colour this phone has in this meeting
  const LAST = "convene:last-color";            // preselected next time, if free

  // -- Colour picker (ADR-25) -----------------------------------------------------------------------------------
  // A phone that already joined this meeting keeps its colour on rejoin (the server replays the registration), so
  // its picker starts locked to that colour instead of offering a choice the server would ignore.
  let mine = store.get(MINE);
  let joined = Boolean(mine);
  let note = "";

  function render(taken) {
    const picked = swatches.querySelector("input:checked")?.value || mine || store.get(LAST);
    swatches.replaceChildren();
    for (const key of Brand.PALETTE) {
      const isMine = key === mine;
      const unavailable = taken.has(key) && !isMine;
      const label = document.createElement("label");
      label.className = "swatch";
      label.dataset.color = key;
      const input = document.createElement("input");
      input.type = "radio"; input.name = "color"; input.value = key;
      input.disabled = unavailable || (joined && !isMine);
      input.checked = !unavailable && key === picked;
      input.setAttribute("aria-label", unavailable ? `${key}, taken` : key);
      label.append(input, Brand.avatar(key));
      if (unavailable) label.title = "Someone in this meeting already has this colour";
      swatches.append(label);
    }
    updateHint();
  }
  function updateHint() {
    const picked = swatches.querySelector("input:checked");
    if (joined) hint.textContent = note || "This is your colour for this meeting.";
    else hint.textContent = picked ? `You'll appear in ${picked.value}. Tap it again to let us pick.` : "Skip it and we'll pick one no one else has.";
  }
  // Tapping the selected swatch again clears the choice, so the picker stays optional.
  let wasChecked = false;
  swatches.addEventListener("pointerdown", event => {
    wasChecked = Boolean(event.target.closest(".swatch")?.querySelector("input")?.checked);
  });
  swatches.addEventListener("click", event => {
    if (event.target.tagName !== "INPUT") return;  // the label's click is re-dispatched to its radio
    if (wasChecked && !joined) event.target.checked = false;
    wasChecked = false;
    updateHint();
  });

  async function refresh() {
    try {
      const response = await fetch(`/api/meetings/${encodeURIComponent(meetingId)}/colors`);
      if (!response.ok) throw new Error();
      const body = await response.json();
      render(new Set(body.colors.filter(entry => entry.taken).map(entry => entry.color)));
    } catch { render(new Set()); }
  }
  refresh();
  if (joined) picker.classList.add("is-set");

  // -- Live view: once registered with a microphone, the form gives way to the orb ------------------------------
  const liveView = document.querySelector("#liveView");
  const orbCanvas = document.querySelector("#orbCanvas");
  const orbAvatar = document.querySelector("#orbAvatar");
  const orbState = document.querySelector("#orbState");
  const orbWho = document.querySelector("#orbWho");
  let orb = null, orbTimer = null;
  document.querySelector("#start").addEventListener("click", () => window.ConveneOrb?.unlock(), true);

  function enterLive(me, stream) {
    const avatar = Brand.avatar(me?.color, me?.participant_id);
    orbAvatar.replaceChildren(avatar);
    orbWho.textContent = me ? `${me.display_name} · ${me.color || "your colour"}` : "";
    document.body.classList.add("is-live");
    liveView.hidden = false;
    orb?.stop();
    orb = window.ConveneOrb?.start(orbCanvas, stream, getComputedStyle(avatar).getPropertyValue("--pc").trim() || "#4338ca");
    clearInterval(orbTimer);
    orbTimer = setInterval(updateOrbState, 250);
    updateOrbState();
  }
  function leaveLive() {
    document.body.classList.remove("is-live");
    liveView.hidden = true;
    orb?.stop(); orb = null; clearInterval(orbTimer);
  }
  function updateOrbState() {
    const state = pill.dataset.state;
    orb?.setActive(state === "connected");
    liveView.dataset.state = state;
    if (state !== "connected") orbState.textContent = state === "problem" ? "Not connected" : "Connecting…";
    else orbState.textContent = orb && orb.level > .12 ? "Hearing you" : "Listening…";
  }

  window.conveneRegistered = (device, stream) => {
    const me = device?.participants?.[0];
    enterLive(me, stream);
    if (!me?.color) return;
    const asked = swatches.querySelector("input:checked")?.value;
    note = asked && asked !== me.color ? `You joined this meeting before, so you keep ${me.color}.` : "";
    mine = me.color; joined = true;
    store.set(MINE, mine); store.set(LAST, mine);
    render(new Set());
    picker.classList.add("is-set");
  };

  // -- Guide: follows the connection status ---------------------------------------------------------------------
  const LOOKS = {
    idle: ["idle", "still", "Hi! Add your name and I'll start listening."],
    signaling: ["connecting", "walk", "Finding the meeting on this Wi-Fi…"],
    new: ["connecting", "walk", "Finding the meeting on this Wi-Fi…"],
    connecting: ["connecting", "walk", "Setting up your microphone…"],
    reconnecting: ["connecting", "walk", "Lost you for a second. Reconnecting…"],
    disconnected: ["connecting", "walk", "Lost you for a second. Reconnecting…"],
    connected: ["connected", "you", "You're live. Keep this page open while you talk."],
    failed: ["problem", "still", "I can't reach the meeting. Check you're on the meeting Wi-Fi."],
    "connection problem": ["problem", "still", "I can't reach the meeting. Check you're on the meeting Wi-Fi."],
    "could not join": ["problem", "still", "The meeting didn't let us in. See the note below."],
    "microphone error": ["problem", "still", "I can't hear you yet. Allow the microphone and try again."],
    stopped: ["idle", "off", "Your microphone is off. Join again whenever you're ready."],
    closed: ["idle", "off", "Your microphone is off. Join again whenever you're ready."],
  };
  let walker = null;

  // Now and then, while live, the guide says something lighter for a few seconds, then goes back to the useful line.
  const QUIRKS = ["Your phone's a mic now. Weird, right?", "No more 'wait, who said that?'",
                  "I promise I won't put words in your mouth.", "Everything stays in this room. I don't gossip."];
  let quirkTimer = null, quirkIndex = Math.floor(Math.random() * QUIRKS.length);
  function scheduleQuirk(first) {
    clearTimeout(quirkTimer);
    quirkTimer = setTimeout(() => {
      if (pill.dataset.state !== "connected") return;
      const usual = guideLine.textContent;
      guideLine.textContent = QUIRKS[quirkIndex++ % QUIRKS.length];
      quirkTimer = setTimeout(() => {
        if (pill.dataset.state === "connected") { guideLine.textContent = usual; scheduleQuirk(false); }
      }, 7000);
    }, first ? 20000 : 60000 + Math.random() * 60000);
  }

  function apply() {
    const value = status.textContent.trim().toLowerCase();
    const [state, pose, line] = LOOKS[value] || ["connecting", "walk", "Working on it…"];
    pill.dataset.state = state;
    guideLine.textContent = line;
    guide.classList.toggle("is-off", pose === "off");
    guide.classList.toggle("is-walking", pose === "walk");
    // Walking means Convene is busy connecting; otherwise the mascot stands beside its line.
    if (pose === "walk") {
      if (!walker) { walker = Brand.walker(); guideWalk.append(walker); }
      guideWalk.hidden = false;
    } else guideWalk.hidden = true;
    // Once live, the guide shows the participant's own avatar: this is how they appear on the transcript.
    const art = guide.querySelector(".mascot-art");
    art.querySelector(".avatar")?.remove();
    guideArt.hidden = false;
    if (pose === "you" && mine) { guideArt.hidden = true; art.append(Brand.avatar(mine)); }
    if (value === "could not join" && !mine) { joined = false; refresh(); }  // e.g. colour_taken: show what is free now
    if (state === "connected") scheduleQuirk(true); else clearTimeout(quirkTimer);
    if (pose === "off") leaveLive();  // stopped, or the meeting ended: back to the join form
  }
  new MutationObserver(apply).observe(status, { childList: true, characterData: true, subtree: true });
  apply();
})();
