"""Persistence for CON-16 action-item notes: append-only updates attached to an existing ``ActionItem``."""
from dataclasses import asdict, dataclass

from . import base


@dataclass(frozen=True, slots=True)
class ActionItemNote:
    note_id: str
    action_item_id: str
    source_meeting_id: str  # the meeting the update was mentioned in; may differ from the item's
    text: str
    created_at: str


def insert(conn, note: ActionItemNote) -> ActionItemNote:
    base.insert(conn, "ActionItemNote", asdict(note))
    return note


def list_for_item(conn, action_item_id: str) -> list[ActionItemNote]:
    """Oldest first."""
    return [ActionItemNote(**{name: row[name] for name in ActionItemNote.__dataclass_fields__}) for row in base.query_all(
        conn, "SELECT * FROM ActionItemNote WHERE action_item_id = ? ORDER BY created_at, rowid", (action_item_id,))]
