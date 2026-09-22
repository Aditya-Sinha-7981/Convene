from types import SimpleNamespace

import pytest

from server.attribution.service import AttributionService
from server.config import AttributionConfig
from server.ids import new_id
from server.pipeline.scheduler import TranscribedWindow
from server import registry
from server.repositories import audit_events, utterances


def outcome(device_id, meeting_id, text="ship Friday", status="ok"):
    return TranscribedWindow(device_id, meeting_id, 1, status, text, .73,
                             "2026-09-23T10:00:00.000Z", "2026-09-23T10:00:02.000Z", 16000, 32000)


def world(db, *, shared=False):
    with db.transaction() as tx:
        meeting = registry.create_meeting(tx, "Review")
        device_id = new_id()
        registration = registry.register_device(tx, meeting.meeting_id, device_id,
                                                None if shared else "Priya", shared, 2 if shared else 1)
    return meeting, registration


@pytest.mark.asyncio
async def test_non_shared_outcome_is_persisted_and_audited(db):
    meeting, registration = world(db)
    service = AttributionService(db, AttributionConfig())
    saved = await service.attribute(outcome(registration.device.device_id, meeting.meeting_id))
    assert saved.participant_id == registration.participants[0].participant_id
    assert saved.attribution_method == "device" and saved.attribution_confidence == .95
    assert saved.stt_confidence == .73 and saved.text == "ship Friday"
    assert utterances.get(db.conn, saved.utterance_id) == saved
    (event,) = audit_events.list_events(db.conn, meeting_id=meeting.meeting_id, event_type="utterance_created")
    assert event.payload["utterance_id"] == saved.utterance_id


@pytest.mark.asyncio
async def test_shared_device_uses_unresolved_stub_and_empty_or_failed_outcomes_are_skipped(db):
    meeting, registration = world(db, shared=True)
    service = AttributionService(db, AttributionConfig())
    saved = await service.attribute(outcome(registration.device.device_id, meeting.meeting_id))
    assert saved.participant_id is None and saved.attribution_method == "generic_unresolved"
    assert saved.attribution_confidence == .2
    service.accept(outcome(registration.device.device_id, meeting.meeting_id, "  "))
    service.accept(outcome(registration.device.device_id, meeting.meeting_id, "ignored", "failed"))
    await service.drain()
    assert len(utterances.list_for_meeting(db.conn, meeting.meeting_id)) == 1


@pytest.mark.asyncio
async def test_post_write_hook_is_non_blocking_and_failure_is_audited(db):
    meeting, registration = world(db)
    service = AttributionService(db, AttributionConfig())
    seen = []
    service.register_post_write_hook(seen.append)
    service.register_post_write_hook(lambda _: (_ for _ in ()).throw(RuntimeError("boom")))
    service.accept(outcome(registration.device.device_id, meeting.meeting_id))
    await service.drain()
    assert len(seen) == 1
    assert audit_events.list_events(db.conn, meeting_id=meeting.meeting_id, event_type="hook_failed")


@pytest.mark.asyncio
async def test_one_devices_integrity_failure_does_not_stop_another_device(db):
    with db.transaction() as tx:
        meeting = registry.create_meeting(tx, "Review")
        broken = registry.register_device(tx, meeting.meeting_id, new_id(), "Broken")
        healthy = registry.register_device(tx, meeting.meeting_id, new_id(), "Healthy")
        tx.conn.execute("DELETE FROM Participant WHERE device_id = ?", (broken.device.device_id,))
    service = AttributionService(db, AttributionConfig())
    service.accept(outcome(broken.device.device_id, meeting.meeting_id, "lost"))
    service.accept(outcome(healthy.device.device_id, meeting.meeting_id, "kept"))
    await service.drain()
    rows = utterances.list_for_meeting(db.conn, meeting.meeting_id)
    assert [row.text for row in rows] == ["kept"]
    assert audit_events.list_events(db.conn, meeting_id=meeting.meeting_id, event_type="hook_failed")
