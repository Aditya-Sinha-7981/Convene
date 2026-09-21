from dataclasses import asdict

from ..errors import MeetingNotFoundError
from . import base
from .models import Meeting

UPDATABLE = frozenset({"title", "status", "started_at", "ended_at"})


def create(conn, meeting: Meeting) -> Meeting:
    base.insert(conn, "Meeting", asdict(meeting))
    return meeting


def get(conn, meeting_id: str) -> Meeting | None:
    row = base.query_one(conn, "SELECT * FROM Meeting WHERE meeting_id = ?", (meeting_id,))
    return Meeting.from_row(row) if row else None


def require(conn, meeting_id: str) -> Meeting:
    meeting = get(conn, meeting_id)
    if meeting is None:
        raise MeetingNotFoundError(f"meeting {meeting_id} does not exist")
    return meeting


def list_meetings(conn, *, status: str | None = None, limit: int | None = None, offset: int = 0) -> list[Meeting]:
    """Newest first (created_at, then meeting_id)."""
    sql, params = "SELECT * FROM Meeting", []
    if status is not None:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC, meeting_id DESC LIMIT ? OFFSET ?"
    params += [-1 if limit is None else limit, offset]
    return [Meeting.from_row(r) for r in base.query_all(conn, sql, params)]


def update(conn, meeting_id: str, /, **changes) -> Meeting:
    base.update(conn, "Meeting", "meeting_id", meeting_id, changes, UPDATABLE, MeetingNotFoundError)
    return require(conn, meeting_id)
