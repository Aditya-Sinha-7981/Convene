// Post-meeting view (CON-10, minimal): summary status, summary text, action items, staleness with regenerate, and
// the transcript, which stays readable whatever happened to the summary. Everything shown comes from the server:
// labels, owner names and staleness are never computed here (docs/frontend.md). CON-14 adds the participants and
// a history-mode Q&A box scoped to this meeting, open once the meeting has ended.
import { mountHistoryQA } from "/static/history_qa.js";
import { confirmDelete, editTitle } from "/static/meeting_actions.js";
import { actionItemRow } from "/static/action_item_row.js";

const meetingId = decodeURIComponent(location.pathname.split("/").filter(Boolean).pop());
const api = `/api/meetings/${encodeURIComponent(meetingId)}`;
const $ = (id) => document.getElementById(id);
let meeting = null, participants = [], busy = false, refreshTimer = null, pollTimer = null, exportState = null;
let mailState = null, mailSending = false, mailFailures = new Map(), summaryReady = false;

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
  const ended = meeting.status === "ended";
  $("status").textContent = ended ? "Meeting ended" : "Meeting in progress";
  $("live").hidden = ended;
  $("live").href = `/dashboard/${encodeURIComponent(meetingId)}`;
  // History Q&A only reads ended meetings; a running meeting's questions belong to the live dashboard.
  $("qaLive").hidden = ended;
  $("qaBox").hidden = !ended;
  $("qaLiveLink").href = `/dashboard/${encodeURIComponent(meetingId)}`;
  qa.refreshScope();
}

function renderPeople() {
  const list = $("people");
  list.replaceChildren();
  for (const person of participants) {
    const li = document.createElement("li");
    li.dataset.color = person.color || "unknown";
    li.textContent = person.display_name || "Unnamed";
    list.append(li);
  }
  $("noPeople").hidden = participants.length > 0;
}

// A citation for this meeting highlights the lines here instead of reloading the page.
function highlightLines(ids) {
  const found = ids.map((id) => document.getElementById(`u-${id}`)).filter(Boolean);
  if (!found.length) return false;
  found[0].scrollIntoView({ behavior: "smooth", block: "center" });
  for (const line of found) { line.classList.remove("cited"); void line.offsetWidth; line.classList.add("cited"); setTimeout(() => line.classList.remove("cited"), 3200); }
  return true;
}

const qa = mountHistoryQA({
  root: $("qa"),
  scope: () => ({ meeting_ids: [meetingId], label: "This meeting only", ready: meeting?.status === "ended" }),
  onCitation: (citation) => citation.meeting_id === meetingId && highlightLines(citation.utterance_ids || []),
});
let jumped = false;

function renderSummary(body) {
  const latest = body?.latest_attempt || null, current = body?.summary || null;
  summaryReady = current !== null;  // sending renders the DOCX if needed, so a ready summary is enough
  if (mailState) renderMail();
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

  // CON-16: editable rows. Owners come from this meeting's participants; a note added here comes from this meeting.
  $("actions").replaceChildren(...(body?.action_items || []).map((item) => actionItemRow(item, {
    participants: async () => participants,
    noteSources: async () => [{ meeting_id: meetingId, title: meeting?.title }],
    onChange: refreshExport,  // an owner or status edit makes the DOCX out of date
  })));
  $("noActions").hidden = !current || (body.action_items || []).length > 0;

  clearTimeout(pollTimer);  // a pushed event normally arrives first; this covers a dropped socket
  if (latest?.status === "pending") pollTimer = setTimeout(refresh, 3000);
}

function renderExport(body) {
  exportState = body;
  const latest = body?.latest_attempt || null, current = body?.export || null;
  $("exportPending").hidden = latest?.status !== "pending";
  $("exportFailed").hidden = latest?.status !== "failed";
  $("exportFailedText").textContent = latest?.status === "failed" ? `Export failed: ${latest.error_message}.` : "";
  $("exportStale").hidden = !(current && body.stale);
  $("exportNone").hidden = current !== null || latest?.status === "pending";
  $("download").hidden = current === null;
  $("download").href = `${api}/export?format=docx`;
}

// ADR-33/34: email the DOCX to the people who added an address at join, each with the parts chosen for them. The
// server decides who can get it and whether it can send; this page holds only the choice (who is ticked, and which
// parts each person gets). Addresses arrive masked, since this page may be on the projector.
const PARTS = [["summary", "Summary"], ["action_items", "Action items"], ["transcript", "Transcript"]];
const PRESETS = [["Everything", ["summary", "action_items", "transcript"]], ["Summary + action items", ["summary", "action_items"]],
                 ["Action items only", ["action_items"]]];
const mailChoice = new Map();  // participant_id -> { selected, sections: Set }; new rows start ticked with everything

