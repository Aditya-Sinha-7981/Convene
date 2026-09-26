import { createState, orderedAnswers, orderedUtterances, reduce } from "/static/dashboard_state.js";

const meetingId = decodeURIComponent(location.pathname.split("/").pop());
const byId = id => document.querySelector(id);
const ui = {
  title: byId("#title"), status: byId("#meetingStatus"), sync: byId("#syncState"), stale: byId("#staleBanner"),
  error: byId("#pageError"), devices: byId("#devices"), count: byId("#deviceCount"), join: byId("#joinUrl"),
  qr: byId("#qr"), transcript: byId("#transcript"), scroll: byId("#transcriptScroll"), empty: byId("#emptyTranscript"),
  newLines: byId("#newLines"), end: byId("#end"), dialog: byId("#correctionDialog"), form: byId("#correctionForm"),
  choices: byId("#participantChoices"), speakerName: byId("#speakerName"), correctionLine: byId("#correctionLine"),
  correctionError: byId("#correctionError"), save: byId("#saveCorrection"), cancel: byId("#cancelCorrection"),
  qaForm: byId("#qaForm"), qaQuestion: byId("#qaQuestion"), qaAsk: byId("#qaAsk"), qaHint: byId("#qaHint"),
  qaEmpty: byId("#qaEmpty"), qaAnswers: byId("#qaAnswers"),
};
let state = createState();
let socket = null;
let buffer = [];
let synced = false;
let reconnectTimer = null;
let delay = 500;
let activeUtterance = null;
let firstUtterance = true;
let unread = 0;
const rows = new Map();
let pendingQuestion = null; // { question, started } while one question is in flight: no double submit
let pendingTimer = null;

// Q&A: the server decides the outcome. These are only the words for each server-provided status and reason.
const NO_GROUNDING_DETAIL = {
  nothing_transcribed_yet: "Nothing has been transcribed in this meeting yet.",
  not_indexed_yet: "The latest speech is still being indexed. Ask again in a few seconds.",
  no_relevant_evidence: "No part of the transcript answers this, so Convene did not guess.",
  model_declined: "The closest excerpts did not contain the answer, so Convene did not guess.",
};

function setError(message = "") { ui.error.hidden = !message; ui.error.textContent = message; }
function setSync(stale, text) { ui.stale.hidden = !stale; ui.sync.textContent = text; ui.sync.classList.toggle("is-stale", stale); }
function dispatch(action) { state = reduce(state, action); }
function json(response) { return response.json().catch(() => ({})); }
function timestamp(value) { const date = new Date(value); return Number.isNaN(date.valueOf()) ? "—" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }
function age(gauge) { if (!gauge || gauge.last_audio_age_ms == null) return "—"; return `${Math.max(0, Math.round((gauge.last_audio_age_ms + Date.now() - (gauge.received_at || Date.now())) / 1000))}s`; }
function atBottom() { return ui.scroll.scrollHeight - ui.scroll.scrollTop - ui.scroll.clientHeight < 48; }

function renderDevices(pulseId = null) {
  const devices = Object.values(state.devices).sort((a, b) => a.joined_at.localeCompare(b.joined_at));
  ui.count.textContent = String(devices.length);
  const fragment = document.createDocumentFragment();
  for (const device of devices) {
    const gauge = state.gauges[device.device_id] || device.gauges || {};
    const name = device.participants.map(person => person.display_name).join(", ") || "Shared phone";
    const stale = device.status === "disconnected" || (gauge.last_audio_age_ms != null && age(gauge) !== "—" && Number.parseInt(age(gauge), 10) > 5);
    const card = document.createElement("article");
    card.className = `device-card ${device.status === "connected" ? "is-connected" : ""} ${stale ? "is-stale" : ""} ${pulseId === device.device_id ? "connected-pulse" : ""}`;
    card.innerHTML = `<p class="device-name"></p><p class="device-meta"></p><div class="device-status"><span class="status-dot ${device.status}"></span><span></span></div><div class="device-metrics"><span class="metric">Last audio<b></b></span><span class="metric">STT queue<b></b></span><span class="metric">Reconnects<b></b></span><span class="metric">Dropped<b></b></span></div>`;
    card.querySelector(".device-name").textContent = name;
    card.querySelector(".device-meta").textContent = device.is_shared ? "Shared device" : "Dedicated microphone";
    card.querySelector(".device-status span:last-child").textContent = device.status;
    const values = card.querySelectorAll(".metric b");
    values[0].textContent = age(gauge); values[1].textContent = gauge.stt_backlog ?? "—";
    values[2].textContent = device.reconnect_count; values[3].textContent = gauge.stt_dropped_windows ?? "—";
    fragment.append(card);
  }
  ui.devices.replaceChildren(fragment);
}

