// Meeting history view (CON-14): list, filter and select past meetings, and ask across all or the selected ones.
// Only ended meetings can be selected; the scope label above the question box always says what will be searched.
import { meetingDate, mountHistoryQA } from "/static/history_qa.js";
import { confirmDelete, editTitle } from "/static/meeting_actions.js";

const $ = (id) => document.getElementById(id);
const PAGE = 50, MAX_SELECTED = 50;
const selected = new Map();  // meeting_id -> title; kept across filter changes
let rows = [], total = 0, loading = null, filterTimer = null;

const STATUS = { ended: "Ended", live: "Live", created: "Not started" };

function mode() { return document.querySelector("input[name=scope]:checked").value; }

function scope() {
  if (mode() === "all") return { meeting_ids: null, label: "All ended meetings", ready: true };
  const n = selected.size;
  if (n === 0) return { meeting_ids: [], label: "No meetings selected", ready: false, hint: "Tick one or more ended meetings in the list." };
  if (n > MAX_SELECTED) return { meeting_ids: [], label: `${n} meetings selected`, ready: false, hint: `Select at most ${MAX_SELECTED} meetings.` };
  const label = n === 1 ? `Searching “${[...selected.values()][0] || "Untitled meeting"}”` : `Searching ${n} meetings`;
  return { meeting_ids: [...selected.keys()], label, ready: true };
}

const qa = mountHistoryQA({ root: $("qa"), scope });

function query(offset) {
  const params = new URLSearchParams({ limit: String(PAGE), offset: String(offset) });
  const q = $("q").value.trim();
  if (q) params.set("q", q);
  if ($("from").value) params.set("from", $("from").value);
  if ($("to").value) params.set("to", $("to").value);
  return `/api/meetings?${params}`;
}

function badge(text, className) {
  const span = document.createElement("span");
  span.className = `hx-badge ${className || ""}`;
  span.textContent = text;
  return span;
}

function row(meeting) {
  const li = document.createElement("li");
  li.className = "hx-row";
  const ended = meeting.status === "ended";
  const pick = document.createElement("input");
  pick.type = "checkbox";
  pick.className = "hx-pick";
  pick.disabled = !ended;
  pick.checked = selected.has(meeting.meeting_id);
  pick.setAttribute("aria-label", ended ? `Include ${meeting.title || "Untitled meeting"} in the search` : "Only ended meetings can be searched here");
  pick.onchange = () => {
    if (pick.checked) selected.set(meeting.meeting_id, meeting.title); else selected.delete(meeting.meeting_id);
    if (pick.checked && mode() === "all") document.querySelector("input[name=scope][value=selected]").checked = true;
    renderSelection();
  };

  const main = document.createElement("div");
  main.className = "hx-main";
  const title = document.createElement("a");
  title.className = "hx-title";
  title.href = ended ? `/meetings/${encodeURIComponent(meeting.meeting_id)}` : `/dashboard/${encodeURIComponent(meeting.meeting_id)}`;
  title.textContent = meeting.title || "Untitled meeting";
  const meta = document.createElement("p");
  meta.className = "hx-meta";
  const people = meeting.participant_count;
  meta.textContent = `${meetingDate(meeting.started_at || meeting.created_at)} · ${people} participant${people === 1 ? "" : "s"}`;
  main.append(title, meta);

  const badges = document.createElement("div");
  badges.className = "hx-badges";
  badges.append(badge(STATUS[meeting.status] || meeting.status, `is-${meeting.status}`));
  if (meeting.has_summary) badges.append(badge("Summary"));
  if (meeting.has_export) badges.append(badge("DOCX"));

  const actions = document.createElement("div");
  actions.className = "hx-actions";
  const rename = document.createElement("button");
  rename.type = "button";
  rename.className = "hx-link";
  rename.textContent = "Rename";
  rename.onclick = () => editTitle(title, meeting, (updated) => {
    if (!updated) return;
    Object.assign(meeting, updated);
    if (selected.has(meeting.meeting_id)) selected.set(meeting.meeting_id, meeting.title);
    render();
  });
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "hx-link hx-danger";
  remove.textContent = "Delete";
  remove.onclick = async () => {
    if (!(await confirmDelete(meeting))) return;
    rows = rows.filter((item) => item.meeting_id !== meeting.meeting_id);
    selected.delete(meeting.meeting_id);
    total = Math.max(0, total - 1);
    render();
  };
  actions.append(rename, remove);
  badges.append(actions);
  li.append(pick, main, badges);
  return li;
}

function renderSelection() {
  $("clearSel").hidden = selected.size === 0;
  $("clearSel").textContent = `Clear selection (${selected.size})`;
  qa.refreshScope();
}

function render() {
  $("meetings").replaceChildren(...rows.map(row));
  const filtered = Boolean($("q").value.trim() || $("from").value || $("to").value);
  $("count").textContent = total === 0 ? "" : `${total} meeting${total === 1 ? "" : "s"}${filtered ? " match" : ""}`;
  $("empty").hidden = rows.length > 0;
  $("emptyText").textContent = filtered ? "No meetings match these filters." : "No meetings yet. Start one and it will appear here once it ends.";
  $("more").hidden = rows.length >= total;
  renderSelection();
}

async function load(append = false) {
  const url = query(append ? rows.length : 0);
  loading = url;
  try {
    const response = await fetch(url);
    const body = await response.json().catch(() => ({}));
    if (loading !== url) return;  // a newer filter already replaced this request
    if (!response.ok) throw new Error(body.error?.message || "Could not load meetings.");
    rows = append ? rows.concat(body.meetings) : body.meetings;
    total = body.total;
    $("error").textContent = "";
    render();
  } catch (error) { $("error").textContent = error.message; }
}

$("filters").addEventListener("submit", (event) => event.preventDefault());
$("filters").addEventListener("input", () => { clearTimeout(filterTimer); filterTimer = setTimeout(() => load(), 200); });
$("more").onclick = () => load(true);
$("clearSel").onclick = () => { selected.clear(); render(); };
for (const radio of document.querySelectorAll("input[name=scope]")) radio.onchange = () => qa.refreshScope();
load();
