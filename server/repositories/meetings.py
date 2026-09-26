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


def _filters(status, q, from_at, to_at) -> tuple[str, list]:
    clauses, params = [], []
    if status is not None:
        clauses.append("status = ?"); params.append(status)
    if q:
        # Case-insensitive substring; LIKE wildcards typed by the user are matched literally.
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("lower(COALESCE(title, '')) LIKE lower(?) ESCAPE '\\'"); params.append(f"%{escaped}%")
    if from_at:
        clauses.append("created_at >= ?"); params.append(from_at)
    if to_at:
        clauses.append("created_at <= ?"); params.append(to_at)
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def list_meetings(conn, *, status: str | None = None, q: str | None = None, from_at: str | None = None,
                  to_at: str | None = None, limit: int | None = None, offset: int = 0) -> list[Meeting]:
    """Newest first (created_at, then meeting_id). ``from_at``/``to_at`` are inclusive ``created_at`` bounds."""
    where, params = _filters(status, q, from_at, to_at)
    sql = f"SELECT * FROM Meeting{where} ORDER BY created_at DESC, meeting_id DESC LIMIT ? OFFSET ?"
    return [Meeting.from_row(r) for r in base.query_all(conn, sql, [*params, -1 if limit is None else limit, offset])]


def count_meetings(conn, *, status: str | None = None, q: str | None = None, from_at: str | None = None,
                   to_at: str | None = None) -> int:
    where, params = _filters(status, q, from_at, to_at)
    return conn.execute(f"SELECT COUNT(*) FROM Meeting{where}", params).fetchone()[0]


def update(conn, meeting_id: str, /, **changes) -> Meeting:
    base.update(conn, "Meeting", "meeting_id", meeting_id, changes, UPDATABLE, MeetingNotFoundError)
    return require(conn, meeting_id)