function createRow(utterance, animate) {
  const row = document.createElement("li"); row.dataset.utteranceId = utterance.utterance_id;
  if (animate) row.classList.add(firstUtterance ? "utterance-first" : "utterance-enter");
  firstUtterance = false; rows.set(utterance.utterance_id, row); updateRow(row, utterance); return row;
}
function updateRow(row, utterance) {
  const entering = row.classList.contains("utterance-enter") ? " utterance-enter" : row.classList.contains("utterance-first") ? " utterance-first" : "";
  row.className = `utterance ${utterance.low_confidence ? "low-confidence" : ""}${entering}`;
  row.dataset.utteranceId = utterance.utterance_id;
  row.replaceChildren();
  const time = document.createElement("time"); time.className = "utterance-time"; time.textContent = timestamp(utterance.t_start);
  const speaker = document.createElement("strong"); speaker.className = "speaker-label"; speaker.textContent = utterance.speaker_label;
  const text = document.createElement("span"); text.className = "utterance-text"; text.textContent = utterance.text;
  const badges = document.createElement("span"); badges.className = "utterance-badges";
  if (utterance.low_confidence) { const badge = document.createElement("span"); badge.className = "badge warning"; badge.textContent = "Needs review"; badge.title = "The server marked this attribution as low confidence."; badges.append(badge); }
  if (utterance.corrected) { const badge = document.createElement("span"); badge.className = "badge"; badge.textContent = "Corrected"; badges.append(badge); }
  const review = document.createElement("button"); review.className = "review-button"; review.type = "button"; review.textContent = "Review / correct"; review.onclick = () => openCorrection(utterance.utterance_id);
  row.onclick = event => { if (event.target !== review) openCorrection(utterance.utterance_id); };
  row.append(time, speaker, text, badges, review);
}
function renderTranscript(changedId = null, animate = false) {
  const lines = orderedUtterances(state); ui.empty.hidden = lines.length > 0;
  const shouldFollow = atBottom(); const wanted = new Set(lines.map(line => line.utterance_id));
  for (const [id, row] of rows) if (!wanted.has(id)) { row.remove(); rows.delete(id); }
  let cursor = ui.transcript.firstChild;
  for (const line of lines) {
    let row = rows.get(line.utterance_id);
    if (!row) row = createRow(line, animate && line.utterance_id === changedId);
    else if (line.utterance_id === changedId) updateRow(row, line);
    if (row !== cursor) ui.transcript.insertBefore(row, cursor);
    cursor = row.nextSibling;
  }
  if (shouldFollow) { ui.scroll.scrollTop = ui.scroll.scrollHeight; unread = 0; ui.newLines.hidden = true; }
  else if (changedId) { unread++; ui.newLines.textContent = `${unread} new line${unread === 1 ? "" : "s"} ↓`; ui.newLines.hidden = false; }
}
function render(event = null) {
  const meeting = state.meeting;
  ui.title.textContent = meeting?.title || "Meeting"; ui.status.textContent = meeting?.status || "—";
  ui.status.className = `meeting-status ${meeting?.status || ""}`; ui.end.disabled = meeting?.status === "ended";
  if (event?.type === "device_status") renderDevices(event.device.device_id); else renderDevices();
  if (event?.type === "utterance" || event?.type === "utterance_updated") renderTranscript(event.utterance.utterance_id, true); else renderTranscript();
  if (!event || event.type === "qa_answer" || event.type === "meeting_status") renderAnswers();
}

