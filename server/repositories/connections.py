"""ConnectionEvent reads. Rows are written only by ``audit.record_connection_event`` (same transaction
as the matching AuditEvent), so this module has no insert."""
from . import base
from .models import ConnectionEvent


def get(conn, event_id: str) -> ConnectionEvent | None:
    row = base.query_one(conn, "SELECT * FROM ConnectionEvent WHERE event_id = ?", (event_id,))
    return ConnectionEvent.from_row(row) if row else None


def list_for_device(conn, device_id: str) -> list[ConnectionEvent]:
    rows = base.query_all(conn, "SELECT * FROM ConnectionEvent WHERE device_id = ? ORDER BY timestamp, rowid",
                          (device_id,))
    return [ConnectionEvent.from_row(r) for r in rows]


def list_for_meeting(conn, meeting_id: str) -> list[ConnectionEvent]:
    rows = base.query_all(conn, "SELECT * FROM ConnectionEvent WHERE meeting_id = ? ORDER BY timestamp, rowid",
                          (meeting_id,))
    return [ConnectionEvent.from_row(r) for r in rows]


def has_connected(conn, device_id: str) -> bool:
    """True if the device has ever reached `connected`, which is what makes its next attach a reconnect."""
    row = base.query_one(
        conn, "SELECT 1 FROM ConnectionEvent WHERE device_id = ? AND event_type IN ('connected', 'reconnected') LIMIT 1",
        (device_id,))
    return row is not None
