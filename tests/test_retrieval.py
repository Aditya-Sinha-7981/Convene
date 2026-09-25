"""CON-09 retrieval: explicit invocation only, meeting scope, as-of rule, threshold."""
import re
from pathlib import Path

import pytest

from server.rag.retrieval import search_meeting
from server.rag.vector_store import VectorStore
from tests.support.qa import TopicEmbedding, indexed, seed_meeting

ROOT = Path(__file__).resolve().parents[1]


def python_sources():
    return [path for path in (ROOT / "server").rglob("*.py") if "__pycache__" not in path.parts]


def test_only_the_qa_service_imports_retrieval():
    importers = sorted(str(path.relative_to(ROOT)) for path in python_sources()
                       if path.name != "retrieval.py"
                       and re.search(r"(from \.+retrieval import|from \.+rag\.retrieval import|"
                                     r"import server\.rag\.retrieval|from server\.rag\.retrieval)", path.read_text()))
    assert importers == ["server/rag/qa.py"]


def test_only_retrieval_searches_the_vector_store():
    callers = sorted(str(path.relative_to(ROOT)) for path in python_sources()
                     if re.search(r"\.search\(\s*conn|store\.search\(", path.read_text()))
    assert callers == ["server/rag/retrieval.py"]


def vector(embedding, text):
    return embedding.embed([text])[0]


@pytest.mark.asyncio
async def test_search_is_scoped_to_one_meeting_even_when_another_is_closer(db):
    embedding = TopicEmbedding()
    ours, _ = seed_meeting(db, [("Asha", 1, "The launch is on Friday and the launch plan is ready."),
                                ("Ben", 5, "The hotel is booked near the airport.")])
    theirs, _ = seed_meeting(db, [("Chitra", 1, "budget budget budget.")], title="Other")
    indexer = await indexed(db, embedding, ours.meeting_id)
    await indexer.flush(theirs.meeting_id)
    try:
        store = VectorStore(384, embedding.model_identifier)
        found = search_meeting(db.conn, store, ours.meeting_id, vector(embedding, "What is the budget?"),
                               top_k=5, min_similarity=-1.0, asked_at="2999-01-01T00:00:00.000Z")
        assert found.evidence and all(item.chunk.meeting_id == ours.meeting_id for item in found.evidence)
        assert all("budget" not in item.chunk.text for item in found.evidence)
    finally:
        await indexer.stop()


@pytest.mark.asyncio
async def test_chunks_whose_speech_was_written_after_the_question_are_not_eligible(db):
    embedding = TopicEmbedding()
    meeting, _ = seed_meeting(db, [("Asha", 1, "The budget is forty thousand.")], created_at="2026-09-26T10:05:00.000Z")
    indexer = await indexed(db, embedding, meeting.meeting_id)
    try:
        store = VectorStore(384, embedding.model_identifier)
        question = vector(embedding, "What is the budget?")
        before = search_meeting(db.conn, store, meeting.meeting_id, question, top_k=5, min_similarity=0.5,
                                asked_at="2026-09-26T10:04:59.999Z")
        after = search_meeting(db.conn, store, meeting.meeting_id, question, top_k=5, min_similarity=0.5,
                               asked_at="2026-09-26T10:05:00.000Z")
        assert before.evidence == [] and before.best_similarity is None
        assert len(after.evidence) == 1
    finally:
        await indexer.stop()


@pytest.mark.asyncio
async def test_threshold_drops_weak_chunks_and_reports_the_best_score(db):
    embedding = TopicEmbedding()
    meeting, _ = seed_meeting(db, [("Asha", 1, "The budget is forty thousand."),
                                   ("Ben", 5, "The hotel is near the airport.")])
    indexer = await indexed(db, embedding, meeting.meeting_id)
    try:
        store = VectorStore(384, embedding.model_identifier)
        found = search_meeting(db.conn, store, meeting.meeting_id, vector(embedding, "What is the budget?"),
                               top_k=5, min_similarity=0.6, asked_at="2999-01-01T00:00:00.000Z")
        assert [item.chunk.text.split("] ")[1] for item in found.evidence] == ["The budget is forty thousand."]
        assert found.eligible == 2 and found.best_similarity == pytest.approx(found.evidence[0].similarity)
        nothing = search_meeting(db.conn, store, meeting.meeting_id, vector(embedding, "Who owns churn?"),
                                 top_k=5, min_similarity=0.6, asked_at="2999-01-01T00:00:00.000Z")
        assert nothing.evidence == [] and nothing.best_similarity < 0.6
    finally:
        await indexer.stop()
