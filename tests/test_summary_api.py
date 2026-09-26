"""CON-10 API: end and summarize triggers, GET summary, the summary_* pushes, and the post-meeting page."""
import asyncio
from dataclasses import replace

import pytest
from aiohttp import ClientSession

from server.config import SummaryConfig
from server.ids import new_id
from server.rag.reasoning import FakeReasoningAdapter
from server.repositories import summaries
from tests.support.server import settings_in, start_server
from tests.support.summary import seed_fixture, summary_json
from tests.test_dashboard_feed import receive_type

REPLY = summary_json("Sam is fixing the rounding bug and Marcus is sending the crash numbers.",
                     [("Fix the rounding bug", "Sam"), ("Send the crash numbers to the client", "marcus"),
                      ("Confirm the beta date", None)])


async def serve(tmp_path, adapter):
    settings = replace(settings_in(tmp_path), summary=SummaryConfig(generation_timeout_s=5.0, drain_timeout_s=1.0))
    return await start_server(settings, reasoning_adapter=adapter)


@pytest.fixture
async def summary_server(tmp_path):
    running = await serve(tmp_path, FakeReasoningAdapter(lambda messages: REPLY))
    yield running
    await running.stop()


async def seeded(server, name="short_standup"):
    runtime = server.runtime
    return (await seed_fixture(runtime.db, runtime.attribution, name))[0]


async def wait_for_outcome(http, server, meeting_id):
    for _ in range(250):
        async with http.get(server.base_url + f"/api/meetings/{meeting_id}/summary") as response:
            body = await response.json()
        if response.status == 200 and not body["summary_pending"]:
            return body
        await asyncio.sleep(.02)
    raise TimeoutError("the summary attempt did not finish")


@pytest.mark.asyncio
async def test_ending_a_meeting_summarizes_once_and_pushes_summary_ready(summary_server):
    meeting = await seeded(summary_server)
    base = summary_server.base_url + f"/api/meetings/{meeting.meeting_id}"
    async with ClientSession() as http:
        ws = await http.ws_connect(summary_server.ws_url(f"/ws/dashboard/{meeting.meeting_id}"))
        try:
            async with http.post(base + "/end") as response:
                assert response.status == 202 and (await response.json())["summary_pending"] is True
            pushed = await receive_type(ws, "summary_ready")
            body = await wait_for_outcome(http, summary_server, meeting.meeting_id)
        finally:
            await ws.close()
        assert pushed["summary_id"] == body["summary"]["summary_id"] and pushed["seq"] is not None
        assert body["summary"]["status"] == "ready" and body["latest_attempt"] == body["summary"]
        assert body["stale"] is False and body["summary"]["summary_text"].startswith("Sam is fixing")
        assert [(item["text"], item["owner_display_name"]) for item in body["action_items"]] == [
            ("Fix the rounding bug", "Sam"), ("Send the crash numbers to the client", "Marcus"),
            ("Confirm the beta date", None)]
        assert body["action_items"][1]["owner_participant_id"] == meeting.participants["Marcus"]

        async with http.post(base + "/end") as response:   # idempotent: nothing further happens
            assert response.status == 200 and (await response.json())["summary_pending"] is False
        async with http.get(base) as response:
            detail = await response.json()
        assert detail["latest_summary"]["summary_id"] == body["summary"]["summary_id"]
        assert detail["summary_pending"] is False
    assert len(summaries.list_for_meeting(summary_server.runtime.db.conn, meeting.meeting_id)) == 1


@pytest.mark.asyncio
async def test_ending_an_empty_meeting_starts_no_summary(summary_server):
    async with ClientSession() as http:
        async with http.post(summary_server.base_url + "/api/meetings", json={}) as response:
            meeting_id = (await response.json())["meeting"]["meeting_id"]
        async with http.post(summary_server.base_url + f"/api/meetings/{meeting_id}/end") as response:
            assert response.status == 202 and (await response.json())["summary_pending"] is False
        async with http.get(summary_server.base_url + f"/api/meetings/{meeting_id}/summary") as response:
            assert response.status == 404 and (await response.json())["error"]["code"] == "summary_not_found"
        async with http.post(summary_server.base_url + f"/api/meetings/{meeting_id}/summarize") as response:
            assert response.status == 409 and (await response.json())["error"]["code"] == "transcript_empty"


