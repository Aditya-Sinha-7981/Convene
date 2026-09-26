"""Scoped similarity search for an explicit question (ADR-11). Only ``server/rag/qa.py`` may import this module;
``tests/test_retrieval.py`` checks that statically, so no other code path can run retrieval.

Scope is an explicit argument: exactly one meeting for live mode, an explicit list for history mode (CON-14).
Every meeting in the scope is searched with its own partition-filtered KNN query and the hits are merged, so a
large or closer meeting outside the scope can never crowd a requested one out of top-k. Relevance is cosine
similarity, ``1 - distance`` from the sqlite-vec cosine-distance column (``docs/rag-and-qa.md``).

As-of rule: a chunk is eligible when the first utterance in its range was written (``Utterance.created_at``) at
or before ``asked_at``. A closed chunk rebuilt later (a correction or a late result) keeps its identity and may
then hold lines written after the question; that is accepted and documented, not filtered line by line.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..repositories import transcript_chunks
from ..repositories.models import TranscriptChunk
from .vector_store import VectorStore

OVERFETCH = 3  # vector hits fetched per requested chunk, so as-of and status filtering cannot starve top-k


@dataclass(frozen=True)
class Evidence:
    chunk: TranscriptChunk
    similarity: float


@dataclass(frozen=True)
class Retrieval:
    evidence: list[Evidence]           # above the threshold, most similar first, at most top_k
    best_similarity: float | None      # of any eligible chunk, kept or not (calibration and the audit trail)
    eligible: int                      # eligible chunks among the hits


def search_meeting(conn, store: VectorStore, meeting_id: str, question_vector, *, top_k: int, min_similarity: float,
                   asked_at: str) -> Retrieval:
    """Live mode: one meeting, chunks as of the question."""
    return search_meetings(conn, store, [meeting_id], question_vector, top_k=top_k, min_similarity=min_similarity,
                           asked_at=asked_at)


def search_meetings(conn, store: VectorStore, meeting_ids: list[str], question_vector, *, top_k: int,
                    min_similarity: float, asked_at: str) -> Retrieval:
    """History mode (and live, through ``search_meeting``): the given meetings only, never an implicit default."""
    if not meeting_ids:
        raise ValueError("retrieval needs an explicit, non-empty meeting scope")
    eligible: list[Evidence] = []
    for meeting_id in dict.fromkeys(meeting_ids):
        for hit in store.search(conn, meeting_id, question_vector, top_k * OVERFETCH):
            chunk = transcript_chunks.get(conn, hit.chunk_id)
            if chunk is None or chunk.meeting_id != meeting_id or chunk.status != "ready":
                continue  # a failed rebuild keeps its old vector: never cite text that the vector does not describe
            first = conn.execute("SELECT created_at FROM Utterance WHERE utterance_id = ?",
                                 (chunk.utterance_id_start,)).fetchone()
            if first is None or first["created_at"] > asked_at:
                continue
            eligible.append(Evidence(chunk, 1.0 - float(hit.distance)))
    eligible.sort(key=lambda item: -item.similarity)
    kept = [item for item in eligible if item.similarity >= min_similarity][:top_k]
    return Retrieval(kept, eligible[0].similarity if eligible else None, len(eligible))
