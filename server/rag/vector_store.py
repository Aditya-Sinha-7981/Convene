"""The small sqlite-vec boundary. Querying is provided for CON-09; no retrieval policy lives here."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..errors import ValidationError
from ..timeutil import utc_now
from ..repositories import transcript_chunks


def _blob(vector) -> bytes:
    values = np.asarray(vector, dtype=np.float32)
    return values.tobytes()


@dataclass(frozen=True)
class VectorHit:
    chunk_id: str
    distance: float


class VectorStore:
    metric = "cosine"

    def __init__(self, dimension: int, model_identifier: str):
        self.dimension, self.model_identifier = dimension, model_identifier

    def guard_model(self, conn) -> None:
        row = conn.execute("SELECT model_identifier, dimension, distance_metric FROM TranscriptIndexMeta WHERE singleton = 1").fetchone()
        if row is None:
            conn.execute("INSERT INTO TranscriptIndexMeta VALUES (1, ?, ?, ?, ?)",
                         (self.model_identifier, self.dimension, self.metric, utc_now()))
            return
        if tuple(row) != (self.model_identifier, self.dimension, self.metric):
            raise ValidationError("configured embedding model/dimension differs from this database; rebuild the index explicitly")

    def upsert(self, conn, chunk_id: str, meeting_id: str, vector) -> None:
        if len(vector) != self.dimension:
            raise ValidationError(f"embedding dimension {len(vector)} differs from configured {self.dimension}")
        exists = conn.execute("SELECT 1 FROM TranscriptChunkVector WHERE chunk_id = ?", (chunk_id,)).fetchone()
        if exists:
            # sqlite-vec partition keys are immutable; a chunk's meeting never changes during an in-place rebuild.
            conn.execute("UPDATE TranscriptChunkVector SET embedding = ? WHERE chunk_id = ?", (_blob(vector), chunk_id))
        else:
            conn.execute("INSERT INTO TranscriptChunkVector(chunk_id, meeting_id, embedding) VALUES (?, ?, ?)",
                         (chunk_id, meeting_id, _blob(vector)))
        transcript_chunks.update(conn, chunk_id, status="ready", error_message=None)

    def delete(self, conn, chunk_id: str) -> None:
        conn.execute("DELETE FROM TranscriptChunkVector WHERE chunk_id = ?", (chunk_id,))
        transcript_chunks.delete(conn, chunk_id)

    def search(self, conn, meeting_id: str, vector, limit: int) -> list[VectorHit]:
        if len(vector) != self.dimension:
            raise ValidationError(f"embedding dimension {len(vector)} differs from configured {self.dimension}")
        rows = conn.execute("SELECT chunk_id, distance FROM TranscriptChunkVector WHERE embedding MATCH ? "
                            "AND k = ? AND meeting_id = ? ORDER BY distance", (_blob(vector), limit, meeting_id)).fetchall()
        return [VectorHit(row["chunk_id"], row["distance"]) for row in rows]
