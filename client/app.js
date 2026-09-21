const meeting = decodeURIComponent(location.pathname.split("/").pop());
const identityKey = `dt17:${meeting}:token`;
let token = sessionStorage.getItem(identityKey);
if (!token) {
  token = Array.from(crypto.getRandomValues(new Uint8Array(24)), n => n.toString(16).padStart(2, "0")).join("");
  sessionStorage.setItem(identityKey, token);
}
let stream = null;
let peer = null;
let socket = null;
let stopped = true;
let retryTimer = null;
let retryDelay = 1000;
let generation = 0;

const status = document.querySelector("#status");
const detail = document.querySelector("#detail");
const startButton = document.querySelector("#start");
const stopButton = document.querySelector("#stop");
function setStatus(value) { status.textContent = value; }
function cleanupConnection() {
  if (peer) { peer.close(); peer = null; }
  if (socket) { socket.close(); socket = null; }
}
function retry() {
  if (stopped || retryTimer) return;
  cleanupConnection();
  setStatus("reconnecting");
  retryTimer = setTimeout(() => {
    retryTimer = null;
    connect();
  }, retryDelay);
  retryDelay = Math.min(retryDelay * 2, 10000);
}
async function makeOffer(ws) {
  const current = ++generation;
  const pc = new RTCPeerConnection({ iceServers: [] });
  peer = pc;
  stream.getTracks().forEach(track => pc.addTrack(track, stream));
  pc.onconnectionstatechange = () => {
    if (pc !== peer || stopped) return;
    setStatus(pc.connectionState);
    if (pc.connectionState === "connected") retryDelay = 1000;
    if (["disconnected", "failed"].includes(pc.connectionState)) retry();
  };
  pc.oniceconnectionstatechange = () => {
    if (pc === peer && pc.iceConnectionState === "failed") retry();
  };
  await pc.setLocalDescription(await pc.createOffer());
  // Send only after ICE gathering so the server needs no trickle messages.
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
  const ws = new WebSocket(`wss://${location.host}/ws/${encodeURIComponent(meeting)}`);
  socket = ws;
  ws.onopen = () => ws.send(JSON.stringify({ type: "join", token }));
  ws.onmessage = async event => {
    if (ws !== socket || stopped) return;
    try {
      const message = JSON.parse(event.data);
      if (message.type === "joined") {
        document.querySelector("#participant").textContent = message.participantId;
        detail.textContent = `Reconnections: ${message.reconnects}`;
        await makeOffer(ws);
      } else if (message.type === "answer" && peer) {
        await peer.setRemoteDescription({ type: "answer", sdp: message.sdp });
      } else if (message.type === "error") {
        detail.textContent = message.message;
        retry();
      }
    } catch (error) {
      detail.textContent = String(error);
      retry();
    }
  };
  ws.onclose = () => { if (ws === socket) retry(); };
  ws.onerror = () => { if (ws === socket) retry(); };
}
startButton.onclick = async () => {
  try {
    if (!navigator.mediaDevices?.getUserMedia) throw new Error("Microphone API unavailable. Check HTTPS certificate trust.");
    stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
    stopped = false;
    startButton.disabled = true;
    stopButton.disabled = false;
    stream.getAudioTracks()[0].onended = () => { detail.textContent = "Microphone stopped; tap Start again."; stopButton.click(); };
    connect();
  } catch (error) { detail.textContent = String(error); setStatus("microphone error"); }
};
stopButton.onclick = () => {
  stopped = true;
  generation++;
  clearTimeout(retryTimer);
  retryTimer = null;
  cleanupConnection();
  stream?.getTracks().forEach(track => track.stop());
  stream = null;
  startButton.disabled = false;
  stopButton.disabled = true;
  setStatus("stopped");
};
window.addEventListener("online", () => { if (!stopped) retry(); });
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && !stopped && (!peer || peer.connectionState !== "connected")) retry();
});
