"""CON-14: GET /api/meetings (history list) and POST /api/qa over HTTP, plus the /history page."""
import asyncio
from dataclasses import replace

import pytest
from aiohttp import ClientSession

from server import registry
from server.ids import new_id
from server.rag.reasoning import FakeReasoningAdapter
from server.repositories import exports, meetings, summaries
from tests.support.qa import QA, RAG, TopicEmbedding
from tests.support.server import settings_in, start_server
from tests.test_qa_api import meeting_with_a_line


@pytest.fixture
async def server(tmp_path):
    settings = replace(settings_in(tmp_path), rag=RAG, qa=QA)
    running = await start_server(settings, embedding_adapter=TopicEmbedding(),
                                 reasoning_adapter=FakeReasoningAdapter(lambda m: "Priya said forty thousand."))
    yield running
    await running.stop()


@pytest.fixture
async def http():
    async with ClientSession() as session:
        yield session


def seed(server, title, created_at, *, people=0, status=None):
    def write(tx):
        meeting = registry.create_meeting(tx, title, now=created_at)
        for number in range(people):
            registry.register_device(tx, meeting.meeting_id, new_id(), f"Person {number}")
        if status is not None:
            meetings.update(tx.conn, meeting.meeting_id, status=status)
        return meeting.meeting_id
    with server.runtime.db.transaction() as tx:
        return write(tx)


async def listing(http, server, query=""):
    async with http.get(server.base_url + "/api/meetings" + query) as response:
        return response.status, await response.json()


# --- GET /api/meetings ------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_empty_history_is_an_empty_list(server, http):
    assert await listing(http, server) == (200, {"meetings": [], "total": 0})


@pytest.mark.asyncio
async def test_meetings_are_newest_first_with_counts_and_flags(server, http):
    old = seed(server, "Sprint planning", "2026-09-20T09:00:00.000Z", people=3, status="ended")
    new = seed(server, "Design review", "2026-09-22T09:00:00.000Z", people=1)
    status, body = await listing(http, server)
    assert status == 200 and body["total"] == 2
    assert [m["meeting_id"] for m in body["meetings"]] == [new, old]
    first, second = body["meetings"]
    assert second["participant_count"] == 3 and first["participant_count"] == 1
    assert second["status"] == "ended" and second["title"] == "Sprint planning"
    assert {"created_at", "started_at", "ended_at", "has_summary", "has_export"} <= set(first)
    assert first["has_summary"] is False and first["has_export"] is False


@pytest.mark.asyncio
async def test_summary_and_export_flags_follow_ready_rows(server, http, monkeypatch):
    meeting_id = seed(server, "Flags", "2026-09-20T09:00:00.000Z")
    monkeypatch.setattr(summaries, "current", lambda conn, mid: object() if mid == meeting_id else None)
    monkeypatch.setattr(exports, "current", lambda conn, mid: None)
    [row] = (await listing(http, server))[1]["meetings"]
    assert row["has_summary"] is True and row["has_export"] is False


@pytest.mark.asyncio
async def test_title_and_date_filters(server, http):
    seed(server, "Sprint planning", "2026-09-20T09:00:00.000Z")
    seed(server, "Design review", "2026-09-21T23:59:00.000Z")
    seed(server, "Sprint retro", "2026-09-22T00:00:00.000Z")
    seed(server, "100% done_", "2026-09-23T10:00:00.000Z")

    titles = lambda body: [m["title"] for m in body["meetings"]]
    assert titles((await listing(http, server, "?q=SPRINT"))[1]) == ["Sprint retro", "Sprint planning"]
    assert titles((await listing(http, server, "?q=%25"))[1]) == ["100% done_"]  # % is literal, not a wildcard
    assert titles((await listing(http, server, "?q=_"))[1]) == ["100% done_"]
    # A date-only `to` includes that whole day; `from` starts at its midnight.
    assert titles((await listing(http, server, "?from=2026-09-21&to=2026-09-21"))[1]) == ["Design review"]
    status, body = await listing(http, server, "?from=2026-09-22T00:00:00Z")
    assert status == 200 and titles(body) == ["100% done_", "Sprint retro"]
    status, body = await listing(http, server, "?q=sprint&to=2026-09-21")
    assert titles(body) == ["Sprint planning"] and body["total"] == 1
    status, body = await listing(http, server, "?limit=1&offset=1")
    assert len(body["meetings"]) == 1 and body["total"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["?status=gone", "?limit=0", "?limit=201", "?offset=-1", "?limit=x",
                                   "?from=yesterday", "?to=2026-13-01", "?from=2026-09-21T10:00:00", "?q=" + "x" * 201])
