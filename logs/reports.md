# Periodic reports (CON-18)

## Implementation — 2026-09-30

Branch `feature/periodic-reports`, from `main` at `41a66ed`.

### Decisions (the CON-18 §11 recommended defaults, adopted when the user asked for the task to be implemented; ADR-30, lead to confirm)

- Range: both bounds inclusive, parsed by the same `_bound` helper as `GET /api/meetings` (date-only = start of day /
  last millisecond, UTC; date-time needs a zone). Both are required. Filter field `created_at`.
- Only `ended` meetings are reported, oldest first; `created`/`live` in range are counted as `excluded_not_ended_count`.
- Figures: opened = current-summary items of reported meetings; closed = the last status change of an item within
  the range (`action_item_updated` with `to.status`) is `done` (`cancelled` listed separately), from any meeting,
  current-summary items only (ADR-28); "Open now" = opened items open at generation time.
- Cap `[reports].max_meetings = 50`: preview returns `over_cap`, download is `400 invalid_request` (never truncated).
- Empty range: preview returns 0, the button is disabled, download is `409 report_empty` (new documented code).
- Regenerated on every download into memory; no table, no `Export` row, no file, no audit event, no model call.
- Routes `GET /api/reports/preview`, `GET /api/reports/download`, page `/reports`.

### What was built

- `server/reports/service.py` (range resolution, per-meeting sections, figures), `server/export/report_renderer.py`
  (pure template; imports `_utc_date`/`elapsed` from `docx_renderer.py`, which is unchanged).
- Routes in `server/routes.py`; the render runs through `asyncio.to_thread`.
- `ReportsConfig` in `server/config.py`, `[reports]` in `config/convene.toml`.
- `client/reports.html/js/css`; links from the home nav and the `/history` intro.
- Docs: `api.md` (two route sections, route index, served page, `report_empty`), `export.md` ("Periodic report"),
  `frontend.md`, `requirements.md` (should-have row), `decisions.md` ADR-30.

### Checks

```sh
.venv/bin/python -m pytest tests/test_report_query.py tests/test_report_renderer.py tests/test_report_api.py tests/test_docx_renderer.py tests/test_meetings_list.py tests/test_api_contract_docs.py -q -m "not model"
```

- Report tests: 13 query + 6 renderer + 13 API, all passing. `tests/test_docx_renderer.py` passes unchanged, and so
  does the API contract doc test.
- The API tests use a fake reasoning adapter that fails if called, and assert that it recorded no call.
- Full non-model suite `-m "not model"` on the development Mac: **775 passed**, 12 model tests deselected (includes the Gemini connector tests).

### Hardware / model checks

| Check | Result |
|---|---|
| Model checks | Not required (no model is used) |
| Render time and size at the cap (50 meetings) on the reference laptop | Not run |
| STT latency while a large report renders | Not run (render is off the event loop; not measured) |
| Download with networking disabled | Not run (no network path exists in the code) |
| Open the DOCX in Word, LibreOffice, Pages/Quick Look | Not run |
| Page in a browser (presets, preview line, disabled button) | Not run: no browser session this time |

### Handoff

- The report reads CON-16's `action_item_updated` payload keys (`action_item_id`, `to.status`). Keep them stable.
- A later "policies changed in this period" section would read CON-17 version upload times. It is not built.