function highlightLines(ids) {
  const found = ids.map(id => rows.get(id)).filter(Boolean);
  if (!found.length) { setError("Those lines are not in the loaded transcript."); return; }
  found[0].scrollIntoView({ block: "center", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  for (const row of found) { row.classList.remove("cited"); void row.offsetWidth; row.classList.add("cited"); setTimeout(() => row.classList.remove("cited"), 3200); }
}
function answerCard(item) {
  const { query, citations = [], reason = null, unindexed_utterances: unindexed = 0 } = item;
  const card = document.createElement("li"); card.className = `qa-card ${query.status}`;
  const head = document.createElement("div"); head.className = "qa-card-head";
  const chip = document.createElement("span"); chip.className = "qa-status";
  chip.textContent = { answered: "Answered", no_grounding: "Not discussed", failed: "System error" }[query.status] || query.status;
  const when = document.createElement("time"); when.className = "qa-time"; when.textContent = timestamp(query.created_at);
  head.append(chip, when);
  const question = document.createElement("p"); question.className = "qa-question"; question.textContent = query.question;
  card.append(head, question);
  const body = document.createElement("p"); body.className = "qa-body";
  if (query.status === "answered") {
    body.textContent = query.answer; card.append(body);
    const list = document.createElement("ul"); list.className = "qa-citations"; list.setAttribute("aria-label", "Sources from the transcript");
    for (const citation of citations) {
      const entry = document.createElement("li"); const button = document.createElement("button"); button.type = "button"; button.className = "qa-citation";
      const who = document.createElement("strong"); who.textContent = `${citation.speakers.join(", ")} · ${timestamp(citation.t_start)}`;
      const excerpt = document.createElement("span"); excerpt.textContent = citation.text.replace(/^\[[^\]]*\]\s*/, "").replace(/\n\[[^\]]*\]\s*/g, " … ").slice(0, 160);
      button.append(who, excerpt); button.title = "Show these lines in the transcript"; button.onclick = () => highlightLines(citation.utterance_ids || []);
      entry.append(button); list.append(entry);
    }
    card.append(list);
  } else if (query.status === "no_grounding") {
    body.textContent = "Not discussed in this meeting so far."; card.append(body);
    const detail = document.createElement("small"); detail.className = "qa-detail"; detail.textContent = NO_GROUNDING_DETAIL[reason] || NO_GROUNDING_DETAIL.no_relevant_evidence; card.append(detail);
  } else {
    body.textContent = `Convene could not answer because of a system problem${query.error?.message ? `: ${query.error.message}` : ""}.`; card.append(body);
    const retry = document.createElement("button"); retry.type = "button"; retry.className = "button button-quiet qa-retry"; retry.textContent = "Try again";
    retry.onclick = () => ask(query.question); card.append(retry);
  }
  if (unindexed > 0 && query.status !== "failed") {
    const note = document.createElement("small"); note.className = "qa-detail"; note.textContent = `${unindexed} recent line${unindexed === 1 ? " was" : "s were"} not searchable yet.`; card.append(note);
  }
  return card;
}
function renderAnswers() {
  const fragment = document.createDocumentFragment();
  if (pendingQuestion) {
    const card = document.createElement("li"); card.className = "qa-card pending";
    const head = document.createElement("div"); head.className = "qa-card-head";
    const chip = document.createElement("span"); chip.className = "qa-status"; chip.textContent = "Searching the transcript";
    const elapsed = document.createElement("span"); elapsed.className = "qa-time"; elapsed.textContent = `${Math.floor((Date.now() - pendingQuestion.started) / 1000)}s`;
    head.append(chip, elapsed);
    const question = document.createElement("p"); question.className = "qa-question"; question.textContent = pendingQuestion.question;
    card.append(head, question); fragment.append(card);
  }
  for (const item of orderedAnswers(state)) fragment.append(answerCard(item));
  ui.qaAnswers.replaceChildren(fragment);
  ui.qaEmpty.hidden = Boolean(pendingQuestion) || Object.keys(state.answers).length > 0;
  const ended = state.meeting?.status === "ended";
  ui.qaQuestion.disabled = ended; ui.qaAsk.disabled = ended || Boolean(pendingQuestion);
  ui.qaHint.textContent = ended ? "Live Q&A closes when the meeting ends." : "Enter to ask · the last few seconds may not be searchable yet";
}
async function ask(question) {
  question = question.trim(); if (!question || pendingQuestion) return;
  pendingQuestion = { question, started: Date.now() }; renderAnswers();
  pendingTimer = setInterval(renderAnswers, 1000);
  try {
    const response = await fetch(`/api/meetings/${encodeURIComponent(meetingId)}/qa`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question, mode: "live" }) });
    const body = await json(response);
    if (!response.ok) throw new Error(body.error?.message || "Could not send the question.");
    dispatch({ type: "local_answer", result: body }); ui.qaQuestion.value = "";
  } catch (error) { setError(error.message); } finally { clearInterval(pendingTimer); pendingQuestion = null; renderAnswers(); }
}

