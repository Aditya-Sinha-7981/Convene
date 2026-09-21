"""Registration, reconnect identity, isolation, and restart reconciliation (CON-03).

These prove the storage rules only. That a real phone keeps its identity across a Wi-Fi drop or a server
restart is checked by CON-04 and CON-06 on real devices.
"""
import pytest

from server import registry
from server.db import Database
from server.errors import (DeviceConflictError, DeviceNotFoundError, MeetingEndedError, MeetingNotFoundError,
                           ValidationError)
from server.ids import new_id
from server.repositories import audit_events, connections, devices, meetings, participants, utterances
from tests.support.storage import T1, T2, utterance

UA = "Mozilla/5.0 (Linux; Android 14) Chrome/128.0"


def make_meeting(db, title="Sprint planning"):
    with db.transaction() as tx:
        return registry.create_meeting(tx, title)


def register(db, meeting_id, name="Priya", device_id=None, **kwargs):
    device_id = device_id or new_id()
    with db.transaction() as tx:
        return registry.register_device(tx, meeting_id, device_id, name, user_agent=UA, **kwargs)


def audit_types(db, meeting_id=None):
    return [e.event_type for e in audit_events.list_events(db.conn, meeting_id=meeting_id)]


# --- meetings -------------------------------------------------------------------------------


def test_create_meeting_defaults_and_audit(db):
    named = make_meeting(db, "  Sprint planning ")
    unnamed = make_meeting(db, None)
    blank = make_meeting(db, "   ")
    assert (named.title, named.status, named.started_at, named.ended_at) == ("Sprint planning", "created", None, None)
    assert unnamed.title.startswith("Meeting 20") and blank.title.startswith("Meeting 20")
    assert audit_types(db) == ["meeting_created"] * 3
    assert audit_events.list_events(db.conn)[0].payload == {"title": "Sprint planning"}


def test_create_meeting_validates(db):
    with pytest.raises(ValidationError):
        make_meeting(db, "x" * 201)
    with pytest.raises(ValidationError):
        make_meeting(db, 42)


# --- register_device ------------------------------------------------------------------------


def test_new_non_shared_device_gets_exactly_one_participant_and_does_not_start_the_meeting(db):
    m = make_meeting(db)
    reg = register(db, m.meeting_id)
    assert reg.created is True
    assert (reg.device.status, reg.device.is_shared, reg.device.declared_speaker_count,
            reg.device.reconnect_count, reg.device.user_agent) == ("joining", False, 1, 0, UA)
    assert len(reg.participants) == 1
    person = reg.participants[0]
    assert (person.display_name, person.enrollment_status, person.device_id) == ("Priya", "not_required", reg.device.device_id)
    assert meetings.get(db.conn, m.meeting_id).status == "created"  # registration alone does not start it
    event = audit_events.list_events(db.conn, event_type="device_registered")[0]
    assert event.payload == {"device_id": reg.device.device_id, "is_shared": False,
                             "declared_speaker_count": 1, "user_agent": UA}


def test_registering_the_same_device_again_is_an_idempotent_replay(db):
    m = make_meeting(db)
    first = register(db, m.meeting_id, "Priya")
    before = audit_types(db)
    again = register(db, m.meeting_id, "A Different Name", device_id=first.device.device_id)
    assert again.created is False
    assert again.device == first.device
    assert again.participants == first.participants  # same participant, name in force is unchanged
    assert again.participants[0].display_name == "Priya"
    assert audit_types(db) == before  # nothing written, nothing audited
    assert len(participants.list_for_meeting(db.conn, m.meeting_id)) == 1
    assert devices.get(db.conn, first.device.device_id).reconnect_count == 0  # registering is not reconnecting


def test_the_same_device_id_in_another_meeting_is_rejected_not_reparented(db):
    a, b = make_meeting(db), make_meeting(db)
    first = register(db, a.meeting_id)
    with pytest.raises(DeviceConflictError, match="different meeting"):
        register(db, b.meeting_id, device_id=first.device.device_id)
    assert devices.get(db.conn, first.device.device_id).meeting_id == a.meeting_id
    assert devices.list_for_meeting(db.conn, b.meeting_id) == []
    assert participants.list_for_meeting(db.conn, b.meeting_id) == []


def test_changing_is_shared_on_replay_is_a_conflict(db):
    m = make_meeting(db)
    first = register(db, m.meeting_id)
    with pytest.raises(DeviceConflictError, match="is_shared"):
        register(db, m.meeting_id, None, device_id=first.device.device_id, is_shared=True, declared_speaker_count=2)


