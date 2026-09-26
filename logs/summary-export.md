# Summary and export (CON-10, CON-11)

## 2026-09-26 — CON-10 summary and action items

Read AGENTS.md, the AI context, `summarization.md`, `api.md`, `data-model.md`, `models.md`, `frontend.md`,
ADR-12/18/22, and `logs/qa.md` before starting.

### What was built

- `server/summary/`: `service.py` (trigger, end-only drain, token check, generate, one stricter retry, persist),
  `prompt.py` (deterministic transcript assembly with `speaker_label`, prompt templates), `schema.py` (unwrap and
  validate, no repair), `views.py` (`GET …/summary` payload and staleness).
- Migration `0006_summary.sql` (`Summary` with `status`/`error_message`, `ActionItem`); `repositories/summaries.py`.
- `POST …/summarize` (202), `GET …/summary`; `end` starts summarization through the `on_meeting_ended` hook and
  reports `summary_pending`; `GET /api/meetings/{id}` fills `latest_summary` and `summary_pending`.
- Dashboard hub pushes `summary_ready` / `summary_failed`. Startup fails any `pending` row left by a stopped server.
- `count_tokens(messages)` on the reasoning adapter (chat template applied, as `generate` does).
- `AttributionService.wait_idle(timeout)`: waits for pending attribution writes without cancelling them (a
  `wait_for` around `drain()` would cancel the writes).
- `[summary]` configuration. Minimal post-meeting page `/meetings/{id}` (`client/post_meeting.html`, `.js`): status,
  summary, action items with owner or "Unassigned", staleness notice with regenerate, transcript with a small
  speaker-correction control. The dashboard links to it and goes there after "End meeting".
- `scripts/measure_summary.py` (cost against meeting length); `--summarize-at` in `scripts/measure_stt.py pipeline`.
- Six synthetic transcript fixtures in `tests/fixtures/transcripts/` (invented people; no real transcript).

### Decisions (ADR-23, proposed)

- Failure state as ADR-18 proposed: `status`/`error_message` on `Summary`, nullable `summary_text`; `generated_at`
  null while pending. This is still marked proposed in `data-model.md` and needs project-lead confirmation.
- Followed `api.md` where the task's recommendations differed: an empty transcript is `409 transcript_empty` on a
  manual trigger and no attempt on `end` (not a failed row); a concurrent trigger is `409 summary_in_progress`.
- Drain only on the end trigger. Drain timeout 15 s (STT backlog at end is normally a few segments; not measured
  on real phones).
- Long transcripts: fail with `transcript_too_long` above 16,000 prompt tokens (measured, below). No truncation.
- Audit: `summary_generated` / `summary_failed` gain `attempts`, `duration_ms`, `drain_timed_out`.

### Prompt iteration (real model, one run per fixture)

The first prompt was structurally reliable (18/18 valid) but gave poor minutes: several summaries copied transcript
lines in the first person, the unresolved-speakers fixture lost Dana's own task, and the long fixture repeated two
tasks. Changes, in order: third-person minutes in the model's own words, one paragraph per topic, chair's tasks
included, each task once (fixed the copying and the duplicates); unowned "someone needs to" tasks included (found
both in `many_action_items`); naming "meet again / follow up / continue the discussion" as non-tasks and stating
that an empty list is correct (stopped two or three filler items in `no_action_items`).

### Checks

```sh
.venv/bin/python -m pytest tests/test_summary_service.py tests/test_summary_schema.py tests/test_summary_api.py -q -m "not model"
.venv/bin/python -m pytest tests/test_summary_service.py -q -m model -s
.venv/bin/python scripts/measure_summary.py --minutes 5 15 30 50 60 75 90
.venv/bin/python scripts/measure_stt.py pipeline --devices 1 5 --seconds 60 --summarize-at 40 --scenarios continuous
.venv/bin/python -m pytest -q
```

- Summary non-model tests: 57 passed. Model tests: 2 passed (reliability, network blocked).
- Full non-model suite: **578 passed**, 11 model tests deselected. JS (`node --test tests/js/`): 7 passed.

### Hardware / model checks (reference laptop, M4 Pro 24 GB, pinned Llama-3.1-8B-Instruct-4bit)

