# RAG ingestion (CON-08 / CON-09)

## CON-08 foundation — 2026-09-26

- Read the authoritative RAG, data-model, model, pipeline, deployment, attribution, decision, and test contracts,
  plus all existing work logs before starting.
- Added migrations `0003_transcript_chunks.sql` and `0004_transcript_chunk_closure.sql`: durable `TranscriptChunk`
  text rows, one-record vector-space guard, a mutable tail marker, and the `TranscriptChunkVector` sqlite-vec table.
  Vectors use a TEXT chunk key, a meeting partition key, and
  cosine distance, matching the CON-03 spike.
- Database connections now load sqlite-vec before migrations or queries. `sqlite-vec` is declared in runtime
  requirements. The extension is installed in this working environment; the sentence-transformers runtime and
  BGE weights are not yet installed/provisioned.
- Chosen initial configuration pending required measurement: `BAAI/bge-small-en-v1.5`, 384 dimensions, 512-token
  input, CPU `sentence-transformers` runtime, pinned to `5c38ec7c405ec4b44b94cc5a9bb96e735b38267a` after provisioning.
  Chunk target/hard bounds are 400/448 tokens and the initial
  settle/quiet-flush values are 2/3 seconds. This is **not validated** until the required model comparison,
  cached/offline test, memory/latency, and fixture retrieval check are recorded.
- Added the pure deterministic chunker. Rendered input format is `[Speaker, HH:MM:SS] text`, with elapsed time
  from `Meeting.started_at`; generic labels pass through unchanged. It counts rendered metadata, favors speaker
  changes after target size, and only sentence-splits a single over-cap utterance.

### Checks

```sh
.venv/bin/python -m pytest tests/test_chunker.py tests/test_config.py tests/test_db_migrations.py tests/test_sqlite_vec_spike.py -q
```

Result: **86 focused non-model tests passed in 0.53 s**, and the cached-model test passed in **2.08 s**. The real
cached-model sanity check passed: the related budget question ranked the budget chunk first, with network disabled.
On the reference laptop, `scripts/measure_embeddings.py` measured cached load at **1,789 ms**, 32 chunks at
**44.7 ms** (**1.4 ms/chunk**), 384 dimensions, and **527.50 MiB** process peak RSS. The fake-adapter 1/2/5-device
automated lag checks each drained within 0.5 s (their assertion bound, not a hardware performance claim). STT
contention with actual audio and real-phone correction rebuild behavior remain **Not run**.

### Next slice

Add recovery/late-arrival coverage and indexing-lag measurement, then provision and measure the real model. Keep
retrieval and Q&A out of this workstream until CON-09.

### Review pointers / remaining sign-off

CON-08 is ready for code review, but is not fully signed off until these checks are recorded:

1. Run the complete non-model repository suite to a conclusive final summary. In this environment it ran cleanly
   through 73% before the command window expired, so that is not a full-suite pass.
2. Compare at least one alternate small local embedding model against the pinned BGE choice, recording latency,
   memory, dependency cost, and the fixture retrieval result.
3. Measure actual STT contention while indexing at one, two, and five active streams.
4. Validate indexing lag and a correction-triggered rebuild with real phones/meeting audio.

The focused code checks, cached offline BGE ranking check, fake 1/2/5-device bounded-lag tests, recovery, retry,
priority, correction, and late-arrival tests have passed. These do not replace the outstanding full-suite and
target-hardware checks above.

## CON-08 completion — 2026-09-26

### Review fixes (indexer)

A review of `69467f4` found, and scratch repro tests confirmed, three indexing bugs:

1. A result written after the meeting-end flush was never indexed: every chunk was closed and `_index` returned
   early, including on restart recovery. STT windows still queued at **End** hit this.
2. A late result whose `t_start` fell between two closed chunks was never indexed or detected by recovery.
3. The meeting-end flush, the queue worker and the correction hook rewrote chunks concurrently; a race produced
   `UNIQUE constraint failed: TranscriptChunk.meeting_id, TranscriptChunk.chunk_index`, and the failure handler
   then hit the same error.

Rewrite of `server/rag/indexer.py`: every trigger runs one reconciliation per meeting under one indexer lock. It
compares stored chunks with the transcript (current labels) and re-embeds only chunks whose text, range or status
changed. Uncovered lines before the tail join the preceding closed chunk (or the following one), rebuilt in place;
lines after an all-closed index become new chunks, closed when the meeting has ended. The meeting-ended hook now
only enqueues (hooks must return quickly); the worker closes the tail after the settle window. Planning and
tokenizing run off the event loop.

Also fixed: split pieces of an over-cap utterance now get chunks of their own, so chunk ranges never overlap and
a correction relabels every piece within the cap; embedding failures back off (doubling from `retry_delay_s` to
`retry_max_delay_s` = 30 s) instead of retrying and auditing every ~4 s forever; one `ModelExecution` row per
embedding batch (was one per chunk with the batch duration); an unchanged tail is no longer re-embedded on every
pass; labels are computed in bulk from `speaker_label` instead of a per-row `utterance_view` with a hard-coded
threshold; the hard-coded `hard_max_tokens < 512` check is gone (the check against `[models.embedding].max_tokens`
remains); `provision_models.py --resource embedding --model X` verifies a candidate with its own dimension.

