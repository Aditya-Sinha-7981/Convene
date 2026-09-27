"""Meeting / device / participant registry: identity that survives reconnects and restarts.

Follows the contract in docs/api.md and docs/transport.md as completed by CON-02:

* ``register_device`` is the REST registration. It is idempotent: a known ``(meeting, device_id)`` returns
  the stored rows unchanged and writes nothing. It never counts a reconnect.
* ``record_device_connected`` is the WebSocket attach / peer-connected step. The first one starts the
  meeting and records ``device_connected``; every later one is a reconnect (``reconnect_count`` + 1,
  ``device_reconnected``) under the same Device and Participant.
* ``record_device_disconnected`` and ``reconcile_after_restart`` mark devices disconnected.

All functions run inside the caller's transaction (``Database.transaction()`` / ``Database.run``).
"""
from dataclasses import dataclass
from datetime import datetime

from . import colors
from .audit import emit, record_connection_event
from .db import Tx
from .errors import DeviceConflictError, MeetingEndedError, ValidationError
from .ids import is_uuid4, new_id
from .repositories import connections, devices, meetings, participants, utterances
from .repositories.models import ConnectionEvent, Device, Meeting, Participant
from .timeutil import utc_now

MAX_TITLE = 200
MAX_DISPLAY_NAME = 80
MAX_USER_AGENT = 512
SHARED_SPEAKER_RANGE = (2, 3)  # docs/api.md: a shared device declares 2 or 3 speakers
RECONNECT_VIA = ("ice_restart", "new_peer")
DISCONNECT_REASONS = ("peer_disconnected", "peer_failed", "peer_closed", "server_restart")
LEFT_REASONS = ("client_leave", "meeting_ended")


@dataclass(frozen=True)
class Registration:
    device: Device
    participants: list[Participant]
    created: bool  # False for an idempotent replay of an existing registration


@dataclass(frozen=True)
class Attachment:
    device: Device
    connection_event: ConnectionEvent
    is_reconnect: bool
    meeting_started: bool


@dataclass(frozen=True)
class MeetingEnd:
    meeting: Meeting
    already_ended: bool
    left_device_ids: list[str]


@dataclass(frozen=True)
class Reconciliation:
    devices: int
    meetings: int


def create_meeting(tx: Tx, title: str | None = None, *, meeting_id: str | None = None,
                   now: str | None = None) -> Meeting:
    """Create a meeting in status ``created`` and emit ``meeting_created``.

    A null, empty or blank title becomes a default label derived from the creation time in the server's
    local timezone, for example ``Meeting 2026-09-21 17:00`` (docs/api.md).
    """
    if title is not None and not isinstance(title, str):
        raise ValidationError("title must be a string")
    title = title.strip() if title else ""
    if len(title) > MAX_TITLE:
        raise ValidationError(f"title is longer than {MAX_TITLE} characters")
    meeting_id = meeting_id or new_id()
    if not is_uuid4(meeting_id):
        raise ValidationError("meeting_id must be a UUID v4")
    now = now or utc_now()
    meeting = meetings.create(tx.conn, Meeting(
        meeting_id=meeting_id, title=title or f"Meeting {datetime.now():%Y-%m-%d %H:%M}",
        status="created", created_at=now))
    emit(tx, "meeting_created", "api", {"title": meeting.title}, meeting_id=meeting_id, timestamp=now)
    return meeting


def rename_meeting(tx: Tx, meeting_id: str, title, *, now: str | None = None) -> Meeting:
    """Set a new title (any status) and emit ``meeting_renamed``. Renaming to the same title writes nothing."""
    if not isinstance(title, str) or not title.strip():
        raise ValidationError("title must be a non-empty string")
    title = " ".join(title.split())
    if len(title) > MAX_TITLE:
        raise ValidationError(f"title is longer than {MAX_TITLE} characters")
    meeting = meetings.require(tx.conn, meeting_id)
    if meeting.title == title:
        return meeting
    meeting = meetings.update(tx.conn, meeting_id, title=title)
    emit(tx, "meeting_renamed", "api", {"title": title}, meeting_id=meeting_id, timestamp=now)
    return meeting


@dataclass(frozen=True)
class MeetingErase:
    utterance_count: int
    device_count: int
    qa_query_count: int
    export_files: list[str]      # storage paths to remove after the transaction commits