@pytest.mark.asyncio
async def test_manual_summarize_on_a_live_meeting_and_the_in_progress_conflict(tmp_path):
    server = await serve(tmp_path, FakeReasoningAdapter(lambda messages: REPLY, delay_s=.3))
    try:
        meeting = await seeded(server)
        base = server.base_url + f"/api/meetings/{meeting.meeting_id}"
        async with ClientSession() as http:
            async with http.post(base + "/summarize", json={}) as response:
                assert response.status == 202
                started = await response.json()
            assert started["summary_pending"] is True
            async with http.post(base + "/summarize") as response:
                assert response.status == 409 and (await response.json())["error"]["code"] == "summary_in_progress"
            async with http.get(base + "/summary") as response:
                pending = await response.json()
            assert pending["summary_pending"] is True and pending["summary"] is None
            assert pending["latest_attempt"]["status"] == "pending"
            assert pending["latest_attempt"]["generated_at"] is None
            body = await wait_for_outcome(http, server, meeting.meeting_id)
            assert body["summary"]["summary_id"] == started["summary_id"]
            async with http.get(base) as response:
                assert (await response.json())["meeting"]["status"] == "live"  # summarize never ends a meeting
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_failed_summary_is_pushed_and_the_transcript_still_works(tmp_path):
    server = await serve(tmp_path, FakeReasoningAdapter(lambda messages: "I cannot produce JSON today."))
    try:
        meeting = await seeded(server)
        base = server.base_url + f"/api/meetings/{meeting.meeting_id}"
        async with ClientSession() as http:
            ws = await http.ws_connect(server.ws_url(f"/ws/dashboard/{meeting.meeting_id}"))
            try:
                async with http.post(base + "/end") as response:
                    assert response.status == 202
                pushed = await receive_type(ws, "summary_failed")
            finally:
                await ws.close()
            assert pushed["error_message"].startswith("model output did not parse after one retry")
            body = await wait_for_outcome(http, server, meeting.meeting_id)
            assert body["summary"] is None and body["action_items"] == [] and body["stale"] is False
            assert body["latest_attempt"]["status"] == "failed"
            assert body["latest_attempt"]["summary_id"] == pushed["summary_id"]
            async with http.get(base + "/transcript") as response:
                assert response.status == 200 and len((await response.json())["utterances"]) == 6
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_correction_after_the_summary_makes_it_stale(summary_server):
    meeting = await seeded(summary_server)
    base = summary_server.base_url + f"/api/meetings/{meeting.meeting_id}"
    async with ClientSession() as http:
        async with http.post(base + "/end"):
            pass
        first = await wait_for_outcome(http, summary_server, meeting.meeting_id)
        line = meeting.utterances[1].utterance_id
        async with http.post(base + f"/utterances/{line}/correct", json={"display_name": "Priya"}) as response:
            assert response.status == 200   # corrections are allowed after the meeting ends
        async with http.get(base + "/summary") as response:
            assert (await response.json())["stale"] is True
        async with http.post(base + "/summarize") as response:
            assert response.status == 202
        second = await wait_for_outcome(http, summary_server, meeting.meeting_id)
        assert second["stale"] is False and second["summary"]["summary_id"] != first["summary"]["summary_id"]


@pytest.mark.asyncio
async def test_request_errors_use_the_error_envelope(summary_server):
    unknown = new_id()
    async with ClientSession() as http:
        cases = [
            (http.post(summary_server.base_url + f"/api/meetings/{unknown}/summarize"), 404, "meeting_not_found"),
            (http.get(summary_server.base_url + f"/api/meetings/{unknown}/summary"), 404, "meeting_not_found"),
            (http.post(summary_server.base_url + "/api/meetings/nope/summarize"), 400, "invalid_request"),
            (http.get(summary_server.base_url + "/api/meetings/nope/summary"), 400, "invalid_request"),
            (http.post(summary_server.base_url + f"/api/meetings/{unknown}/summarize", data="x",
                       headers={"content-type": "text/plain"}), 415, "unsupported_media_type"),
        ]
        for request, status, code in cases:
            async with request as response:
                assert (response.status, (await response.json())["error"]["code"]) == (status, code)


@pytest.mark.asyncio
async def test_the_post_meeting_page_is_served_and_an_ended_dashboard_redirects_to_it(summary_server):
    meeting = await seeded(summary_server)
    async with ClientSession() as http:
        async with http.get(summary_server.base_url + f"/meetings/{meeting.meeting_id}") as response:
            assert response.status == 200 and "post_meeting.js" in await response.text()
        async with http.get(summary_server.base_url + f"/meetings/{new_id()}") as response:
            assert response.status == 404
        async with http.post(summary_server.base_url + f"/api/meetings/{meeting.meeting_id}/end"):
            pass
        async with http.get(summary_server.base_url + f"/dashboard/{meeting.meeting_id}",
                            allow_redirects=False) as response:
            assert response.status == 302 and response.headers["location"] == f"/meetings/{meeting.meeting_id}"
        await wait_for_outcome(http, summary_server, meeting.meeting_id)
