// Post-meeting view (CON-10, minimal): summary status, summary text, action items, staleness with regenerate, and
// the transcript, which stays readable whatever happened to the summary. Everything shown comes from the server:
// labels, owner names and staleness are never computed here (docs/frontend.md).
const meetingId = decodeURIComponent(location.pathname.split("/").filter(Boolean).pop());
const api = `/api/meetings/${encodeURIComponent(meetingId)}`;
const $ = (id) => document.getElementById(id);
let meeting = null, participants = [], busy = false, refreshTimer = null, pollTimer = null;

async function json(response) {
  try { return await response.json(); } catch { return {}; }
}

function setError(message) { $("error").textContent = message || ""; }

function clock(start, t) {
  const seconds = Math.max(0, Math.floor((Date.parse(t) - Date.parse(start)) / 1000));
  return [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60].map((n) => String(n).padStart(2, "0")).join(":");
}

function renderMeeting() {
  $("title").textContent = meeting.title || "Untitled meeting";
  document.title = `Convene — ${meeting.title || "Meeting summary"}`;
  $("status").textContent = meeting.status === "ended" ? "Meeting ended" : "Meeting in progress";
  $("live").hidden = meeting.status === "ended";
  $("live").href = `/dashboard/${encodeURIComponent(meetingId)}`;
}

function renderSummary(body) {
  const latest = body?.latest_attempt || null, current = body?.summary || null;
  $("none").hidden = latest !== null;
  $("pending").hidden = latest?.status !== "pending";
  $("failed").hidden = latest?.status !== "failed";
  $("failedText").textContent = latest?.status === "failed"
    ? `The latest summary attempt failed: ${latest.error_message}.${current ? " The previous summary is shown below." : ""} The transcript is not affected.`
    : "";
  $("stale").hidden = !(current && body.stale && latest?.status !== "pending");
  for (const button of [$("retry"), $("regenerate"), $("summarize")]) button.disabled = busy || latest?.status === "pending";

  const text = $("summaryText");
  text.replaceChildren();
  for (const paragraph of (current?.summary_text || "").split(/\n\s*\n/).filter((p) => p.trim())) {
    const p = document.createElement("p");
    p.textContent = paragraph.trim();
    text.append(p);
  }
  $("generated").textContent = current ? `Generated ${new Date(current.generated_at).toLocaleString()} by ${current.model_identifier}` : "";

  const list = $("actions");
  list.replaceChildren();
  for (const item of body?.action_items || []) {
    const li = document.createElement("li");
    const owner = document.createElement("span");
    owner.className = "owner";
    owner.textContent = ` — ${item.owner_display_name || "Unassigned"}`;
    li.append(item.text, owner);
    list.append(li);
  }
  $("noActions").hidden = !current || (body.action_items || []).length > 0;

  clearTimeout(pollTimer);  // a pushed event normally arrives first; this covers a dropped socket
  if (latest?.status === "pending") pollTimer = setTimeout(refresh, 3000);
}

function correctionForm(utterance, row) {
  const form = document.createElement("form");
  form.className = "fix";
  const select = document.createElement("select");
  select.setAttribute("aria-label", "Speaker");
  for (const person of participants) {
    const option = new Option(person.display_name, person.participant_id, false, person.participant_id === utterance.participant_id);
    select.append(option);
  }
  select.append(new Option("New name…", ""));
  const name = document.createElement("input");
  name.placeholder = "Name";
  name.maxLength = 60;
  name.hidden = select.value !== "";
  select.onchange = () => { name.hidden = select.value !== ""; };
  const save = document.createElement("button");
  save.textContent = "Save";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.textContent = "Cancel";
  cancel.onclick = () => form.remove();
  form.onsubmit = async (event) => {
    event.preventDefault();
    const target = select.value ? { participant_id: select.value } : { display_name: name.value.trim() };
    if (!select.value && !target.display_name) return;
    save.disabled = true;
    try {
      const response = await fetch(`${api}/utterances/${encodeURIComponent(utterance.utterance_id)}/correct`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(target) });
      const body = await json(response);
      if (!response.ok) throw new Error(body.error?.message || "Could not correct the speaker.");
      setError("");
      await refresh();
    } catch (error) { setError(error.message); save.disabled = false; }
  };
  form.append(select, name, save, cancel);
  row.append(form);
}

function renderTranscript(lines) {
  const start = meeting.started_at || lines[0]?.t_start;
  const list = $("transcript");
  list.replaceChildren();
  for (const utterance of lines) {
    const li = document.createElement("li");
    const when = document.createElement("span");
    when.className = "when";
    when.textContent = clock(start, utterance.t_start);
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = utterance.speaker_label;
    li.append(when, who, utterance.text);
    if (utterance.corrected) { const tag = document.createElement("span"); tag.className = "tag"; tag.textContent = "corrected"; li.append(tag); }
    else if (utterance.low_confidence) { const tag = document.createElement("span"); tag.className = "tag"; tag.textContent = "needs review"; li.append(tag); }
    const fix = document.createElement("button");
    fix.type = "button";
    fix.className = "link";
    fix.textContent = "Change speaker";
    fix.onclick = () => { if (!li.querySelector("form")) correctionForm(utterance, li); };
    li.append(fix);
    list.append(li);
  }
  $("noLines").hidden = lines.length > 0;
}

async function refresh() {
  try {
    const [detail, transcript, summary] = await Promise.all([fetch(api), fetch(`${api}/transcript`), fetch(`${api}/summary`)]);
    const detailBody = await json(detail);
    if (!detail.ok) throw new Error(detailBody.error?.message || "Could not load the meeting.");
    meeting = detailBody.meeting;
    participants = detailBody.devices.flatMap((device) => device.participants);
    renderMeeting();
    const transcriptBody = await json(transcript);
    if (transcript.ok) renderTranscript(transcriptBody.utterances);
    const summaryBody = await json(summary);
    if (summary.ok) renderSummary(summaryBody);
    else if (summaryBody.error?.code === "summary_not_found") renderSummary(null);
    else throw new Error(summaryBody.error?.message || "Could not load the summary.");
  } catch (error) { setError(error.message); }
}

async function summarize() {
  busy = true;
  document.querySelectorAll("#retry, #regenerate, #summarize").forEach((button) => { button.disabled = true; });
  try {
    const response = await fetch(`${api}/summarize`, { method: "POST" });
    const body = await json(response);
    if (!response.ok && body.error?.code !== "summary_in_progress") throw new Error(body.error?.message || "Could not start the summary.");
    setError("");
  } catch (error) { setError(error.message); }
  busy = false;
  await refresh();
}

function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(refresh, 150);
}

function listen() {
  const socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/dashboard/${encodeURIComponent(meetingId)}`);
  socket.onmessage = (message) => {
    const event = JSON.parse(message.data);
    if (["summary_ready", "summary_failed", "utterance", "utterance_updated", "meeting_status"].includes(event.type)) scheduleRefresh();
  };
  socket.onclose = () => setTimeout(() => { listen(); refresh(); }, 2000);
}

$("retry").onclick = summarize;
$("regenerate").onclick = summarize;
$("summarize").onclick = summarize;
refresh().then(listen);