def erase_meeting(tx: Tx, meeting_id: str, *, now: str | None = None) -> MeetingErase:
    """Permanently delete a meeting and everything it owns, in one transaction (explicit user action only).

    Removed: devices, participants, utterances, connection events, transcript chunks and their vectors, Q&A
    queries about it (including multi-meeting history answers that cited its chunks, since their text came from
    it), summaries, action items and every note on them, the notes it was the source of on other meetings' items
    (with their note events), export rows, its model-execution rows and every audit event carrying its
    ``meeting_id``. Afterwards one ``meeting_deleted`` event (no meeting_id) keeps only the id and counts.
    The caller refuses first when a phone is connected or a summary is running, and removes the export files.
    """
    conn = tx.conn
    meetings.require(conn, meeting_id)
    ids = lambda sql: [row[0] for row in conn.execute(sql, (meeting_id,)).fetchall()]
    chunk_ids = ids("SELECT chunk_id FROM TranscriptChunk WHERE meeting_id = ?")
    device_ids = ids("SELECT device_id FROM Device WHERE meeting_id = ?")
    summary_ids = ids("SELECT summary_id FROM Summary WHERE meeting_id = ?")
    query_ids = ids("SELECT query_id FROM QAQuery WHERE meeting_id = ?")
    if chunk_ids:
        marks = ",".join("?" * len(chunk_ids))
        query_ids += [row[0] for row in conn.execute(
            f"SELECT DISTINCT q.query_id FROM QAQuery q, json_each(q.cited_chunk_ids) c "
            f"WHERE q.meeting_id IS NULL AND c.value IN ({marks})", chunk_ids).fetchall()]
    export_files = ids("SELECT storage_path FROM Export WHERE meeting_id = ? AND storage_path IS NOT NULL")
    utterance_count = utterances.count_for_meeting(conn, meeting_id)

    def delete_in(table: str, column: str, values: list[str]) -> None:
        for start in range(0, len(values), 500):
            batch = values[start:start + 500]
            conn.execute(f"DELETE FROM {table} WHERE {column} IN ({','.join('?' * len(batch))})", batch)

    # Emitted first so its seq stays above every deleted one: seq is MAX(seq) + 1, and a reused seq would make
    # other dashboards' resync cursors skip events.
    emit(tx, "meeting_deleted", "api", {"deleted_meeting_id": meeting_id, "utterance_count": utterance_count,
                                        "device_count": len(device_ids), "qa_query_count": len(query_ids)},
         timestamp=now)
    related = chunk_ids + summary_ids + query_ids
    delete_in("ModelExecution", "related_id", related)
    for device_id in device_ids:  # STT rows are related to "<device_id>/<window_id>"
        conn.execute("DELETE FROM ModelExecution WHERE related_id LIKE ?", (f"{device_id}/%",))
    # Audit rows outside the meeting that point at a deleted query (history model errors).
    for query_id in query_ids:
        conn.execute("DELETE FROM AuditEvent WHERE meeting_id IS NULL AND event_type IN ('model_error', 'qa_query') "
                     "AND (json_extract(payload, '$.related_id') = ? OR json_extract(payload, '$.query_id') = ?)",
                     (query_id, query_id))
    delete_in("QAQuery", "query_id", query_ids)
    # CON-16 notes: those on this meeting's items (from any meeting), and those this meeting was the source of on
    # other meetings' items, with their note events. A status change such a note made stays on the surviving item.
    conn.execute("DELETE FROM AuditEvent WHERE event_type = 'action_item_note_added' "
                 "AND json_extract(payload, '$.source_meeting_id') = ?", (meeting_id,))
    conn.execute("DELETE FROM ActionItemNote WHERE source_meeting_id = ? OR action_item_id IN "
                 "(SELECT action_item_id FROM ActionItem WHERE meeting_id = ?)", (meeting_id, meeting_id))
    for table in ("ActionItem", "Export", "Summary"):
        conn.execute(f"DELETE FROM {table} WHERE meeting_id = ?", (meeting_id,))
    delete_in("TranscriptChunkVector", "chunk_id", chunk_ids)
    for table in ("TranscriptChunk", "ConnectionEvent", "AuditEvent", "Utterance", "Participant", "Device", "Meeting"):
        conn.execute(f"DELETE FROM {table} WHERE meeting_id = ?", (meeting_id,))
    return MeetingErase(utterance_count, len(device_ids), len(query_ids), export_files)


