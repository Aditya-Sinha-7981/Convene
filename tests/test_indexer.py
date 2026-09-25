import asyncio
import time
import pytest

from server import registry
from server.config import RagConfig
from server.config import AttributionConfig
from server.attribution.service import AttributionService
from server.ids import new_id
from server.rag.embedding import FakeEmbeddingAdapter
from server.rag.indexer import TranscriptIndexer
from server.repositories.models import Utterance
from server.repositories import transcript_chunks, utterances
from server.timeutil import utc_now


def insert_utterance(tx, meeting, registration, text, second):
    return utterances.insert(tx.conn, Utterance(
        new_id(), meeting.meeting_id, registration.device.device_id, registration.participants[0].participant_id,
        text, f"2026-09-26T10:00:{second:02d}.000Z", f"2026-09-26T10:00:{second:02d}.000Z",
        .9, "device", .95, utc_now()))


@pytest.mark.asyncio
async def test_indexer_persists_an_embedded_mutable_tail_and_reports_status(db):
    with db.transaction() as tx:
        meeting = registry.create_meeting(tx, "Review")
        registration = registry.register_device(tx, meeting.meeting_id, new_id(), "Asha")
        insert_utterance(tx, meeting, registration, "Ship Friday after testing.", 1)
    indexer = TranscriptIndexer(db, FakeEmbeddingAdapter(384), RagConfig(target_tokens=20, hard_max_tokens=40))
    await indexer.start()
    try:
        await indexer.flush(meeting.meeting_id)
        chunks = transcript_chunks.list_for_meeting(db.conn, meeting.meeting_id)
        assert len(chunks) == 1 and chunks[0].status == "ready" and chunks[0].is_closed
        assert "[Asha, 00:00:00] Ship Friday" in chunks[0].text
        assert await indexer.index_status(meeting.meeting_id) == {
            "meeting_id": meeting.meeting_id, "ready": 1, "pending": 0, "failed": 0, "indexed": 1,
            "pending_utterances": 0, "lag_s": 0.0}
    finally:
        await indexer.stop()


class FailOnceAdapter(FakeEmbeddingAdapter):
    def __init__(self):
        super().__init__(384)
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary embedding fault")
        return super().embed(texts)


@pytest.mark.asyncio
async def test_embedding_failure_is_audited_retried_and_does_not_lose_the_chunk(db):
    with db.transaction() as tx:
        meeting = registry.create_meeting(tx, "Review")
        registration = registry.register_device(tx, meeting.meeting_id, new_id(), "Asha")
        insert_utterance(tx, meeting, registration, "Retry this chunk.", 1)
    adapter = FailOnceAdapter()
    config = RagConfig(target_tokens=20, hard_max_tokens=40, settle_delay_s=.01, quiet_flush_s=.01, retry_delay_s=.01)
    indexer = TranscriptIndexer(db, adapter, config)
    await indexer.start()
    try:
        await indexer.flush(meeting.meeting_id)
        assert transcript_chunks.list_for_meeting(db.conn, meeting.meeting_id)[0].status == "failed"
        await asyncio.sleep(.06)
        assert transcript_chunks.list_for_meeting(db.conn, meeting.meeting_id)[0].status == "ready"
        assert adapter.calls >= 2
    finally:
        await indexer.stop()


@pytest.mark.asyncio
async def test_startup_recovery_enqueues_uncovered_utterances(db):
    with db.transaction() as tx:
        meeting = registry.create_meeting(tx, "Review")
        registration = registry.register_device(tx, meeting.meeting_id, new_id(), "Asha")
        insert_utterance(tx, meeting, registration, "Recover after crash.", 1)
    indexer = TranscriptIndexer(db, FakeEmbeddingAdapter(384),
                                RagConfig(target_tokens=20, hard_max_tokens=40, settle_delay_s=.01, quiet_flush_s=.01))
    await indexer.start()
    try:
        await asyncio.sleep(.05)
        assert transcript_chunks.list_for_meeting(db.conn, meeting.meeting_id)[0].status == "ready"
    finally:
        await indexer.stop()


