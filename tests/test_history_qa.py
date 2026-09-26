"""CON-14 history Q&A: scope, isolation, crowding, model mismatch, unindexed meetings, citations, persistence."""
import json
from dataclasses import replace

import pytest

from server import registry
from server.errors import MeetingNotEndedError, MeetingNotFoundError, ValidationError
from server.ids import new_id
from server.rag.qa import HISTORY_SYSTEM_PROMPT, QAService
from server.rag.reasoning import FakeReasoningAdapter
from server.repositories import audit_events, meetings, model_executions, qa_queries, transcript_chunks
from server.repositories.model_executions import ModelExecution
from tests.support.qa import QA, TopicEmbedding, indexed, seed_meeting

# A fact that exists only in meeting A (the budget) and one only in meeting B (the hotel).
A_LINES = [("Asha", 1, "The budget for the pilot is forty thousand rupees."),
           ("Ben", 10, "The intern starts on Monday.")]
B_LINES = [("Chitra", 1, "The hotel for the offsite is booked near the airport."),
           ("Dev", 10, "The design review moves to Thursday.")]


def end(db, meeting):
    with db.transaction() as tx:
        registry.end_meeting(tx, meeting.meeting_id)
    return meetings.get(db.conn, meeting.meeting_id)


def go_live(db, meeting):
    with db.transaction() as tx:
        return meetings.update(tx.conn, meeting.meeting_id, status="live", started_at="2026-09-26T10:00:00.000Z")


def answer_first_excerpt(messages):
    return "From the excerpts: " + messages[-1]["content"].split("\n")[3]


class World:
    """Two ended, indexed meetings (A and B) and a history-capable service over them."""

    def __init__(self, db, service, a, b, indexer, reasoning, embedding):
        self.db, self.service, self.a, self.b = db, service, a, b
        self.indexer, self.reasoning, self.embedding = indexer, reasoning, embedding

    def ask(self, question, meeting_ids=None, **extra):
        body = {"question": question, "mode": "history", **extra}
        if meeting_ids is not None:
            body["meeting_ids"] = meeting_ids
        return self.service.ask_history(body)


@pytest.fixture
async def world(db):
    embedding = TopicEmbedding()
    a, _ = seed_meeting(db, A_LINES, title="Pilot budget sync")
    b, _ = seed_meeting(db, B_LINES, title="Offsite planning")
    indexer = await indexed(db, embedding, a.meeting_id)
    await indexer.flush(b.meeting_id)
    a, b = end(db, a), end(db, b)
    reasoning = FakeReasoningAdapter(answer_first_excerpt)
    service = QAService(db, QA, embedding=embedding, reasoning=reasoning, indexer=indexer)
    yield World(db, service, a, b, indexer, reasoning, embedding)
    await indexer.stop()


def events(db):
    return [event for event in audit_events.list_events(db.conn, event_type="qa_query")]


# --- scope ------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_fact_only_in_meeting_a_is_not_found_when_only_b_is_searched(world):
    result = await world.ask("What is the budget?", [world.b.meeting_id])
    assert result["query"]["status"] == "no_grounding" and result["reason"] == "no_relevant_evidence"
    assert result["citations"] == [] and world.reasoning.calls == []


@pytest.mark.asyncio
async def test_scope_a_answers_with_a_citation_carrying_meeting_identity(world):
    result = await world.ask("What is the budget?", [world.a.meeting_id])
    query = result["query"]
    assert query["status"] == "answered" and query["mode"] == "history"
    [citation] = result["citations"]
    assert citation["meeting_id"] == world.a.meeting_id and citation["meeting_title"] == "Pilot budget sync"
    assert citation["meeting_started_at"] == (world.a.started_at or world.a.created_at)
    assert citation["speakers"] == ["Asha"] and citation["t_start"] == "2026-09-26T10:00:01.000Z"
    assert citation["utterance_ids"] and "forty thousand" in citation["text"]
    assert set(query["cited_chunk_ids"]) <= {c.chunk_id for c in transcript_chunks.list_for_meeting(world.db.conn, world.a.meeting_id)}


@pytest.mark.asyncio
async def test_all_ended_meetings_answers_and_cites_the_meeting_the_fact_came_from(world):
    result = await world.ask("What is the budget?")
    assert result["query"]["status"] == "answered" and result["scope"]["all_ended"] is True
    assert {item["meeting_id"] for item in result["scope"]["coverage"]} == {world.a.meeting_id, world.b.meeting_id}
    assert [c["meeting_id"] for c in result["citations"]] == [world.a.meeting_id]
    # The model is told which meeting each excerpt came from, under the history prompt.
    [messages] = world.reasoning.calls
    assert messages[0]["content"] == HISTORY_SYSTEM_PROMPT
    assert "Meeting: Pilot budget sync (" in messages[1]["content"] and "past meetings" in messages[1]["content"]


