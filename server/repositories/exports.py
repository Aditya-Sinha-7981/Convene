"""Persistence for DOCX export attempts (CON-11)."""
from dataclasses import asdict, dataclass

from . import base


@dataclass(frozen=True, slots=True)
class Export:
    export_id: str
    meeting_id: str
    type: str
    status: str
    storage_path: str | None
    error_message: str | None
    created_at: str


def _export(row):
    return Export(**{name: row[name] for name in Export.__dataclass_fields__})


def insert_pending(conn, export: Export) -> Export:
    base.insert(conn, "Export", asdict(export))
    return export


def finish(conn, export_id: str, *, status: str, storage_path: str | None = None,
           error_message: str | None = None) -> Export:
    cursor = base.execute(conn, "UPDATE Export SET status = ?, storage_path = ?, error_message = ? "
                                "WHERE export_id = ? AND status = 'pending'",
                          (status, storage_path, error_message, export_id))
    if cursor.rowcount == 0:
        raise RuntimeError(f"no pending export {export_id}")
    return get(conn, export_id)


def get(conn, export_id: str) -> Export | None:
    row = base.query_one(conn, "SELECT * FROM Export WHERE export_id = ?", (export_id,))
    return _export(row) if row else None


def latest_attempt(conn, meeting_id: str) -> Export | None:
    row = base.query_one(conn, "SELECT * FROM Export WHERE meeting_id = ? AND type = 'docx' "
                               "ORDER BY rowid DESC LIMIT 1", (meeting_id,))
    return _export(row) if row else None


def current(conn, meeting_id: str) -> Export | None:
    row = base.query_one(conn, "SELECT * FROM Export WHERE meeting_id = ? AND type = 'docx' AND status = 'ready' "
                               "ORDER BY rowid DESC LIMIT 1", (meeting_id,))
    return _export(row) if row else None
