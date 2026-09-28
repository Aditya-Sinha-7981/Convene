// Global action-item list (CON-16). The server filters, sorts, pages and decides what is overdue; this page only
// sends the chosen filters and renders the rows it gets back (client/action_item_row.js does the edits).
import { actionItemRow } from "/static/action_item_row.js";

const $ = (id) => document.getElementById(id);
const PAGE = 50;
const people = new Map();  // meeting_id -> Promise of that meeting's participants
let rows = [], total = 0, loading = null, filterTimer = null, sources = null;

async function getJson(url) {
  const response = await fetch(url);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error?.message || "The request failed.");
  return body;
}

// An owner must be a participant of the item's own meeting (ADR-28), so each meeting's roster is read once.
function participants(item) {
  if (!people.has(item.meeting_id)) {
    people.set(item.meeting_id, getJson(`/api/meetings/${encodeURIComponent(item.meeting_id)}`)
      .then((body) => body.devices.flatMap((device) => device.participants))
      .catch((error) => { people.delete(item.meeting_id); throw error; }));
  }
  return people.get(item.meeting_id);
}

// A note comes from an ended meeting, newest first.
function noteSources() {
  sources ??= getJson("/api/meetings?status=ended&limit=200")
    .then((body) => body.meetings)
    .catch((error) => { sources = null; throw error; });
  return sources;
}

function query(offset) {
  const params = new URLSearchParams({ status: $("status").value, sort: $("sort").value, limit: String(PAGE), offset: String(offset) });
  const owner = $("owner").value.trim();
  if (owner) params.set("owner", owner);
  if ($("dueAfter").value) params.set("due_after", $("dueAfter").value);
  if ($("dueBefore").value) params.set("due_before", $("dueBefore").value);
  if ($("overdue").checked) params.set("overdue", "true");
  return `/api/action-items?${params}`;
}

function row(item) {
  return actionItemRow(item, { participants, noteSources, showMeeting: true });
}

// "Show more" appends rows; only a filter change redraws the list, so an open note form survives paging.
function render(added) {
  if (added) $("items").append(...added.map(row)); else $("items").replaceChildren(...rows.map(row));
  const filtered = $("status").value !== "open" || Boolean($("owner").value.trim() || $("dueAfter").value || $("dueBefore").value || $("overdue").checked);
  $("count").textContent = total === 0 ? "" : `${total} item${total === 1 ? "" : "s"}`;
  $("empty").hidden = rows.length > 0;
  $("emptyText").textContent = filtered ? "No action items match these filters." : "Nothing open. Action items appear here once a meeting's summary is written.";
  $("more").hidden = rows.length >= total;
}

async function load(append = false) {
  const url = query(append ? rows.length : 0);
  loading = url;
  try {
    const body = await getJson(url);
    if (loading !== url) return;  // a newer filter already replaced this request
    rows = append ? rows.concat(body.action_items) : body.action_items;
    total = body.total;
    $("error").textContent = "";
    render(append ? body.action_items : null);
  } catch (error) { $("error").textContent = error.message; }
}

$("filters").addEventListener("submit", (event) => event.preventDefault());
$("filters").addEventListener("input", () => { clearTimeout(filterTimer); filterTimer = setTimeout(() => load(), 200); });
$("more").onclick = () => load(true);
load();
