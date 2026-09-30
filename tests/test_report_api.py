"""CON-18: GET /api/reports/preview, GET /api/reports/download and the /reports page, over HTTP."""
from dataclasses import replace
from io import BytesIO

import pytest
from aiohttp import ClientSession
from docx import Document

from server.config import ReportsConfig
from server.export.service import MIME
from server.rag.reasoning import FakeReasoningAdapter
from tests.support.reports import meeting_at
from tests.support.server import settings_in, start_server


@pytest.fixture
async def server(tmp_path):
    settings = replace(settings_in(tmp_path), reports=ReportsConfig(max_meetings=3))
    running = await start_server(settings, reasoning_adapter=FakeReasoningAdapter(
        fail=AssertionError("the report path must never call the reasoning model")))
    yield running
    await running.stop()


@pytest.fixture
async def http():
    async with ClientSession() as session:
        yield session


def seed(server):
    db = server.runtime.db
    meeting_at(db, "2026-09-02T10:00:00.000Z", title="One")
    meeting_at(db, "2026-09-03T10:00:00.000Z", title="Two", summary="failed")
    meeting_at(db, "2026-09-04T10:00:00.000Z", title="Running", ended=False)


def counts(server):
    conn = server.runtime.db.conn
    return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in ("AuditEvent", "Export", "Summary")}


@pytest.mark.asyncio
async def test_preview_matches_the_history_filter(server, http):
    seed(server)
    async with http.get(server.base_url + "/api/reports/preview?from=2026-09-01&to=2026-09-30") as response:
        body = await response.json()
    assert response.status == 200
    assert body == {"from": "2026-09-01T00:00:00.000Z", "to": "2026-09-30T23:59:59.999Z", "meeting_count": 2,
                    "with_summary_count": 1, "excluded_not_ended_count": 1, "max_meetings": 3, "over_cap": False}
    async with http.get(server.base_url + "/api/meetings?status=ended&from=2026-09-01&to=2026-09-30") as response:
        assert (await response.json())["total"] == body["meeting_count"]
    async with http.get(server.base_url + "/api/reports/preview?from=2026-09-02T15:30:00%2B05:30&to=2026-09-02") as response:
        assert (await response.json())["meeting_count"] == 1   # 10:00Z on the 2nd is after 10:00Z, inside the day


@pytest.mark.asyncio
async def test_download_is_a_docx_named_by_the_range_and_writes_nothing(server, http):
    seed(server)
    before = counts(server)
    async with http.get(server.base_url + "/api/reports/download?from=2026-09-01&to=2026-09-30") as response:
        content = await response.read()
        assert response.status == 200 and response.headers["Content-Type"] == MIME
        assert response.headers["Content-Disposition"] == 'attachment; filename="convene-report-2026-09-01-to-2026-09-30.docx"'
    body = "\n".join(p.text for p in Document(BytesIO(content)).paragraphs)
    assert "One — 2026-09-02 UTC" in body and "Summary failed: summary_invalid_output" in body and "Running" not in body
    assert counts(server) == before and server.runtime.reasoning_adapter.calls == []
    assert not server.runtime.settings.exports_dir.exists() or not any(server.runtime.settings.exports_dir.iterdir())


@pytest.mark.asyncio
async def test_empty_range_is_409_report_empty(server, http):
    async with http.get(server.base_url + "/api/reports/preview?from=2026-01-01&to=2026-01-31") as response:
        assert (await response.json())["meeting_count"] == 0
    async with http.get(server.base_url + "/api/reports/download?from=2026-01-01&to=2026-01-31") as response:
        assert response.status == 409 and (await response.json())["error"]["code"] == "report_empty"


@pytest.mark.asyncio
async def test_over_the_cap(server, http):
    for day in range(1, 5):
        meeting_at(server.runtime.db, f"2026-09-0{day}T10:00:00.000Z", title=str(day))
    async with http.get(server.base_url + "/api/reports/preview?from=2026-09-01&to=2026-09-30") as response:
        assert (await response.json())["over_cap"] is True
    async with http.get(server.base_url + "/api/reports/download?from=2026-09-01&to=2026-09-30") as response:
        body = await response.json()
    assert response.status == 400 and body["error"]["code"] == "invalid_request" and "narrow" in body["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", "from=2026-09-01", "to=2026-09-30", "from=2026-09-30&to=2026-09-01",
                                   "from=2026-9-1&to=2026-09-30", "from=2026-09-01T10:00:00&to=2026-09-30"])
async def test_bad_ranges_are_invalid_request(server, http, query):
    for path in ("preview", "download"):
        async with http.get(server.base_url + f"/api/reports/{path}?{query}") as response:
            assert response.status == 400, (path, query)
            assert (await response.json())["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_unsupported_format(server, http):
    async with http.get(server.base_url + "/api/reports/download?from=2026-09-01&to=2026-09-30&format=pdf") as response:
        assert response.status == 400 and (await response.json())["error"]["code"] == "unsupported_format"


@pytest.mark.asyncio
async def test_render_failure_is_500_and_stores_nothing(server, http, monkeypatch):
    seed(server)
    from server import routes

    def broken(*args):
        raise RuntimeError("python-docx broke")
    monkeypatch.setattr(routes, "render_report_bytes", broken)
    before = counts(server)
    async with http.get(server.base_url + "/api/reports/download?from=2026-09-01&to=2026-09-30") as response:
        assert response.status == 500 and (await response.json())["error"]["code"] == "export_render_failed"
    assert counts(server) == before


@pytest.mark.asyncio
async def test_page_is_served_and_linked(server, http):
    async with http.get(server.base_url + "/reports") as response:
        assert response.status == 200 and "reports.js" in await response.text()
    for page in ("/", "/history"):
        async with http.get(server.base_url + page) as response:
            assert 'href="/reports"' in await response.text(), page
