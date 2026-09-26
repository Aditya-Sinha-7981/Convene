// Visual layer for the join page: mirrors the connection status that app.js writes into #status onto the status
// pill and the mascot. It only reads that text; app.js keeps all connection logic.
(function () {
  const status = document.querySelector("#status");
  const pill = document.querySelector(".join-status");
  const mascot = document.querySelector("[data-mascot]");
  if (!status || !pill) return;

  const LOOKS = {
    idle: ["idle", "waiting", "Hi! Add your name and I'll start listening."],
    signaling: ["connecting", "thinking", "Finding the meeting on this Wi-Fi…"],
    new: ["connecting", "thinking", "Finding the meeting on this Wi-Fi…"],
    connecting: ["connecting", "thinking", "Almost there…"],
    reconnecting: ["connecting", "thinking", "Lost you for a second. Reconnecting…"],
    disconnected: ["connecting", "thinking", "Lost you for a second. Reconnecting…"],
    connected: ["connected", "listening", "Listening. Keep this page open while you talk."],
    failed: ["problem", "worried", "Hmm, I can't reach the meeting."],
    "connection problem": ["problem", "worried", "Hmm, I can't reach the meeting."],
    "could not join": ["problem", "worried", "Hmm, the meeting didn't let us in."],
    "microphone error": ["problem", "worried", "I can't hear you yet. Allow the microphone."],
    stopped: ["idle", "off", "Mic's off. Join again whenever you're ready."],
    closed: ["idle", "off", "Mic's off. Join again whenever you're ready."],
  };

  function apply() {
    const [state, mood, line] = LOOKS[status.textContent.trim().toLowerCase()] || ["connecting", "thinking", "Working on it…"];
    pill.dataset.state = state;
    if (window.ConveneMascot && mascot) window.ConveneMascot.set(mascot, mood, line);
  }
  new MutationObserver(apply).observe(status, { childList: true, characterData: true, subtree: true });
  apply();
})();
