"""Repository round-trips and constraint behavior against a temporary database file."""
from dataclasses import replace

import pytest

from server import audit
from server.errors import ConstraintError, DeviceNotFoundError, MeetingNotFoundError, NotFoundError, ValidationError
from server.ids import new_id
from server.repositories import (audit_events, connections, devices, meetings, participants, utterances)
from tests.support.storage import T0, T1, T2, device, meeting, participant, utterance


@pytest.fixture
def world(db):
    """A meeting with one device and one participant, committed."""
    m = meeting()
    d = device(m.meeting_id)
    p = participant(m.meeting_id, d.device_id)
    with db.transaction() as tx:
        meetings.create(tx.conn, m)
        devices.create(tx.conn, d)
        participants.create(tx.conn, p)
    return m, d, p


# --- round trips ----------------------------------------------------------------------------


def test_meeting_round_trip_and_update(db):
    m = meeting()
    with db.transaction() as tx:
        assert meetings.create(tx.conn, m) == m
    with db.transaction() as tx:
        assert meetings.get(tx.conn, m.meeting_id) == m
        updated = meetings.update(tx.conn, m.meeting_id, status="live", started_at=T1)
    assert updated == replace(m, status="live", started_at=T1)
    with db.transaction() as tx:
        assert meetings.require(tx.conn, m.meeting_id).started_at == T1


def test_device_round_trip_preserves_booleans_and_updates(db, world):
    m, d, _ = world
    shared = device(m.meeting_id, is_shared=True, declared_speaker_count=3, status="enrolling", joined_at=T1)
    with db.transaction() as tx:
        devices.create(tx.conn, shared)
        assert devices.get(tx.conn, d.device_id) == d
        assert devices.get(tx.conn, shared.device_id).is_shared is True
        assert devices.get(tx.conn, d.device_id).is_shared is False
        updated = devices.update(tx.conn, d.device_id, status="connected", reconnect_count=2)
        assert (updated.status, updated.reconnect_count) == ("connected", 2)
        assert [x.device_id for x in devices.list_for_meeting(tx.conn, m.meeting_id)] == [d.device_id, shared.device_id]


def test_participant_round_trip_and_update(db, world):
    m, d, p = world
    with db.transaction() as tx:
        assert participants.get(tx.conn, p.participant_id) == p
        renamed = participants.update(tx.conn, p.participant_id, display_name="Priya S", enrollment_status="enrolled")
        assert (renamed.display_name, renamed.enrollment_status) == ("Priya S", "enrolled")
        assert participants.list_for_device(tx.conn, d.device_id) == [renamed]
        assert participants.list_for_meeting(tx.conn, m.meeting_id) == [renamed]


def test_utterance_round_trip_ordering_and_correction_fields(db, world):
    m, d, p = world
    other = participant(m.meeting_id, d.device_id, display_name="Sam")
    late = utterance(m.meeting_id, d.device_id, p.participant_id, t_start="2026-09-21T11:30:05.000Z",
                     t_end="2026-09-21T11:30:06.000Z")
    early = utterance(m.meeting_id, d.device_id, p.participant_id)
    generic = utterance(m.meeting_id, d.device_id, None, attribution_method="generic_unresolved",
                        attribution_confidence=0.2, t_start="2026-09-21T11:30:03.000Z", t_end="2026-09-21T11:30:04.000Z")
    with db.transaction() as tx:
        participants.create(tx.conn, other)
        for u in (late, early, generic):
            utterances.insert(tx.conn, u)
        assert utterances.get(tx.conn, early.utterance_id) == early
        assert [u.utterance_id for u in utterances.list_for_meeting(tx.conn, m.meeting_id)] == [
            early.utterance_id, generic.utterance_id, late.utterance_id]
        assert [u.utterance_id for u in utterances.list_for_meeting(tx.conn, m.meeting_id, participant_id=p.participant_id)] == [
            early.utterance_id, late.utterance_id]
        fixed = utterances.update(tx.conn, generic.utterance_id, participant_id=other.participant_id,
                                  attribution_method="manual_correction", attribution_confidence=1.0,
                                  corrected=True, original_participant_id=None)
    assert fixed.corrected is True and fixed.participant_id == other.participant_id
    assert utterances.get(db.conn, early.utterance_id).corrected is False