def test_ended_and_unknown_meetings(db):
    m = make_meeting(db)
    known = register(db, m.meeting_id)
    with db.transaction() as tx:
        meetings.update(tx.conn, m.meeting_id, status="ended", ended_at=T2)
    with pytest.raises(MeetingEndedError):
        register(db, m.meeting_id)
    with pytest.raises(MeetingEndedError):  # even a replay
        register(db, m.meeting_id, device_id=known.device.device_id)
    with pytest.raises(MeetingNotFoundError):
        register(db, new_id())


def test_shared_device_is_stored_as_enrolling_with_no_participants(db):
    m = make_meeting(db)
    reg = register(db, m.meeting_id, None, is_shared=True, declared_speaker_count=3)
    assert (reg.device.status, reg.device.is_shared, reg.device.declared_speaker_count) == ("enrolling", True, 3)
    assert reg.participants == []
    assert participants.list_for_device(db.conn, reg.device.device_id) == []
    ignored_name = register(db, m.meeting_id, "Ignored", is_shared=True, declared_speaker_count=2)
    assert ignored_name.participants == []


@pytest.mark.parametrize("kwargs", [
    {"device_id": "not-a-uuid"},
    {"device_id": "0D4F6A52-7C1B-4E7A-B0A3-51E1F4C2A9D8"},          # upper case is not the documented form
    {"device_id": "0d4f6a52-7c1b-1e7a-b0a3-51e1f4c2a9d8"},          # a UUID v1, not v4
    {"name": ""}, {"name": "   "}, {"name": None}, {"name": "x" * 81}, {"name": 5},
    {"declared_speaker_count": 2},                                   # not shared => exactly 1
    {"declared_speaker_count": 0},
    {"is_shared": True, "name": None, "declared_speaker_count": 1},
    {"is_shared": True, "name": None, "declared_speaker_count": 4},
    {"is_shared": True, "name": None, "declared_speaker_count": None},
    {"is_shared": True, "name": None, "declared_speaker_count": True},
])
def test_registration_validates_its_input_and_writes_nothing(db, kwargs):
    m = make_meeting(db)
    before = audit_types(db)
    kwargs = dict(kwargs)
    name = kwargs.pop("name", "Priya")
    device_id = kwargs.pop("device_id", new_id())
    with pytest.raises(ValidationError):
        with db.transaction() as tx:
            registry.register_device(tx, m.meeting_id, device_id, name, **kwargs)
    assert devices.list_for_meeting(db.conn, m.meeting_id) == []
    assert audit_types(db) == before


def test_display_name_is_trimmed_and_user_agent_is_capped(db):
    m = make_meeting(db)
    with db.transaction() as tx:
        reg = registry.register_device(tx, m.meeting_id, new_id(), "  Priya  ", user_agent="u" * 900)
    assert reg.participants[0].display_name == "Priya"
    assert len(reg.device.user_agent) == 512


# --- attach, first connection, and reconnect ------------------------------------------------


def attach(db, device_id, **kwargs):
    with db.transaction() as tx:
        return registry.record_device_connected(tx, device_id, **kwargs)


def test_first_connection_starts_the_meeting_exactly_once(db):
    m = make_meeting(db)
    a, b = register(db, m.meeting_id, "A"), register(db, m.meeting_id, "B")
    first = attach(db, a.device.device_id)
    assert (first.is_reconnect, first.meeting_started, first.device.status) == (False, True, "connected")
    started = meetings.get(db.conn, m.meeting_id)
    assert started.status == "live" and started.started_at is not None
    second = attach(db, b.device.device_id)
    assert second.meeting_started is False
    assert meetings.get(db.conn, m.meeting_id).started_at == started.started_at
    types = audit_types(db, m.meeting_id)
    assert types == ["meeting_created", "device_registered", "device_registered",
                     "meeting_started", "device_connected", "device_connected"]  # started precedes the first connect
    assert audit_events.list_events(db.conn, event_type="meeting_started")[0].payload == {"first_device_id": a.device.device_id}