def register_device(tx: Tx, meeting_id: str, device_id: str, display_name: str | None, is_shared: bool = False,
                    declared_speaker_count: int | None = None, user_agent: str | None = None, *,
                    color: str | None = None, now: str | None = None) -> Registration:
    """Register a device (and, if it is not shared, its one Participant). Idempotent.

    * New device: ``Device`` in ``joining`` (``enrolling`` if shared) and, for a non-shared device, one
      ``Participant`` with ``enrollment_status = not_required``; audit ``device_registered``. Shared
      devices get no participants here; CON-13 creates them at enrollment.
    * ``color``: an optional palette key for that participant (ADR-25). A colour already used in the meeting is
      ``ColorTakenError``; with none, the server picks an unused one.
    * Known ``device_id`` in this meeting: the stored rows are returned unchanged, nothing is written (a
      different ``display_name`` or ``color`` in the replay is ignored), so a rejoining phone keeps its colour.
    * ``device_id`` registered in another meeting, or here with a different ``is_shared``:
      ``DeviceConflictError``. Devices are never re-parented.
    * Ended meeting: ``MeetingEndedError``. Unknown meeting: ``MeetingNotFoundError``.

    Registration does not start the meeting; the first successful connection does.
    """
    if not is_uuid4(device_id):
        raise ValidationError("device_id must be a UUID v4")
    is_shared = bool(is_shared)
    if is_shared:
        low, high = SHARED_SPEAKER_RANGE
        if declared_speaker_count is None or isinstance(declared_speaker_count, bool) \
                or not isinstance(declared_speaker_count, int) or not low <= declared_speaker_count <= high:
            raise ValidationError(f"a shared device must declare {low} to {high} speakers")
        name = None
    else:
        if declared_speaker_count not in (None, 1) or isinstance(declared_speaker_count, bool):
            raise ValidationError("a device that is not shared declares exactly 1 speaker")
        declared_speaker_count = 1
        if not isinstance(display_name, str) or not (1 <= len(display_name.strip()) <= MAX_DISPLAY_NAME):
            raise ValidationError(f"display_name must be 1 to {MAX_DISPLAY_NAME} characters")
        name = display_name.strip()
    if color is not None and (is_shared or not isinstance(color, str)):
        raise ValidationError("color is a palette key, and only for a device that is not shared")
    if user_agent is not None:
        if not isinstance(user_agent, str):
            raise ValidationError("user_agent must be a string")
        user_agent = user_agent[:MAX_USER_AGENT]

    meeting = meetings.require(tx.conn, meeting_id)
    if meeting.status == "ended":
        raise MeetingEndedError(f"meeting {meeting_id} has ended")

    existing = devices.get(tx.conn, device_id)
    if existing is not None:
        if existing.meeting_id != meeting_id:
            raise DeviceConflictError(f"device {device_id} is registered in a different meeting")
        if existing.is_shared != is_shared:
            raise DeviceConflictError(f"device {device_id} is already registered with is_shared={existing.is_shared}")
        return Registration(existing, participants.list_for_device(tx.conn, device_id), created=False)

    now = now or utc_now()
    chosen = None if is_shared else colors.choose(participants.colors_in_meeting(tx.conn, meeting_id), color)
    device = devices.create(tx.conn, Device(
        device_id=device_id, meeting_id=meeting_id, joined_at=now,
        status="enrolling" if is_shared else "joining", is_shared=is_shared,
        declared_speaker_count=declared_speaker_count, reconnect_count=0, user_agent=user_agent))
    people = []
    if not is_shared:
        people.append(participants.create(tx.conn, Participant(
            participant_id=new_id(), meeting_id=meeting_id, device_id=device_id,
            display_name=name, enrollment_status="not_required", color=chosen)))
    emit(tx, "device_registered", "registry",
         {"device_id": device_id, "is_shared": is_shared, "declared_speaker_count": declared_speaker_count,
          "user_agent": user_agent, "color": chosen}, meeting_id=meeting_id, timestamp=now)
    return Registration(device, people, created=True)


def record_device_connected(tx: Tx, device_id: str, *, via: str = "new_peer", remote_addr: str | None = None,
                            user_agent: str | None = None, now: str | None = None) -> Attachment:
    """A registered device attached and its peer connected.

    First attach: ``connected`` event; if the meeting was ``created`` it becomes ``live`` with
    ``started_at`` and ``meeting_started`` is emitted first. Any later attach is a reconnect: the same
    Device and Participant, ``reconnect_count`` + 1, ``reconnected`` event. A shared device still
    enrolling stays ``enrolling`` (CON-13 moves it to ``connected``). Raises ``MeetingEndedError`` for an
    ended meeting.
    """
    device = devices.require(tx.conn, device_id)
    meeting = meetings.require(tx.conn, device.meeting_id)
    if meeting.status == "ended":
        raise MeetingEndedError(f"meeting {meeting.meeting_id} has ended")
    now = now or utc_now()
    is_reconnect = connections.has_connected(tx.conn, device_id)

    started = False
    if meeting.status == "created":
        meetings.update(tx.conn, meeting.meeting_id, status="live", started_at=now)
        emit(tx, "meeting_started", "transport", {"first_device_id": device_id},
             meeting_id=meeting.meeting_id, timestamp=now)
        started = True

    status = "enrolling" if device.is_shared and device.status == "enrolling" else "connected"
    if is_reconnect:
        if via not in RECONNECT_VIA:
            raise ValidationError(f"via must be one of {RECONNECT_VIA}")
        count = device.reconnect_count + 1
        device = devices.update(tx.conn, device_id, status=status, reconnect_count=count)
        connection, _ = record_connection_event(
            tx, device_id=device_id, event_type="reconnected", timestamp=now,
            payload={"reconnect_count": count, "via": via, "remote_addr": remote_addr, "user_agent": user_agent})
    else:
        device = devices.update(tx.conn, device_id, status=status)
        connection, _ = record_connection_event(
            tx, device_id=device_id, event_type="connected", timestamp=now,
            payload={"reconnect_count": device.reconnect_count})
    return Attachment(device, connection, is_reconnect, started)


