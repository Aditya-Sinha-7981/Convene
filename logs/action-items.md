# Action-item lifecycle (CON-16)

## Implementation — 2026-09-27

Branch `feature/action-items`, from `main` at `c4c19e7`.

### Decisions (project lead, 2026-09-27: all CON-16 §11 recommended defaults; recorded as ADR-28)

- **Regeneration:** lists show only each meeting's current (most recent `ready`) summary. A regenerated summary
  starts fresh `open` items, and edits do not carry over. The post-meeting Regenerate/Summarize action warns first.
  Superseded items and their audit history stay, and remain editable by ID (`PATCH`/`GET /api/action-items/{id}`).
- **Owner:** `owner_participant_id`, restricted to participants of the item's own meeting. Across meetings, the
  `owner` filter matches display names the way owner mapping does (case and whitespace ignored).
- **Status:** `open` | `done` | `cancelled`. **Due date:** nullable `due_date` TEXT `YYYY-MM-DD` (UTC date).
- **Change record:** derived from the audit stream (`last_changed_at`, `last_changed_by` = `manual` | `summary`); no
  stored column. Last write wins; there is no version check.
- **Entities and events:** `ActionItemNote` (`note_id`, `action_item_id`, `source_meeting_id`, `text`, `created_at`),
  plus two events with component `api`, both with `meeting_id` = the item's meeting:
  - `action_item_updated`: `action_item_id`, `summary_id`, `from`, `to` (changed fields only), `via` (`edit` | `note`)
  - `action_item_note_added`: `note_id`, `action_item_id`, `source_meeting_id`
  No item or note text is in either payload. **CON-18 depends on these names and keys; keep them stable.**
- **Deletion (ADR-27):** deleting a meeting removes its items and every note on them. It also removes the notes it
  was the source of on other meetings' items, together with those `action_item_note_added` events. A status change
  carried by such a note stays on the item, with its `action_item_updated` event.
- **Routes:** `GET /api/action-items`, `GET`/`PATCH /api/action-items/{id}`, `POST /api/action-items/{id}/notes`,
  and the page `/action-items`. `GET /api/action-items/{id}` (item plus notes) was added beyond the §11 list,
  because notes that cannot be read back are useless in the UI.
- **Export:** an `action_item_updated` newer than `export_created` makes the DOCX stale; a note does not. Summary
  staleness is unaffected. The DOCX template is unchanged, and it prints the stored status, now including
  `cancelled`.

### What was built

- `server/migrations/0009_action_item_lifecycle.sql`: `ActionItem` rebuilt with the 0003 pattern (rows copied in
  rowid order, so the model's order is kept). It adds a `due_date` CHECK that rejects impossible dates through
  `date(x) = x`, the index `idx_action_item_status_due`, and the `ActionItemNote` table with its two indexes.
- `server/action_items/service.py` (validation, `update_action_item`, `add_action_item_note`, each in the caller's
  transaction with `emit`) and `server/action_items/views.py` (one SQL view with derived fields; the list query with
  filters, sort, pagination and the current-summary filter; the detail view).
- `repositories/summaries.py`: `ActionItem.due_date` (default None, so the frozen summary service is unchanged),
  `get_action_item`, `update_action_item`. New `repositories/action_item_notes.py`.
- `summary/views.py` now uses the shared item view (additive fields only). There are export staleness changes in
  `export/service.py`, the note cascade in `registry.erase_meeting`, and `ActionItemNote` in the `/database`
  inspector.
- UI: `client/action_item_row.js` (a shared editable row: owner picker, date, one-click status, notes, add note;
  redraws from the server's reply only), the `/action-items` page (`action_items.html/js/css`), and edit rows on
  the post-meeting view. Links are in the home nav, the `/history` intro and the post-meeting header.
- Docs: `data-model.md`, `api.md`, `export.md`, `frontend.md`, and `decisions.md` ADR-28.
- `tests/test_db_migrations.py` was updated to version 9 and the current table set. It had already been failing
  on `main` (it expected version 7 and no `Export` table).

### Verification

- `.venv/bin/python -m pytest tests/test_action_item_edit.py tests/test_action_item_list.py tests/test_action_item_notes.py tests/test_summary_service.py tests/test_audit_emit.py tests/test_api_contract_docs.py tests/test_meeting_admin.py -q -m "not model"`
  → 169 passed.
- Full suite `-m "not model"` → 716 passed.
- Rendered `/action-items` and `/meetings/{id}` in headless Chromium against a loopback server with seeded data.
  Rows, sort, the Overdue tag and meeting links rendered correctly, with no horizontal scroll at 500 px. Headless
  Chrome cannot render narrower than about 500 px, so 390 px was not checked.
- Model checks: not required (no model is used).
- **Not run:** clicking through edits and notes in a browser (no browser automation was connected), and the
  reference-laptop demo browser with networking disabled. Manual checks 1–7 in the task (§8) remain to be done.

### Handoff

- The post-meeting page redraws its action-item rows on every transcript or summary push, so an open "Add note"
  form there can close while a live meeting is still producing lines. Once the meeting has ended this is quiet.
- The Regenerate warning uses the browser's `confirm()` dialog.
