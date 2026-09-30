# Export

## Principle

The `reasoning` model produces structured summary data (`summarization.md`); this layer deterministically renders that data into a file using fixed code and a fixed template — the model never authors formatting or document markup (ADR-12). This guarantees an export can never come out malformed because a model went off-script live in front of judges.

## Format

DOCX only for the hackathon build (`requirements.md` — PDF/Markdown export is nice-to-have, not required). Rendered with `python-docx` from a fixed template.

## Template structure (fixed, not model-controlled)

1. **Title block** — meeting title, date, participant list (display names, pulled from `Participant`, not typed by the model).
2. **Summary section** — `Summary.summary_text`, rendered as plain paragraphs.
3. **Action items section** — a table: item text, owner (or "Unassigned"), status — from `ActionItem` rows. Owner and status are the current stored values, including manual edits (CON-16). Due dates and notes are not rendered (ADR-28).
4. **Full transcript appendix** — every `Utterance` in order, speaker label + timestamp + text, with corrected utterances shown using their corrected attribution (never the original — `speaker-attribution.md`). Low-confidence/generic-label lines are visually distinguished (e.g. italicized or footnoted) in the export, not just in the live dashboard — the export should carry the same honesty about attribution confidence that the live view does.

## Periodic report (CON-18)

A second fixed template in the same renderer package (`server/export/report_renderer.py`, `python-docx`), covering every **ended** meeting whose `created_at` is inside an inclusive UTC date range (the same rule as `GET /api/meetings` `from`/`to`). It aggregates stored rows only: no model is called, and a meeting without a summary is reported as such, never summarized on the fly (ADR-30).

1. **Title block**: "Convene report", the period as UTC dates, the generation date, and the counts (ended meetings, how many have a summary, and how many `created`/`live` meetings in range were left out).
2. **Action items**: four figures, each followed by a table of every item behind it (item, owner or "Unassigned", due date, status, originating meeting and date), so no number is unexplained:
   - **Opened in this period**: the items of the current summary of each meeting in the report.
   - **Closed in this period (done)**: items from *any* meeting whose last `action_item_updated` status change with a timestamp inside the range set them to `done`. An item reopened later in the same range does not count.
   - **Cancelled in this period**: the same rule for `cancelled`, reported separately.
   - **Open now**: opened items whose status is `open` **when the report is generated**, not as of the end of the period (reconstructing that would need event replay).
   Only items of each meeting's current summary count (ADR-28); items of a superseded summary never appear.
3. **One section per meeting, oldest first**: heading (title and UTC date), duration, participants, then the summary paragraphs or an explicit state: "No summary", "Summary failed: `<error_code>`" (from the attempt's `summary_failed` event) or a pending note. A stale summary is marked "may be out of date". Then the meeting's action items (item, owner, due, status). Transcripts are not included.

Determinism follows the minutes template: fixed core timestamps, UTC dates, the generation date passed in, equivalent document XML for equal data. The report is regenerated on every download from an in-memory buffer. It has no `Export` row (whose `meeting_id` is per meeting) and no file under `data/exports/`. At most `[reports].max_meetings` (default 50) meetings go in one report; a wider range is refused with a message to narrow it and is never truncated. A render failure is `500 export_render_failed` and stores nothing.

## Generation flow

1. Triggered after summarization completes (automatically) or manually re-triggered from the dashboard.
2. Render function pulls `Meeting`, `Participant`, `Summary`, `ActionItem`, and `Utterance` rows directly from SQLite — no model call happens during rendering itself.
3. File written to `data/exports/<meeting_id>.docx`.
4. An `Export` attempt row is created (`pending`, then `ready`), `export_created` AuditEvent fired. Attempts are
   retained so a failed retry cannot replace a prior ready file.
5. Dashboard surfaces a download link once the `Export` row exists.

## Staleness

A rendered file is stale, and `GET …/export` re-renders it before serving, when the transcript or current summary changed after it was rendered, when the meeting was renamed, or when an action item of the meeting was edited (`action_item_updated`). The rule is in `data-model.md`, "Derived state". A note added to an item changes nothing in the file and does not make it stale.

## Failure handling

A rendering failure (e.g. a `python-docx` exception) marks the export attempt `failed` and is fully independent of the underlying data — the summary and transcript remain intact and viewable in the dashboard regardless of whether the file render succeeded. Retry is safe and idempotent. Determinism means identical document content: the fixed renderer sets fixed core timestamps and produces equivalent document XML for equal stored rows. DOCX is a ZIP container whose entry timestamps may differ, so byte identity is not promised. Dates and transcript clocks render in UTC.

## Why this is worth its own document

Export is the one artifact a judge can actually hold and take away from the demo. It deserves the same reliability guarantee as the live transcript itself, which is why its generation is deliberately kept boring and deterministic rather than another creative model call.
