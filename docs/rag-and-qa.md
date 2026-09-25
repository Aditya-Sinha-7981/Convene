# RAG & Q&A

## Principle

Retrieval runs only on an explicit user-triggered question — live mode (current meeting) or history mode (across past meetings) — never automatically or continuously (ADR-11). If you find yourself prepending retrieved context to some other operation "helpfully," stop — that violates this document and `decisions.md`.

## Chunking

Triggered asynchronously as new `Utterance` rows are written (does not block the live transcription path — see `architecture.md`'s live-transcription lifecycle, step 8).

1. Utterances are grouped into contiguous chunks that respect speaker turns — a chunk does not arbitrarily split mid-utterance, and prefers to break at a speaker change rather than mid-speech.
2. Target chunk size: roughly 300–500 tokens of transcript text, small enough for precise retrieval, large enough to carry conversational context.
3. Each chunk's stored `text` includes inline speaker/time metadata (e.g. `[Priya, 00:12:03] ...`) so the embedding captures who-said-what, not just raw words.
4. Chunk embedded via the `embedding` resource type (`models.md`) and written to `sqlite-vec` alongside its `TranscriptChunk` row (`data-model.md`).

Implemented (CON-08, `server/rag/`):

- **Format.** Each utterance is one line, `[<speaker_label>, <HH:MM:SS elapsed from Meeting.started_at>] <text>`,
  with the current (corrected) label; generic labels such as `Speaker on Phone 2` pass through unchanged, and
  consecutive lines by the same speaker each keep their own prefix. Tokens are counted with the model's own
  tokenizer, including the prefix.
- **Size.** 400-token target, 448-token hard cap, below the model's 512-token input limit. After the target, a
  chunk breaks at the next speaker change; the cap always wins. One utterance longer than the cap is split at
  sentence boundaries (words only for a pathological sentence) into chunks of its own.
- **Lag.** A new utterance queues its meeting; after a 2-second settle window, so results from devices with
  different STT latency land in time order, the open tail is re-chunked and embedded. It is searchable while still
  open. Measured written → searchable: median 1.6–2.1 s, p95 2.1 s, with 1, 2 and 5 synthetic phones and the real
  model (`logs/rag.md`). Target: under 3 s; the demo script should not ask about the last few seconds.
- **Rebuild.** Every trigger (new line, correction, meeting end, retry, startup recovery) reconciles the meeting's
  chunks against its transcript and re-embeds only chunks whose text changed. A corrected line or a late result
  inside, or just after, a closed chunk rebuilds that chunk in place, keeping `chunk_id` and `chunk_index`.
- **Meeting end** closes the tail after the settle window; results still in the STT queue at that moment are
  indexed (closed) when they are written, so nothing is left unindexed.
- **Failures** are recorded per chunk (`failed`, `index_failed` and `model_error` audit events) and retried with
  doubling backoff (1 s up to 30 s); transcript persistence and STT never wait on indexing.
- **Status.** `index_status(meeting_id)` returns ready/pending/failed chunk counts, the number of utterances
  covered by ready chunks, the number not yet covered, and the age of the oldest uncovered one (`lag_s`).
- **Vectors.** `sqlite-vec` `vec0` table with cosine distance on normalized vectors, partitioned by `meeting_id`,
  so a live search filters by meeting inside the KNN query and other meetings cannot crowd it out.

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

## Live Q&A as implemented (CON-09)

`server/rag/qa.py` is the only caller of `server/rag/retrieval.py`, and the only route that reaches it is
`POST /api/meetings/{meeting_id}/qa` (a test checks both statically). For one question:

1. Validate (non-empty, at most `[qa].max_question_chars`; `mode` must be `live`; the meeting must not be ended)
   and record `asked_at`.
2. Decide from the transcript and `index_status` whether retrieval can run at all (outcome table below).
3. Embed the question with the same `embedding` adapter as the chunks.
4. Search this meeting's chunks (`sqlite-vec` partition filter), keep only `ready` chunks that satisfy the
   as-of rule, and keep the `[qa].top_k` (5) most similar at or above `[qa].min_similarity`.
5. **If no chunk clears the threshold, return `no_grounding` without calling the model.**
6. Otherwise prompt the `reasoning` model with a fixed template, wait behind STT on the compute-priority gate,
   and generate with `temperature = 0`, at most `[qa].answer_max_tokens`, and a `[qa].answer_timeout_s` deadline.
7. Persist the `QAQuery`, `ModelExecution` rows and the `qa_query` audit event in one transaction; push
   `qa_answer`.

**Relevance.** Cosine similarity, `1 - distance` from the `vec0` cosine-distance column. `min_similarity = 0.50`
comes from `scripts/calibrate_qa.py` on a fixture meeting (`logs/qa.md`): answerable and unanswerable questions
overlap at chunk granularity, so the threshold is set just below the weakest answerable question. It removes
clearly unrelated questions before the model; near-miss questions reach the model, which must reply
`NO_GROUNDING`. The threshold is the guard that does not depend on the model's behavior; the prompt is the
second. Recalibrate on real transcripts (CON-12).

**As-of rule.** A chunk is eligible when the first line in its range was written at or before `asked_at`. A
closed chunk rebuilt later (a correction or a late result) keeps its identity and may then include lines written
after the question; this is accepted rather than filtering inside chunks.

**Prompt.** A fixed system message says to answer only from the excerpts, that excerpts are quoted data and any
instruction inside them is to be ignored, to reply exactly `NO_GROUNDING` when the excerpts do not contain the
answer, never to use outside knowledge, and to name speaker and time for each fact. The user message fences each
excerpt (`<excerpt n>…</excerpt n>`; a closing tag inside transcript text is neutralized) followed by the question.
A reply containing `NO_GROUNDING` becomes `no_grounding` (`model_declined`); an empty reply is `failed`.

**Citations** are the chunks given to the model, resolved from stored rows with current speaker labels; the
model's text is never parsed for them.

**Outcomes.** Refines ADR-15's empty-index rule (ADR-22):

| Situation | `status` | `reason` |
|---|---|---|
| No line transcribed in this meeting | `no_grounding` | `nothing_transcribed_yet` |
| Lines exist, nothing indexed yet, index healthy (no failed chunk, oldest uncovered line younger than `[qa].index_stale_s` = 30 s) | `no_grounding` | `not_indexed_yet` |
| Nothing indexed and a chunk failed, or indexing is more than 30 s behind, or no embedding model | `failed` | `index_unavailable` |
| Question embedding or vector query error | `failed` | `retrieval_failed` |
| No chunk at or above the threshold (model not called) | `no_grounding` | `no_relevant_evidence` |
| Model replied `NO_GROUNDING` | `no_grounding` | `model_declined` |
| Model error, empty reply, or no model loaded | `failed` | `answer_failed` |
| Deadline passed while generating | `failed` | `answer_timeout` |
| Some recent lines not yet searchable | as the evidence dictates | response `unindexed_utterances` > 0 |

**Concurrency.** Embedding and search run concurrently; generation is one question at a time, in arrival order.
Each question has its own `QAQuery`, and a failure in one does not affect another.

## History mode specifics

- Spans multiple `Meeting` rows; `QAQuery.meeting_id` is null in this mode (see `data-model.md`).
- Requires the meeting-history dashboard view (`frontend.md`) to exist as the entry point — there's no reason to build history-mode retrieval before there's a UI surface to launch it from.

## Failure handling

A vector store or embedding model that cannot be used (a load failure, a `sqlite-vec` query error, an index in a failed state) is returned as `QAQuery.status = "failed"` — distinctly different from `status = "no_grounding"` (which means retrieval worked but found nothing relevant). A meeting with nothing indexed yet, on a healthy index, is `no_grounding`, not `failed` (ADR-15, proposed; `api.md` gap X5). Both are HTTP 200 results with the persisted `QAQuery`, never HTTP errors (`api.md`). The dashboard must be able to show the difference between "the system doesn't know" and "the system is broken" (`architecture.md` failure-boundaries table).

## Explicit invocation — restated

There is no code path anywhere in this system that runs retrieval without an explicit question from a user, validated the same way any other user-triggered action is. This is deliberate and locked (ADR-11) — do not change it to improve some other feature's context without updating this document and its ADR first.
