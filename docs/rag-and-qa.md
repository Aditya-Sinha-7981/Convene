# RAG & Q&A

## Principle

Retrieval runs only on an explicit user-triggered question — live mode (current meeting) or history mode (across past meetings) — never automatically or continuously (ADR-11). If you find yourself prepending retrieved context to some other operation "helpfully," stop — that violates this document and `decisions.md`.

## Chunking

Triggered asynchronously as new `Utterance` rows are written (does not block the live transcription path — see `architecture.md`'s live-transcription lifecycle, step 8).

1. Utterances are grouped into contiguous chunks that respect speaker turns — a chunk does not arbitrarily split mid-utterance, and prefers to break at a speaker change rather than mid-speech.
2. Target chunk size: roughly 300–500 tokens of transcript text, small enough for precise retrieval, large enough to carry conversational context.
3. Each chunk's stored `text` includes inline speaker/time metadata (e.g. `[Priya, 00:12:03] ...`) so the embedding captures who-said-what, not just raw words.
4. Chunk embedded via the `embedding` resource type (`models.md`) and written to `sqlite-vec` alongside its `TranscriptChunk` row (`data-model.md`).

The current ingestion implementation uses a 400-token target and 448-token hard cap (including metadata), below
the embedding model's 512-token limit. It holds new cross-device results for a 2-second settle window and closes a
mutable tail after 3 seconds of quiet; the tail is always flushed at meeting end. A correction or late result only
rebuilds the chunk range that covers it, preserving its `chunk_id` and `chunk_index` for citations. Index status
reports ready/pending/failed chunk counts, uncovered utterance count, and current lag; failures remain retryable and
never block transcript persistence.

## Retrieval

1. Embed the incoming question (same `embedding` resource type as ingestion — embedding space must match).
2. Vector similarity search against `sqlite-vec`, scoped:
   - `mode: live` → chunks from the current, still-in-progress meeting only.
   - `mode: history` → chunks across some or all past meetings, per the query's scope.
3. Return top-k chunks (k is a tuning parameter — start small, e.g. 5, and adjust from real testing, not guessed) with their similarity scores.
4. No reranking step for the hackathon build — plain similarity search is expected to be sufficient at this corpus scale. Add only if retrieval quality genuinely disappoints in testing (`testing.md`); do not build it preemptively.

## Answer generation

1. Retrieved chunks assembled into context, passed to the `reasoning` resource type with the question.
2. If no chunk clears a minimum relevance threshold, the correct behavior is an honest "I don't have grounding for that in this meeting" response — never a fabricated answer drawn from the model's general knowledge. This is a testable behavior, not a hope (see `testing.md`'s RAG-honesty test case).
3. The response should reference which chunk(s)/speaker(s)/timestamp(s) it drew from in its own text (a prompt-level instruction), in addition to the structured `cited_chunk_ids` the dashboard displays regardless — the citation is a readability nicety for the answer text, not the only source of evidence.

## Live mode specifics

- Scoped strictly to chunks that exist *as of the question being asked* — a live question should never be answered using chunks from a later point in the same meeting (this matters for the "ask what was said 10 minutes ago" demo moment, `demo.md`, to be honest about what it actually retrieved).
- Because chunking runs asynchronously slightly behind live transcription, the most recent few seconds of speech may not yet be chunk-searchable at question time — this is an accepted, small latency gap, not a bug to hide; if it matters for the demo script, account for it by not asking about something said in the last few seconds.

## History mode specifics

- Spans multiple `Meeting` rows; `QAQuery.meeting_id` is null in this mode (see `data-model.md`).
- Requires the meeting-history dashboard view (`frontend.md`) to exist as the entry point — there's no reason to build history-mode retrieval before there's a UI surface to launch it from.

## Failure handling

A vector store or embedding model that cannot be used (a load failure, a `sqlite-vec` query error, an index in a failed state) is returned as `QAQuery.status = "failed"` — distinctly different from `status = "no_grounding"` (which means retrieval worked but found nothing relevant). A meeting with nothing indexed yet, on a healthy index, is `no_grounding`, not `failed` (ADR-15, proposed; `api.md` gap X5). Both are HTTP 200 results with the persisted `QAQuery`, never HTTP errors (`api.md`). The dashboard must be able to show the difference between "the system doesn't know" and "the system is broken" (`architecture.md` failure-boundaries table).

## Explicit invocation — restated

There is no code path anywhere in this system that runs retrieval without an explicit question from a user, validated the same way any other user-triggered action is. This is deliberate and locked (ADR-11) — do not change it to improve some other feature's context without updating this document and its ADR first.