@pytest.mark.asyncio
async def test_embedding_runs_off_the_event_loop_and_honors_compute_priority(db):
    class SlowAdapter(FakeEmbeddingAdapter):
        def embed(self, texts):
            time.sleep(.05)
            return super().embed(texts)
    class Priority:
        def __init__(self): self.kinds = []
        async def wait_for_turn(self, kind): self.kinds.append(kind)
    with db.transaction() as tx:
        meeting = registry.create_meeting(tx, "Review")
        registration = registry.register_device(tx, meeting.meeting_id, new_id(), "Asha")
        insert_utterance(tx, meeting, registration, "Do not block the loop.", 1)
    priority = Priority()
    indexer = TranscriptIndexer(db, SlowAdapter(384), RagConfig(target_tokens=20, hard_max_tokens=40), priority=priority)
    await indexer.start()
    try:
        task = asyncio.create_task(indexer.flush(meeting.meeting_id))
        await asyncio.sleep(.005)
        assert not task.done()  # the event loop progressed while embedding ran in the executor
        await task
        assert priority.kinds == ["embedding"]
    finally:
        await indexer.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("device_count", [1, 2, 5])
async def test_indexing_lag_is_bounded_for_simulated_device_counts(db, device_count):
    with db.transaction() as tx:
        meeting = registry.create_meeting(tx, "Lag")
        for number in range(device_count):
            registration = registry.register_device(tx, meeting.meeting_id, new_id(), f"P{number}")
            insert_utterance(tx, meeting, registration, f"Line from phone {number}.", number + 1)
    config = RagConfig(target_tokens=20, hard_max_tokens=40, settle_delay_s=.01, quiet_flush_s=.01)
    indexer = TranscriptIndexer(db, FakeEmbeddingAdapter(384), config)
    began = time.monotonic()
    await indexer.start()
    try:
        while (await indexer.index_status(meeting.meeting_id))["pending_utterances"]:
            assert time.monotonic() - began < .5
            await asyncio.sleep(.01)
        assert (await indexer.index_status(meeting.meeting_id))["lag_s"] == 0.0
    finally:
        await indexer.stop()


@pytest.mark.asyncio
async def test_correction_rebuilds_only_the_covered_chunk_in_place(db):
    with db.transaction() as tx:
        meeting = registry.create_meeting(tx, "Review")
        registration = registry.register_device(tx, meeting.meeting_id, new_id(), "Asha")
        row = insert_utterance(tx, meeting, registration, "Ship Friday.", 1)
    indexer = TranscriptIndexer(db, FakeEmbeddingAdapter(384), RagConfig(target_tokens=20, hard_max_tokens=40))
    await indexer.start()
    service = AttributionService(db, AttributionConfig())
    service.register_correction_hook(indexer.corrected)
    try:
        await indexer.flush(meeting.meeting_id)
        before = transcript_chunks.list_for_meeting(db.conn, meeting.meeting_id)[0]
        await service.correct(meeting.meeting_id, row.utterance_id, {"display_name": "Bharat"})
        await service.drain()
        after = transcript_chunks.list_for_meeting(db.conn, meeting.meeting_id)[0]
        assert after.chunk_id == before.chunk_id and after.chunk_index == before.chunk_index
        assert "[Bharat, 00:00:00] Ship Friday." in after.text and after.status == "ready"
    finally:
        await indexer.stop()


@pytest.mark.asyncio
async def test_late_utterance_rebuilds_its_closed_time_range_in_place(db):
    with db.transaction() as tx:
        meeting = registry.create_meeting(tx, "Review")
        registration = registry.register_device(tx, meeting.meeting_id, new_id(), "Asha")
        insert_utterance(tx, meeting, registration, "First.", 1)
        insert_utterance(tx, meeting, registration, "Third.", 3)
    indexer = TranscriptIndexer(db, FakeEmbeddingAdapter(384), RagConfig(target_tokens=20, hard_max_tokens=40,
                                                                           settle_delay_s=.01, quiet_flush_s=.01))
    await indexer.start()
    try:
        await indexer.flush(meeting.meeting_id)
        before = transcript_chunks.list_for_meeting(db.conn, meeting.meeting_id)[0]
        with db.transaction() as tx:
            late = insert_utterance(tx, meeting, registration, "Second, late.", 2)
        indexer.enqueue(late)
        await asyncio.sleep(.05)
        after = transcript_chunks.list_for_meeting(db.conn, meeting.meeting_id)[0]
        assert after.chunk_id == before.chunk_id
        assert "Second, late." in after.text and after.status == "ready"
    finally:
        await indexer.stop()
