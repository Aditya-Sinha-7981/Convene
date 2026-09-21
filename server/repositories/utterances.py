"""Utterance storage only: insert, get, list, update. Creating utterances from STT is CON-06."""
from dataclasses import asdict

from ..errors import NotFoundError
from . import base
from .models import Utterance

UPDATABLE = frozenset({"participant_id", "text", "attribution_method", "attribution_confidence",
                       "corrected", "original_participant_id"})


def insert(conn, utterance: Utterance) -> Utterance:
    base.insert(conn, "Utterance", asdict(utterance))
    return utterance


def get(conn, utterance_id: str) -> Utterance | None:
    row = base.query_one(conn, "SELECT * FROM Utterance WHERE utterance_id = ?", (utterance_id,))
    return Utterance.from_row(row) if row else None


def require(conn, utterance_id: str) -> Utterance:
    utterance = get(conn, utterance_id)
    if utterance is None:
        raise NotFoundError(f"utterance {utterance_id} does not exist")
    return utterance


def list_for_meeting(conn, meeting_id: str, *, participant_id: str | None = None) -> list[Utterance]:
    """Transcript order: t_start ascending, ties by utterance_id (docs/api.md)."""
    sql, params = "SELECT * FROM Utterance WHERE meeting_id = ?", [meeting_id]
    if participant_id is not None:
        sql += " AND participant_id = ?"
        params.append(participant_id)
    sql += " ORDER BY t_start, utterance_id"
    return [Utterance.from_row(r) for r in base.query_all(conn, sql, params)]


def update(conn, utterance_id: str, /, **changes) -> Utterance:
    base.update(conn, "Utterance", "utterance_id", utterance_id, changes, UPDATABLE)
    return require(conn, utterance_id)


def count_for_meeting(conn, meeting_id: str) -> int:
    return base.query_one(conn, "SELECT COUNT(*) FROM Utterance WHERE meeting_id = ?", (meeting_id,))[0]
