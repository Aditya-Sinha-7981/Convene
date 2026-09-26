"""Persistence for CON-10 summaries and action items. A ``Summary`` row is written ``pending`` and finalized once."""
from dataclasses import asdict, dataclass

from ..errors import NotFoundError
from . import base


@dataclass(frozen=True, slots=True)
class Summary:
    summary_id: str
    meeting_id: str
    status: str  # pending | ready | failed
    summary_text: str | None
    error_message: str | None
    model_identifier: str
    generated_at: str | None


@dataclass(frozen=True, slots=True)
class ActionItem:
    action_item_id: str
    summary_id: str
    meeting_id: str
    text: str
    owner_participant_id: str | None
    status: str  # open | done


def _summary(row) -> Summary:
    return Summary(**{name: row[name] for name in Summary.__dataclass_fields__})


def insert_pending(conn, *, summary_id: str, meeting_id: str, model_identifier: str) -> Summary:
    summary = Summary(summary_id, meeting_id, "pending", None, None, model_identifier, None)
    base.insert(conn, "Summary", asdict(summary))
    return summary


def finish(conn, summary_id: str, *, status: str, generated_at: str, summary_text: str | None = None,
           error_message: str | None = None) -> Summary:
    """Move a ``pending`` attempt to ``ready`` or ``failed``. A finished attempt is never rewritten."""
    cursor = base.execute(conn, "UPDATE Summary SET status = ?, summary_text = ?, error_message = ?, generated_at = ? "
                                "WHERE summary_id = ? AND status = 'pending'",
                          (status, summary_text, error_message, generated_at, summary_id))
    if cursor.rowcount == 0:
        raise NotFoundError(f"no pending summary {summary_id}")
    return get(conn, summary_id)


def get(conn, summary_id: str) -> Summary | None:
    row = base.query_one(conn, "SELECT * FROM Summary WHERE summary_id = ?", (summary_id,))
    return _summary(row) if row else None


def latest_attempt(conn, meeting_id: str) -> Summary | None:
    row = base.query_one(conn, "SELECT * FROM Summary WHERE meeting_id = ? ORDER BY rowid DESC LIMIT 1", (meeting_id,))
    return _summary(row) if row else None


def current(conn, meeting_id: str) -> Summary | None:
    """The meeting's current summary: its most recent ``ready`` attempt (a failed re-run never replaces it)."""
    row = base.query_one(conn, "SELECT * FROM Summary WHERE meeting_id = ? AND status = 'ready' "
                               "ORDER BY rowid DESC LIMIT 1", (meeting_id,))
    return _summary(row) if row else None


def list_for_meeting(conn, meeting_id: str) -> list[Summary]:
    return [_summary(row) for row in base.query_all(
        conn, "SELECT * FROM Summary WHERE meeting_id = ? ORDER BY rowid", (meeting_id,))]


def list_pending(conn) -> list[Summary]:
    return [_summary(row) for row in base.query_all(conn, "SELECT * FROM Summary WHERE status = 'pending'")]


def insert_action_item(conn, item: ActionItem) -> ActionItem:
    base.insert(conn, "ActionItem", asdict(item))
    return item


def action_items(conn, summary_id: str) -> list[ActionItem]:
    """In the order the model listed them."""
    return [ActionItem(**{name: row[name] for name in ActionItem.__dataclass_fields__}) for row in base.query_all(
        conn, "SELECT * FROM ActionItem WHERE summary_id = ? ORDER BY rowid", (summary_id,))]
