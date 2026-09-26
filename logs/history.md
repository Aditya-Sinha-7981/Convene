# Meeting history and cross-meeting Q&A (CON-14)

## Started — 2026-09-26

- Work began on `feature/history-qa` before the CON-12 stability gate at the project lead's direction.
- Finalized the history scope contract: explicit list (maximum 50) or all ended meetings; all-ended excludes live
  meetings; a single selected history meeting remains on `QAQuery.meeting_id`, while multi-meeting scope is in the
  `qa_query` audit payload.
- Existing implementation is live-only: `search_meeting` filters one meeting in sqlite-vec, `QAService.ask` rejects
  ended meetings, and `/meetings/<id>` has no Q&A panel. Next work is list/query services, multi-meeting retrieval,
  then the themed history and post-meeting UI.

## Implementation — 2026-09-27

Branch `feature/history-qa` (the 2026-09-26 uncommitted start was carried over from `main`).

### What was built

- `GET /api/meetings`: title substring (case-insensitive, `%`/`_` literal, ≤200 chars), inclusive `from`/`to` on
  `created_at` (a date-only `to` covers the whole day), newest first, a real `COUNT(*)` for `total`,
  `participant_count`, `has_summary`, `has_export`. Empty → `{"meetings": [], "total": 0}`.
- `server/rag/retrieval.py`: `search_meetings(conn, store, meeting_ids, ...)` with an explicit, non-empty scope. One
  partition-filtered KNN query per meeting, then merged, so other meetings cannot crowd out a requested one. Live
  `search_meeting` is now the one-meeting case.
- `VectorStore.foreign_model_meetings`: the index-meta check plus each meeting's latest embedding `ModelExecution`.
- `QAService.ask_history` and `POST /api/qa`. Live and history share `_search_and_generate` and `_persist`. History
  has its own prompt, with a `Meeting: <title> (<date>)` line per excerpt. Citations add `meeting_title` and
  `meeting_started_at`. The response adds `scope.all_ended` and `scope.coverage` (the index state of each meeting).
- `MeetingNotEndedError` → `409 meeting_not_ended`; `[qa].history_max_meetings = 50`.
- The dashboard hub no longer pushes `qa_answer` for history queries.
- UI: `/history` (list, filters, selection, scope-labelled Q&A), the shared `client/history_qa.js` component, and a
  post-meeting "Ask about this meeting" panel, participant list, and `#u-<utterance_id>` transcript anchors. There
  is a "Past meetings" link on the home nav and in the post-meeting header.

### Decisions (documented in api.md, rag-and-qa.md, data-model.md, frontend.md)

- Request `{question, mode: "history", meeting_ids: [..] | null}`; null = all **ended** meetings; cap 50.
- A listed meeting that has not ended → `409 meeting_not_ended`. An unknown id → `404 meeting_not_found` with the ids
  named. This follows `api.md` over the work order's suggested `invalid_request`.
- `QAQuery.meeting_id` = the meeting for a single-meeting history query; null for several. Scope in `qa_query.meeting_ids`.
- An embedding-model mismatch in any scoped meeting fails the whole query (`embedding_model_mismatch`, meeting named).
  Unindexed meetings are listed in `scope.coverage`, and the rest are still searched.
- History answers are not pushed to dashboards (contract change to `api.md` side effects).

### Checks

```sh
.venv/bin/python -m pytest tests/test_meetings_list.py tests/test_history_qa.py -q -m "not model"   # 32 passed
.venv/bin/python -m pytest -q -m "not model"   # 635 passed, 3 failed (test_db_migrations; pre-existing on main)
node --test tests/js/                          # 7 passed
```

- `test_db_migrations` expects 7 migrations but `0008_export.sql` exists; it fails identically on a clean `main`
  worktree, so it was not caused by this work.
- HTTP smoke check against the real app with fake models and 3 seeded ended meetings + 1 live meeting. The list,
  filter, "all ended" answer citing the right meeting, `no_grounding` for an absent fact, `meeting_not_ended` for the
  live meeting, and the pages were all served. New client scripts pass `node --check`.

### Hardware / model checks

| Check | Result |
|---|---|
| Browser rendering of `/history` and post-meeting Q&A | **Not run**: Chrome extension not connected this session |
| Two real meetings searchable after restart, right meeting and speaker | **Not run**: no real rehearsal meetings or loaded models this session |
| History honesty on real transcripts (`-m model`) | **Not run**: no model-marked history test written yet; needs weights |
| Different-embedding-model meeting fails clearly | **Passed** in automated tests (simulated); not run on a real database |
| History latency over several meetings on the reference laptop | **Not run** |
| Networking disabled | **Not run** (same code path as live Q&A, which passed in-process offline) |

### Open / handoff

- Add a `-m model` history honesty test on a two-meeting fixture (`tests/fixtures/qa/`) and record rates.
- Check on real data whether one `min_similarity` works across meetings.
- Do a visual/responsive pass of `/history` and the post-meeting panel in a browser (390 px and desktop).

## Rename and delete — 2026-09-27

The project lead asked for rename and delete. Deletion was out of CON-14 scope and the retention docs said "no
deletion". The lead chose **permanent erase** over a hidden flag, and no End button in the list (ending a meeting
stays on the dashboard). Recorded as ADR-27.

- `PATCH /api/meetings/{id}` `{title}`: any status, whitespace collapsed, ≤200 chars. Emits audit `meeting_renamed`
  and pushes `meeting_status`. Renaming to the same title writes nothing. A rename after the DOCX was rendered makes
  it stale; the summary is not affected.
- `DELETE /api/meetings/{id}`: `registry.erase_meeting` removes all of the meeting's rows in one transaction,
  including its audit events, its vectors, its `ModelExecution` rows (chunk/summary/query ids and
  `<device_id>/<window>`), and the multi-meeting history queries that cited its chunks. The DOCX files are removed
  after commit.
  - `meeting_deleted` (no `meeting_id`) is emitted *before* the deletes. `seq` is MAX+1, so this keeps it from going
    backwards and breaking other dashboards' resync cursors.
  - Refused with `409 meeting_active` while a device is `connected`/`joining`, and with `409 summary_in_progress`.
  - Runs under the indexer lock (`TranscriptIndexer.exclusive`). `_index` skips a meeting that no longer exists.
- UI: Rename/Delete on each `/history` row and on the post-meeting page. Delete uses a dialog that only enables once
  the exact title is typed.
- Docs: api.md (two route sections, the `meeting_active` code), data-model.md (catalog rows, export staleness,
  retention), decisions.md ADR-27, frontend.md.

Checks: `tests/test_meeting_admin.py` 9 passed. Full non-model suite 646 passed, plus the same 3 pre-existing
`test_db_migrations` failures. JS 7 passed. `tests/test_audit_emit.py`: the pinned set of events that may have no
meeting now includes `meeting_deleted`. Not checked in a browser: the Chrome extension was unavailable.
