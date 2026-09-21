from dataclasses import asdict

from ..errors import NotFoundError
from . import base
from .models import Participant

UPDATABLE = frozenset({"display_name", "enrollment_status"})


def create(conn, participant: Participant) -> Participant:
    base.insert(conn, "Participant", asdict(participant))
    return participant


def get(conn, participant_id: str) -> Participant | None:
    row = base.query_one(conn, "SELECT * FROM Participant WHERE participant_id = ?", (participant_id,))
    return Participant.from_row(row) if row else None


def require(conn, participant_id: str) -> Participant:
    participant = get(conn, participant_id)
    if participant is None:
        raise NotFoundError(f"participant {participant_id} does not exist")
    return participant


def list_for_meeting(conn, meeting_id: str) -> list[Participant]:
    rows = base.query_all(conn, "SELECT * FROM Participant WHERE meeting_id = ? ORDER BY rowid", (meeting_id,))
    return [Participant.from_row(r) for r in rows]


def list_for_device(conn, device_id: str) -> list[Participant]:
    rows = base.query_all(conn, "SELECT * FROM Participant WHERE device_id = ? ORDER BY rowid", (device_id,))
    return [Participant.from_row(r) for r in rows]


def update(conn, participant_id: str, /, **changes) -> Participant:
    base.update(conn, "Participant", "participant_id", participant_id, changes, UPDATABLE)
    return require(conn, participant_id)
