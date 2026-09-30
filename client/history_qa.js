// History Q&A panel (CON-14), shared by the history view and the post-meeting view. It only calls POST /api/qa and
// displays the server's outcome: no answer logic lives here (docs/frontend.md). The scope is always labelled, so a
// history answer can never be mistaken for the live dashboard's "Ask the room" panel.

const STATUS = { answered: "Answered", no_grounding: "Not found", failed: "System error" };
const NO_GROUNDING = {
  no_ended_meetings: "There are no ended meetings to search yet.",
  nothing_transcribed_yet: "Nothing was transcribed in the searched meetings.",
  not_indexed_yet: "These meetings are still being indexed. Try again in a few seconds.",
  no_relevant_evidence: "No part of the searched meetings answers this, so Convene did not guess.",
  model_declined: "The closest excerpts did not contain the answer, so Convene did not guess.",
};

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

export function meetingDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "—" : date.toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });
}

export function clock(start, t) {
  const seconds = Math.max(0, Math.floor((Date.parse(t) - Date.parse(start)) / 1000));
  return [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60].map((n) => String(n).padStart(2, "0")).join(":");
}

const quote = (title) => `“${title || "Untitled meeting"}”`;

function coverageNotes(scope) {
  const notes = [];
  for (const item of scope?.coverage || []) {
    const name = quote(item.title), n = item.unindexed_utterances;
    if (item.state === "partial") notes.push(`${n} line${n === 1 ? "" : "s"} in ${name} ${n === 1 ? "was" : "were"} not searchable yet.`);
    else if (item.state === "not_indexed") notes.push(`${name} is not indexed yet and was not searched.`);
    else if (item.state === "failed") notes.push(`${name} could not be indexed and was not searched.`);
    else if (item.state === "empty") notes.push(`${name} has no transcript.`);
    else if (item.state === "model_mismatch") notes.push(`${name} was indexed with a different embedding model.`);
  }
  return notes;
}

