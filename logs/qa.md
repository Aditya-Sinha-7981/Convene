# Live Q&A (CON-09)

## 2026-09-26 — implementation

Read AGENTS.md, the AI context, the Q&A, API, data-model, models, architecture, frontend, testing and demo docs,
ADR-06/11/14/15, and `logs/rag.md` before starting. Project-lead decisions this session: measure two 7–8B
candidates; commit when done.

### What was built

- `server/rag/reasoning.py`: the shared `generate(messages, max_tokens, temperature, timeout)` adapter (reused by
  CON-10), `mlx-lm` implementation and a fake. Loads from the local cache only, warms up at startup and stays
  resident. The deadline is checked between tokens and covers waiting for an earlier generation.
- `server/rag/retrieval.py`: meeting-scoped search, as-of filter, threshold. Imported only by `qa.py` (a test
  checks statically; `scripts/calibrate_qa.py` also uses it offline).
- `server/rag/qa.py`: validate → index state → embed question → search → threshold → prompt → generate →
  persist → push. No model call below the threshold.
- `server/rag/citations.py`: citations and the `QAQuery` view from stored rows (shared with the dashboard hub).
- Migration `0005_qa_query.sql`, `repositories/qa_queries.py`; `POST /api/meetings/{id}/qa`; `qa_answer` push.
- `[models.reasoning]` and `[qa]` configuration; startup loads the reasoning model and fails loudly if missing.
- Dashboard "Ask the room" panel: three distinct outcome states, pending state, clickable citations that
  highlight transcript lines, retry on failure.
- `scripts/calibrate_qa.py` (threshold + honesty report), `scripts/measure_reasoning.py`, `--qa` in
  `scripts/measure_stt.py pipeline`, `--resource reasoning` in `scripts/provision_models.py`.
- Dependency added: `mlx-lm>=0.31,<0.32` (approved as part of the model download decision). It upgrades
  `transformers` to 5.x; `sentence-transformers` 3.4 still passes its model tests on it.

### Decisions (ADR-22)

- Outcome reasons travel in the response and `qa_query` audit payload, not a new column. `error.code` stays
  `retrieval_failed` / `generation_failed`.
- Empty index: `not_indexed_yet` (`no_grounding`) while healthy; `index_unavailable` (`failed`) when a chunk
  failed with nothing ready or indexing is over 30 s behind.
- As-of: a chunk is eligible if its first line was written at or before the question.
- Cited chunks = the chunks given to the model. One generation at a time.
- No list-past-questions route (G18); a reload clears the panel.

### Threshold calibration

`scripts/calibrate_qa.py` on `tests/fixtures/qa/product_sync.json` (30 lines, 3 chunks at 400 tokens; 12 answerable,
12 unanswerable questions, several deliberate near-misses). Best cosine similarity per question:

| Class | n | min | median | max |
|---|---|---|---|---|
| answerable | 12 | 0.521 | 0.646 | 0.746 |
| unanswerable | 12 | 0.400 | 0.525 | 0.675 |

The classes overlap, so no threshold separates them at chunk granularity. `min_similarity = 0.50` keeps every
answerable question and stops 5 of 12 unanswerable ones (including the general-knowledge "capital of France")
before the model; the rest reach the model, which must decline. Tried and not adopted: the BGE query
instruction prefix (5 vs 6 of 12 unanswerable passing, marginal) and smaller chunks (no better separation).
Recalibrate on real transcripts in CON-12.

### Reasoning model comparison (reference laptop)

| Candidate | Load | Peak MLX | Generation | Grounded correct (3 repeats) | Fabricated | Answer median / max |
|---|---|---|---|---|---|---|
| Llama-3.1-8B-Instruct-4bit (**pinned**) | 1.7 s | 5.49 GB | 49 tok/s | 36/36 | 0/36 | 3.9 / 4.8 s |
| Qwen2.5-7B-Instruct-4bit | 0.7 s | 5.00 GB | 53 tok/s | 33/36 | 0/36 | 5.2 / 6.8 s |

Qwen declined "How many users are invited to the beta?" every time (safe, but a miss). In one earlier Qwen run,
during the concurrent 4.5 GB Llama download, two attempts at the iOS near-miss question ran past 30 s: the first
hung in one step, and the second waited behind it on the adapter lock until the backstop. Both were reported as
`failed`, not fabricated. The same questions took 3.4 s when rerun. Fix: the deadline now covers waiting for the
lock (unit-tested), so a stuck generation cannot stall the next question. The cause of the slow step is not known.

### Checks

```sh
.venv/bin/python -m pytest tests/test_retrieval.py tests/test_qa_service.py tests/test_qa_api.py -q -m "not model"
.venv/bin/python -m pytest tests/test_qa_service.py tests/test_embedding_adapter.py -q -m model
node --test tests/js/
.venv/bin/python -m pytest -q -m "not model"
```

- Q&A non-model: 22 passed. Model: 3 passed (honesty gate over 72 asks with Llama; network-blocked run with no
  socket attempts; embedding). JS state: 7 passed. Full non-model suite: **521 passed**, 9 model tests deselected.
- Dashboard panel checked in headless Chromium against a server with fake models: the three states render
  distinctly, a citation click highlights the cited line, no console errors, no horizontal scroll at 390 px.
  This found and fixed a CON-07 bug: `.empty-state`/`.qa-placeholder` `display:grid` overrode `hidden`, so
  "Listening for the first voice" stayed visible above transcript lines.

### Hardware / model checks

| Check | Result |
|---|---|
| RAG honesty (fixture, 3 repeats) | **Passed** with Llama: 0/36 fabricated, 36/36 grounded correct; demo question and its absent-fact counterpart correct every time |
| Latency with STT running | **Measured** (synthetic phones, real models, a question every 8 s): answers 1.1–4.2 s. STT post-window median / p95: 1 phone 1.69/1.69 → 1.72/2.23 s; 2 phones 1.97/2.88 → 2.21/3.28 s; 5 phones 3.76/5.26 → 4.46/6.70 s; no drops; event-loop max stall 119 ms (416 ms once with Qwen) |
| Memory with STT + embedding + reasoning | **Measured**: 5.7 GB peak RSS, 7.1 GB peak MLX (budget 7–9 GB estimate) |
| Offline | **Passed** in-process with every non-loopback socket and DNS lookup raising; Wi-Fi physically off **Not run** |
| Live scenario with real phones | **Not run**: no phones this session (`docs/manual-tests.md` Part E) |
| Point-in-time with real speech | **Not run** on phones; automated as-of tests pass |

### Handoff

- CON-10: reuse `build_reasoning_adapter` / `runtime.reasoning_adapter.generate(...)` behind
  `priority.wait_for_turn("reasoning")`. Generation is serialized by the adapter lock; a summary and a question
  queue behind each other.
- CON-12: recalibrate `[qa].min_similarity` on real transcripts; rehearse the demo pair — "What did we decide about
  the beta launch date?" (answered, cites Priya's line) and "Who is responsible for the marketing budget?"
  (`no_grounding`).
- CON-14: `search_meeting` takes the scope explicitly; history mode adds a multi-meeting variant.