@pytest.mark.asyncio
async def test_all_excludes_a_live_meeting_and_listing_one_explicitly_is_rejected(world):
    live, _ = seed_meeting(world.db, [("Eve", 1, "The budget budget budget is ninety thousand.")], title="Live now")
    await world.indexer.flush(live.meeting_id)
    live = go_live(world.db, live)
    result = await world.ask("What is the budget?")
    assert live.meeting_id not in {item["meeting_id"] for item in result["scope"]["coverage"]}
    assert all(c["meeting_id"] != live.meeting_id for c in result["citations"])
    with pytest.raises(MeetingNotEndedError, match="Live now"):
        await world.ask("What is the budget?", [world.a.meeting_id, live.meeting_id])
    created, _ = seed_meeting(world.db, [], title="Never started")
    with pytest.raises(MeetingNotEndedError):
        await world.ask("What is the budget?", [created.meeting_id])


@pytest.mark.asyncio
async def test_request_validation(world):
    with pytest.raises(ValidationError):
        await world.ask("   ", [world.a.meeting_id])
    with pytest.raises(ValidationError, match="mode"):
        await world.ask("What is the budget?", [world.a.meeting_id], mode="live")
    with pytest.raises(ValidationError, match="non-empty"):
        await world.ask("What is the budget?", [])
    with pytest.raises(ValidationError, match="non-empty"):
        await world.ask("What is the budget?", world.a.meeting_id)
    with pytest.raises(ValidationError, match="not-a-uuid"):
        await world.ask("What is the budget?", ["not-a-uuid"])
    with pytest.raises(ValidationError, match="limit is 50"):
        await world.ask("What is the budget?", [new_id() for _ in range(51)])
    missing = new_id()
    with pytest.raises(MeetingNotFoundError, match=missing):
        await world.ask("What is the budget?", [world.a.meeting_id, missing])
    assert events(world.db) == []  # nothing recorded for a rejected request


@pytest.mark.asyncio
async def test_duplicate_ids_are_searched_once(world):
    result = await world.ask("What is the budget?", [world.a.meeting_id, world.a.meeting_id])
    assert [item["meeting_id"] for item in result["scope"]["coverage"]] == [world.a.meeting_id]
    assert result["query"]["meeting_id"] == world.a.meeting_id


@pytest.mark.asyncio
async def test_no_ended_meetings_is_an_honest_empty_result(db):
    service = QAService(db, QA, embedding=TopicEmbedding(), reasoning=FakeReasoningAdapter(), indexer=None)
    result = await service.ask_history({"question": "What is the budget?"})
    assert result["query"]["status"] == "no_grounding" and result["reason"] == "no_ended_meetings"
    assert result["scope"] == {"all_ended": True, "coverage": []}


# --- isolation --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_questions_never_return_other_meetings_and_history_never_touches_live_state(world):
    live, _ = seed_meeting(world.db, [("Eve", 1, "The launch is on Friday.")], title="Live now")
    await world.indexer.flush(live.meeting_id)
    live = go_live(world.db, live)
    # Meeting A holds a much closer chunk for this question; the live endpoint must still stay in its meeting.
    result = await world.service.ask(live.meeting_id, {"question": "What is the budget?"})
    assert all(c["meeting_id"] == live.meeting_id for c in result["citations"])
    assert result["query"]["status"] == "no_grounding"

    before = (meetings.get(world.db.conn, live.meeting_id),
              len(audit_events.list_events(world.db.conn, meeting_id=live.meeting_id)))
    await world.ask("What is the budget?")
    await world.ask("What is the budget?", [world.a.meeting_id])
    after = (meetings.get(world.db.conn, live.meeting_id),
             len(audit_events.list_events(world.db.conn, meeting_id=live.meeting_id)))
    assert before == after


@pytest.mark.asyncio
async def test_many_closer_chunks_in_other_meetings_cannot_crowd_out_the_requested_meeting(db):
    embedding = TopicEmbedding()
    wanted, _ = seed_meeting(db, [("Asha", 1, "The budget came up briefly next to the launch and the hotel.")],
                             title="Wanted")
    noisy = []
    for number in range(4):
        meeting, _ = seed_meeting(db, [("Ben", second, "budget budget budget.") for second in range(1, 12)],
                                  title=f"Noisy {number}")
        noisy.append(meeting)
    indexer = await indexed(db, embedding, wanted.meeting_id)
    try:
        for meeting in noisy:
            await indexer.flush(meeting.meeting_id)
        for meeting in [wanted, *noisy]:
            end(db, meeting)
        service = QAService(db, replace(QA, min_similarity=0.3), embedding=embedding,
                            reasoning=FakeReasoningAdapter(), indexer=indexer)
        result = await service.ask_history({"question": "What is the budget?", "meeting_ids": [wanted.meeting_id]})
        assert result["query"]["status"] == "answered"
        assert [c["meeting_id"] for c in result["citations"]] == [wanted.meeting_id]
    finally:
        await indexer.stop()


