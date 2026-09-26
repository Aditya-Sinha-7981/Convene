# Summarization & Action Items

## Scope

Producing a meeting summary and action-item list from a completed (or in-progress, if manually triggered early) transcript. Rendering that data into a downloadable file is a separate, deterministic step — see `export.md`.

**Implementation status (CON-10):** implemented in `server/summary/` and verified with automated tests (fake model) and a real-model reliability run on the reference laptop (`logs/summary-export.md`). Not yet verified on a real multi-phone meeting.

## Trigger

Runs once, on an explicit action: the meeting-end action, or a manual "summarize now" button (the post-meeting page, `/meetings/{meeting_id}`, which also serves a live meeting). It never runs continuously or per-utterance — summarizing the whole transcript at once is both cheaper and produces a more coherent result than incremental summarization.

Each trigger inserts a `pending` `Summary` row and returns at once (`api.md`); the work runs in the background and ends with `summary_ready` or `summary_failed`. At most one attempt per meeting runs at a time: a second trigger gets `409 summary_in_progress` and the running attempt is unaffected. If a manual attempt is running when the meeting ends, the end trigger does not start another; any lines it missed make it stale. A re-trigger creates a new `Summary`; older rows are kept, and the current summary is always the most recent `ready` one. A `pending` row left by a server that stopped mid-attempt is marked `failed` at the next startup.

**End sequence.** `POST …/end` marks the meeting ended and closes the phones (`transport.md`); the summary hook then cuts every device's partial final segment, waits for queued and in-flight STT windows and for the attribution writes that turn them into lines, and only then reads the transcript. The wait is bounded by `[summary].drain_timeout_s` (15 s); if it runs out, summarization proceeds with the lines that exist and the outcome event records `drain_timed_out: true`. The end trigger starts an attempt only if the meeting has at least one utterance or still has speech queued. A manual trigger never flushes or waits (on a live meeting that would cut off a sentence being spoken); it summarizes the transcript as it stands.

## Input

The full attributed transcript for the meeting, in order, using the *current* (possibly corrected) attribution for every utterance — never the original pre-correction attribution (`speaker-attribution.md`). Utterances with `attribution_method = "generic_unresolved"` are included using their generic label as-is; the summary should not invent a name for an unresolved speaker.

Rendering is deterministic (`server/summary/prompt.py`): utterances ordered by `t_start`, then `utterance_id`, one line each as `[HH:MM:SS] Speaker: text`, where the time is elapsed since the meeting started and `Speaker` is the utterance's `speaker_label` (`api.md`, derived fields; for example `Speaker on Phone 2`). The prompt also lists the meeting's participant display names, so the model can name owners exactly. The lines, the participant list and the `input_as_of_seq` recorded for staleness are read in one transaction. The transcript is fenced as untrusted data and the model is told to ignore instructions inside it.

The prompt asks for minutes in the model's own words (third person, one short paragraph per topic, decisions and open items, plain text), every task a speaker took on, was asked to do, or said needs doing (with a null owner when nobody took it), each task once, no filler tasks, and an empty list when there are none. These rules came from prompt iteration on the fixtures in `tests/fixtures/transcripts/` (`logs/summary-export.md`).

## Long transcripts

The full transcript must fit in one model call; it is never silently shortened. Before calling the model the prompt's tokens are counted with the model's own tokenizer and chat template. Above `[summary].max_input_tokens` the attempt fails with `transcript_too_long` and a message giving the count and the limit, and the model is not called.

The limit is 16,000 prompt tokens, chosen from measurements on the reference laptop with the pinned model (`scripts/measure_summary.py`, `logs/summary-export.md`). Nonstop talk renders to about 310 prompt tokens per minute, so the limit is about 50 minutes of continuous speech; a turn-taking meeting with pauses runs well below that rate (the 17-minute fixture used about 110 tokens per minute), so ordinary meetings of an hour or more fit. At the limit a summary takes about 100 s (the prompt prefill runs at only 160–290 tokens per second) and peaks at about 7.1 GB of MLX memory, against 5.5 GB for a short meeting. Memory is not the constraint: 90 minutes of nonstop talk peaked at 8.7 GB but took over four minutes and filled the output budget. A larger limit, or a time-sliced map-then-merge pass for longer meetings, is a later change if real meetings need it; the demo meeting is a few minutes long.

`[summary].max_output_tokens` (1,536) bounds the reply and `[summary].generation_timeout_s` (240 s) bounds each model call, prefill included. If a reply is cut off at the output limit it fails validation, and the one retry asks for a shorter summary.

## Output contract

The `reasoning` resource type is invoked with a constrained-output instruction to produce structured data only:

```json
{
  "summary": "string — a few paragraphs, plain text",
  "action_items": [
    { "text": "string", "owner": "string or null" }
  ]
}
```

