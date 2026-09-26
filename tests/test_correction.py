import pytest
from aiohttp import ClientSession

from server import registry
from server.attribution.service import AttributionService
from server.attribution.staleness import artifact_is_stale, transcript_high_water
from server.colors import PALETTE
from server.config import AttributionConfig
from server.errors import ParticipantNotFoundError, ValidationError
from server.ids import new_id
from server.pipeline.scheduler import TranscribedWindow
from server.repositories import audit_events, utterances


async def setup_utterance(db):
    with db.transaction() as tx:
        meeting = registry.create_meeting(tx, "Review")
        a = registry.register_device(tx, meeting.meeting_id, new_id(), "A")
        b = registry.register_device(tx, meeting.meeting_id, new_id(), "B")
    service = AttributionService(db, AttributionConfig())
    item = await service.attribute(TranscribedWindow(
        a.device.device_id, meeting.meeting_id, 1, "ok", "hello", .9,
        "2026-09-23T10:00:00.000Z", "2026-09-23T10:00:01.000Z", 16000, 16000))
    return service, meeting, a, b, item


@pytest.mark.asyncio
async def test_correction_preserves_first_original_and_audits_every_change(db):
    service, meeting, a, b, item = await setup_utterance(db)
    first = await service.correct(meeting.meeting_id, item.utterance_id,
                                  {"participant_id": b.participants[0].participant_id})
    assert first.changed and first.utterance.corrected
    assert first.utterance.original_participant_id == a.participants[0].participant_id
    assert first.utterance.attribution_method == "manual_correction" and first.utterance.attribution_confidence == 1
    second = await service.correct(meeting.meeting_id, item.utterance_id,
                                   {"participant_id": a.participants[0].participant_id})
    assert second.utterance.original_participant_id == a.participants[0].participant_id
    events = audit_events.list_events(db.conn, meeting_id=meeting.meeting_id, event_type="utterance_corrected")
    assert len(events) == 2 and events[0].payload["from"]["attribution_method"] == "device"
    assert events[1].payload["from"]["corrected"] is True


@pytest.mark.asyncio
async def test_confirmation_writes_once_then_replay_is_noop(db):
    service, meeting, a, _, item = await setup_utterance(db)
    target = {"participant_id": a.participants[0].participant_id}
    confirmed = await service.correct(meeting.meeting_id, item.utterance_id, target)
    replay = await service.correct(meeting.meeting_id, item.utterance_id, target)
    assert not confirmed.changed and confirmed.wrote and not replay.wrote
    assert len(audit_events.list_events(db.conn, event_type="utterance_corrected")) == 1


@pytest.mark.asyncio
async def test_name_can_create_participant_and_invalid_target_rolls_back(db):
    service, meeting, _, _, item = await setup_utterance(db)
    before = utterances.get(db.conn, item.utterance_id)
    with pytest.raises(ParticipantNotFoundError):
        await service.correct(meeting.meeting_id, item.utterance_id, {"participant_id": new_id()})
    assert utterances.get(db.conn, item.utterance_id) == before
    named = await service.correct(meeting.meeting_id, item.utterance_id, {"display_name": " Sam "})
    assert named.created_participant and named.participant.display_name == "Sam"
    assert named.participant.color in PALETTE  # a speaker named during correction gets a colour too (ADR-25)
    assert named.participant.device_id == item.device_id
    with pytest.raises(ValidationError):
        await service.correct(meeting.meeting_id, item.utterance_id, {})


@pytest.mark.asyncio
async def test_staleness_uses_the_audit_high_water(db):
    service, meeting, _, b, item = await setup_utterance(db)
    built_at = transcript_high_water(db.conn, meeting.meeting_id)
    assert not artifact_is_stale(db.conn, meeting.meeting_id, built_at)
    await service.correct(meeting.meeting_id, item.utterance_id,
                          {"participant_id": b.participants[0].participant_id})
    assert artifact_is_stale(db.conn, meeting.meeting_id, built_at)


@pytest.mark.asyncio
async def test_correction_hooks_run_after_commit_and_failures_are_isolated(db):
    service, meeting, _, b, item = await setup_utterance(db)
    seen = []
    service.register_correction_hook(lambda utterance_id, meeting_id: seen.append((utterance_id, meeting_id)))
    service.register_correction_hook(lambda *_: (_ for _ in ()).throw(RuntimeError("boom")))
    result = await service.correct(meeting.meeting_id, item.utterance_id,
                                   {"participant_id": b.participants[0].participant_id})
    await service.drain()
    assert result.wrote and seen == [(item.utterance_id, meeting.meeting_id)]
    assert utterances.get(db.conn, item.utterance_id).participant_id == b.participants[0].participant_id
    assert audit_events.list_events(db.conn, meeting_id=meeting.meeting_id, event_type="hook_failed")


@pytest.mark.asyncio
async def test_transcript_and_correction_api_round_trip(server):
    async with ClientSession() as http:
        async with http.post(server.base_url + "/api/meetings", json={}) as response:
            meeting_id = (await response.json())["meeting"]["meeting_id"]
        device_id = new_id()
        async with http.post(server.base_url + f"/api/meetings/{meeting_id}/devices",
                             json={"device_id": device_id, "display_name": "A"}) as response:
            registered = await response.json()
        item = await server.runtime.attribution.attribute(TranscribedWindow(
            device_id, meeting_id, 1, "ok", "hello", .9,
            "2026-09-23T10:00:00.000Z", "2026-09-23T10:00:01.000Z", 16000, 16000))
        async with http.get(server.base_url + f"/api/meetings/{meeting_id}/transcript") as response:
            body = await response.json()
            assert response.status == 200 and body["utterances"][0]["speaker_label"] == "A"
            assert body["utterances"][0]["seq"] <= body["as_of_seq"]
        participant_id = registered["device"]["participants"][0]["participant_id"]
        url = server.base_url + f"/api/meetings/{meeting_id}/utterances/{item.utterance_id}/correct"
        async with http.post(url, json={"participant_id": participant_id}) as response:
            body = await response.json()
            assert response.status == 200 and body["utterance"]["attribution_method"] == "manual_correction"
            assert body["changed"] is False
        async with http.post(url, json={}) as response:
            assert response.status == 400 and (await response.json())["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_correction_api_has_specific_not_found_codes(server):
    async with ClientSession() as http:
        async with http.post(server.base_url + "/api/meetings", json={}) as response:
            meeting_id = (await response.json())["meeting"]["meeting_id"]
        url = server.base_url + f"/api/meetings/{meeting_id}/utterances/{new_id()}/correct"
        async with http.post(url, json={"participant_id": new_id()}) as response:
            assert response.status == 404 and (await response.json())["error"]["code"] == "utterance_not_found"
