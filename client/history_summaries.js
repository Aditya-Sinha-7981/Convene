// Summaries of several meetings at once, on the history page: the stored summary of each ticked meeting, oldest
// first, under its title. Read-only over GET /api/meetings/{id}/summary; a meeting without a summary can be
// summarized from here with the existing POST …/summarize. "Summarize all missing" starts them one after another
// (the local model writes one at a time anyway), so progress is visible and a failure stops nothing else.
import { meetingDate } from "/static/history_qa.js";

const POLL_MS = 3000;

async function json(response) {
  try { return await response.json(); } catch { return {}; }
}

export function mountSummaries({ root, selection, onSummarized = () => {} }) {
  const $ = (name) => root.querySelector(`[data-sum-${name}]`);
  const cache = new Map();  // meeting_id -> { state: loading|ready|none|pending|failed|empty|error, ... }
  let queue = [], current = null, running = false, pollTimer = null;

  // One meeting's summary state, from the server. A 404 summary_not_found is simply "no summary yet".
  async function load(id) {
    try {
      const response = await fetch(`/api/meetings/${encodeURIComponent(id)}/summary`);
      const body = await json(response);
      if (response.status === 404 && body.error?.code === "summary_not_found") return { state: "none" };
      if (!response.ok) return { state: "error", message: body.error?.message || "Could not load the summary." };
      const current = body.summary, latest = body.latest_attempt;
      if (latest?.status === "pending") return { state: "pending", current };
      if (current) return { state: "ready", current, stale: body.stale, failedAfter: latest?.status === "failed" ? latest.error_message : null };
      return { state: "failed", message: latest?.error_message || "the summary could not be written" };
    } catch { return { state: "error", message: "Could not reach Convene." }; }
  }

  async function fetchMissing() {
    const ids = selection().map((m) => m.meeting_id).filter((id) => !cache.has(id));
    ids.forEach((id) => cache.set(id, { state: "loading" }));
    if (ids.length) render();
    await Promise.all(ids.map(async (id) => { cache.set(id, await load(id)); }));
    if (ids.length) render();
  }

  // Poll only while something visible is being written.
  function schedulePoll() {
    clearTimeout(pollTimer);
    const pending = selection().filter((m) => cache.get(m.meeting_id)?.state === "pending");
    if (!pending.length) return;
    pollTimer = setTimeout(async () => {
      await Promise.all(pending.map(async (m) => { cache.set(m.meeting_id, await load(m.meeting_id)); }));
      if (pending.some((m) => cache.get(m.meeting_id)?.state === "ready")) onSummarized();  // e.g. the list's badges
      render();
      advance();
    }, POLL_MS);
  }

  async function start(id) {
    cache.set(id, { state: "pending" });
    render();
    try {
      const response = await fetch(`/api/meetings/${encodeURIComponent(id)}/summarize`, { method: "POST" });
      const body = await json(response);
      if (response.status === 409 && body.error?.code === "transcript_empty") cache.set(id, { state: "empty" });
      else if (!response.ok && body.error?.code !== "summary_in_progress") cache.set(id, { state: "error", message: body.error?.message || "Could not start the summary." });
    } catch { cache.set(id, { state: "error", message: "Could not reach Convene." }); }
    render();
  }

  // The bulk queue: one meeting at a time. Each id is tried once; a failure is shown on its card and the queue moves on.
  async function advance() {
    if (!running || (current && cache.get(current)?.state === "pending")) return;
    current = null;
    const chosen = new Set(selection().map((m) => m.meeting_id));
    while (queue.length) {
      const id = queue.shift();
      if (!chosen.has(id) || !["none", "failed"].includes(cache.get(id)?.state)) continue;
      current = id;
      await start(id);
      if (cache.get(id)?.state === "pending") return;  // the poll calls advance again when it is done
      current = null;
    }
    running = false;
    render();
  }

  function card(meeting) {
    const entry = cache.get(meeting.meeting_id) || { state: "loading" };
    const article = document.createElement("article");
    article.className = `hs-card is-${entry.state}`;
    const head = document.createElement("header");
    const title = document.createElement("a");
    title.className = "hs-title";
    title.href = `/meetings/${encodeURIComponent(meeting.meeting_id)}`;
    title.textContent = meeting.title || "Untitled meeting";
    const meta = document.createElement("p");
    meta.className = "hs-meta";
    const people = meeting.participant_count;
    meta.textContent = `${meetingDate(meeting.started_at || meeting.created_at)}${people == null ? "" : ` · ${people} participant${people === 1 ? "" : "s"}`}`;
    head.append(title, meta);
    article.append(head);

    const note = (text, className = "hs-note") => { const p = document.createElement("p"); p.className = className; p.textContent = text; article.append(p); };
    const action = (label, onClick) => {
      const button = document.createElement("button");
      button.type = "button"; button.className = "hs-action"; button.textContent = label;
      button.disabled = running; button.onclick = onClick;
      article.append(button);
    };
    if (entry.current) {
      for (const text of entry.current.summary_text.split(/\n\s*\n/).filter((p) => p.trim())) note(text.trim(), "hs-text");
    }
    if (entry.state === "loading") note("Loading…");
    else if (entry.state === "pending") note(entry.current ? "Writing a newer summary…" : "Writing the summary… this can take up to a minute.", "hs-note is-busy");
    else if (entry.state === "none") { note("No summary yet."); action("Summarize now", () => start(meeting.meeting_id)); }
    else if (entry.state === "empty") note("Nothing was transcribed, so there is nothing to summarize.");
    else if (entry.state === "failed") { note(`The summary could not be written: ${entry.message}.`, "hs-note is-failed"); action("Try again", () => start(meeting.meeting_id)); }
    else if (entry.state === "error") note(entry.message, "hs-note is-failed");
    else if (entry.stale) note("The transcript changed after this summary was written. Open the meeting to regenerate it.", "hs-note is-stale");
    if (entry.state === "ready" && entry.failedAfter) note(`A newer attempt failed (${entry.failedAfter}); this is the previous summary.`, "hs-note is-stale");
    return article;
  }

  function render() {
    const meetings = selection().slice().sort((a, b) => (a.started_at || a.created_at).localeCompare(b.started_at || b.created_at));
    const list = $("list");
    list.replaceChildren(...meetings.map(card));
    $("empty").hidden = meetings.length > 0;
    const missing = meetings.filter((m) => ["none", "failed"].includes(cache.get(m.meeting_id)?.state));
    const ready = meetings.filter((m) => cache.get(m.meeting_id)?.current);
    $("scope").textContent = meetings.length ? `${meetings.length} meeting${meetings.length === 1 ? "" : "s"} · ${ready.length} with a summary` : "No meetings selected";
    const all = $("all");
    all.hidden = !missing.length && !running;
    all.disabled = running;
    all.textContent = running ? "Summarizing one by one…" : `Summarize ${missing.length === 1 ? "the 1" : `all ${missing.length}`} without a summary`;
    $("copy").disabled = !ready.length;
    // The same meetings as one DOCX, built by the server from stored rows (GET /api/summaries/download).
    const download = $("download");
    if (meetings.length) {
      download.href = `/api/summaries/download?meeting_ids=${meetings.map((m) => encodeURIComponent(m.meeting_id)).join(",")}`;
      download.removeAttribute("aria-disabled");
    } else {
      download.removeAttribute("href");
      download.setAttribute("aria-disabled", "true");
    }
    schedulePoll();
  }

  $("all").onclick = () => {
    queue = selection().map((m) => m.meeting_id).filter((id) => ["none", "failed"].includes(cache.get(id)?.state));
    running = queue.length > 0;
    render();
    advance();
  };

  // Plain text: title, date, then the summary, one meeting after another, ready to paste.
  $("copy").onclick = async () => {
    const meetings = selection().slice().sort((a, b) => (a.started_at || a.created_at).localeCompare(b.started_at || b.created_at));
    const text = meetings.filter((m) => cache.get(m.meeting_id)?.current).map((m) =>
      `${m.title || "Untitled meeting"}\n${meetingDate(m.started_at || m.created_at)}\n\n${cache.get(m.meeting_id).current.summary_text.trim()}`).join("\n\n---\n\n");
    try { await navigator.clipboard.writeText(text); $("copy").textContent = "Copied"; }
    catch { $("copy").textContent = "Copy failed"; }
    setTimeout(() => { $("copy").textContent = "Copy all"; }, 1800);
  };

  return {
    refresh() { render(); fetchMissing(); },
    // A pushed or polled change elsewhere (for example a rename) only needs a redraw.
    redraw: render,
  };
}
