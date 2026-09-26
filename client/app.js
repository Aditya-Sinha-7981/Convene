// Convene join page (phone). Registers the device over REST, then attaches over the signaling WebSocket and
// streams the microphone over WebRTC. Contract: docs/api.md and docs/transport.md.
//
// Identity: a UUID device_id is stored in localStorage under a meeting-scoped key, so reloading or reopening
// the page resumes as the same device and participant. Recovery: every reconnect builds a fresh peer
// connection (the server cannot restart ICE), with the retry behavior proven in the DT-17 prototype.
const meetingId = decodeURIComponent(location.pathname.split("/").pop());
const keyPrefix = `convene:${meetingId}:`;
const MAX_QUIET_FAILURES = 5;

function storageGet(key) {
  try { return localStorage.getItem(keyPrefix + key); } catch { return null; }
}
function storageSet(key, value) {
  try { localStorage.setItem(keyPrefix + key, value); } catch { /* private mode: identity lasts until reload */ }
}
function newUuid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = Array.from(b, n => n.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

let deviceId = storageGet("device_id");
if (!deviceId) {
  deviceId = newUuid();
  storageSet("device_id", deviceId);
}

let stream = null;
let peer = null;
let socket = null;
let stopped = true;
let retryTimer = null;
let retryDelay = 1000;
let failures = 0;
let generation = 0;

const nameInput = document.querySelector("#name");
const status = document.querySelector("#status");
const detail = document.querySelector("#detail");
const startButton = document.querySelector("#start");
const stopButton = document.querySelector("#stop");
const retryButton = document.querySelector("#retry");
nameInput.value = storageGet("name") || "";

function setStatus(value) { status.textContent = value; }
function setDetail(value) { detail.textContent = value; }

function cleanupConnection() {
  if (peer) { peer.close(); peer = null; }
  if (socket) { socket.close(); socket = null; }
}

function finish(message) {
  // The meeting ended, or this device cannot join: stop for good, do not retry.
  stopped = true;
  generation++;
  clearTimeout(retryTimer);
  retryTimer = null;
  cleanupConnection();
  if (stream) stream.getTracks().forEach(track => track.stop());
  stream = null;
  startButton.disabled = false;
  stopButton.disabled = true;
  retryButton.hidden = true;
  setStatus("stopped");
  setDetail(message);
}

function retry() {
  if (stopped || retryTimer) return;
  cleanupConnection();
  failures++;
  if (failures >= MAX_QUIET_FAILURES) {
    // Repeated failures: stop retrying quietly and offer a clear action.
    setStatus("connection problem");
    setDetail("Could not reach the meeting. Check that you are on the meeting Wi-Fi, then try again.");
    retryButton.hidden = false;
    return;
  }
  setStatus("reconnecting");
  retryTimer = setTimeout(() => {
    retryTimer = null;
    connect();
  }, retryDelay);
  retryDelay = Math.min(retryDelay * 2, 10000);
}

// The optional colour picker (ADR-25): a palette key, or null to let the server pick an unused one.
function chosenColor() {
  const picked = document.querySelector("input[name=color]:checked");
  return picked ? picked.value : null;
}

async function register() {
  const response = await fetch(`/api/meetings/${encodeURIComponent(meetingId)}/devices`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ device_id: deviceId, display_name: nameInput.value.trim(), is_shared: false,
                           color: chosenColor() }),
  });
  let body = {};
  try { body = await response.json(); } catch { /* not JSON */ }
  if (!response.ok) {
    const error = body.error || {};
    const failure = new Error(error.message || `registration failed (${response.status})`);
    failure.code = error.code;
    throw failure;
  }
  return body.device;
}

