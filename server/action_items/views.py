"""Action-item API views and the global list query (CON-16): stored rows plus the derived fields in docs/api.md.

Everything a page shows is decided here: which items appear (only each meeting's current summary), the sort,
``overdue``, and the last change, which is derived from the audit stream rather than stored (ADR-18, ADR-28).
"""
from dataclasses import asdict
from datetime import datetime, timezone

from ..errors import ActionItemNotFoundError
from ..repositories import action_item_notes, base, meetings
from ..summary.service import normalize_name

STATUSES = ("open", "done", "cancelled")
SORTS = ("due", "recent")
CHANGE_EVENTS = ("action_item_updated", "action_item_note_added")
_STORED = ("action_item_id", "summary_id", "meeting_id", "text", "owner_participant_id", "status", "due_date")

# One row per item with its derived columns. ``last_seq`` is the item's newest manual change, or else the
# ``summary_generated`` event of its summary; the outer query turns it into a timestamp and channel.
_ITEMS = """
SELECT a.rowid AS item_rowid, a.action_item_id, a.summary_id, a.meeting_id, a.text, a.owner_participant_id, a.status, a.due_date,
       p.display_name AS owner_display_name, m.title AS meeting_title,
       COALESCE(m.started_at, m.created_at) AS meeting_started_at,
       (SELECT COUNT(*) FROM ActionItemNote n WHERE n.action_item_id = a.action_item_id) AS note_count,
       COALESCE(
           (SELECT MAX(e.seq) FROM AuditEvent e WHERE e.meeting_id = a.meeting_id
               AND e.event_type IN ('action_item_updated', 'action_item_note_added')
               AND json_extract(e.payload, '$.action_item_id') = a.action_item_id),
           (SELECT MAX(e.seq) FROM AuditEvent e WHERE e.meeting_id = a.meeting_id
               AND e.event_type = 'summary_generated' AND json_extract(e.payload, '$.summary_id') = a.summary_id)
       ) AS last_seq
FROM ActionItem a
JOIN Meeting m ON m.meeting_id = a.meeting_id
LEFT JOIN Participant p ON p.participant_id = a.owner_participant_id
"""
_CURRENT = ("a.summary_id = (SELECT s.summary_id FROM Summary s WHERE s.meeting_id = a.meeting_id "
            "AND s.status = 'ready' ORDER BY s.rowid DESC LIMIT 1)")
_ORDER = {
    "due": "i.due_date IS NULL, i.due_date, i.last_seq DESC, i.action_item_id",
    "recent": "i.last_seq DESC, i.action_item_id",
}


def utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _select(where: str) -> str:
    return (f"SELECT i.*, e.timestamp AS last_changed_at, e.event_type AS last_event_type "
            f"FROM ({_ITEMS} WHERE {where}) i LEFT JOIN AuditEvent e ON e.seq = i.last_seq")


def _view(row, today: str) -> dict:
    view = {name: row[name] for name in _STORED}
    view.update(owner_display_name=row["owner_display_name"], meeting_title=row["meeting_title"],
                meeting_started_at=row["meeting_started_at"], note_count=row["note_count"],
                last_changed_at=row["last_changed_at"],
                last_changed_by="manual" if row["last_event_type"] in CHANGE_EVENTS else "summary",
                overdue=row["status"] == "open" and row["due_date"] is not None and row["due_date"] < today)
    return view


def item_view(conn, action_item_id: str, *, today: str | None = None) -> dict:
    """One item, current or superseded, with every derived field."""
    row = base.query_one(conn, _select("a.action_item_id = ?"), (action_item_id,))
    if row is None:
        raise ActionItemNotFoundError(f"no action item with id {action_item_id}")
    return _view(row, today or utc_today())


def items_for_summary(conn, summary_id: str, *, today: str | None = None) -> list[dict]:
    """A summary's items in the order the model listed them (the post-meeting view)."""
    rows = base.query_all(conn, _select("a.summary_id = ?") + " ORDER BY i.item_rowid", (summary_id,))
    today = today or utc_today()
    return [_view(row, today) for row in rows]


def note_view(conn, note) -> dict:
    meeting = meetings.get(conn, note.source_meeting_id)
    return {**asdict(note), "source_meeting_title": meeting.title if meeting else None}


def detail(conn, action_item_id: str) -> dict:
    """``GET /api/action-items/{id}``: the item and its notes, oldest first."""
    item = item_view(conn, action_item_id)
    return {"action_item": item,
            "notes": [note_view(conn, note) for note in action_item_notes.list_for_item(conn, action_item_id)]}


def list_items(conn, *, statuses=("open",), owner: str | None = None, meeting_id: str | None = None,
               due_after: str | None = None, due_before: str | None = None, overdue: bool = False,
               sort: str = "due", limit: int = 50, offset: int = 0, today: str | None = None) -> dict:
    """``GET /api/action-items``. Arguments are already validated by the route."""
    today = today or utc_today()
    where, params = [_CURRENT], []
    where.append(f"a.status IN ({','.join('?' * len(statuses))})")
    params += list(statuses)
    if owner is not None:
        # Participants belong to one meeting; one person across meetings is found by name, as owner mapping does.
        wanted = normalize_name(owner)
        ids = [row[0] for row in base.query_all(conn, "SELECT participant_id, display_name FROM Participant")
               if normalize_name(row[1]) == wanted]
        where.append(f"a.owner_participant_id IN ({','.join('?' * len(ids))})" if ids else "0")
        params += ids
    if meeting_id is not None:
        where.append("a.meeting_id = ?")
        params.append(meeting_id)
    if due_after is not None:
        where.append("a.due_date >= ?")
        params.append(due_after)
    if due_before is not None:
        where.append("a.due_date <= ?")
        params.append(due_before)
    if overdue:
        where.append("a.status = 'open' AND a.due_date < ?")
        params.append(today)
    condition = " AND ".join(where)
    total = base.query_one(conn, f"SELECT COUNT(*) FROM ActionItem a WHERE {condition}", params)[0]
    rows = base.query_all(conn, _select(condition) + f" ORDER BY {_ORDER[sort]} LIMIT ? OFFSET ?",
                          [*params, limit, offset])
    return {"action_items": [_view(row, today) for row in rows], "total": total}
