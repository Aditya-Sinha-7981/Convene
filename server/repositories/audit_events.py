"""AuditEvent reads. Rows are written only by ``audit.emit``; no component inserts audit rows directly."""
from . import base
from .models import AuditEvent


def get(conn, event_id: str) -> AuditEvent | None:
    row = base.query_one(conn, "SELECT * FROM AuditEvent WHERE event_id = ?", (event_id,))
    return AuditEvent.from_row(row) if row else None


def max_seq(conn) -> int:
    """Highest committed ``seq``; 0 when there are no events (the dashboard's ``as_of_seq``)."""
    return base.query_one(conn, "SELECT COALESCE(MAX(seq), 0) FROM AuditEvent")[0]


def list_events(conn, *, meeting_id: str | None = None, event_type: str | None = None,
                after_seq: int = 0, limit: int | None = None) -> list[AuditEvent]:
    sql, params = "SELECT * FROM AuditEvent WHERE seq > ?", [after_seq]
    if meeting_id is not None:
        sql += " AND meeting_id = ?"
        params.append(meeting_id)
    if event_type is not None:
        sql += " AND event_type = ?"
        params.append(event_type)
    sql += " ORDER BY seq LIMIT ?"
    params.append(-1 if limit is None else limit)
    return [AuditEvent.from_row(r) for r in base.query_all(conn, sql, params)]