def test_reconnect_keeps_device_and_participant_and_counts_it(db):
    m = make_meeting(db)
    reg = register(db, m.meeting_id)
    device_id, person = reg.device.device_id, reg.participants[0]
    attach(db, device_id)
    with db.transaction() as tx:
        registry.record_device_disconnected(tx, device_id, "peer_disconnected")
    assert devices.get(db.conn, device_id).status == "disconnected"

    again = attach(db, device_id, via="ice_restart", remote_addr="192.168.50.23", user_agent=UA)
    assert (again.is_reconnect, again.device.status, again.device.reconnect_count) == (True, "connected", 1)
    again = attach(db, device_id)
    assert again.device.reconnect_count == 2

    assert participants.list_for_meeting(db.conn, m.meeting_id) == [person]  # never a second participant
    assert len(devices.list_for_meeting(db.conn, m.meeting_id)) == 1
    events = audit_events.list_events(db.conn, event_type="device_reconnected")
    assert [e.payload["reconnect_count"] for e in events] == [1, 2]
    assert events[0].payload == {"device_id": device_id, "reconnect_count": 1, "via": "ice_restart",
                                 "remote_addr": "192.168.50.23", "user_agent": UA}
    assert [c.event_type for c in connections.list_for_device(db.conn, device_id)] == [
        "connected", "disconnected", "reconnected", "reconnected"]


def test_a_reconnect_over_a_still_connected_device_also_counts(db):
    """A phone can attach again while the server still thinks the old peer is up (a half-dead socket)."""
    m = make_meeting(db)
    device_id = register(db, m.meeting_id).device.device_id
    attach(db, device_id)
    assert attach(db, device_id).device.reconnect_count == 1


def test_bad_reconnect_via_is_rejected(db):
    m = make_meeting(db)
    device_id = register(db, m.meeting_id).device.device_id
    attach(db, device_id)
    with pytest.raises(ValidationError):
        attach(db, device_id, via="magic")
    assert devices.get(db.conn, device_id).reconnect_count == 0


def test_a_device_registered_but_never_connected_is_not_a_reconnect_after_restart_reconciliation(db):
    m = make_meeting(db)
    device_id = register(db, m.meeting_id).device.device_id
    with db.transaction() as tx:
        registry.reconcile_after_restart(tx)  # joining -> disconnected without ever connecting
    result = attach(db, device_id)
    assert result.is_reconnect is False and result.device.reconnect_count == 0


def test_shared_device_stays_enrolling_when_it_connects(db):
    m = make_meeting(db)
    device_id = register(db, m.meeting_id, None, is_shared=True, declared_speaker_count=2).device.device_id
    assert attach(db, device_id).device.status == "enrolling"


def test_attach_to_an_ended_or_unknown_device_or_meeting_fails(db):
    m = make_meeting(db)
    device_id = register(db, m.meeting_id).device.device_id
    with pytest.raises(DeviceNotFoundError):
        attach(db, new_id())
    with db.transaction() as tx:
        meetings.update(tx.conn, m.meeting_id, status="ended", ended_at=T2)
    with pytest.raises(MeetingEndedError):
        attach(db, device_id)


def test_disconnect_is_idempotent_and_validates_the_reason(db):
    m = make_meeting(db)
    device_id = register(db, m.meeting_id).device.device_id
    attach(db, device_id)
    with db.transaction() as tx:
        assert registry.record_device_disconnected(tx, device_id, "peer_failed") is not None
        assert registry.record_device_disconnected(tx, device_id, "peer_failed") is None
    assert len(audit_events.list_events(db.conn, event_type="device_disconnected")) == 1
    with pytest.raises(ValidationError):
        with db.transaction() as tx:
            registry.record_device_disconnected(tx, device_id, "because")


# --- isolation ------------------------------------------------------------------------------


def snapshot(db, device_id):
    return (devices.get(db.conn, device_id), participants.list_for_device(db.conn, device_id),
            connections.list_for_device(db.conn, device_id))


def test_registering_and_reconnecting_one_device_never_touches_another(db):
    m = make_meeting(db)
    a = register(db, m.meeting_id, "A").device.device_id
    attach(db, a)
    before = snapshot(db, a)

    b = register(db, m.meeting_id, "B").device.device_id
    attach(db, b)
    with db.transaction() as tx:
        registry.record_device_disconnected(tx, b, "peer_failed")
    attach(db, b, via="new_peer")
    with pytest.raises(DeviceConflictError):
        register(db, new_meeting_id(db), device_id=b)
    assert snapshot(db, a) == before


def new_meeting_id(db):
    return make_meeting(db, "other").meeting_id


