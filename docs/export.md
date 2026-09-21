# Export

## Principle

The `reasoning` model produces structured summary data (`summarization.md`); this layer deterministically renders that data into a file using fixed code and a fixed template — the model never authors formatting or document markup (ADR-12). This guarantees an export can never come out malformed because a model went off-script live in front of judges.

## Format

DOCX only for the hackathon build (`requirements.md` — PDF/Markdown export is nice-to-have, not required). Rendered with `python-docx` from a fixed template.

## Template structure (fixed, not model-controlled)

1. **Title block** — meeting title, date, participant list (display names, pulled from `Participant`, not typed by the model).
2. **Summary section** — `Summary.summary_text`, rendered as plain paragraphs.
3. **Action items section** — a table: item text, owner (or "Unassigned"), status — from `ActionItem` rows.
4. **Full transcript appendix** — every `Utterance` in order, speaker label + timestamp + text, with corrected utterances shown using their corrected attribution (never the original — `speaker-attribution.md`). Low-confidence/generic-label lines are visually distinguished (e.g. italicized or footnoted) in the export, not just in the live dashboard — the export should carry the same honesty about attribution confidence that the live view does.

## Generation flow

1. Triggered after summarization completes (automatically) or manually re-triggered from the dashboard.
2. Render function pulls `Meeting`, `Participant`, `Summary`, `ActionItem`, and `Utterance` rows directly from SQLite — no model call happens during rendering itself.
3. File written to `data/exports/<meeting_id>.docx`.
4. An `Export` row created (`data-model.md`), `export_created` AuditEvent fired.
5. Dashboard surfaces a download link once the `Export` row exists.

## Failure handling

A rendering failure (e.g. a `python-docx` exception) marks the export attempt `failed` and is fully independent of the underlying data — the summary and transcript remain intact and viewable in the dashboard regardless of whether the file render succeeded. Retry is safe and idempotent — re-rendering from the same stored data always produces the same file.

## Why this is worth its own document

Export is the one artifact a judge can actually hold and take away from the demo. It deserves the same reliability guarantee as the live transcript itself, which is why its generation is deliberately kept boring and deterministic rather than another creative model call.
