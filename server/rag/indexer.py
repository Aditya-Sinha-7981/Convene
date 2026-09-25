"""Asynchronous CON-08 transcript indexer; it never runs retrieval or generation."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from datetime import datetime, timezone

from ..audit import emit
from ..attribution.views import utterance_view
from ..ids import new_id
from ..repositories import meetings, model_executions, transcript_chunks, utterances
from ..repositories.model_executions import ModelExecution
from ..repositories.models import TranscriptChunk
from ..timeutil import utc_now
from .chunker import ChunkInput, chunk_utterances, render_line
from .vector_store import VectorStore

log = logging.getLogger("convene.rag.indexer")


class TranscriptIndexer:
    def __init__(self, db, adapter, rag_config, *, priority=None):
        self.db, self.adapter, self.config, self.priority = db, adapter, rag_config, priority
        self.store = VectorStore(adapter.dimension, adapter.model_identifier)
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._queued: set[str] = set()
        self._new_utterance_ids: dict[str, set[str]] = {}
        self._task: asyncio.Task | None = None
        self._retry_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        began = time.monotonic()
        await asyncio.get_running_loop().run_in_executor(None, self.adapter.load)
        with self.db.transaction() as tx:
            self.store.guard_model(tx.conn)
        duration_ms = round(getattr(self.adapter, "load_seconds", time.monotonic() - began) * 1000)
        await self.db.run(lambda tx: emit(tx, "model_load", "models", {
            "resource_type": "embedding", "model_identifier": self.adapter.model_identifier,
            "runtime": self.adapter.runtime, "duration_ms": duration_ms}))
        self._task = asyncio.create_task(self._run(), name="rag-indexer")
        for meeting_id in await self.db.run(self._recoverable_meetings):
            self.enqueue_meeting(meeting_id)

    @staticmethod
    def _recoverable_meetings(tx) -> list[str]:
        """Recover rows written before a crash without revisiting already-covered healthy meetings."""
        pending = []
        for meeting in meetings.list_meetings(tx.conn):
            rows = utterances.list_for_meeting(tx.conn, meeting.meeting_id)
            if not rows:
                continue
            chunks = transcript_chunks.list_for_meeting(tx.conn, meeting.meeting_id)
            if not chunks or any(chunk.status != "ready" for chunk in chunks):
                pending.append(meeting.meeting_id)
                continue
            if chunks[-1].utterance_id_end != rows[-1].utterance_id:
                pending.append(meeting.meeting_id)
        return pending

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        for task in self._retry_tasks:
            task.cancel()
        await asyncio.gather(*([self._task] if self._task else []), *self._retry_tasks, return_exceptions=True)

    def enqueue(self, utterance) -> None:
        self._new_utterance_ids.setdefault(utterance.meeting_id, set()).add(utterance.utterance_id)
        self.enqueue_meeting(utterance.meeting_id)

    def enqueue_meeting(self, meeting_id: str) -> None:
        if meeting_id not in self._queued:
            self._queued.add(meeting_id)
            self._queue.put_nowait(meeting_id)

    async def corrected(self, _utterance_id: str, meeting_id: str) -> None:
        # The source labels have committed already. Rebuild only the stable chunk range that covers this line.
        await self._rebuild_corrected(_utterance_id, meeting_id)

    async def _rebuild_corrected(self, utterance_id: str, meeting_id: str) -> None:
        meeting, rows, inputs = await self.db.run(lambda tx: self._inputs(tx.conn, meeting_id))
        ids = [row.utterance_id for row in rows]
        if utterance_id not in ids:
            return
        target = ids.index(utterance_id)
        chunks = await self.db.run(lambda tx: transcript_chunks.list_for_meeting(tx.conn, meeting_id))
        for chunk in chunks:
            start, end = ids.index(chunk.utterance_id_start), ids.index(chunk.utterance_id_end)
            if start <= target <= end:
                await self._rebuild_range(chunk, inputs[start:end + 1], meeting.started_at or rows[0].t_start)
                return

    async def _rebuild_range(self, chunk, inputs, meeting_started_at: str) -> None:
        text = "\n".join(render_line(item, meeting_started_at) for item in inputs)
        await self._write_chunks([replace(chunk, text=text, status="pending", error_message=None)])

    async def flush(self, meeting_id: str) -> None:
        await self._index(meeting_id, closing=True, new_utterance_ids=set())

    async def _run(self) -> None:
        while True:
            meeting_id = await self._queue.get()
            self._queued.discard(meeting_id)
            new_ids = self._new_utterance_ids.pop(meeting_id, set())
            # A source line is allowed to settle across devices, then the quiet-flush bound makes its tail searchable.
            await asyncio.sleep(max(self.config.settle_delay_s, self.config.quiet_flush_s))
            try:
                await self._index(meeting_id, closing=False, new_utterance_ids=new_ids)
            except Exception:
                log.exception("indexing failed for meeting %s", meeting_id)

    def _inputs(self, conn, meeting_id: str):
        meeting = meetings.require(conn, meeting_id)
        rows = utterances.list_for_meeting(conn, meeting_id)
        if not rows:
            return meeting, rows, []
        started = meeting.started_at or rows[0].t_start
        values = [utterance_view(conn, row, 0.8) for row in rows]
        return meeting, rows, [ChunkInput(row["utterance_id"], row["speaker_label"], row["t_start"], row["text"])
                               for row in values]

    async def _index(self, meeting_id: str, *, closing: bool, new_utterance_ids: set[str]) -> None:
        meeting, rows, inputs = await self.db.run(lambda tx: self._inputs(tx.conn, meeting_id))
        if not rows:
            return
        existing = await self.db.run(lambda tx: transcript_chunks.list_for_meeting(tx.conn, meeting_id))
        if not existing:
            specs = chunk_utterances(inputs, meeting.started_at or rows[0].t_start,
                                     target_tokens=self.config.target_tokens, hard_max_tokens=self.config.hard_max_tokens,
                                     count_tokens=self.adapter.count_tokens)
            chunks = [TranscriptChunk(new_id(), meeting_id, spec.utterance_id_start, spec.utterance_id_end, spec.text,
                                      spec.chunk_index, "pending", utc_now(), None, closing or index < len(specs) - 1)
                      for index, spec in enumerate(specs)]
            await self._write_chunks(chunks)
            return
        # Failed embeddings are retried from their durable source text. No transcript write or live STT work waits.
        failed = [replace(chunk, status="pending", error_message=None) for chunk in existing if chunk.status == "failed"]
        if failed:
            await self._write_chunks(failed)
            existing = await self.db.run(lambda tx: transcript_chunks.list_for_meeting(tx.conn, meeting_id))
        ids = [row.utterance_id for row in rows]
        for utterance_id in new_utterance_ids:
            if utterance_id not in ids:
                continue
            target = ids.index(utterance_id)
            for chunk in existing:
                start, end = ids.index(chunk.utterance_id_start), ids.index(chunk.utterance_id_end)
                if chunk.is_closed and start <= target <= end:
                    await self._rebuild_range(chunk, inputs[start:end + 1], meeting.started_at or rows[0].t_start)
                    break
        # Only the open tail grows on normal arrival. Closed citation ranges retain both identity and index.
        open_chunks = [chunk for chunk in existing if not chunk.is_closed]
        if not open_chunks:
            return
        tail = open_chunks[-1]
        start = ids.index(tail.utterance_id_start)
        tail_inputs = inputs[start:]
        specs = chunk_utterances(tail_inputs, meeting.started_at or rows[0].t_start,
                                 target_tokens=self.config.target_tokens, hard_max_tokens=self.config.hard_max_tokens,
                                 count_tokens=self.adapter.count_tokens)
        rebuilt = [replace(tail, utterance_id_start=specs[0].utterance_id_start, utterance_id_end=specs[0].utterance_id_end,
                           text=specs[0].text, status="pending", error_message=None,
                           is_closed=closing or len(specs) > 1)]
        next_index = tail.chunk_index + 1
        rebuilt.extend(TranscriptChunk(new_id(), meeting_id, spec.utterance_id_start, spec.utterance_id_end, spec.text,
                                       next_index + index, "pending", utc_now(), None,
                                       closing or index < len(specs[1:]) - 1)
                       for index, spec in enumerate(specs[1:]))
        await self._write_chunks(rebuilt)

    async def _write_chunks(self, chunks: list[TranscriptChunk]) -> None:
        if not chunks:
            return
        if self.priority is not None:
            await self.priority.wait_for_turn("embedding")
        began = time.monotonic()
        try:
            vectors = await asyncio.get_running_loop().run_in_executor(None, self.adapter.embed, [chunk.text for chunk in chunks])
            await self.db.run(lambda tx: self._persist_ready(tx, chunks, vectors, round((time.monotonic() - began) * 1000)))
        except Exception as exc:
            await self.db.run(lambda tx: self._persist_failed(tx, chunks, exc))
            for meeting_id in {chunk.meeting_id for chunk in chunks}:
                self._schedule_retry(meeting_id)

    def _schedule_retry(self, meeting_id: str) -> None:
        async def retry():
            await asyncio.sleep(self.config.retry_delay_s)
            self.enqueue_meeting(meeting_id)
        task = asyncio.create_task(retry(), name="rag-index-retry")
        self._retry_tasks.add(task)
        task.add_done_callback(self._retry_tasks.discard)

    def _persist_ready(self, tx, chunks, vectors, duration_ms: int) -> None:
        for chunk, vector in zip(chunks, vectors, strict=True):
            if transcript_chunks.get(tx.conn, chunk.chunk_id) is None:
                transcript_chunks.insert(tx.conn, chunk)
            else:
                transcript_chunks.update(tx.conn, chunk.chunk_id, text=chunk.text, status="pending", error_message=None,
                                         utterance_id_start=chunk.utterance_id_start, utterance_id_end=chunk.utterance_id_end,
                                         is_closed=chunk.is_closed)
            self.store.upsert(tx.conn, chunk.chunk_id, chunk.meeting_id, vector)
            model_executions.insert(tx.conn, ModelExecution(new_id(), "embedding", self.adapter.model_identifier,
                                    self.adapter.runtime, duration_ms, chunk.chunk_id, utc_now()))

    def _persist_failed(self, tx, chunks, exc: Exception) -> None:
        message = f"{type(exc).__name__}: {exc}"[:500]
        for chunk in chunks:
            if transcript_chunks.get(tx.conn, chunk.chunk_id) is None:
                transcript_chunks.insert(tx.conn, chunk)
            transcript_chunks.update(tx.conn, chunk.chunk_id, status="failed", error_message=message)
            emit(tx, "index_failed", "rag", {"utterance_id_start": chunk.utterance_id_start,
                 "utterance_id_end": chunk.utterance_id_end, "error": message}, meeting_id=chunk.meeting_id)
            emit(tx, "model_error", "models", {"resource_type": "embedding", "model_identifier": self.adapter.model_identifier,
                 "device_id": None, "window_id": None, "related_id": chunk.chunk_id, "error": message}, meeting_id=chunk.meeting_id)

    async def index_status(self, meeting_id: str) -> dict:
        def read(tx):
            source = utterances.list_for_meeting(tx.conn, meeting_id)
            rows = transcript_chunks.list_for_meeting(tx.conn, meeting_id)
            counts = {status: sum(row.status == status for row in rows) for status in ("ready", "pending", "failed")}
            ids = [row.utterance_id for row in source]
            covered = set()
            for chunk in rows:
                if chunk.status != "ready":
                    continue
                start, end = ids.index(chunk.utterance_id_start), ids.index(chunk.utterance_id_end)
                covered.update(ids[start:end + 1])
            outstanding = [row for row in source if row.utterance_id not in covered]
            if outstanding:
                now = datetime.now(timezone.utc)
                lag_s = round(max((now - datetime.fromisoformat(row.created_at.replace("Z", "+00:00"))).total_seconds()
                                  for row in outstanding), 3)
            else:
                lag_s = 0.0
            return {"meeting_id": meeting_id, **counts, "indexed": len(covered), "pending_utterances": len(outstanding),
                    "lag_s": lag_s}
        return await self.db.run(read)
