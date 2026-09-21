// Runs the real client/app.js against a fake browser and checks the join page's behavior.
// Usage: node join_page_harness.mjs --list | node join_page_harness.mjs <scenario>
// This shows the page's protocol and retry logic. It is NOT a browser: no real WebRTC, microphone,
// certificate trust, or Wi-Fi. Those need real phones.
import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(path.join(here, "..", "..", "client", "app.js"), "utf8");
const MEETING = "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8";
const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function element() {
  return { value: "", textContent: "", disabled: false, hidden: false, onclick: null,
           focus() { this.focused = true; }, click() { return this.onclick && this.onclick(); } };
}

// Objects made inside the vm sandbox have that realm's prototypes, so compare them by their JSON form.
const plain = value => JSON.parse(JSON.stringify(value));

function makePage({ meetingId = MEETING, storage = new Map(), registerReply, online = true } = {}) {
  const els = { "#name": element(), "#status": element(), "#detail": element(), "#start": element(),
                "#stop": element(), "#retry": element() };
  els["#retry"].hidden = true;
  const timers = []; let now = 0; let nextTimer = 1;
  const sockets = []; const peers = []; const fetches = []; const mediaRequests = [];
  const windowHandlers = {}; const documentHandlers = {};
  let registerCalls = 0;

  class FakeWebSocket {
    static OPEN = 1;
    constructor(url) { this.url = url; this.readyState = 1; this.sent = []; sockets.push(this); }
    send(data) { this.sent.push(JSON.parse(data)); }
    close() { this.readyState = 3; this.closedByPage = true; }
    receive(message) { return this.onmessage({ data: JSON.stringify(message) }); }
  }
  class FakePeer {
    constructor(config) {
      this.config = config; this.tracks = []; this.connectionState = "new"; this.iceConnectionState = "new";
      this.iceGatheringState = "complete"; this.closed = false; peers.push(this);
    }
    addTrack(track) { this.tracks.push(track); }
    async createOffer() { return { type: "offer", sdp: "v=0 fake-offer m=audio" }; }
    async setLocalDescription(d) { this.localDescription = d; }
    async setRemoteDescription(d) { this.remoteDescription = d; }
    addEventListener() {}
    close() { this.closed = true; }
    setState(state) { this.connectionState = state; this.onconnectionstatechange(); }
  }
  const track = { onended: null, stopped: false, stop() { this.stopped = true; } };
  const stream = { getTracks: () => [track], getAudioTracks: () => [track] };
  const sandbox = {
    document: {
      querySelector: sel => els[sel],
      addEventListener: (name, fn) => { documentHandlers[name] = fn; }, hidden: false,
    },
    window: { addEventListener: (name, fn) => { windowHandlers[name] = fn; } },
    location: { pathname: `/join/${meetingId}`, host: "192.168.50.10:8443" },
    localStorage: {
      getItem: k => (storage.has(k) ? storage.get(k) : null), setItem: (k, v) => storage.set(k, String(v)),
    },
    crypto: { randomUUID: () => "11111111-2222-4333-8444-555555555555", getRandomValues: a => a.fill(7) },
    navigator: { onLine: online, mediaDevices: { getUserMedia: async c => { mediaRequests.push(c); return stream; } } },
    fetch: async (url, init) => {
      fetches.push({ url, init: { ...init, body: init.body ? JSON.parse(init.body) : null } });
      const reply = registerReply ? registerReply(++registerCalls) : { status: 201, body: { device: {} } };
      return { ok: reply.status < 400, status: reply.status, json: async () => reply.body };
    },
    WebSocket: FakeWebSocket, RTCPeerConnection: FakePeer,
    setTimeout: (fn, ms) => { const id = nextTimer++; timers.push({ id, fn, at: now + ms, ms }); return id; },
    clearTimeout: id => { const i = timers.findIndex(t => t.id === id); if (i >= 0) timers.splice(i, 1); },
    encodeURIComponent, decodeURIComponent, JSON, Array, Uint8Array, Promise, Error, String, console,
  };
  sandbox.window.location = sandbox.location;
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: "client/app.js" });

  const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
  const page = {
    els, sockets, peers, fetches, mediaRequests, storage, track, sandbox, flush, windowHandlers, documentHandlers,
    pendingTimers: () => timers.map(t => t.ms),
    async advance() {  // run the earliest pending timer
      timers.sort((a, b) => a.at - b.at);
      const t = timers.shift(); if (!t) return false;
      now = t.at; t.fn(); await flush(); return true;
    },
    async join(name = "Priya") {  // click "Join" and answer the protocol up to a connected peer
      els["#name"].value = name;
      await els["#start"].onclick(); await flush();
      const ws = sockets.at(-1);
      ws.onopen(); await flush();
      await ws.receive({ type: "joined", device_id: "x", participant_ids: ["p"], device_status: "joining",
                         reconnect_count: 0, is_reconnect: false, peer_active: false });
      await flush();
      await ws.receive({ type: "answer", sdp: "v=0 fake-answer" }); await flush();
      peers.at(-1).setState("connected"); await flush();
      return ws;
    },
  };
  return page;
}

