"""ParticipantEmail (ADR-33): the optional address a participant gave at join. Never part of a participant view."""
from . import base


def set_email(conn, participant_id: str, meeting_id: str, email: str, now: str) -> None:
    """Store or replace the participant's address. A changed address is sent to afresh, so ``last_sent_at`` resets."""
    base.execute(conn, "INSERT INTO ParticipantEmail (participant_id, meeting_id, email, created_at) VALUES (?, ?, ?, ?) "
                       "ON CONFLICT (participant_id) DO UPDATE SET email = excluded.email, created_at = excluded.created_at, "
                       "last_sent_at = NULL WHERE ParticipantEmail.email <> excluded.email",
                 (participant_id, meeting_id, email, now))


def get(conn, participant_id: str) -> str | None:
    row = base.query_one(conn, "SELECT email FROM ParticipantEmail WHERE participant_id = ?", (participant_id,))
    return row["email"] if row else None


def list_for_meeting(conn, meeting_id: str) -> list[dict]:
    """Every address in the meeting with the participant's current name, in join order."""
    rows = base.query_all(conn, "SELECT e.participant_id, p.display_name, e.email, e.last_sent_at "
                                "FROM ParticipantEmail e JOIN Participant p USING (participant_id) "
                                "WHERE e.meeting_id = ? ORDER BY p.rowid", (meeting_id,))
    return [dict(row) for row in rows]


def mark_sent(conn, participant_id: str, now: str) -> None:
    base.execute(conn, "UPDATE ParticipantEmail SET last_sent_at = ? WHERE participant_id = ?", (now, participant_id))