function allParticipants() { return Object.values(state.devices).flatMap(device => device.participants || []); }
function openCorrection(id) {
  const utterance = state.utterances[id]; if (!utterance) return; activeUtterance = utterance;
  ui.correctionLine.textContent = `${utterance.speaker_label}: ${utterance.text}`; ui.choices.replaceChildren(); ui.speakerName.value = ""; setCorrectionError();
  for (const participant of allParticipants()) {
    const label = document.createElement("label"); label.className = "participant-choice";
    const radio = document.createElement("input"); radio.type = "radio"; radio.name = "participant"; radio.value = participant.participant_id; radio.checked = participant.participant_id === utterance.participant_id;
    const text = document.createElement("span"); text.textContent = participant.display_name; label.append(radio, text); ui.choices.append(label);
  }
  ui.dialog.showModal();
}
function setCorrectionError(message = "") { ui.correctionError.hidden = !message; ui.correctionError.textContent = message; }
async function correct(event) {
  event.preventDefault(); if (!activeUtterance) return;
  const name = ui.speakerName.value.trim(); const picked = ui.form.querySelector("input[name=participant]:checked");
  if (name && picked) return setCorrectionError("Choose a participant or enter a name, not both.");
  if (!name && !picked) return setCorrectionError("Choose a participant or enter a name.");
  ui.save.disabled = true; setCorrectionError();
  try {
    const response = await fetch(`/api/meetings/${encodeURIComponent(meetingId)}/utterances/${encodeURIComponent(activeUtterance.utterance_id)}/correct`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(name ? { display_name: name } : { participant_id: picked.value }) });
    const body = await json(response); if (!response.ok) throw new Error(body.error?.message || "Could not save correction.");
    dispatch({ type: "local_utterance", utterance: body.utterance }); render({ type: "utterance_updated", utterance: body.utterance }); ui.dialog.close();
  } catch (error) { setCorrectionError(error.message); } finally { ui.save.disabled = false; }
}

async function synchronize() {
  try {
    const [meetingResponse, transcriptResponse] = await Promise.all([fetch(`/api/meetings/${encodeURIComponent(meetingId)}`), fetch(`/api/meetings/${encodeURIComponent(meetingId)}/transcript`)]);
    const [meeting, transcript] = await Promise.all([json(meetingResponse), json(transcriptResponse)]);
    if (!meetingResponse.ok || !transcriptResponse.ok) throw new Error(meeting.error?.message || transcript.error?.message || "Could not load meeting.");
    dispatch({ type: "snapshot", meeting: meeting.meeting, devices: meeting.devices, utterances: transcript.utterances, as_of_seq: Math.min(meeting.as_of_seq, transcript.as_of_seq) });
    ui.join.textContent = meeting.join_url || "No join link available";
    if (meeting.qr_svg) { ui.qr.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(meeting.qr_svg)}`; ui.qr.hidden = false; } else ui.qr.hidden = true;
    for (const event of buffer.sort((a, b) => (a.seq || 0) - (b.seq || 0))) dispatch({ type: "event", event }); buffer = []; synced = true; delay = 500; setError(); setSync(false, "Live"); render();
  } catch (error) { setError(error.message); setSync(true, "Reconnecting"); scheduleReconnect(); }
}
function scheduleReconnect() { if (reconnectTimer) return; reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, delay); delay = Math.min(delay * 2, 10000); }
function handleMessage(message) { if (!synced) { buffer.push(message); return; } dispatch({ type: "event", event: message }); render(message); }
function connect() {
  if (socket?.readyState === WebSocket.OPEN || socket?.readyState === WebSocket.CONNECTING) return;
  synced = false; setSync(true, "Reconnecting");
  socket = new WebSocket(`wss://${location.host}/ws/dashboard/${encodeURIComponent(meetingId)}`);
  socket.onopen = synchronize;
  socket.onmessage = event => { try { handleMessage(JSON.parse(event.data)); } catch { console.warn("Ignored malformed dashboard event"); } };
  socket.onclose = () => { socket = null; synced = false; setSync(true, "Reconnecting"); scheduleReconnect(); };
  socket.onerror = () => socket?.close();
}

ui.form.addEventListener("submit", correct);
ui.qaForm.addEventListener("submit", event => { event.preventDefault(); ask(ui.qaQuestion.value); });
ui.qaQuestion.addEventListener("keydown", event => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); ask(ui.qaQuestion.value); } }); ui.cancel.onclick = () => ui.dialog.close(); ui.speakerName.oninput = () => { if (ui.speakerName.value.trim()) { const picked = ui.form.querySelector("input[name=participant]:checked"); if (picked) picked.checked = false; } }; ui.choices.onchange = () => { ui.speakerName.value = ""; }; ui.newLines.onclick = () => { ui.scroll.scrollTop = ui.scroll.scrollHeight; unread = 0; ui.newLines.hidden = true; };
byId("#summaryLink").href = `/meetings/${encodeURIComponent(meetingId)}`;
ui.end.onclick = async () => { if (!window.confirm("End this meeting? Phones will be disconnected.")) return; ui.end.disabled = true; try { const response = await fetch(`/api/meetings/${encodeURIComponent(meetingId)}/end`, { method: "POST" }); const body = await json(response); if (!response.ok) throw new Error(body.error?.message || "Could not end meeting."); dispatch({ type: "local_meeting", meeting: body.meeting }); render(); renderAnswers(); location.href = `/meetings/${encodeURIComponent(meetingId)}`; } catch (error) { setError(error.message); ui.end.disabled = false; } };
setInterval(() => renderDevices(), 1000);
connect();
