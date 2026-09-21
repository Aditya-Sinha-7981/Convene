"""The single audit write path (ADR-13).

Every ``INSERT`` into ``AuditEvent`` and ``ConnectionEvent`` in the code base lives in this file;
``tests/test_audit_emit.py`` scans the source tree to keep it that way. Components never write audit rows
directly.
"""
import json

from .audit_catalog import CATALOG, CONNECTION_TO_AUDIT
from .db import Tx
from .errors import InvalidPayloadError, NotFoundError, UnknownEventTypeError, ValidationError
from .ids import new_id
from .repositories import base, devices
from .repositories.models import AuditEvent, ConnectionEvent
from .timeutil import utc_now


def emit(tx: Tx, event_type: str, component: str, payload: dict, meeting_id: str | None = None, *,
         timestamp: str | None = None) -> AuditEvent:
    """Record one audit event inside the caller's transaction and return it.

    Validates ``event_type`` against the catalog, ``component`` against that type, and ``payload`` as a
    JSON object whose keys are exactly the catalogued keys (a nullable key is present with value null).
    Assigns ``seq`` = highest existing ``seq`` + 1; ``Database`` takes the write lock at BEGIN IMMEDIATE,
    so concurrent writers cannot collide. Subscribers are notified only after the transaction commits.
    Raises before writing anything if validation fails.
    """
    spec = CATALOG.get(event_type)
    if spec is None:
        raise UnknownEventTypeError(f"{event_type!r} is not an event type in docs/data-model.md")
    if component != spec.component:
        raise InvalidPayloadError(f"{event_type} is emitted by component {spec.component!r}, not {component!r}")
    if meeting_id is None and spec.meeting_required:
        raise InvalidPayloadError(f"{event_type} requires a meeting_id")
    if not isinstance(payload, dict):
        raise InvalidPayloadError(f"{event_type} payload must be a JSON object")
    if set(payload) != spec.payload_keys:
        missing, extra = sorted(spec.payload_keys - set(payload)), sorted(set(payload) - spec.payload_keys)
        raise InvalidPayloadError(f"{event_type} payload keys must be exactly {sorted(spec.payload_keys)}; "
                                  f"missing {missing}, unexpected {extra}")
    try:
        encoded = json.dumps(payload, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise InvalidPayloadError(f"{event_type} payload is not JSON-serializable: {exc}") from exc

    seq = base.query_one(tx.conn, "SELECT COALESCE(MAX(seq), 0) + 1 FROM AuditEvent")[0]
    event = AuditEvent(event_id=new_id(), seq=seq, meeting_id=meeting_id, event_type=event_type,
                       component=component, timestamp=timestamp or utc_now(), payload=json.loads(encoded))
    base.execute(
        tx.conn,
        "INSERT INTO AuditEvent (event_id, seq, meeting_id, event_type, component, timestamp, payload) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (event.event_id, event.seq, event.meeting_id, event.event_type, event.component, event.timestamp, encoded))
    tx.events.append(event)
    return event


def _insert_connection_event(tx: Tx, event: ConnectionEvent) -> None:
    base.execute(
        tx.conn,
        "INSERT INTO ConnectionEvent (event_id, device_id, meeting_id, event_type, timestamp) VALUES (?, ?, ?, ?, ?)",
        (event.event_id, event.device_id, event.meeting_id, event.event_type, event.timestamp))


def record_connection_event(tx: Tx, *, device_id: str, event_type: str, payload: dict | None = None,
                            timestamp: str | None = None) -> tuple[ConnectionEvent, AuditEvent]:
    """Write a ConnectionEvent and its matching AuditEvent in the same transaction.

    ``event_type`` is the ConnectionEvent type (connected, disconnected, reconnected, audio_resumed); the
    audit type follows ``CONNECTION_TO_AUDIT``. ``payload`` holds the audit keys other than ``device_id``,
    which is added here. The ConnectionEvent reuses the audit event's ``event_id``, so the projection is
    linked to the record it came from. If either insert fails the caller's transaction rolls back both.
    """
    audit_type = CONNECTION_TO_AUDIT.get(event_type)
    if audit_type is None:
        raise ValidationError(f"unknown connection event type {event_type!r}")
    device = devices.get(tx.conn, device_id)
    if device is None:
        raise NotFoundError(f"device {device_id} does not exist")
    audit = emit(tx, audit_type, "transport", {"device_id": device_id, **(payload or {})},
                 meeting_id=device.meeting_id, timestamp=timestamp)
    connection = ConnectionEvent(event_id=audit.event_id, device_id=device_id, meeting_id=device.meeting_id,
                                 event_type=event_type, timestamp=audit.timestamp)
    _insert_connection_event(tx, connection)
    return connection, audit
