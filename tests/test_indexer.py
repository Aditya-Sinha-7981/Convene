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
    config = RagConfig(target_tokens=20, hard_max_tokens=40, settle_delay_s=.01, retry_delay_s=.01)
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
                                RagConfig(target_tokens=20, hard_max_tokens=40, settle_delay_s=.01))
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
    config = RagConfig(target_tokens=20, hard_max_tokens=40, settle_delay_s=.01)
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
                                                                           settle_delay_s=.01))
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


FAST = dict(settle_delay_s=.01, retry_delay_s=.01)


def two_people(db):
    with db.transaction() as tx:
        meeting = registry.create_meeting(tx, "Review")
        asha = registry.register_device(tx, meeting.meeting_id, new_id(), "Asha")
        ben = registry.register_device(tx, meeting.meeting_id, new_id(), "Ben")
    return meeting, asha, ben


def assert_disjoint_and_complete(db, meeting_id):
    rows = utterances.list_for_meeting(db.conn, meeting_id)
    position = {row.utterance_id: index for index, row in enumerate(rows)}
    covered = []
    for chunk in transcript_chunks.list_for_meeting(db.conn, meeting_id):
        covered.extend(range(position[chunk.utterance_id_start], position[chunk.utterance_id_end] + 1))
    assert sorted(set(covered)) == list(range(len(rows)))


async def wait_until_indexed(indexer, meeting_id, limit=1.0):
    began = time.monotonic()
    while (await indexer.index_status(meeting_id))["pending_utterances"]:
        assert time.monotonic() - began < limit, await indexer.index_status(meeting_id)
        await asyncio.sleep(.01)


@pytest.mark.asyncio
async def test_result_written_after_meeting_end_is_indexed_and_closed(db):
    meeting, asha, _ = two_people(db)
    with db.transaction() as tx:
        insert_utterance(tx, meeting, asha, "First line.", 1)
    indexer = TranscriptIndexer(db, FakeEmbeddingAdapter(384), RagConfig(target_tokens=20, hard_max_tokens=40, **FAST))
    await indexer.start()
    try:
        await wait_until_indexed(indexer, meeting.meeting_id)
        with db.transaction() as tx:
            registry.end_meeting(tx, meeting.meeting_id)
        await indexer.meeting_ended(meeting.meeting_id)  # returns at once; the worker closes the tail
        await asyncio.sleep(.05)
        assert all(chunk.is_closed for chunk in transcript_chunks.list_for_meeting(db.conn, meeting.meeting_id))
        with db.transaction() as tx:
            straggler = insert_utterance(tx, meeting, asha, "Straggler still in the STT queue.", 5)
        indexer.enqueue(straggler)
        await wait_until_indexed(indexer, meeting.meeting_id)
        chunks = transcript_chunks.list_for_meeting(db.conn, meeting.meeting_id)
        assert all(chunk.is_closed and chunk.status == "ready" for chunk in chunks)
        assert any("Straggler" in chunk.text for chunk in chunks)
    finally:
        await indexer.stop()


@pytest.mark.asyncio
async def test_late_result_between_two_closed_chunks_joins_the_earlier_chunk_in_place(db):
    meeting, asha, ben = two_people(db)
    with db.transaction() as tx:
        insert_utterance(tx, meeting, asha, "alpha alpha alpha alpha alpha.", 1)
        insert_utterance(tx, meeting, ben, "beta beta beta beta beta.", 10)
        insert_utterance(tx, meeting, asha, "gamma gamma gamma gamma gamma.", 20)
    indexer = TranscriptIndexer(db, FakeEmbeddingAdapter(384), RagConfig(target_tokens=5, hard_max_tokens=40, **FAST))
    await indexer.start()
    try:
        await wait_until_indexed(indexer, meeting.meeting_id)
        before = transcript_chunks.list_for_meeting(db.conn, meeting.meeting_id)
        assert len(before) == 3 and before[0].is_closed
        with db.transaction() as tx:
            late = insert_utterance(tx, meeting, ben, "late late late.", 5)
        indexer.enqueue(late)
        await wait_until_indexed(indexer, meeting.meeting_id)
        after = transcript_chunks.list_for_meeting(db.conn, meeting.meeting_id)
        assert [chunk.chunk_id for chunk in after] == [chunk.chunk_id for chunk in before]
        assert "late late late." in after[0].text and after[0].utterance_id_end == late.utterance_id
        assert after[1].text == before[1].text  # the neighbouring chunk was not rebuilt
        assert_disjoint_and_complete(db, meeting.meeting_id)
    finally:
        await indexer.stop()


