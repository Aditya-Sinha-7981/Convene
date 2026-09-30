const $ = (id) => document.getElementById(id);
const nf = new Intl.NumberFormat();
const date = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" });

const mascotLines = [
  "I’m counting the good ideas and the “let’s circle back”s.",
  "No loose thought left behind. I’ve got the transcript.",
  "A tidy record is my love language. Don’t tell the others.",
  "I kept the receipts — every word, right here on this laptop."
];

function plural(n, word) { return `${nf.format(n)} ${word}${n === 1 ? "" : "s"}`; }
function meetingDate(meeting) { const value = meeting.started_at || meeting.created_at; return value ? date.format(new Date(value)) : "Recently"; }

function renderRecent(meetings) {
  const root = $("recent");
  if (!meetings.length) { root.innerHTML = '<p class="empty-recent">No meetings yet. Start one when your room is ready.</p>'; return; }
  root.replaceChildren(...meetings.map((meeting) => {
    const row = document.createElement("article"); row.className = "recent-row";
    const dot = document.createElement("span"); dot.className = `recent-dot ${meeting.status === "live" ? "live" : ""}`;
    const main = document.createElement("div");
    const title = document.createElement("a"); title.className = "recent-name"; title.href = meeting.status === "ended" ? `/meetings/${encodeURIComponent(meeting.meeting_id)}` : `/dashboard/${encodeURIComponent(meeting.meeting_id)}`; title.textContent = meeting.title || "Untitled meeting";
    const meta = document.createElement("p"); meta.className = "recent-meta"; meta.textContent = `${meetingDate(meeting)} · ${plural(meeting.participant_count, "participant")}${meeting.has_summary ? " · Summary ready" : ""}`;
    main.append(title, meta);
    const count = document.createElement("span"); count.className = "recent-count"; count.textContent = plural(meeting.utterance_count, "line");
    row.append(dot, main, count); return row;
  }));
}

function render(data) {
  const totals = data.totals;
  $("meetingCount").textContent = nf.format(totals.meetings);
  $("wordCount").textContent = nf.format(totals.words);
  $("peopleCount").textContent = nf.format(totals.participants);
  $("actionCount").textContent = nf.format(totals.open_action_items);
  $("meetingDetail").textContent = `${plural(totals.ended_meetings, "meeting")} wrapped up`;
  $("utteranceCount").textContent = nf.format(totals.utterances);
  $("recordState").textContent = totals.meetings ? `${plural(totals.meetings, "meeting")} stored` : "Fresh and ready";
  $("liveState").textContent = totals.live_meetings ? `${plural(totals.live_meetings, "room")} live now` : "No room live now";
  $("utteranceState").textContent = plural(totals.utterances, "line");
  $("mascotLine").textContent = mascotLines[totals.words % mascotLines.length];
  renderRecent(data.recent_meetings);
}

async function load() {
  try {
    const response = await fetch("/api/overview");
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error?.message || "Could not load the dashboard.");
    render(body);
  } catch (error) { const notice = $("error"); notice.hidden = false; notice.textContent = error.message; }
}

load();