# --- model mismatch and unindexed meetings ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_meeting_embedded_by_another_model_fails_by_name_and_nothing_is_searched(world):
    [chunk, *_] = transcript_chunks.list_for_meeting(world.db.conn, world.b.meeting_id)
    with world.db.transaction() as tx:
        model_executions.insert(tx.conn, ModelExecution(new_id(), "embedding", "old/other-embedding",
                                                        "sentence_transformers", 5, chunk.chunk_id,
                                                        "2999-01-01T00:00:00.000Z"))
    result = await world.ask("What is the budget?", [world.a.meeting_id, world.b.meeting_id])
    query = result["query"]
    assert query["status"] == "failed" and result["reason"] == "embedding_model_mismatch"
    assert query["error"]["code"] == "retrieval_failed"
    assert "Offsite planning" in query["error"]["message"] and "old/other-embedding" in query["error"]["message"]
    assert result["citations"] == [] and world.reasoning.calls == []
    states = {item["meeting_id"]: item["state"] for item in result["scope"]["coverage"]}
    assert states[world.b.meeting_id] == "model_mismatch" and states[world.a.meeting_id] != "model_mismatch"
    assert not [e for e in model_executions.list_executions(world.db.conn, resource_type="embedding")
                if e.related_id == query["query_id"]]  # the question was never embedded
    # Meeting A alone is still searchable.
    assert (await world.ask("What is the budget?", [world.a.meeting_id]))["query"]["status"] == "answered"


@pytest.mark.asyncio
async def test_a_database_indexed_by_another_model_fails_every_history_query(world):
    with world.db.transaction() as tx:
        tx.conn.execute("UPDATE TranscriptIndexMeta SET model_identifier = 'old/other', dimension = 768")
    result = await world.ask("What is the budget?", [world.a.meeting_id])
    assert result["query"]["status"] == "failed" and result["reason"] == "embedding_model_mismatch"
    assert "768 dimensions" in result["query"]["error"]["message"] and world.reasoning.calls == []


@pytest.mark.asyncio
async def test_an_unindexed_meeting_is_reported_not_silently_omitted(world):
    # Seeded after the indexer started and never enqueued: its lines exist but no chunk does.
    fresh, _ = seed_meeting(world.db, [("Fay", 1, "The latency budget is two hundred milliseconds.")], title="Unflushed")
    fresh = end(world.db, fresh)
    alone = await world.ask("What is the latency?", [fresh.meeting_id])
    assert alone["query"]["status"] == "no_grounding" and alone["reason"] == "not_indexed_yet"
    assert alone["unindexed_utterances"] == 1 and alone["scope"]["coverage"][0]["state"] == "not_indexed"

    both = await world.ask("What is the budget?", [world.a.meeting_id, fresh.meeting_id])
    assert both["query"]["status"] == "answered" and both["unindexed_utterances"] == 1
    states = {item["meeting_id"]: item["state"] for item in both["scope"]["coverage"]}
    assert states == {world.a.meeting_id: "searched", fresh.meeting_id: "not_indexed"}

    old, _ = seed_meeting(world.db, [("Gus", 1, "The latency is fine.")], title="Stale", created_at="2026-01-01T00:00:00.000Z")
    old = end(world.db, old)
    stale = await world.ask("What is the latency?", [old.meeting_id])
    assert stale["query"]["status"] == "failed" and stale["reason"] == "index_unavailable"


@pytest.mark.asyncio
async def test_a_selected_meeting_with_no_speech_is_nothing_transcribed_yet(world):
    silent, _ = seed_meeting(world.db, [], title="Silent")
    silent = end(world.db, silent)
    result = await world.ask("What is the budget?", [silent.meeting_id])
    assert result["query"]["status"] == "no_grounding" and result["reason"] == "nothing_transcribed_yet"
    assert result["scope"]["coverage"][0]["state"] == "empty"


# --- persistence and honesty ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_meeting_id_is_set_for_one_meeting_null_for_several_and_scope_is_audited(world):
    single = await world.ask("What is the budget?", [world.a.meeting_id])
    several = await world.ask("What is the budget?")
    one = qa_queries.get(world.db.conn, single["query"]["query_id"])
    many = qa_queries.get(world.db.conn, several["query"]["query_id"])
    assert one["mode"] == many["mode"] == "history"
    assert one["meeting_id"] == world.a.meeting_id and many["meeting_id"] is None
    assert json.loads(many["cited_chunk_ids"]) == several["query"]["cited_chunk_ids"]
    first, second = events(world.db)
    assert first.meeting_id == world.a.meeting_id and first.payload["meeting_ids"] == [world.a.meeting_id]
    assert second.meeting_id is None and set(second.payload["meeting_ids"]) == {world.a.meeting_id, world.b.meeting_id}
    assert second.payload["mode"] == "history" and second.payload["status"] == "answered"
    assert second.payload["chunk_count"] == 1 and second.payload["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_an_absent_fact_over_history_is_no_grounding_without_calling_the_model(world):
    result = await world.ask("What happened to churn?")
    assert result["query"]["status"] == "no_grounding" and result["reason"] == "no_relevant_evidence"
    assert result["query"]["answer"] is None and world.reasoning.calls == []


@pytest.mark.asyncio
async def test_a_model_decline_over_history_is_no_grounding(world):
    world.service.reasoning = FakeReasoningAdapter(lambda messages: "NO_GROUNDING")
    result = await world.ask("What is the budget?")
    assert result["query"]["status"] == "no_grounding" and result["reason"] == "model_declined"
    assert result["citations"] == []