@pytest.mark.asyncio
async def test_concurrent_flush_worker_and_correction_do_not_collide(db):
    class Slow(FakeEmbeddingAdapter):
        def embed(self, texts):
            time.sleep(.02)
            return super().embed(texts)
    meeting, asha, ben = two_people(db)
    with db.transaction() as tx:
        rows = [insert_utterance(tx, meeting, asha if i % 2 else ben, f"word {i} " * 6, i) for i in range(1, 20)]
    indexer = TranscriptIndexer(db, Slow(384), RagConfig(target_tokens=10, hard_max_tokens=40, **FAST))
    service = AttributionService(db, AttributionConfig())
    service.register_correction_hook(indexer.corrected)
    await indexer.start()
    try:
        await wait_until_indexed(indexer, meeting.meeting_id)
        with db.transaction() as tx:
            more = [insert_utterance(tx, meeting, asha, f"more {i} " * 6, 30 + i) for i in range(10)]
        for row in more:
            indexer.enqueue(row)
        await asyncio.sleep(.015)
        await asyncio.gather(indexer.flush(meeting.meeting_id),
                             service.correct(meeting.meeting_id, rows[3].utterance_id, {"display_name": "Chitra"}))
        await service.drain()
        await wait_until_indexed(indexer, meeting.meeting_id)
        chunks = transcript_chunks.list_for_meeting(db.conn, meeting.meeting_id)
        assert all(chunk.status == "ready" for chunk in chunks)
        assert len({chunk.chunk_index for chunk in chunks}) == len(chunks)
        assert any("[Chitra," in chunk.text for chunk in chunks)
        assert_disjoint_and_complete(db, meeting.meeting_id)
    finally:
        await indexer.stop()


@pytest.mark.asyncio
async def test_correction_of_a_split_utterance_relabels_every_piece_within_the_cap(db):
    meeting, asha, _ = two_people(db)
    with db.transaction() as tx:
        long = insert_utterance(tx, meeting, asha, " ".join(f"Sentence number {i} is here." for i in range(15)), 1)
    adapter = FakeEmbeddingAdapter(384)
    indexer = TranscriptIndexer(db, adapter, RagConfig(target_tokens=20, hard_max_tokens=30, **FAST))
    service = AttributionService(db, AttributionConfig())
    service.register_correction_hook(indexer.corrected)
    await indexer.start()
    try:
        await indexer.flush(meeting.meeting_id)
        before = transcript_chunks.list_for_meeting(db.conn, meeting.meeting_id)
        assert len(before) >= 3
        name = "Bharat Kumar Rao Venkata Subramanian Iyer"
        await service.correct(meeting.meeting_id, long.utterance_id, {"display_name": name})
        await service.drain()
        after = transcript_chunks.list_for_meeting(db.conn, meeting.meeting_id)
        assert all(chunk.text.startswith(f"[{name}, ") for chunk in after)
        assert all(adapter.count_tokens(chunk.text) <= 30 for chunk in after)
        assert [chunk.chunk_id for chunk in after][:len(before)] == [chunk.chunk_id for chunk in before][:len(after)]
        assert " ".join(chunk.text.split("] ", 1)[1] for chunk in after) == long.text
        assert len(after) > len(before)  # the longer label needed more pieces
        # Back to a short name: fewer pieces, so the surplus chunks and their vectors are retired.
        await service.correct(meeting.meeting_id, long.utterance_id, {"display_name": "Asha"})
        await service.drain()
        final = transcript_chunks.list_for_meeting(db.conn, meeting.meeting_id)
        assert [chunk.chunk_id for chunk in final] == [chunk.chunk_id for chunk in before]
        vectors = db.conn.execute("SELECT count(*) FROM TranscriptChunkVector").fetchone()[0]
        assert vectors == len(final) and all(chunk.text.startswith("[Asha, ") for chunk in final)
    finally:
        await indexer.stop()