def test_list_meetings_newest_first_with_status_filter(db):
    older = meeting(created_at="2026-09-20T09:00:00.000Z")
    newer = meeting(created_at="2026-09-21T09:00:00.000Z", status="live")
    with db.transaction() as tx:
        meetings.create(tx.conn, older)
        meetings.create(tx.conn, newer)
        assert [m.meeting_id for m in meetings.list_meetings(tx.conn)] == [newer.meeting_id, older.meeting_id]
        assert [m.meeting_id for m in meetings.list_meetings(tx.conn, status="live")] == [newer.meeting_id]
        assert len(meetings.list_meetings(tx.conn, limit=1)) == 1


def test_missing_rows(db):
    ghost = new_id()
    with db.transaction() as tx:
        assert meetings.get(tx.conn, ghost) is None
        assert devices.get(tx.conn, ghost) is None
        assert participants.get(tx.conn, ghost) is None
        assert utterances.get(tx.conn, ghost) is None
        assert audit_events.get(tx.conn, ghost) is None
        with pytest.raises(MeetingNotFoundError):
            meetings.require(tx.conn, ghost)
        with pytest.raises(DeviceNotFoundError):
            devices.require(tx.conn, ghost)
        with pytest.raises(NotFoundError):
            participants.require(tx.conn, ghost)
        with pytest.raises(MeetingNotFoundError):
            meetings.update(tx.conn, ghost, title="x")


def test_update_rejects_columns_outside_the_whitelist(db, world):
    m, d, _ = world
    with db.transaction() as tx:
        with pytest.raises(ValidationError, match="cannot update"):
            meetings.update(tx.conn, m.meeting_id, meeting_id=new_id())
        with pytest.raises(ValidationError):
            devices.update(tx.conn, d.device_id, meeting_id=new_id())
        with pytest.raises(ValidationError):
            devices.update(tx.conn, d.device_id, is_shared=True)


# --- constraints ----------------------------------------------------------------------------


@pytest.mark.parametrize("build", [
    lambda m, d, p: replace(meeting(), status="running"),
    lambda m, d, p: device(m.meeting_id, status="online"),
    lambda m, d, p: participant(m.meeting_id, d.device_id, enrollment_status="done"),
    lambda m, d, p: utterance(m.meeting_id, d.device_id, p.participant_id, attribution_method="guess"),
], ids=["Meeting.status", "Device.status", "Participant.enrollment_status", "Utterance.attribution_method"])
def test_enum_violations_are_rejected(db, world, build):
    m, d, p = world
    row = build(m, d, p)
    repo = {"Meeting": meetings.create, "Device": devices.create, "Participant": participants.create,
            "Utterance": utterances.insert}[type(row).__name__]
    with pytest.raises(ConstraintError):
        with db.transaction() as tx:
            repo(tx.conn, row)


def test_every_documented_connection_event_type_is_accepted_and_others_are_not(db, world):
    _, d, _ = world
    for event_type in ("connected", "disconnected", "reconnected", "audio_resumed"):
        with db.transaction() as tx:
            audit.record_connection_event(tx, device_id=d.device_id, event_type=event_type,
                                          payload=_payload_for(event_type))
    assert [e.event_type for e in connections.list_for_device(db.conn, d.device_id)] == [
        "connected", "disconnected", "reconnected", "audio_resumed"]
    with pytest.raises(ConstraintError):  # a raw sqlite3 error inside a transaction is translated too
        with db.transaction() as tx:
            tx.conn.execute("INSERT INTO ConnectionEvent VALUES (?, ?, ?, 'flapping', ?)",
                            (new_id(), d.device_id, d.meeting_id, T1))


