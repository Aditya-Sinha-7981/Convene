"""CON-09 API: POST /api/meetings/{id}/qa and the qa_answer dashboard push."""
import asyncio
from dataclasses import replace

import pytest
from aiohttp import ClientSession

from server.ids import new_id
from server.pipeline.scheduler import TranscribedWindow
from server.rag.reasoning import FakeReasoningAdapter
from tests.support.qa import QA, RAG, TopicEmbedding
from tests.support.server import settings_in, start_server
from tests.test_dashboard_feed import receive_type


@pytest.fixture
async def qa_server(tmp_path):
    settings = replace(settings_in(tmp_path), rag=RAG, qa=QA)
    running = await start_server(settings, embedding_adapter=TopicEmbedding(),
                                 reasoning_adapter=FakeReasoningAdapter(lambda m: "Priya said forty thousand."))
    yield running
    await running.stop()


async def meeting_with_a_line(http, server, text):
    async with http.post(server.base_url + "/api/meetings", json={}) as response:
        meeting_id = (await response.json())["meeting"]["meeting_id"]
    device_id = new_id()
    async with http.post(server.base_url + f"/api/meetings/{meeting_id}/devices",
                         json={"device_id": device_id, "display_name": "Priya"}):
        pass
    await server.runtime.attribution.attribute(TranscribedWindow(
        device_id, meeting_id, 1, "ok", text, .9, "2026-09-26T10:00:00.000Z", "2026-09-26T10:00:01.000Z",
        16000, 16000))
    for _ in range(100):
        if (await server.runtime.indexer.index_status(meeting_id))["pending_utterances"] == 0:
            break
        await asyncio.sleep(.02)
    return meeting_id


@pytest.mark.asyncio
async def test_a_question_is_answered_persisted_and_pushed_to_the_dashboard(qa_server):
    async with ClientSession() as http:
        meeting_id = await meeting_with_a_line(http, qa_server, "The budget is forty thousand.")
        ws = await http.ws_connect(qa_server.ws_url(f"/ws/dashboard/{meeting_id}"))
        try:
            async with http.post(qa_server.base_url + f"/api/meetings/{meeting_id}/qa",
                                 json={"question": "What is the budget?", "mode": "live"}) as response:
                assert response.status == 200
                body = await response.json()
            assert body["query"]["status"] == "answered" and body["reason"] is None
            assert body["citations"][0]["speakers"] == ["Priya"]
            pushed = await receive_type(ws, "qa_answer")
            assert pushed["query"]["query_id"] == body["query"]["query_id"]
            assert pushed["citations"] == body["citations"] and pushed["seq"] is not None

            async with http.post(qa_server.base_url + f"/api/meetings/{meeting_id}/qa",
                                 json={"question": "Where is the hotel?"}) as response:
                absent = await response.json()
            assert response.status == 200 and absent["query"]["status"] == "no_grounding"
            assert (await receive_type(ws, "qa_answer"))["query"]["status"] == "no_grounding"
        finally:
            await ws.close()


@pytest.mark.asyncio
async def test_request_level_errors_use_the_error_envelope(qa_server):
    async with ClientSession() as http:
        meeting_id = await meeting_with_a_line(http, qa_server, "The budget is forty thousand.")
        url = qa_server.base_url + f"/api/meetings/{meeting_id}/qa"
        cases = [
            (http.post(url, json={"question": ""}), 400, "invalid_request"),
            (http.post(url, json={"question": "ok?", "mode": "history"}), 400, "invalid_request"),
            (http.post(url, data="question", headers={"content-type": "text/plain"}), 415, "unsupported_media_type"),
            (http.post(qa_server.base_url + f"/api/meetings/{new_id()}/qa", json={"question": "hi?"}), 404,
             "meeting_not_found"),
            (http.post(qa_server.base_url + "/api/meetings/not-a-uuid/qa", json={"question": "hi?"}), 400,
             "invalid_request"),
        ]
        for request, status, code in cases:
            async with request as response:
                assert (response.status, (await response.json())["error"]["code"]) == (status, code)
        async with http.post(qa_server.base_url + f"/api/meetings/{meeting_id}/end") as response:
            assert response.status == 202
        async with http.post(url, json={"question": "What is the budget?"}) as response:
            assert response.status == 409 and (await response.json())["error"]["code"] == "meeting_ended"