Behavior decision: `quiet_flush_s` is removed. The open tail is embedded after the settle window and is searchable
while open, which gives the bounded lag the time flush was for without closing a tail at every pause (which would
make many one-line chunks). The tail closes on target size at a speaker change, the hard cap, or meeting end.
`chunk_index` is creation order: rebuild overflow takes the next index. Documented in `docs/data-model.md` and
`docs/rag-and-qa.md`.

### Checks

```sh
.venv/bin/python -m pytest tests/test_chunker.py tests/test_indexer.py tests/test_vector_store.py tests/test_embedding_adapter.py tests/test_config.py -q -m "not model"
.venv/bin/python -m pytest tests/test_embedding_adapter.py -q -m model
.venv/bin/python -m pytest -q -m "not model"
```

- Focused: **46 passed**. New regression tests fail on the previous indexer (13 of 16 in `test_indexer.py`) and pass now.
- Real model: **1 passed** (cached BGE, `HF_HUB_OFFLINE=1`).
- Full non-model suite: **499 passed**, 7 model tests deselected (includes a new runtime test: two synthetic
  phones → chunks → `POST /end` → every line covered and every chunk closed).

### Measurements (reference laptop, M4 Pro 24 GB)

Embedding comparison (`scripts/measure_embeddings.py`, own process each, cached, offline):

| Candidate | Dim | Max input | Load | Per chunk | Peak RSS | Fixture top-1 |
|---|---|---|---|---|---|---|
| `BAAI/bge-small-en-v1.5` (pinned) | 384 | 512 | 58 ms | 3.8 ms | 618 MiB | 8/8 |
| `sentence-transformers/all-MiniLM-L6-v2` | 384 | 256 | 47 ms | 2.1 ms | 574 MiB | 8/8 |
| `BAAI/bge-base-en-v1.5` | 768 | 512 | 47 ms | 10.3 ms | 963 MiB | 8/8 |

Load excludes the `sentence_transformers` import; the earlier 1,789 ms figure timed `adapter.load()`, which includes it. BGE-small stays
pinned: MiniLM's 256-token input would truncate 300–500-token chunks; BGE-base costs 2.7× time and +345 MiB for no
fixture gain. The fixture is easy (all 8/8); it is a sanity check, not a quality benchmark.

STT with and without the indexer (`scripts/measure_stt.py pipeline --devices 1 2 5 --scenarios continuous
--seconds 30 [--embedding]`, synthetic phones through the real server, turbo STT, real BGE):

| Phones | STT post-window median / p95 off | on | Queue wait p95 off / on | Written → searchable median / p95 / max |
|---|---|---|---|---|
| 1 | 1,667 / 1,677 ms | 1,691 / 1,693 ms | 0 / 1 ms | 2.07 / 2.13 / 2.14 s |
| 2 | 1,942 / 2,894 ms | 1,974 / 2,878 ms | 1,670 / 1,720 ms | 1.57 / 2.12 / 2.12 s |
| 5 | 3,774 / 5,295 ms | 3,763 / 5,260 ms | 4,146 / 4,167 ms | 1.90 / 2.10 / 2.11 s |

No drops or failures in either run; event-loop lag p95 ≤ 2 ms. Peak RSS 1.93 GB off, 2.33 GB on. Indexing lag is
dominated by the 2 s settle window; lag target recorded as under 3 s.

### Hardware / model checks

| Check | Result |
|---|---|
| Embedding model comparison and pinned choice on the reference laptop | **Passed** (above) |
| Live transcription does not slow while indexing | **Passed** with synthetic phones through the real server and models; not repeated with real phones |
| Server starts and indexes with networking disabled | **Partly passed**: model tests and measurements ran with `HF_HUB_OFFLINE=1` and cache-only loading; a run with Wi-Fi physically off was **Not run** (needs the demo setup) |
| Indexing lag with live phones (≥ 2 phones) | **Not run**: no phones available in this session. Manual steps: `docs/manual-tests.md` Part D |
| Correction round-trip on a real transcript | **Not run** on real phones (automated correction/rebuild tests pass). `docs/manual-tests.md` D2 |

### Handoff to CON-09 / CON-14

- Tables: `TranscriptChunk` (text, range, status, `is_closed`), `TranscriptChunkVector` (`vec0`, `chunk_id` TEXT
  PK, `meeting_id` partition key, `float[384]`, cosine), `TranscriptIndexMeta` (model guard).
- Search: `VectorStore.search(conn, meeting_id, vector, k)`, meeting filter inside KNN; distance is cosine
  distance (0 = identical); CON-09 calibrates its relevance threshold on this metric.
- Question embedding: the same adapter (`runtime.embedding_adapter`); `index_status()` separates "not indexed
  yet" (`pending_utterances > 0`, `failed > 0`) from "nothing relevant".
- `chunk_index` is creation order, not time order; order citations by the utterance range.
- Real-phone checks above remain for the next hardware session.