def test_a_failed_registration_does_not_disturb_other_devices(db):
    m = make_meeting(db)
    a = register(db, m.meeting_id, "A").device.device_id
    before = snapshot(db, a)
    with pytest.raises(ValidationError):
        register(db, m.meeting_id, "")
    assert snapshot(db, a) == before


# --- restart --------------------------------------------------------------------------------


def test_identity_and_transcript_survive_a_restart(tmp_path):
    path = tmp_path / "restart.db"
    first = Database.open(path)
    m = make_meeting(first)
    reg = register(first, m.meeting_id)
    attach(first, reg.device.device_id)
    said = utterance(m.meeting_id, reg.device.device_id, reg.participants[0].participant_id)
    with first.transaction() as tx:
        utterances.insert(tx.conn, said)
    first.close()  # the process ends

    second = Database.open(path)
    assert devices.get(second.conn, reg.device.device_id).meeting_id == m.meeting_id
    assert participants.list_for_device(second.conn, reg.device.device_id) == reg.participants
    assert utterances.get(second.conn, said.utterance_id) == said
    assert utterances.get(second.conn, said.utterance_id).device_id == reg.device.device_id  # source device kept
    # the phone comes back with the same persisted device_id: same participant, and it counts as a reconnect
    with second.transaction() as tx:
        registry.reconcile_after_restart(tx)
    back = attach(second, reg.device.device_id, via="new_peer")
    assert back.is_reconnect and back.device.reconnect_count == 1
    assert participants.list_for_meeting(second.conn, m.meeting_id) == reg.participants
    second.close()


def test_reconciliation_marks_open_devices_disconnected_and_leaves_ended_meetings_alone(tmp_path):
    path = tmp_path / "reconcile.db"
    db = Database.open(path)
    live = make_meeting(db, "live one")
    done = make_meeting(db, "finished")
    d = {name: register(db, live.meeting_id, name).device.device_id for name in ("connected", "joining", "gone", "left")}
    ended_device = register(db, done.meeting_id, "old").device.device_id
    attach(db, d["connected"])
    attach(db, d["gone"])
    with db.transaction() as tx:
        registry.record_device_disconnected(tx, d["gone"], "peer_closed")
        devices.update(tx.conn, d["left"], status="left")
        devices.update(tx.conn, ended_device, status="connected")  # stale row inside an ended meeting
        meetings.update(tx.conn, done.meeting_id, status="ended", ended_at=T2)
    ended_before = devices.get(db.conn, ended_device)
    seen = []
    db.subscribe(seen.append)

    with db.transaction() as tx:
        result = registry.reconcile_after_restart(tx)

    assert result == registry.Reconciliation(devices=2, meetings=1)
    assert devices.get(db.conn, d["connected"]).status == "disconnected"
    assert devices.get(db.conn, d["joining"]).status == "disconnected"
    assert devices.get(db.conn, d["gone"]).status == "disconnected"
    assert devices.get(db.conn, d["left"]).status == "left"
    assert devices.get(db.conn, ended_device) == ended_before  # ended meetings untouched
    assert meetings.get(db.conn, live.meeting_id).status == "live"  # the meeting stays live
    assert meetings.get(db.conn, done.meeting_id).status == "ended"

    reasons = {e.payload["device_id"]: e.payload["reason"] for e in audit_events.list_events(db.conn, event_type="device_disconnected")
               if e.payload["reason"] == "server_restart"}
    assert reasons == {d["connected"]: "server_restart", d["joining"]: "server_restart"}
    started = audit_events.list_events(db.conn, event_type="server_started")[-1]
    assert started.payload == {"reconciled_devices": 2, "reconciled_meetings": 1} and started.meeting_id is None
    # each disconnect has its ConnectionEvent projection, and subscribers heard about every event
    assert [c.event_type for c in connections.list_for_device(db.conn, d["joining"])] == ["disconnected"]
    assert [e.event_type for e in seen].count("device_disconnected") == 2 and seen[-1].event_type == "server_started"

    with db.transaction() as tx:  # running it again finds nothing more to do
        assert registry.reconcile_after_restart(tx) == registry.Reconciliation(devices=0, meetings=0)
    db.close()


def test_a_live_meeting_stays_live_through_reconciliation(db):
    m = make_meeting(db)
    device_id = register(db, m.meeting_id).device.device_id
    attach(db, device_id)
    with db.transaction() as tx:
        registry.reconcile_after_restart(tx)
    assert meetings.get(db.conn, m.meeting_id).status == "live"
