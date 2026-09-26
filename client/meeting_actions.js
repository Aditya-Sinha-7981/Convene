// Rename and permanent delete for one meeting (ADR-27), shared by the history view and the post-meeting view.
// The server does the work; this only collects the new title or the typed confirmation and reports errors.

async function send(url, options) {
  const response = await fetch(url, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error?.message || "The request failed.");
  return body;
}

export function renameMeeting(meetingId, title) {
  return send(`/api/meetings/${encodeURIComponent(meetingId)}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title }) });
}

// Inline editor: replaces `target` with an input until saved or cancelled. onDone(meeting | null).
export function editTitle(target, meeting, onDone) {
  const form = document.createElement("form");
  form.className = "ma-rename";
  const input = document.createElement("input");
  input.className = "field";
  input.value = meeting.title || "";
  input.maxLength = 200;
  input.required = true;
  input.setAttribute("aria-label", "Meeting title");
  const save = document.createElement("button");
  save.className = "btn btn-primary";
  save.textContent = "Save";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "btn btn-secondary";
  cancel.textContent = "Cancel";
  const error = document.createElement("p");
  error.className = "ma-error";
  error.setAttribute("role", "alert");
  form.append(input, save, cancel, error);
  const restore = () => { form.replaceWith(target); };
  cancel.onclick = () => { restore(); onDone(null); };
  input.onkeydown = (event) => { if (event.key === "Escape") cancel.onclick(); };
  form.onsubmit = async (event) => {
    event.preventDefault();
    const title = input.value.trim();
    if (!title) return;
    save.disabled = true;
    try {
      const body = await renameMeeting(meeting.meeting_id, title);
      restore();
      onDone(body.meeting);
    } catch (failure) { error.textContent = failure.message; save.disabled = false; }
  };
  target.replaceWith(form);
  input.focus();
  input.select();
}

// Type-to-confirm dialog. Resolves true once the meeting is deleted, false if cancelled.
export function confirmDelete(meeting) {
  const name = meeting.title || "Untitled meeting";
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "ma-dialog";
    dialog.innerHTML = `<form method="dialog">
      <h2>Delete this meeting?</h2>
      <p>This permanently erases <strong></strong>: its transcript, summary, DOCX, questions and history. It cannot be undone.</p>
      <label>Type the meeting title to confirm<input class="field" autocomplete="off" spellcheck="false"></label>
      <p class="ma-error" role="alert"></p>
      <div class="ma-actions"><button type="button" class="btn btn-secondary" value="cancel">Cancel</button><button type="submit" class="btn ma-danger" disabled>Delete permanently</button></div>
    </form>`;
    dialog.querySelector("strong").textContent = name;
    const input = dialog.querySelector("input"), submit = dialog.querySelector("button[type=submit]");
    const error = dialog.querySelector(".ma-error");
    let deleted = false;
    input.oninput = () => { submit.disabled = input.value.trim() !== name.trim(); };
    dialog.querySelector("button[value=cancel]").onclick = () => dialog.close();
    dialog.querySelector("form").onsubmit = async (event) => {
      event.preventDefault();
      submit.disabled = true;
      try {
        await send(`/api/meetings/${encodeURIComponent(meeting.meeting_id)}`, { method: "DELETE" });
        deleted = true;
        dialog.close();
      } catch (failure) { error.textContent = failure.message; submit.disabled = false; }
    };
    dialog.onclose = () => { dialog.remove(); resolve(deleted); };
    document.body.append(dialog);
    dialog.showModal();
    input.focus();
  });
}