async def test_bad_list_parameters_are_invalid_request(server, http, query):
    status, body = await listing(http, server, query)
    assert status == 400 and body["error"]["code"] == "invalid_request"


# --- POST /api/qa -----------------------------------------------------------------------------------------------


async def ask(http, server, body):
    async with http.post(server.base_url + "/api/qa", json=body) as response:
        return response.status, await response.json()


async def ended_meeting_with_a_line(http, server, text):
    meeting_id = await meeting_with_a_line(http, server, text)
    async with http.post(server.base_url + f"/api/meetings/{meeting_id}/end", json={}):
        pass
    for _ in range(100):
        state = await server.runtime.indexer.index_status(meeting_id)
        if state["pending_utterances"] == 0 and state["pending"] == 0:
            break
        await asyncio.sleep(.02)
    return meeting_id


@pytest.mark.asyncio
async def test_history_question_over_http_is_answered_and_not_pushed_to_dashboards(server, http):
    meeting_id = await ended_meeting_with_a_line(http, server, "The budget is forty thousand.")
    ws = await http.ws_connect(server.ws_url(f"/ws/dashboard/{meeting_id}"))
    try:
        status, body = await ask(http, server, {"question": "What is the budget?", "mode": "history",
                                                "meeting_ids": [meeting_id]})
        assert status == 200 and body["query"]["status"] == "answered" and body["query"]["mode"] == "history"
        assert body["query"]["meeting_id"] == meeting_id
        assert body["citations"][0]["meeting_id"] == meeting_id and body["citations"][0]["speakers"] == ["Priya"]
        assert body["scope"]["coverage"][0]["state"] == "searched"
        deadline = asyncio.get_running_loop().time() + 1.0
        while (left := deadline - asyncio.get_running_loop().time()) > 0:
            try:
                message = await ws.receive_json(timeout=left)
            except asyncio.TimeoutError:
                break
            assert message["type"] != "qa_answer", "a history answer reached a dashboard panel"
    finally:
        await ws.close()

    status, body = await ask(http, server, {"question": "What is the budget?"})  # mode and scope default
    assert status == 200 and body["scope"]["all_ended"] is True and body["query"]["meeting_id"] == meeting_id


@pytest.mark.asyncio
async def test_history_request_errors(server, http):
    live_id = await meeting_with_a_line(http, server, "The budget is forty thousand.")
    missing = new_id()
    cases = [
        ({"question": "", "meeting_ids": None}, 400, "invalid_request"),
        ({"question": "Budget?", "mode": "live"}, 400, "invalid_request"),
        ({"question": "Budget?", "meeting_ids": []}, 400, "invalid_request"),
        ({"question": "Budget?", "meeting_ids": ["nope"]}, 400, "invalid_request"),
        ({"question": "Budget?", "meeting_ids": [new_id() for _ in range(51)]}, 400, "invalid_request"),
        ({"question": "Budget?", "meeting_ids": [missing]}, 404, "meeting_not_found"),
        ({"question": "Budget?", "meeting_ids": [live_id]}, 409, "meeting_not_ended"),
    ]
    for body, expected_status, code in cases:
        status, response = await ask(http, server, body)
        assert (status, response["error"]["code"]) == (expected_status, code), body
    status, response = await ask(http, server, {"question": "Budget?", "meeting_ids": [missing]})
    assert missing in response["error"]["message"]
    async with http.post(server.base_url + "/api/qa", data="question=x",
                         headers={"Content-Type": "text/plain"}) as response:
        assert response.status == 415


@pytest.mark.asyncio
async def test_history_page_is_served(server, http):
    async with http.get(server.base_url + "/history") as response:
        assert response.status == 200
        page = await response.text()
    assert "/static/history.js" in page and "History" in page