const scenarios = {
  async "device id is a persisted meeting-scoped UUID"() {
    const storage = new Map();
    const first = makePage({ storage });
    const key = `convene:${MEETING}:device_id`;
    assert.match(storage.get(key), UUID_V4);
    const kept = storage.get(key);
    makePage({ storage });  // reload / reopen the page
    assert.equal(storage.get(key), kept);
    const other = makePage({ storage, meetingId: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee" });
    assert.ok(storage.has("convene:aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee:device_id"));
    assert.ok(first && other);
  },

  async "a name is required before anything is requested"() {
    const page = makePage();
    await page.els["#start"].onclick(); await page.flush();
    assert.equal(page.mediaRequests.length, 0);
    assert.equal(page.fetches.length, 0);
    assert.match(page.els["#detail"].textContent, /name/i);
    assert.equal(page.els["#name"].focused, true);
  },

  async "happy path: microphone, registration, join, offer, answer, connected"() {
    const page = makePage();
    const ws = await page.join("  Priya  ");
    assert.deepEqual(plain(page.mediaRequests), [{ audio: true, video: false }]);  // constraints unchanged from the prototype
    const [reg] = page.fetches;
    assert.equal(reg.url, `/api/meetings/${MEETING}/devices`);
    assert.equal(reg.init.method, "POST");
    assert.equal(reg.init.headers["Content-Type"], "application/json");
    const deviceId = page.storage.get(`convene:${MEETING}:device_id`);
    assert.deepEqual(plain(reg.init.body), { device_id: deviceId, display_name: "Priya", is_shared: false });
    assert.equal(ws.url, `wss://192.168.50.10:8443/ws/signal/${MEETING}`);
    assert.deepEqual(plain(ws.sent[0]), { type: "join", device_id: deviceId });
    assert.equal(ws.sent[1].type, "offer");
    assert.ok(ws.sent[1].sdp.includes("m=audio"));
    assert.ok(!("ice_restart" in ws.sent[1]));  // the server cannot restart ICE, so the page never asks
    assert.deepEqual(plain(page.peers[0].config), { iceServers: [] });  // no STUN/TURN
    assert.equal(page.peers[0].tracks.length, 1);
    assert.deepEqual(plain(page.peers[0].remoteDescription), { type: "answer", sdp: "v=0 fake-answer" });
    assert.equal(page.els["#status"].textContent, "connected");
    assert.equal(page.storage.get(`convene:${MEETING}:name`), "Priya");
    assert.equal(page.els["#start"].disabled, true);
    assert.equal(page.els["#stop"].disabled, false);
  },

  async "the consent notice is on the page"() {
    const html = fs.readFileSync(path.join(here, "..", "..", "client", "index.html"), "utf8");
    assert.match(html, /audio is being transcribed/i);
    assert.match(html, /id="name"/);
  },

  async "registration refused because the meeting ended: no connection is attempted"() {
    const page = makePage({ registerReply: () => ({ status: 409, body: { error: { code: "meeting_ended", message: "meeting has ended" } } }) });
    page.els["#name"].value = "Priya";
    await page.els["#start"].onclick(); await page.flush();
    assert.equal(page.sockets.length, 0);
    assert.match(page.els["#detail"].textContent, /ended/i);
    assert.equal(page.track.stopped, true);  // the microphone is released
    assert.equal(page.els["#status"].textContent, "stopped");
  },

  async "an unknown meeting says so and stops"() {
    const page = makePage({ registerReply: () => ({ status: 404, body: { error: { code: "meeting_not_found", message: "x" } } }) });
    page.els["#name"].value = "Priya";
    await page.els["#start"].onclick(); await page.flush();
    assert.equal(page.sockets.length, 0);
    assert.match(page.els["#detail"].textContent, /not found/i);
  },

  async "a peer that fails triggers a fresh connection with backoff"() {
    const page = await makePage();
    await page.join();
    page.peers.at(-1).setState("failed"); await page.flush();
    assert.equal(page.els["#status"].textContent, "reconnecting");
    assert.deepEqual(page.pendingTimers(), [1000]);
    await page.advance();
    assert.equal(page.sockets.length, 2);  // a new signaling socket...
    page.sockets[1].onopen(); await page.flush();
    await page.sockets[1].receive({ type: "joined", is_reconnect: true, peer_active: false, participant_ids: [], device_status: "connected", reconnect_count: 0, device_id: "x" });
    await page.flush();
    assert.equal(page.peers.length, 2);  // ...and a fresh peer connection, not a restart
    assert.equal(page.peers[0].closed, true);
    assert.deepEqual(page.sockets[1].sent[0].type, "join");
    assert.equal(page.sockets[1].sent[1].type, "offer");
    assert.equal(page.els["#detail"].textContent, "Reconnected.");
  },

  async "retry delay doubles up to 10 seconds and resets after a connection"() {
    const page = makePage();
    await page.join();
    const delays = [];
    for (let i = 0; i < 4; i++) {
      page.sockets.at(-1).onclose(); await page.flush();
      delays.push(...page.pendingTimers());
      await page.advance();
      if (i < 3) { page.sockets.at(-1).onopen(); await page.flush(); }
    }
    assert.deepEqual(delays, [1000, 2000, 4000, 8000]);
  },

  async "repeated failures stop the automatic retries and offer a clear retry button"() {
    const page = makePage();
    await page.join();
    for (let i = 0; i < 4; i++) { page.sockets.at(-1).onclose(); await page.flush(); await page.advance(); }
    page.sockets.at(-1).onclose(); await page.flush();  // the fifth failure
    assert.equal(page.els["#status"].textContent, "connection problem");
    assert.equal(page.els["#retry"].hidden, false);
    assert.deepEqual(page.pendingTimers(), []);  // it has stopped trying on its own
    const before = page.sockets.length;
    page.els["#retry"].onclick(); await page.flush();
    assert.equal(page.sockets.length, before + 1);  // the button tries again immediately
    assert.equal(page.els["#retry"].hidden, true);
  },

  async "a server error is shown and a fatal one retries"() {
    const page = makePage();
    await page.join();
    await page.sockets.at(-1).receive({ type: "error", code: "invalid_message", message: "bad frame", fatal: true });
    await page.flush();
    assert.equal(page.els["#detail"].textContent, "bad frame");
    assert.deepEqual(page.pendingTimers(), [1000]);
  },

  async "a non-fatal error is shown without tearing the connection down"() {
    const page = makePage();
    const ws = await page.join();
    await ws.receive({ type: "error", code: "renegotiation_failed", message: "cannot restart", fatal: false });
    await page.flush();
    assert.equal(page.els["#detail"].textContent, "cannot restart");
    assert.deepEqual(page.pendingTimers(), []);
    assert.equal(ws.closedByPage, undefined);
  },

  async "meeting_ended, as a message or an error, stops for good"() {
    for (const deliver of [ws => ws.receive({ type: "meeting_ended" }),
                           ws => ws.receive({ type: "error", code: "meeting_ended", message: "ended", fatal: true })]) {
      const page = makePage();
      const ws = await page.join();
      await deliver(ws); await page.flush();
      assert.match(page.els["#detail"].textContent, /ended/i);
      assert.equal(page.els["#status"].textContent, "stopped");
      assert.deepEqual(page.pendingTimers(), []);
      assert.equal(page.track.stopped, true);
      ws.onclose?.(); await page.flush();  // the closing socket must not restart anything
      assert.deepEqual(page.pendingTimers(), []);
    }
  },

  async "an unregistered device registers again and then retries"() {
    const page = makePage();
    const ws = await page.join();
    const registrations = page.fetches.length;
    await ws.receive({ type: "error", code: "device_not_registered", message: "unknown device", fatal: true });
    await page.flush();
    assert.equal(page.fetches.length, registrations + 1);
    assert.deepEqual(page.pendingTimers(), [1000]);
  },

  async "Stop tells the server the phone left and releases the microphone"() {
    const page = makePage();
    const ws = await page.join();
    page.els["#stop"].onclick(); await page.flush();
    assert.deepEqual(plain(ws.sent.at(-1)), { type: "leave" });
    assert.equal(page.track.stopped, true);
    assert.equal(page.els["#status"].textContent, "stopped");
    assert.equal(page.els["#start"].disabled, false);
    ws.onclose?.(); await page.flush();
    assert.deepEqual(page.pendingTimers(), []);  // stopping does not trigger a reconnect
  },

  async "coming back online or returning to the tab reconnects a broken connection"() {
    const page = makePage();
    await page.join();
    page.peers.at(-1).connectionState = "failed";
    page.windowHandlers.online(); await page.flush();
    assert.equal(page.els["#status"].textContent, "reconnecting");
    await page.advance();
    assert.equal(page.sockets.length, 2);
    page.sandbox.document.hidden = false;
    page.peers.at(-1).connectionState = "new";
    page.documentHandlers.visibilitychange(); await page.flush();
    assert.equal(page.els["#status"].textContent, "reconnecting");
  },

  async "an ended microphone stops the session"() {
    const page = makePage();
    await page.join();
    page.track.onended(); await page.flush();
    assert.equal(page.els["#status"].textContent, "stopped");
    assert.match(page.els["#detail"].textContent, /Microphone stopped/);
  },

  async "a microphone permission error is reported and nothing is registered"() {
    const page = makePage();
    page.sandbox.navigator.mediaDevices.getUserMedia = async () => { throw new Error("NotAllowedError"); };
    page.els["#name"].value = "Priya";
    await page.els["#start"].onclick(); await page.flush();
    assert.equal(page.els["#status"].textContent, "microphone error");
    assert.equal(page.fetches.length, 0);
  },
};

const arg = process.argv[2];
if (arg === "--list") {
  console.log(Object.keys(scenarios).join("\n"));
} else {
  const run = scenarios[arg];
  if (!run) { console.error(`unknown scenario: ${arg}`); process.exit(2); }
  try { await run(); console.log(`ok: ${arg}`); }
  catch (error) { console.error(`FAILED: ${arg}\n${error.stack}`); process.exit(1); }
}