// options: { root, scope(): { meeting_ids: [..] | null, label, ready, hint }, onCitation(citation) -> bool }
export function mountHistoryQA({ root, scope, onCitation }) {
  const ui = {
    form: root.querySelector("[data-qa-form]"), question: root.querySelector("[data-qa-question]"),
    ask: root.querySelector("[data-qa-ask]"), label: root.querySelector("[data-qa-scope]"),
    hint: root.querySelector("[data-qa-hint]"), error: root.querySelector("[data-qa-error]"),
    answers: root.querySelector("[data-qa-answers]"), empty: root.querySelector("[data-qa-empty]"),
  };
  const results = [];
  let pending = null, ticker = null;

  function refreshScope() {
    const current = scope();
    ui.label.textContent = current.label;
    ui.hint.textContent = current.hint || "Enter to ask · answers come only from the meetings in scope";
    ui.ask.disabled = Boolean(pending) || !current.ready;
  }

  function citationItem(citation) {
    const entry = el("li");
    if (citation.source_type === "policy") {
      const link = el("a", "hq-citation is-policy");
      link.href = `/policies/${encodeURIComponent(citation.policy_id)}`;
      link.append(el("span", "hq-cite-meeting", `Policy · ${citation.policy_title} · version ${citation.version_number}`),
        el("strong", "", "Policy source"), el("span", "hq-excerpt", citation.text.replace(/^Policy:[^\n]*\n/, "").slice(0, 180)));
      entry.append(link);
      return entry;
    }
    const link = el("a", "hq-citation");
    link.href = `/meetings/${encodeURIComponent(citation.meeting_id)}#u-${encodeURIComponent(citation.utterance_ids?.[0] || "")}`;
    link.title = "Open this moment in the meeting's transcript";
    const where = el("span", "hq-cite-meeting", `${citation.meeting_title || "Untitled meeting"} · ${meetingDate(citation.meeting_started_at)}`);
    const who = el("strong", "", `${citation.speakers.join(", ")} · ${clock(citation.meeting_started_at, citation.t_start)}`);
    const excerpt = el("span", "hq-excerpt", citation.text.replace(/^\[[^\]]*\]\s*/, "").replace(/\n\[[^\]]*\]\s*/g, " … ").slice(0, 180));
    link.append(where, who, excerpt);
    link.onclick = (event) => { if (onCitation && onCitation(citation)) event.preventDefault(); };
    entry.append(link);
    return entry;
  }

  function card(item) {
    const { query, citations = [], reason = null, scope: searched = null, label } = item;
    const node = el("li", `hq-card ${query.status}`);
    const head = el("div", "hq-card-head");
    const chips = el("span", "hq-chips");
    chips.append(el("span", "hq-status", STATUS[query.status] || query.status), el("span", "hq-mode", `History · ${label}`));
    const when = el("time", "hq-time", new Date(query.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
    head.append(chips, when);
    node.append(head, el("p", "hq-question", query.question));
    if (query.status === "answered") {
      node.append(el("p", "hq-body", query.answer));
      if (citations.length) {
        const sources = el("details", "hq-sources");
        sources.open = true;
        const list = el("ul", "hq-citations");
        list.setAttribute("aria-label", "Sources from past meetings");
        for (const citation of citations) list.append(citationItem(citation));
        sources.append(el("summary", "", `Sources (${citations.length})`), list);
        node.append(sources);
      }
    } else if (query.status === "no_grounding") {
      node.append(el("p", "hq-body", "Not discussed in the searched meetings."));
      node.append(el("small", "hq-detail", NO_GROUNDING[reason] || NO_GROUNDING.no_relevant_evidence));
    } else {
      node.append(el("p", "hq-body", `Convene could not answer because of a system problem${query.error?.message ? `: ${query.error.message}` : ""}.`));
      const retry = el("button", "hq-retry", "Try again");
      retry.type = "button";
      retry.onclick = () => ask(query.question, item.request);
      node.append(retry);
    }
    for (const note of coverageNotes(searched)) node.append(el("small", "hq-detail", note));
    return node;
  }

  function render() {
    const fragment = document.createDocumentFragment();
    if (pending) {
      const node = el("li", "hq-card pending");
      const head = el("div", "hq-card-head");
      head.append(el("span", "hq-mode", `History · ${pending.label}`), el("span", "hq-time", `${Math.floor((Date.now() - pending.started) / 1000)}s`));
      node.append(head, el("p", "hq-question", pending.question), el("p", "hq-detail", "Searching past meetings…"));
      fragment.append(node);
    }
    for (const item of results) fragment.append(card(item));
    ui.answers.replaceChildren(fragment);
    ui.empty.hidden = Boolean(pending) || results.length > 0;
    refreshScope();
  }

  async function ask(text, request) {
    const question = (text || "").trim();
    const current = scope();
    const body = request || { question, mode: "history", meeting_ids: current.meeting_ids, sources: current.sources || "meetings" };
    if (!question || pending || (!request && !current.ready)) return;
    body.question = question;
    ui.error.textContent = "";
    pending = { question, started: Date.now(), label: request ? request.label : current.label };
    ticker = setInterval(render, 1000);
    render();
    try {
      const response = await fetch("/api/qa", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: body.question, mode: "history", meeting_ids: body.meeting_ids, sources: body.sources || "meetings" }) });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.error?.message || "Could not ask the question.");
      results.unshift({ ...result, label: pending.label, request: { ...body, label: pending.label } });
      if (!request) ui.question.value = "";
    } catch (error) {
      ui.error.textContent = error.message;
    } finally {
      clearInterval(ticker);
      pending = null;
      render();
    }
  }

  ui.form.addEventListener("submit", (event) => { event.preventDefault(); ask(ui.question.value); });
  ui.question.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); ask(ui.question.value); }
  });
  render();
  return { refreshScope };
}