def record_device_disconnected(tx: Tx, device_id: str, reason: str, *, now: str | None = None) -> ConnectionEvent | None:
    """Mark a device ``disconnected`` and record why. Returns None (and writes nothing) if it already is
    ``disconnected`` or has ``left``; other devices are never touched."""
    if reason not in DISCONNECT_REASONS:
        raise ValidationError(f"reason must be one of {DISCONNECT_REASONS}")
    device = devices.require(tx.conn, device_id)
    if device.status in ("disconnected", "left"):
        return None
    devices.update(tx.conn, device_id, status="disconnected")
    connection, _ = record_connection_event(tx, device_id=device_id, event_type="disconnected",
                                            payload={"reason": reason}, timestamp=now)
    return connection


def reconcile_after_restart(tx: Tx, *, now: str | None = None) -> Reconciliation:
    """Run once at startup, before accepting connections.

    Every ``connected`` or ``joining`` device in a meeting that has not ended becomes ``disconnected``
    with ``device_disconnected`` (reason ``server_restart``). Meetings stay ``live``, so phones resume as
    the same device and participant when they reconnect. Ended meetings are untouched. Finishes with a
    ``server_started`` audit event recording the counts.
    """
    now = now or utc_now()
    stale = devices.list_by_status_in_open_meetings(tx.conn, ("connected", "joining"))
    touched_meetings = set()
    for device in stale:
        record_device_disconnected(tx, device.device_id, "server_restart", now=now)
        touched_meetings.add(device.meeting_id)
    emit(tx, "server_started", "api",
         {"reconciled_devices": len(stale), "reconciled_meetings": len(touched_meetings)}, timestamp=now)
    return Reconciliation(devices=len(stale), meetings=len(touched_meetings))


def record_device_left(tx: Tx, device_id: str, reason: str, *, now: str | None = None) -> Device | None:
    """The phone pressed Stop, or the meeting ended: status ``left`` and audit ``device_left``.

    Returns None (writing nothing) if the device has already left. A later attach of the same device_id
    resumes as the same participant and counts as a reconnect.
    """
    if reason not in LEFT_REASONS:
        raise ValidationError(f"reason must be one of {LEFT_REASONS}")
    device = devices.require(tx.conn, device_id)
    if device.status == "left":
        return None
    device = devices.update(tx.conn, device_id, status="left")
    emit(tx, "device_left", "transport", {"device_id": device_id, "reason": reason},
         meeting_id=device.meeting_id, timestamp=now)
    return device


def end_meeting(tx: Tx, meeting_id: str, *, now: str | None = None) -> MeetingEnd:
    """Explicit meeting end. Idempotent: an ended meeting is returned unchanged with ``already_ended``.

    One transaction: meeting ``ended`` with ``ended_at``, audit ``meeting_ended``, and every device that has
    not already left becomes ``left`` (audit ``device_left``, reason ``meeting_ended``). Tearing down peers
    and running hooks is the caller's job, after this commits.
    """
    meeting = meetings.require(tx.conn, meeting_id)
    if meeting.status == "ended":
        return MeetingEnd(meeting, True, [])
    now = now or utc_now()
    roster = devices.list_for_meeting(tx.conn, meeting_id)
    meeting = meetings.update(tx.conn, meeting_id, status="ended", ended_at=now)
    emit(tx, "meeting_ended", "api",
         {"utterance_count": utterances.count_for_meeting(tx.conn, meeting_id), "device_count": len(roster)},
         meeting_id=meeting_id, timestamp=now)
    left = []
    for device in roster:
        if record_device_left(tx, device.device_id, "meeting_ended", now=now) is not None:
            left.append(device.device_id)
    return MeetingEnd(meeting, False, left)