@pytest.mark.asyncio
async def test_failures_back_off_and_one_execution_row_is_written_per_batch(db):
    from server.repositories import model_executions

    class Broken(FakeEmbeddingAdapter):
        def __init__(self):
            super().__init__(384)
            self.calls = 0

        def embed(self, texts):
            self.calls += 1
            raise RuntimeError("model gone")
    meeting, asha, ben = two_people(db)
    with db.transaction() as tx:
        insert_utterance(tx, meeting, asha, "alpha alpha alpha alpha alpha.", 1)
        insert_utterance(tx, meeting, ben, "beta beta beta beta beta.", 2)
    adapter = Broken()
    indexer = TranscriptIndexer(db, adapter, RagConfig(target_tokens=5, hard_max_tokens=40, settle_delay_s=.001,
                                                       retry_delay_s=.02, retry_max_delay_s=.08))
    await indexer.start()
    try:
        await asyncio.sleep(.4)
        # Without backoff a .02 s retry would run ~20 times; doubling to a .08 s cap allows about half that.
        assert 3 <= adapter.calls <= 9
        status = await indexer.index_status(meeting.meeting_id)
        assert status["failed"] == 2 and status["pending_utterances"] == 2
        indexer.adapter = FakeEmbeddingAdapter(384)
        await wait_until_indexed(indexer, meeting.meeting_id)
        executions = model_executions.list_executions(db.conn, resource_type="embedding")
        assert len(executions) == 1  # both chunks were embedded in one batch
    finally:
        await indexer.stop()


@pytest.mark.asyncio
async def test_an_unchanged_tail_is_not_re_embedded(db):
    class Counting(FakeEmbeddingAdapter):
        calls = 0

        def embed(self, texts):
            Counting.calls += 1
            return super().embed(texts)
    meeting, asha, _ = two_people(db)
    with db.transaction() as tx:
        insert_utterance(tx, meeting, asha, "Only line.", 1)
    indexer = TranscriptIndexer(db, Counting(384), RagConfig(target_tokens=20, hard_max_tokens=40, **FAST))
    await indexer.start()
    try:
        await wait_until_indexed(indexer, meeting.meeting_id)
        calls = Counting.calls
        indexer.enqueue_meeting(meeting.meeting_id)
        await asyncio.sleep(.05)
        assert Counting.calls == calls
    finally:
        await indexer.stop()


@pytest.mark.asyncio
async def test_restart_recovers_an_ended_meeting_with_an_uncovered_trailing_line(db):
    meeting, asha, _ = two_people(db)
    with db.transaction() as tx:
        insert_utterance(tx, meeting, asha, "Before the crash.", 1)
    config = RagConfig(target_tokens=20, hard_max_tokens=40, **FAST)
    first = TranscriptIndexer(db, FakeEmbeddingAdapter(384), config)
    await first.start()
    await first.flush(meeting.meeting_id)
    await first.stop()
    with db.transaction() as tx:
        insert_utterance(tx, meeting, asha, "Written, then the server died.", 4)
        registry.end_meeting(tx, meeting.meeting_id)
    second = TranscriptIndexer(db, FakeEmbeddingAdapter(384), config)
    await second.start()
    try:
        await wait_until_indexed(second, meeting.meeting_id)
        assert all(chunk.is_closed for chunk in transcript_chunks.list_for_meeting(db.conn, meeting.meeting_id))
    finally:
        await second.stop()