def _payload_for(event_type):
    return {"connected": {"reconnect_count": 0}, "disconnected": {"reason": "peer_closed"},
            "reconnected": {"reconnect_count": 1, "via": "new_peer", "remote_addr": None, "user_agent": None},
            "audio_resumed": {"gap_ms": 800}}[event_type]


def test_foreign_keys_are_enforced(db, world):
    m, d, p = world
    ghost = new_id()
    for bad, repo in [(device(ghost), devices.create),
                      (participant(m.meeting_id, ghost), participants.create),
                      (utterance(m.meeting_id, d.device_id, ghost), utterances.insert),
                      (utterance(ghost, d.device_id, p.participant_id), utterances.insert)]:
        with pytest.raises(ConstraintError, match="FOREIGN KEY"):
            with db.transaction() as tx:
                repo(tx.conn, bad)


def test_duplicate_primary_key_is_rejected(db, world):
    m, _, _ = world
    with pytest.raises(ConstraintError, match="UNIQUE"):
        with db.transaction() as tx:
            meetings.create(tx.conn, m)


def test_cross_field_checks(db, world):
    m, d, p = world
    with db.transaction() as tx:
        # generic_unresolved may have a null participant, and nothing else may
        utterances.insert(tx.conn, utterance(m.meeting_id, d.device_id, None,
                                             attribution_method="generic_unresolved", attribution_confidence=0.2))
    for bad in (
        utterance(m.meeting_id, d.device_id, None),                                           # null participant, method device
        utterance(m.meeting_id, d.device_id, p.participant_id, original_participant_id=p.participant_id),  # original without corrected
        utterance(m.meeting_id, d.device_id, p.participant_id, t_start=T2, t_end=T1),         # ends before it starts
        utterance(m.meeting_id, d.device_id, p.participant_id, stt_confidence=1.5),
        utterance(m.meeting_id, d.device_id, p.participant_id, attribution_confidence=-0.1),
    ):
        with pytest.raises(ConstraintError):
            with db.transaction() as tx:
                utterances.insert(tx.conn, bad)
    for bad_device in (device(m.meeting_id, is_shared=False, declared_speaker_count=2),
                       device(m.meeting_id, declared_speaker_count=0),
                       device(m.meeting_id, reconnect_count=-1)):
        with pytest.raises(ConstraintError):
            with db.transaction() as tx:
                devices.create(tx.conn, bad_device)
    with pytest.raises(ConstraintError):
        with db.transaction() as tx:
            participants.create(tx.conn, participant(m.meeting_id, d.device_id, display_name=""))


@pytest.mark.parametrize("bad_time", ["2026-09-21T11:30:00Z", "2026-09-21 11:30:00.000", "yesterday",
                                      "2026-09-21T11:30:00.000+00:00"])
def test_timestamps_must_be_utc_milliseconds_with_z(db, bad_time):
    with pytest.raises(ConstraintError):
        with db.transaction() as tx:
            meetings.create(tx.conn, meeting(created_at=bad_time))


def test_a_failed_write_leaves_no_partial_row(db):
    m = meeting()
    with pytest.raises(ConstraintError):
        with db.transaction() as tx:
            meetings.create(tx.conn, m)                                # valid...
            devices.create(tx.conn, device(m.meeting_id, status="x"))  # ...then a violation
    assert meetings.get(db.conn, m.meeting_id) is None


def test_audit_payload_check_requires_a_json_object(db):
    for bad in ("not json", "[1, 2]", "42", "null"):
        with pytest.raises(ConstraintError):
            with db.transaction() as tx:
                tx.conn.execute(
                    "INSERT INTO AuditEvent VALUES (?, 1, NULL, 'server_started', 'api', ?, ?)",
                    (new_id(), T0, bad))
