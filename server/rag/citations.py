"""Q&A views built from stored data only: the ``QAQuery`` view and its resolved citations (docs/api.md).

Citations are the chunks that were given to the model as evidence (``QAQuery.cited_chunk_ids``), resolved here
from ``TranscriptChunk`` and ``Utterance`` rows with current speaker labels. Nothing is parsed from model text,
so every citation names real stored lines. Shared by the Q&A service (HTTP response) and the dashboard hub
(``qa_answer`` push).
"""
from __future__ import annotations

import json

from ..attribution.labels import speaker_label
from ..repositories import devices, participants, transcript_chunks, utterances

ERROR_CODES = {"index_unavailable": "retrieval_failed", "retrieval_failed": "retrieval_failed",
               "answer_failed": "generation_failed", "answer_timeout": "generation_failed"}
ERROR_MESSAGES = {
    "index_unavailable": "the transcript index is not available",
    "retrieval_failed": "searching the transcript failed",
    "answer_failed": "the answer model failed",
    "answer_timeout": "the answer model did not finish in time",
}


def speaker_labels(conn, meeting_id: str, rows) -> list[str]:
    """Current ``speaker_label`` of each utterance row (docs/api.md, derived fields), computed in bulk."""
    people = {person.participant_id: person for person in participants.list_for_meeting(conn, meeting_id)}
    ordinals = {device.device_id: number for number, device in enumerate(devices.list_for_meeting(conn, meeting_id), 1)}
    return [speaker_label(row, people.get(row.participant_id), ordinals.get(row.device_id, 0)) for row in rows]


def resolve_citations(conn, meeting_id: str, chunk_ids: list[str]) -> list[dict]:
    if not chunk_ids:
        return []
    rows = utterances.list_for_meeting(conn, meeting_id)
    labels = speaker_labels(conn, meeting_id, rows)
    position = {row.utterance_id: index for index, row in enumerate(rows)}
    citations = []
    for chunk_id in chunk_ids:
        chunk = transcript_chunks.get(conn, chunk_id)
        if chunk is None or chunk.utterance_id_start not in position or chunk.utterance_id_end not in position:
            continue  # a chunk retired after the answer: it is still listed in cited_chunk_ids, never invented here
        start, end = position[chunk.utterance_id_start], position[chunk.utterance_id_end]
        span = rows[start:end + 1]
        speakers = list(dict.fromkeys(labels[start:end + 1]))  # every speaker in the chunk, in order of speech
        citations.append({
            "chunk_id": chunk.chunk_id, "chunk_index": chunk.chunk_index, "meeting_id": chunk.meeting_id,
            "utterance_id_start": chunk.utterance_id_start, "utterance_id_end": chunk.utterance_id_end,
            "utterance_ids": [row.utterance_id for row in span], "speakers": speakers,
            "t_start": span[0].t_start, "t_end": max(row.t_end for row in span), "text": chunk.text})
    return citations


def query_view(row: dict, error_code: str | None, reason: str | None) -> dict:
    """The stored ``QAQuery`` with ``cited_chunk_ids`` as a JSON array and the derived ``error`` field."""
    view = dict(row)
    view["cited_chunk_ids"] = json.loads(view["cited_chunk_ids"])
    view["error"] = ({"code": error_code, "message": ERROR_MESSAGES.get(reason, "the question could not be answered")}
                     if view["status"] == "failed" else None)
    return view