function choiceFor(id) {
  if (!mailChoice.has(id)) mailChoice.set(id, { selected: true, sections: new Set(PARTS.map(([key]) => key)) });
  return mailChoice.get(id);
}

function checkbox(checked, label, onChange, className) {
  const wrap = document.createElement("label");
  wrap.className = className;
  const input = document.createElement("input");
  input.type = "checkbox"; input.checked = checked; input.disabled = mailSending;
  input.onchange = () => { onChange(input.checked); renderMail(); };
  const text = document.createElement("span"); text.textContent = label;
  wrap.append(input, text);
  return wrap;
}

function renderMail() {
  const state = mailState;
  const intro = $("mailIntro"), send = $("mailSend"), list = $("mailList"), bulk = $("mailBulk");
  list.replaceChildren(); bulk.replaceChildren();
  if (!state) { intro.textContent = "Could not check who asked for the minutes."; send.hidden = true; bulk.hidden = true; return; }
  const people = state.recipients;
  const withEmail = new Set(people.map((p) => p.participant_id));
  for (const person of people) {
    const choice = choiceFor(person.participant_id);
    const li = document.createElement("li");
    li.classList.toggle("is-selected", choice.selected);
    const pick = checkbox(choice.selected, person.display_name, (on) => { choice.selected = on; }, "pick");
    pick.querySelector("span").className = "who";
    const addr = document.createElement("span"); addr.className = "addr"; addr.textContent = person.email_masked;
    const parts = document.createElement("div"); parts.className = "parts";
    parts.setAttribute("role", "group"); parts.setAttribute("aria-label", `Parts for ${person.display_name}`);
    for (const [key, label] of PARTS) {
      parts.append(checkbox(choice.sections.has(key), label, (on) => { on ? choice.sections.add(key) : choice.sections.delete(key); }, "part"));
    }
    parts.hidden = !choice.selected;
    const sent = document.createElement("span"); sent.className = "sent";
    const failure = mailFailures.get(person.participant_id);
    if (failure) { sent.classList.add("is-failed"); sent.textContent = `Not sent: ${failure}`; }
    else sent.textContent = person.last_sent_at ? `Sent ${new Date(person.last_sent_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}` : "Not sent yet";
    if (choice.selected && !choice.sections.size) { sent.classList.add("is-failed"); sent.textContent = "Pick at least one part"; }
    li.append(pick, addr, sent, parts);
    list.append(li);
  }
  // Everyone else in the meeting, so the count is complete; they cannot be picked without an address.
  for (const person of participants.filter((p) => !withEmail.has(p.participant_id))) {
    const li = document.createElement("li"); li.className = "is-unavailable";
    const who = document.createElement("span"); who.className = "who"; who.textContent = person.display_name || "Unnamed";
    const why = document.createElement("span"); why.className = "addr"; why.textContent = "didn't add an email";
    li.append(who, why);
    list.append(li);
  }

  const chosen = people.filter((p) => choiceFor(p.participant_id).selected);
  const empty = chosen.filter((p) => !choiceFor(p.participant_id).sections.size);
  let reason = "";
  if (!state.configured) reason = "Email isn't set up on this laptop. Add RESEND_API_KEY and CONVENE_MAIL_FROM to mail.env and restart Convene.";
  else if (!people.length) reason = "No one added an email address when they joined.";
  else if (!state.meeting_ended) reason = "You can send the minutes once the meeting ends.";
  else if (!summaryReady) reason = "You can send the minutes once the summary is ready.";
  const total = participants.length === 1 ? "1 participant" : `${participants.length} participants`;
  const asked = people.length === 1 ? "1 added an email" : `${people.length} added an email`;
  intro.textContent = reason || `${total} · ${asked}. Tick people, choose what each one gets, then send. Each gets their own email from ${state.sender}.`;

  // Presets apply to everyone ticked; each person's parts can still be changed one by one.
  bulk.hidden = !people.length || !state.configured;
  if (!bulk.hidden) {
    const label = document.createElement("span"); label.className = "bulk-label";
    label.textContent = chosen.length ? `For the ${chosen.length} ticked:` : "Tick someone to choose what they get.";
    bulk.append(label);
    for (const [name, keys] of PRESETS) {
      const button = document.createElement("button"); button.type = "button"; button.textContent = name;
      button.disabled = !chosen.length || mailSending;
      button.onclick = () => { for (const p of chosen) choiceFor(p.participant_id).sections = new Set(keys); renderMail(); };
      bulk.append(button);
    }
    const all = document.createElement("button"); all.type = "button"; all.className = "link";
    const everyone = chosen.length === people.length;
    all.textContent = everyone ? "Untick all" : "Tick all";
    all.disabled = mailSending;
    all.onclick = () => { for (const p of people) choiceFor(p.participant_id).selected = !everyone; renderMail(); };
    bulk.append(all);
  }

  send.hidden = !state.configured || !people.length;
  send.disabled = Boolean(reason) || mailSending || state.sending || !chosen.length || empty.length > 0;
  const who = chosen.length === 1 ? "1 person" : `${chosen.length} people`;
  send.textContent = mailSending || state.sending ? "Sending…" : chosen.length ? `Send to ${who}` : "Send minutes by email";
}