| Check | Result |
|---|---|
| Structured-output reliability (6 fixtures x 3 runs, final prompt) | **Passed**: 18 runs, 18 model calls, parse-failure rate 0.000, retry rate 0.000, final-failure rate 0.000, no invented owners. Temperature 0 gave identical items across the three runs. The retry path is covered by unit tests only; the real model never needed it |
| Owners where the transcript is explicit | **Passed on fixtures**: every explicit owner correct; the unresolved speaker's task was given the generic label, which maps to a null owner; unowned tasks null |
| Summary quality, read by a person | **Fixtures only**: minutes are accurate and in the third person. Nits: the short stand-up summary is one sentence; "the offsite was booked" when it was pending Wi-Fi; one extra "Priya and Marcus agreed" sentence. Real meeting **Not run** (no phones this session) |
| Corrected line appears with its corrected name | **Passed** in automated tests (fake model: prompt carries the corrected name). Real correction on phones **Not run** |
| Context limit and memory (`measure_summary.py`, synthetic continuous talk ~150 wpm) | 5 min: 1,950 tokens, 16 s, 5.5 GB peak MLX · 15 min: 4,931, 31 s, 5.9 GB · 30 min: 9,411, 52 s, 6.3 GB · 50 min: 15,385, 98 s, 7.1 GB · 60 min: 18,359, 175 s, 7.6 GB, **invalid** (output hit the 1,200-token cap then in force) · 75 min: 22,844, 183 s, 8.2 GB · 90 min: 27,322, 262 s, 8.7 GB, **invalid** (cap). About 305–390 prompt tokens per minute; prefill 156–285 tokens/s. Limit set to 16,000 prompt tokens and output cap raised to 1,536 |
| Time to summary, longest fixture | `long_planning` (17 min, turn-taking, 1,941 tokens): 14–16 s. At the 16k limit: about 100 s |
| STT latency while a summary runs (manual, live meeting) | 1 phone continuous / 2 phones turn-taking: summary 5.8 s / 7.0 s, no window ended during it. 5 phones turn-taking: summary 21 s; windows ending during it 5.8 s median post-window latency vs 2.9 s outside; no drops. **5 phones continuous: summary 30 s; windows ending during it 17.5 s median / 20.4 s p95 (CON-09 baseline 3.8 s); the backlog lingered afterwards; the console showed 3 dropped windows.** At meeting end STT is drained first, so nothing competes |
| Offline | **Passed** in-process: a full summary (load, generate, persist) with every non-loopback socket and DNS lookup raising, `HF_HUB_OFFLINE=1`, zero connection attempts. Wi-Fi physically off **Not run** |
| Post-meeting page | Checked in headless Chromium against a fake-model server: ready, stale (after a correction; regenerate shown), and failed-rerun-with-earlier-success states render; 390 px width has no horizontal overflow. Button clicks and live push updates were not driven in the browser |

### Handoff

- CON-11: render `summaries.current(meeting_id)` and `summaries.action_items(summary_id)`; staleness via
  `summary.views.is_stale` / `attribution.staleness`. `latest_export` in `GET /api/meetings/{id}` is still null.
  Add export controls to `client/post_meeting.html`.
- CON-12: rehearse end → summary on real phones; confirm the drain timeout covers the real end-of-meeting backlog;
  record time to summary. Summarize at meeting end in the demo: a manual summary during heavy live speech delays
  STT (above).
- Open: project-lead confirmation of the proposed `Summary` failure fields (ADR-18) and ADR-23.

## 2026-09-26 — CON-11 deterministic DOCX export

### What was built

- `server/export/` provides a pure fixed-template `python-docx` renderer and a service that snapshots stored rows,
  writes to a temporary file, fsyncs, and atomically replaces `data/exports/<meeting_id>.docx`.
- Migration `0008_export.sql` and `repositories/exports.py` store retained `pending`, `ready`, and `failed` attempts.
  A failed retry therefore cannot remove or hide the earlier ready file.
- `GET /api/meetings/{id}/export?format=docx` renders only when absent or stale; the status route supplies the
  post-meeting view. The service derives staleness from the export, summary, and transcript audit sequences.
- Successful and failed attempts emit `export_created` / `export_failed`, which the dashboard maps to
  `export_ready` / `export_failed`. A ready summary triggers a best-effort, isolated automatic export.
- The post-meeting view shows ready, pending, failed, and stale export states plus a DOCX download link.

### Decisions

- Determinism is semantic document-content identity, not byte identity. Core timestamps are fixed; DOCX ZIP entry
  timestamps are implementation details and can differ. Rendered dates and elapsed transcript clocks use UTC.
- Export attempts are retained rather than overwritten. The current export is the most recent `ready` attempt.

### Checks

```sh
.venv/bin/python -m pytest tests/test_docx_renderer.py tests/test_api_contract_docs.py -q
node --check client/post_meeting.js
.venv/bin/python -m compileall -q server
```

- **Passed:** 40 focused tests. Renderer smoke test re-opened a generated DOCX; migration smoke test confirmed
  schema version 8 and all Export columns.
- **Not run:** existing `tests/test_summary_api.py` requires a loopback server, but this sandbox denies binding
  `127.0.0.1` (`[Errno 1] operation not permitted`).

### Hardware / manual checks

| Check | Result |
|---|---|
| Open in Word, Pages, and LibreOffice | Not run — viewers/demo laptop unavailable in this session |
| Real corrected/generic/null-owner meeting | Not run — no real meeting used |
| Longest expected meeting render time | Not run — synthetic scale benchmark still needed |
| Network-disabled export | Not run — service has no network path, but physical check remains |
