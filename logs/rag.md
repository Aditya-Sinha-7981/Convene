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