async function refreshMail() {
  try {
    const response = await fetch(`${api}/email`);
    mailState = response.ok ? await json(response) : null;
  } catch { mailState = null; }
  renderMail();
}

async function sendMail() {
  if (!mailState || mailSending) return;
  const chosen = mailState.recipients.filter((p) => choiceFor(p.participant_id).selected);
  if (chosen.some((p) => p.last_sent_at) && !confirm("Some of the ticked people already got the minutes. Send to them again?")) return;
  const recipients = chosen.map((p) => ({ participant_id: p.participant_id,
    sections: PARTS.map(([key]) => key).filter((key) => choiceFor(p.participant_id).sections.has(key)) }));
  mailSending = true;
  $("mailStatus").className = "";
  $("mailStatus").textContent = "Sending… this needs an internet connection.";
  renderMail();
  try {
    const response = await fetch(`${api}/email`, { method: "POST", headers: { "Content-Type": "application/json" },
                                                    body: JSON.stringify({ recipients }) });
    const body = await json(response);
    if (!response.ok) throw new Error(body.error?.message || "Could not send the minutes.");
    mailFailures = new Map(body.failed.map((f) => [f.participant_id, f.error]));
    mailState = { ...mailState, recipients: body.recipients };
    const total = body.sent_count + body.failed.length;
    $("mailStatus").className = body.failed.length ? "is-failed" : "";
    $("mailStatus").textContent = body.failed.length ? `Sent to ${body.sent_count} of ${total}. The addresses that failed are marked above.` : `Sent to ${total === 1 ? "1 person" : `all ${total}`}.`;
  } catch (error) {
    $("mailStatus").className = "is-failed";
    $("mailStatus").textContent = error.message;
  }
  mailSending = false;
  await refreshExport();  // sending may have rendered a fresh DOCX
  await refreshMail();
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
    li.id = `u-${utterance.utterance_id}`;
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
  // A citation link from the history view lands here as #u-<utterance_id>.
  if (!jumped && location.hash.startsWith("#u-")) { jumped = true; highlightLines([decodeURIComponent(location.hash.slice(3))]); }
}

async function refresh() {
  try {
    const [detail, transcript, summary, exported] = await Promise.all([fetch(api), fetch(`${api}/transcript`), fetch(`${api}/summary`), fetch(`${api}/export/status`)]);
    const detailBody = await json(detail);
    if (!detail.ok) throw new Error(detailBody.error?.message || "Could not load the meeting.");
    meeting = detailBody.meeting;
    participants = detailBody.devices.flatMap((device) => device.participants);
    renderMeeting();
    renderPeople();
    const transcriptBody = await json(transcript);
    if (transcript.ok) renderTranscript(transcriptBody.utterances);
    const summaryBody = await json(summary);
    if (summary.ok) renderSummary(summaryBody);
    else if (summaryBody.error?.code === "summary_not_found") renderSummary(null);
    else throw new Error(summaryBody.error?.message || "Could not load the summary.");
    const exportBody = await json(exported);
    if (exported.ok) renderExport(exportBody); else renderExport(null);
    await refreshMail();
  } catch (error) { setError(error.message); }
}

async function refreshExport() {
  try {
    const response = await fetch(`${api}/export/status`);
    if (response.ok) renderExport(await json(response));
  } catch { /* the next full refresh shows it */ }
}

async function summarize() {
  // A new summary brings a fresh list of action items; edits to the current ones do not carry over (ADR-28).
  if ($("actions").children.length && !confirm("Regenerating writes a new list of action items. Edits and notes on the current items will not carry over. Continue?")) return;
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
    if (["summary_ready", "summary_failed", "export_ready", "export_failed", "utterance", "utterance_updated", "meeting_status"].includes(event.type)) scheduleRefresh();
  };
  socket.onclose = () => setTimeout(() => { listen(); refresh(); }, 2000);
}

$("rename").onclick = () => {
  if (!meeting) return;
  $("rename").hidden = true;
  editTitle($("title"), meeting, (updated) => { $("rename").hidden = false; if (updated) { meeting = updated; renderMeeting(); } });
};
$("delete").onclick = async () => {
  if (!meeting) return;
  if (await confirmDelete(meeting)) location.href = "/history";
};
$("retry").onclick = summarize;
$("regenerate").onclick = summarize;
$("summarize").onclick = summarize;
$("mailSend").onclick = sendMail;
$("exportRetry").onclick = () => { $("download").click(); };
refresh().then(listen);
