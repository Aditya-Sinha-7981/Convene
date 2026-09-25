"""Persistence for CON-08 transcript chunks; vector bytes stay in sqlite-vec."""
from dataclasses import asdict

from . import base
from .models import TranscriptChunk

UPDATABLE = frozenset({"text", "status", "error_message", "utterance_id_start", "utterance_id_end", "is_closed"})


def insert(conn, chunk: TranscriptChunk) -> TranscriptChunk:
    base.insert(conn, "TranscriptChunk", asdict(chunk))
    return chunk


def get(conn, chunk_id: str) -> TranscriptChunk | None:
    row = base.query_one(conn, "SELECT * FROM TranscriptChunk WHERE chunk_id = ?", (chunk_id,))
    return TranscriptChunk.from_row(row) if row else None


def list_for_meeting(conn, meeting_id: str) -> list[TranscriptChunk]:
    return [TranscriptChunk.from_row(row) for row in base.query_all(
        conn, "SELECT * FROM TranscriptChunk WHERE meeting_id = ? ORDER BY chunk_index", (meeting_id,))]


def update(conn, chunk_id: str, /, **changes) -> TranscriptChunk:
    base.update(conn, "TranscriptChunk", "chunk_id", chunk_id, changes, UPDATABLE)
    chunk = get(conn, chunk_id)
    assert chunk is not None
    return chunk


def delete(conn, chunk_id: str) -> None:
    base.execute(conn, "DELETE FROM TranscriptChunk WHERE chunk_id = ?", (chunk_id,))
