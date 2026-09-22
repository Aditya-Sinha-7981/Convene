"""CON-07 dashboard feed coverage: complete server views arrive as replaceable client state."""
import asyncio

import pytest
from aiohttp import ClientSession

from server.ids import new_id
from server.pipeline.scheduler import TranscribedWindow


async def receive_type(ws, kind: str):
    for _ in range(12):
        message = await ws.receive_json(timeout=3)
        if message["type"] == kind:
            return message
    raise AssertionError(f"did not receive {kind}")


@pytest.mark.asyncio
async def test_dashboard_receives_attributed_lines_and_corrections_as_full_views(server):
    async with ClientSession() as http:
        async with http.post(server.base_url + "/api/meetings", json={"title": "Review"}) as response:
            meeting_id = (await response.json())["meeting"]["meeting_id"]
        ws = await http.ws_connect(server.ws_url(f"/ws/dashboard/{meeting_id}"))
        try:
            device_id = new_id()
            async with http.post(server.base_url + f"/api/meetings/{meeting_id}/devices",
                                 json={"device_id": device_id, "display_name": "Priya"}) as response:
                registered = await response.json()
            device_event = await receive_type(ws, "device_status")
            assert device_event["device"]["device_id"] == device_id and device_event["seq"] is not None
            item = await server.runtime.attribution.attribute(TranscribedWindow(
                device_id, meeting_id, 1, "ok", "ship Friday", .9,
                "2026-09-23T10:00:00.000Z", "2026-09-23T10:00:01.000Z", 16000, 16000))
            created = await receive_type(ws, "utterance")
            assert created["utterance"]["utterance_id"] == item.utterance_id
            assert created["utterance"]["speaker_label"] == "Priya"
            assert created["utterance"]["low_confidence"] is False
            participant_id = registered["device"]["participants"][0]["participant_id"]
            async with http.post(server.base_url + f"/api/meetings/{meeting_id}/utterances/{item.utterance_id}/correct",
                                 json={"participant_id": participant_id}) as response:
                assert response.status == 200
            corrected = await receive_type(ws, "utterance_updated")
            assert corrected["utterance"]["corrected"] is True
            assert corrected["utterance"]["attribution_method"] == "manual_correction"
            await ws.send_json({"type": "not_an_action"})
            await asyncio.sleep(.05)
            assert not ws.closed
        finally:
            await ws.close()


@pytest.mark.asyncio
async def test_dashboard_snapshots_and_other_meeting_events_stay_isolated(server):
    async with ClientSession() as http:
        async with http.post(server.base_url + "/api/meetings", json={}) as response:
            first = (await response.json())["meeting"]["meeting_id"]
        async with http.post(server.base_url + "/api/meetings", json={}) as response:
            second = (await response.json())["meeting"]["meeting_id"]
        ws = await http.ws_connect(server.ws_url(f"/ws/dashboard/{first}"))
        try:
            async with http.get(server.base_url + f"/api/meetings/{first}") as response:
                meeting = await response.json()
            async with http.get(server.base_url + f"/api/meetings/{first}/transcript") as response:
                transcript = await response.json()
            assert isinstance(meeting["as_of_seq"], int) and isinstance(transcript["as_of_seq"], int)
            async with http.post(server.base_url + f"/api/meetings/{second}/devices",
                                 json={"device_id": new_id(), "display_name": "Elsewhere"}) as response:
                assert response.status == 201
            with pytest.raises(asyncio.TimeoutError):
                await ws.receive_json(timeout=.15)
        finally:
            await ws.close()