This structured output is what gets validated and persisted (`Summary`, `ActionItem` rows — `data-model.md`) — the model never produces the final document's formatting or markup directly (ADR-12). If the model's output doesn't parse as valid structured data, the summarization attempt is retried once with a stricter instruction, then marked `failed` rather than falling back to using malformed output.

**Parsing and validation** (`server/summary/schema.py`). Generation runs at temperature 0. Parsing removes one wrapping code fence, or prose before and after a single JSON object, and nothing else; a reply with more than one JSON value is rejected. It never repairs meaning: no missing key is filled in and no wrong type is converted. Validation rejects a non-object, a missing, non-string or blank `summary`, a missing or non-list `action_items`, an item that is not an object, an item without a non-empty string `text`, an item without an `owner` key, and an `owner` that is neither a string nor null. Keys outside the contract are ignored and never stored. Whitespace in `text` is collapsed, `summary` is trimmed, and a blank `owner` string is read as null (both mean "no owner").

**Retry.** Only invalid output is retried, once, with the same transcript and a stricter instruction that states the rejection reason (never transcript text) and asks for the bare JSON object with every key present. A model error or timeout is not retried. Every model call writes a `ModelExecution` row (`related_id` = `summary_id`).

**Persistence.** On success one transaction sets the `Summary` to `ready` with `summary_text`, inserts its `ActionItem` rows (`status = open`, in the model's order) and writes `summary_generated`. If that transaction fails, nothing from it remains and the attempt is recorded as `failed` instead.

## Owner inference

If the model can reasonably infer an action item's owner from the transcript context (e.g. someone explicitly says they'll do something), it sets `owner`; otherwise `owner` is left null rather than guessed. A null owner is a normal, expected outcome, not a failure — the dashboard should let a user assign one manually afterward (should-have feature, `requirements.md`).

The model's `owner` string becomes `owner_participant_id` only by exact match against the meeting's participant display names, ignoring case and repeated whitespace. No match (including a generic label such as `Speaker on Phone 2`, which the model may use for a task an unresolved speaker took on) or a name shared by two participants leaves the owner null. There is no fuzzy or partial matching.

## Quality expectations

Local-model summarization quality is good enough for meeting minutes at this scale, but is not held to the same bar as the largest cloud models (ADR-06's accepted tradeoff). If summary quality genuinely disappoints in testing, the first fix is prompt iteration, the second is trying a slightly larger local model within the memory budget (`models.md`), and only as a last resort is the cloud `reasoning` fallback activated — deliberately, not silently.

## Failure handling

A summarization failure (model error, parse failure after retry) is marked `failed` and surfaced to the dashboard; the transcript itself remains fully intact and viewable — a summarization failure must never appear to affect or invalidate the underlying transcript data.

A failed attempt sets its own `Summary` row to `failed` with a short `error_message` (never transcript text) and writes `summary_failed` with one of these `error_code` values; an earlier `ready` summary stays current.

| `error_code` | When |
|---|---|
| `summary_invalid_output` | the reply failed validation twice (after the one stricter retry) |
| `summary_generation_failed` | the model raised or timed out (also `model_error`, `related_id` = `summary_id`), no reasoning model is loaded, the result could not be stored, or the server stopped mid-attempt |
| `transcript_too_long` | the prompt exceeds `[summary].max_input_tokens`; the model was not called |
| `transcript_empty` | the end trigger found queued speech, but it produced no lines |

A manual trigger on a meeting with no utterances is refused with `409 transcript_empty` and creates no row.

## Staleness

A summary is stale when a line was created or corrected after its input was read (`data-model.md`, "Derived state"). `GET …/summary` returns `stale`, and the post-meeting page shows a notice with a regenerate action, which calls `POST …/summarize`.

## Scheduling

Summarization shares the resident `reasoning` model with live Q&A; the adapter runs one generation at a time, so a question asked during a summary waits (and may reach its own 30 s deadline). Before each call it waits on the STT compute-priority gate, but MLX cannot preempt a call once it starts. At meeting end nothing competes, because STT has been drained. A manual summary during a busy live meeting does delay STT: with five phones talking nonstop, windows that ended during a 30 s summary waited a median 17.5 s, against about 4 s normally (`logs/summary-export.md`). Summarize at meeting end in the demo.

## Relationship to RAG

Summarization and Q&A (`rag-and-qa.md`) both use the `reasoning` resource type but are otherwise independent pipelines — summarization reads the full transcript directly (no retrieval step needed, since the whole meeting fits in context up to the limit in "Long transcripts"), while Q&A always goes through chunk retrieval first. Do not conflate the two or try to unify them into one pipeline; they have different inputs and different failure semantics.
