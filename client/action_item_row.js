// One editable action item (CON-16), shared by the post-meeting view and the global /action-items page.
// The server decides everything shown here (owner name, overdue, last change); this only renders the item it was
// given and sends edits. Every change redraws from the server's reply. A failed save shows its error and leaves
// the stored values on screen: nothing is updated optimistically.

const STATUS_LABELS = { open: "Open", done: "Done", cancelled: "Cancelled" };

async function send(url, options = {}) {
  const response = await fetch(url, options.body === undefined ? options
    : { ...options, headers: { "Content-Type": "application/json" }, body: JSON.stringify(options.body) });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error?.message || "The change could not be saved.");
  return body;
}

const itemUrl = (item) => `/api/action-items/${encodeURIComponent(item.action_item_id)}`;
const day = (value) => new Date(`${value}T00:00:00Z`).toLocaleDateString(undefined, { timeZone: "UTC", day: "numeric", month: "short", year: "numeric" });
const when = (value) => new Date(value).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// options:
//   participants(item) -> Promise<[{participant_id, display_name}]>  owners allowed for this item (its own meeting)
//   noteSources(item)  -> Promise<[{meeting_id, title}]>             meetings a note can come from; one entry hides the picker
//   showMeeting        -> boolean                                     link to the originating meeting (global page)
//   onChange(item)                                                   called with the server's item after a save
export function actionItemRow(initial, options) {
  const li = el("li", "ai-row");
  let item = initial, busy = false, notesOpen = false, openForm = null;  // an open "Add note" form, kept across redraws

  async function save(request) {
    if (busy) return;
    busy = true;
    draw();
    try {
      const body = await send(request.url, request);
      item = body.action_item;
      error.textContent = "";
      options.onChange?.(item);
    } catch (failure) {
      error.textContent = failure.message;  // `item` still holds the stored values
    }
    busy = false;
    draw();
    if (notesOpen) loadNotes();
  }

  const error = el("p", "ai-error");
  error.setAttribute("role", "alert");
  const notes = el("div", "ai-notes");

  async function loadNotes() {
    notes.replaceChildren(el("p", "muted", "Loading notes…"));
    try {
      const body = await send(itemUrl(item));
      const list = el("ol", "ai-note-list");
      for (const note of body.notes) {
        const entry = el("li");
        entry.append(el("p", "ai-note-text", note.text),
                     el("p", "ai-note-meta", `${note.source_meeting_title || "Deleted meeting"} · ${when(note.created_at)}`));
        list.append(entry);
      }
      notes.replaceChildren(body.notes.length ? list : el("p", "muted", "No notes yet."));
    } catch (failure) { notes.replaceChildren(el("p", "ai-error", failure.message)); }
  }

  async function noteForm() {
    const form = el("form", "ai-note-form");
    form.hidden = true;  // shown once its meeting choices have loaded
    const text = el("textarea", "field");
    text.rows = 2;
    text.maxLength = 2000;
    text.required = true;
    text.placeholder = "What was said about this item?";
    text.setAttribute("aria-label", "Note");
    const source = el("select", "field");
    source.setAttribute("aria-label", "Meeting this update came from");
    const sources = await options.noteSources(item);
    for (const meeting of sources) source.append(new Option(`From: ${meeting.title || "Untitled meeting"}`, meeting.meeting_id));
    source.hidden = sources.length <= 1;
    const doneLabel = el("label", "ai-check");
    const done = el("input");
    done.type = "checkbox";
    doneLabel.append(done, " Mark done");
    doneLabel.hidden = item.status === "done";
    const submit = el("button", "btn btn-primary", "Add note");
    const cancel = el("button", "btn btn-secondary", "Cancel");
    cancel.type = "button";
    cancel.onclick = () => { form.remove(); if (openForm === form) openForm = null; };
    form.onsubmit = async (event) => {
      event.preventDefault();
      if (!text.value.trim() || !source.value) return;
      const body = { source_meeting_id: source.value, text: text.value };
      if (done.checked) body.status = "done";
      notesOpen = true;
      submit.disabled = true;
      await save({ url: `${itemUrl(item)}/notes`, method: "POST", body });
      submit.disabled = false;
      if (!error.textContent) cancel.onclick();  // on failure the typed note stays for another try
    };
    const actions = el("div", "ai-note-actions");
    actions.append(doneLabel, submit, cancel);
    form.append(text, source, actions);
    form.hidden = false;
    return form;
  }

  function draw() {
    li.replaceChildren();
    li.dataset.status = item.status;
    const head = el("div", "ai-head");
    head.append(el("p", "ai-text", item.text));
    const tags = el("div", "ai-tags");
    if (item.overdue) tags.append(el("span", "ai-tag is-overdue", "Overdue"));
    if (item.status !== "open") tags.append(el("span", `ai-tag is-${item.status}`, STATUS_LABELS[item.status]));
    head.append(tags);

    const controls = el("div", "ai-controls");
    const owner = el("select", "field ai-owner");
    owner.setAttribute("aria-label", "Owner");
    owner.append(new Option(item.owner_display_name || "Unassigned", item.owner_participant_id || ""));
    owner.disabled = busy;
    // The picker lists only the item's own meeting's participants (ADR-28), loaded from that meeting.
    options.participants(item).then((people) => {
      owner.replaceChildren(new Option("Unassigned", ""));
      for (const person of people) owner.append(new Option(person.display_name, person.participant_id));
      owner.value = item.owner_participant_id || "";
    }).catch(() => {});
    owner.onchange = () => save({ url: itemUrl(item), method: "PATCH", body: { owner_participant_id: owner.value || null } });

    const due = el("input", "field ai-due");
    due.type = "date";
    due.value = item.due_date || "";
    due.disabled = busy;
    due.setAttribute("aria-label", "Due date");
    due.onchange = () => save({ url: itemUrl(item), method: "PATCH", body: { due_date: due.value || null } });

    const status = el("div", "ai-status");
    status.setAttribute("role", "group");
    status.setAttribute("aria-label", "Status");
    for (const [value, label] of Object.entries(STATUS_LABELS)) {
      const button = el("button", "ai-status-btn", label);
      button.type = "button";
      button.disabled = busy;
      button.setAttribute("aria-pressed", String(item.status === value));
      button.onclick = () => { if (item.status !== value) save({ url: itemUrl(item), method: "PATCH", body: { status: value } }); };
      status.append(button);
    }
    controls.append(owner, due, status);

    const meta = el("div", "ai-meta");
    if (options.showMeeting) {
      const link = el("a", "ai-meeting", item.meeting_title || "Untitled meeting");
      link.href = `/meetings/${encodeURIComponent(item.meeting_id)}`;
      meta.append(link, el("span", "", ` · ${new Date(item.meeting_started_at).toLocaleDateString()}`), el("span", "", " · "));
    }
    meta.append(el("span", "", item.due_date ? `Due ${day(item.due_date)}` : "No due date"), el("span", "", " · "),
                el("span", "", item.last_changed_by === "manual" ? `Edited ${when(item.last_changed_at)}` : "From summary"));
    const notesToggle = el("button", "hx-link", `Notes (${item.note_count})`);
    notesToggle.type = "button";
    notesToggle.setAttribute("aria-expanded", String(notesOpen));
    notesToggle.onclick = () => { notesOpen = !notesOpen; draw(); if (notesOpen) loadNotes(); };
    const addNote = el("button", "hx-link", "Add note");
    addNote.type = "button";
    addNote.disabled = busy;
    addNote.onclick = async () => {
      if (openForm) return;
      openForm = el("div");  // placeholder while the meeting choices load
      try { openForm = await noteForm(); li.append(openForm); openForm.querySelector("textarea").focus(); }
      catch (failure) { openForm = null; error.textContent = failure.message; }
    };
    meta.append(notesToggle, addNote);

    li.append(head, controls, meta, error);
    notes.hidden = !notesOpen;
    li.append(notes);
    if (openForm?.tagName === "FORM") li.append(openForm);
  }

  draw();
  return li;
}