async function makeOffer(ws) {
  const current = ++generation;
  const pc = new RTCPeerConnection({ iceServers: [] });
  peer = pc;
  stream.getTracks().forEach(track => pc.addTrack(track, stream));
  pc.onconnectionstatechange = () => {
    if (pc !== peer || stopped) return;
    setStatus(pc.connectionState);
    if (pc.connectionState === "connected") { retryDelay = 1000; failures = 0; retryButton.hidden = true; }
    if (["disconnected", "failed"].includes(pc.connectionState)) retry();
  };
  pc.oniceconnectionstatechange = () => {
    if (pc === peer && pc.iceConnectionState === "failed") retry();
  };
  await pc.setLocalDescription(await pc.createOffer());
  // Non-trickle ICE: send one offer after gathering, so the protocol needs no candidate messages.
  if (pc.iceGatheringState !== "complete") {
    await new Promise(resolve => {
      const timeout = setTimeout(resolve, 5000);
      pc.addEventListener("icegatheringstatechange", () => {
        if (pc.iceGatheringState === "complete") { clearTimeout(timeout); resolve(); }
      });
    });
  }
  if (current !== generation || ws !== socket || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type: "offer", sdp: pc.localDescription.sdp }));
}

function connect() {
  if (stopped || !navigator.onLine) { if (!stopped) retry(); return; }
  cleanupConnection();
  setStatus("signaling");
  const ws = new WebSocket(`wss://${location.host}/ws/signal/${encodeURIComponent(meetingId)}`);
  socket = ws;
  ws.onopen = () => ws.send(JSON.stringify({ type: "join", device_id: deviceId }));
  ws.onmessage = async event => {
    if (ws !== socket || stopped) return;
    try {
      const message = JSON.parse(event.data);
      if (message.type === "joined") {
        setDetail(message.is_reconnect ? "Reconnected." : "");
        await makeOffer(ws);
      } else if (message.type === "answer" && peer) {
        await peer.setRemoteDescription({ type: "answer", sdp: message.sdp });
      } else if (message.type === "meeting_ended") {
        finish("The meeting has ended.");
      } else if (message.type === "error") {
        if (message.code === "meeting_ended") return finish("The meeting has ended.");
        if (message.code === "meeting_not_found") return finish("This meeting no longer exists.");
        if (message.code === "device_not_registered") {
          // The server does not know this device (for example its data was reset): register again, then retry.
          try { await register(); } catch (error) { return finish(error.message); }
        }
        setDetail(message.message);
        if (message.fatal) retry();
      }
    } catch (error) {
      setDetail(String(error));
      retry();
    }
  };
  ws.onclose = () => { if (ws === socket) retry(); };
  ws.onerror = () => { if (ws === socket) retry(); };
}

async function start() {
  const name = nameInput.value.trim();
  if (!name) { setDetail("Enter your name first."); nameInput.focus(); return; }
  try {
    if (!navigator.mediaDevices?.getUserMedia) throw new Error("Microphone API unavailable. Check HTTPS certificate trust.");
    stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
  } catch (error) {
    setDetail(String(error));
    setStatus("microphone error");
    return;
  }
  try {
    const device = await register();
    if (typeof window.conveneRegistered === "function") window.conveneRegistered(device);  // visual layer only
  } catch (error) {
    stream.getTracks().forEach(track => track.stop());
    stream = null;
    if (error.code === "meeting_ended") return finish("This meeting has already ended.");
    if (error.code === "meeting_not_found") return finish("This meeting was not found. Scan the QR code again.");
    setDetail(error.message);
    setStatus("could not join");
    return;
  }
  storageSet("name", name);
  stopped = false;
  failures = 0;
  retryDelay = 1000;
  startButton.disabled = true;
  stopButton.disabled = false;
  stream.getAudioTracks()[0].onended = () => { setDetail("Microphone stopped; tap Join again."); stopButton.click(); };
  connect();
}

startButton.onclick = start;
retryButton.onclick = () => {
  if (stopped) return;
  failures = 0;
  retryDelay = 1000;
  retryButton.hidden = true;
  connect();
};
stopButton.onclick = () => {
  if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "leave" }));
  stopped = true;
  generation++;
  clearTimeout(retryTimer);
  retryTimer = null;
  cleanupConnection();
  if (stream) stream.getTracks().forEach(track => track.stop());
  stream = null;
  startButton.disabled = false;
  stopButton.disabled = true;
  retryButton.hidden = true;
  setStatus("stopped");
};
window.addEventListener("online", () => { if (!stopped) retry(); });
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && !stopped && (!peer || peer.connectionState !== "connected")) retry();
});
