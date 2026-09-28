"""Action-item edits and notes (CON-16, ADR-28). Row updates only: nothing here calls a model.

Each function runs inside the caller's transaction and writes its audit events through ``emit`` in that same
transaction, so a change and its record commit or roll back together. Payloads carry ids, dates and enums only;
item and note text never enter the audit stream.
"""
from datetime import date

from ..audit import emit
from ..errors import ActionItemNotFoundError, ValidationError
from ..ids import is_uuid4, new_id
from ..repositories import action_item_notes, meetings, participants, summaries
from ..timeutil import utc_now
from .views import STATUSES

EDITABLE = ("owner_participant_id", "due_date", "status")
NOTE_MAX = 2000


def _require_item(conn, action_item_id: str):
    item = summaries.get_action_item(conn, action_item_id)
    if item is None:
        raise ActionItemNotFoundError(f"no action item with id {action_item_id}")
    return item


def _due_date(value):
    if value is None:
        return None
    try:
        if not isinstance(value, str) or len(value) != 10 or date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError:
        raise ValidationError("due_date must be a calendar date YYYY-MM-DD or null") from None
    return value


def _status(value):
    if value not in STATUSES:
        raise ValidationError(f"status must be one of {', '.join(STATUSES)}")
    return value


def validate_changes(conn, item, changes: dict) -> dict:
    """Check each requested field against the item; returns the normalized values. Raises before any write."""
    if not isinstance(changes, dict) or not changes:
        raise ValidationError(f"send at least one of {', '.join(EDITABLE)}")
    unknown = sorted(set(changes) - set(EDITABLE))
    if unknown:
        raise ValidationError(f"cannot edit {', '.join(unknown)}; editable fields are {', '.join(EDITABLE)}")
    clean = {}
    if "owner_participant_id" in changes:
        owner = changes["owner_participant_id"]
        if owner is not None:
            person = participants.get(conn, owner) if isinstance(owner, str) and is_uuid4(owner) else None
            if person is None or person.meeting_id != item.meeting_id:
                raise ValidationError("owner_participant_id must be a participant of the item's own meeting, or null")
        clean["owner_participant_id"] = owner
    if "due_date" in changes:
        clean["due_date"] = _due_date(changes["due_date"])
    if "status" in changes:
        clean["status"] = _status(changes["status"])
    return clean


def _apply(tx, item, clean: dict, via: str) -> bool:
    """Update the changed fields and emit one ``action_item_updated``. A no-op writes and emits nothing."""
    changed = {field: value for field, value in clean.items() if getattr(item, field) != value}
    if not changed:
        return False
    summaries.update_action_item(tx.conn, item.action_item_id, changed)
    emit(tx, "action_item_updated", "api", {
        "action_item_id": item.action_item_id, "summary_id": item.summary_id,
        "from": {field: getattr(item, field) for field in changed}, "to": changed, "via": via},
        meeting_id=item.meeting_id)
    return True


def update_action_item(tx, action_item_id: str, changes: dict) -> bool:
    """Edit owner, due date and/or status of one item (last write wins). Returns whether anything changed."""
    item = _require_item(tx.conn, action_item_id)
    return _apply(tx, item, validate_changes(tx.conn, item, changes), "edit")


def add_action_item_note(tx, action_item_id: str, source_meeting_id, text, status=None):
    """Append a note from ``source_meeting_id`` and optionally change the status, in one transaction.

    Never creates an ``ActionItem``. Returns the stored note.
    """
    item = _require_item(tx.conn, action_item_id)
    if not isinstance(source_meeting_id, str) or not is_uuid4(source_meeting_id):
        raise ValidationError("source_meeting_id must be a UUID v4")
    meetings.require(tx.conn, source_meeting_id)
    if not isinstance(text, str) or not text.strip():
        raise ValidationError("text must be a non-empty string")
    text = text.strip()
    if len(text) > NOTE_MAX:
        raise ValidationError(f"text is longer than {NOTE_MAX} characters")
    clean = {"status": _status(status)} if status is not None else {}
    note = action_item_notes.insert(tx.conn, action_item_notes.ActionItemNote(
        new_id(), action_item_id, source_meeting_id, text, utc_now()))
    emit(tx, "action_item_note_added", "api", {"note_id": note.note_id, "action_item_id": action_item_id,
                                               "source_meeting_id": source_meeting_id}, meeting_id=item.meeting_id)
    _apply(tx, item, clean, "note")
    return note
