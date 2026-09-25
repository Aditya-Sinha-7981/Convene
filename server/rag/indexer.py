"""Asynchronous CON-08 transcript indexer; it never runs retrieval or generation.

Every trigger (a new utterance, a correction, meeting end, a retry, startup recovery) runs the same
reconciliation for one meeting under one lock: compare the stored chunks with the current transcript and
re-embed only the chunks whose text, range, or status is out of date. Closed chunks keep ``chunk_id`` and
``chunk_index`` when rebuilt; only the open tail is re-chunked on normal arrival.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from ..audit import emit
from ..attribution.labels import speaker_label
from ..ids import new_id
from ..repositories import devices, meetings, model_executions, participants, transcript_chunks, utterances
from ..repositories.model_executions import ModelExecution
from ..repositories.models import TranscriptChunk
from ..timeutil import utc_now
from .chunker import ChunkInput, ChunkSpec, chunk_utterances, render_line
from .vector_store import VectorStore

log = logging.getLogger("convene.rag.indexer")


@dataclass
class _State:
    meeting_id: str
    meeting_ended: bool
    started_at: str
    ids: list[str]
    inputs: list[ChunkInput]
    chunks: list[TranscriptChunk]


@dataclass
class _Plan:
    embed: list[TranscriptChunk] = field(default_factory=list)    # new or changed text: needs a vector
    update: list[TranscriptChunk] = field(default_factory=list)   # closure flag only; the vector is current
    retire: list[TranscriptChunk] = field(default_factory=list)   # a rebuild needed fewer split pieces


def _positions(ids: list[str]) -> dict[str, int]:
    return {utterance_id: index for index, utterance_id in enumerate(ids)}


class TranscriptIndexer:
    def __init__(self, db, adapter, rag_config, *, priority=None):
        self.db, self.adapter, self.config, self.priority = db, adapter, rag_config, priority
        self.store = VectorStore(adapter.dimension, adapter.model_identifier)
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._queued: set[str] = set()
        self._task: asyncio.Task | None = None
        self._retry_tasks: set[asyncio.Task] = set()
        self._failures: dict[str, int] = {}
        # One lock for every write path: chunk_index allocation and the tokenizer are not safe to share.
        self._lock = asyncio.Lock()

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
        """Meetings with an uncovered utterance, an unembedded chunk, or an ended meeting's open tail."""
        pending = []
        for meeting in meetings.list_meetings(tx.conn):
            ids = [row.utterance_id for row in utterances.list_for_meeting(tx.conn, meeting.meeting_id)]
            if not ids:
                continue
            chunks = transcript_chunks.list_for_meeting(tx.conn, meeting.meeting_id)
            covered = TranscriptIndexer._covered(ids, chunks, ready_only=False)
            if (len(covered) < len(ids) or any(chunk.status != "ready" for chunk in chunks)
                    or (meeting.status == "ended" and any(not chunk.is_closed for chunk in chunks))):
                pending.append(meeting.meeting_id)
        return pending

    @staticmethod
    def _covered(ids: list[str], chunks, *, ready_only: bool) -> set[str]:
        position = _positions(ids)
        covered: set[str] = set()
        for chunk in chunks:
            if ready_only and chunk.status != "ready":
                continue
            if chunk.utterance_id_start in position and chunk.utterance_id_end in position:
                covered.update(ids[position[chunk.utterance_id_start]:position[chunk.utterance_id_end] + 1])
        return covered

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        for task in self._retry_tasks:
            task.cancel()
        await asyncio.gather(*([self._task] if self._task else []), *self._retry_tasks, return_exceptions=True)

    def enqueue(self, utterance) -> None:
        """Attribution post-write hook: never blocks the writer."""
        self.enqueue_meeting(utterance.meeting_id)

    def enqueue_meeting(self, meeting_id: str) -> None:
        if meeting_id not in self._queued:
            self._queued.add(meeting_id)
            self._queue.put_nowait(meeting_id)

    async def corrected(self, _utterance_id: str, meeting_id: str) -> None:
        """Attribution correction hook. The labels have committed; reconciliation rebuilds the covering chunk."""
        await self._index(meeting_id)

    async def meeting_ended(self, meeting_id: str) -> None:
        """Meeting-ended hook: returns at once. The worker closes the tail after the settle window, and any STT
        result still in flight for the ended meeting is indexed (and closed) when it is written."""
        self.enqueue_meeting(meeting_id)

    async def flush(self, meeting_id: str) -> None:
        """Index and close everything for a meeting now (tests and scripts; the runtime uses ``meeting_ended``)."""
        await self._index(meeting_id, closing=True)

    async def _run(self) -> None:
        while True:
            meeting_id = await self._queue.get()
            self._queued.discard(meeting_id)
            # Cross-device STT results settle before the tail is re-chunked and embedded.
            await asyncio.sleep(self.config.settle_delay_s)
            try:
                await self._index(meeting_id)
            except Exception:
                log.exception("indexing failed for meeting %s", meeting_id)

    # -- reconciliation ---------------------------------------------------------------------

    def _load(self, conn, meeting_id: str) -> _State:
        meeting = meetings.require(conn, meeting_id)
        rows = utterances.list_for_meeting(conn, meeting_id)
        people = {person.participant_id: person for person in participants.list_for_meeting(conn, meeting_id)}
        ordinals = {device.device_id: number for number, device in enumerate(devices.list_for_meeting(conn, meeting_id), 1)}
        inputs = [ChunkInput(row.utterance_id,
                             speaker_label(row, people.get(row.participant_id), ordinals.get(row.device_id, 0)),
                             row.t_start, row.text) for row in rows]
        return _State(meeting_id, meeting.status == "ended", meeting.started_at or (rows[0].t_start if rows else ""),
                      [row.utterance_id for row in rows], inputs, transcript_chunks.list_for_meeting(conn, meeting_id))

    def _spec(self, inputs, started_at: str, *, rebuild: bool) -> list[ChunkSpec]:
        # A rebuilt closed range keeps as few chunks as the hard cap allows; the tail follows the target size.
        target = self.config.hard_max_tokens if rebuild else self.config.target_tokens
        return chunk_utterances(inputs, started_at, target_tokens=target, hard_max_tokens=self.config.hard_max_tokens,
                                count_tokens=self.adapter.count_tokens)

    def _plan(self, state: _State, closing: bool) -> _Plan:
        """Pure planning over one meeting's current transcript and chunks. Runs off the event loop."""
        plan = _Plan()
        closing = closing or state.meeting_ended
        position, count = _positions(state.ids), len(state.ids)
        next_index = max((chunk.chunk_index for chunk in state.chunks), default=-1) + 1

        # Group chunks by utterance range. A group of several chunks is one sentence-split utterance.
        groups: dict[tuple[int, int], list[TranscriptChunk]] = {}
        for chunk in state.chunks:
            if chunk.utterance_id_start not in position or chunk.utterance_id_end not in position:
                # Utterances are never deleted (foreign keys), so this means a damaged row: leave it alone.
                log.warning("chunk %s references a missing utterance; skipped", chunk.chunk_id)
                continue
            groups.setdefault((position[chunk.utterance_id_start], position[chunk.utterance_id_end]), []).append(chunk)
        for members in groups.values():
            members.sort(key=lambda chunk: chunk.chunk_index)
        owner = [None] * count
        for key in groups:
            for index in range(key[0], key[1] + 1):
                owner[index] = key
        open_keys = sorted((key for key, members in groups.items() if not members[-1].is_closed),
                           key=lambda key: groups[key][-1].chunk_index)
        tail_key = open_keys[-1] if open_keys else None
        tail = groups.pop(tail_key)[-1] if tail_key else None
        tail_start = tail_key[0] if tail_key else count

        def closed_group(key) -> bool:
            return key is not None and key != tail_key and len(groups.get(key, ())) == 1

        # Uncovered utterances before the tail arrived late, between two chunks: join a neighbouring closed
        # chunk (which is then rebuilt in place) or, when both neighbours are split pieces, form their own chunk.
        ranges = {key: list(key) for key in groups}
        new_runs: list[tuple[int, int]] = []
        index = 0
        while index < tail_start:
            if owner[index] is not None:
                index += 1
                continue
            run_start = index
            while index < tail_start and owner[index] is None:
                index += 1
            run_end = index - 1
            before = owner[run_start - 1] if run_start > 0 else None
            after = owner[run_end + 1] if run_end + 1 < count else None
            if closed_group(before):
                ranges[before][1] = run_end
            elif after is not None and after == tail_key:
                tail_start = run_start
            elif closed_group(after):
                ranges[after][0] = run_start
            elif after is None and tail is None:
                tail_start = run_start  # trailing utterances after every chunk closed: a new tail
                break
            else:
                new_runs.append((run_start, run_end))

        # Closed ranges: re-render cheaply and re-split only when the text changed.
        for key, members in groups.items():
            start, end = ranges[key]
            rendered = "\n".join(render_line(item, state.started_at) for item in state.inputs[start:end + 1])
            unchanged = (len(members) == 1 and rendered == members[0].text and (start, end) == key)
            if unchanged:
                if members[0].status != "ready" or not members[0].is_closed:
                    target = replace(members[0], status="pending", error_message=None, is_closed=True)
                    (plan.embed if members[0].status != "ready" else plan.update).append(target)
                continue
            specs = self._spec(state.inputs[start:end + 1], state.started_at, rebuild=True)
            texts_match = [spec.text for spec in specs] == [member.text for member in members]
            for number, spec in enumerate(specs):
                if number < len(members):
                    member = members[number]
                    target = replace(member, utterance_id_start=spec.utterance_id_start,
                                     utterance_id_end=spec.utterance_id_end, text=spec.text, status="pending",
                                     error_message=None, is_closed=True)
                    if texts_match and member.status == "ready" and (start, end) == key:
                        if not member.is_closed:
                            plan.update.append(target)
                        continue
                    plan.embed.append(target)
                else:
                    plan.embed.append(self._new_chunk(state, spec, next_index, True))
                    next_index += 1
            plan.retire.extend(members[len(specs):])

        for run_start, run_end in new_runs:
            for spec in self._spec(state.inputs[run_start:run_end + 1], state.started_at, rebuild=True):
                plan.embed.append(self._new_chunk(state, spec, next_index, True))
                next_index += 1

        # The open tail (or the utterances after the last closed chunk) grows and splits by target size.
        if tail_start < count:
            specs = self._spec(state.inputs[tail_start:], state.started_at, rebuild=False)
            for number, spec in enumerate(specs):
                is_closed = closing or number < len(specs) - 1 or spec.split
                if number == 0 and tail is not None:
                    target = replace(tail, utterance_id_start=spec.utterance_id_start,
                                     utterance_id_end=spec.utterance_id_end, text=spec.text, status="pending",
                                     error_message=None, is_closed=is_closed)
                    if (spec.text == tail.text and tail.status == "ready"
                            and spec.utterance_id_end == tail.utterance_id_end
                            and spec.utterance_id_start == tail.utterance_id_start):
                        if is_closed != tail.is_closed:
                            plan.update.append(target)
                    else:
                        plan.embed.append(target)
                else:
                    plan.embed.append(self._new_chunk(state, spec, next_index, is_closed))
                    next_index += 1
        return plan

    @staticmethod
    def _new_chunk(state: _State, spec: ChunkSpec, chunk_index: int, is_closed: bool) -> TranscriptChunk:
        return TranscriptChunk(new_id(), state.meeting_id, spec.utterance_id_start, spec.utterance_id_end, spec.text,
                               chunk_index, "pending", utc_now(), None, is_closed)

    async def _index(self, meeting_id: str, *, closing: bool = False) -> None:
        async with self._lock:
            state = await self.db.run(lambda tx: self._load(tx.conn, meeting_id))
            if not state.ids:
                return
            plan = await asyncio.get_running_loop().run_in_executor(None, self._plan, state, closing)
            if plan.update or plan.retire:
                await self.db.run(lambda tx: self._persist_metadata(tx, plan))
            if plan.embed:
                await self._write_chunks(meeting_id, plan.embed)
            elif not any(chunk.status == "failed" for chunk in state.chunks):
                self._failures.pop(meeting_id, None)

    def _persist_metadata(self, tx, plan: _Plan) -> None:
        for chunk in plan.retire:
            self.store.delete(tx.conn, chunk.chunk_id)
        for chunk in plan.update:
            transcript_chunks.update(tx.conn, chunk.chunk_id, is_closed=chunk.is_closed)

    async def _write_chunks(self, meeting_id: str, chunks: list[TranscriptChunk]) -> None:
        if self.priority is not None:
            await self.priority.wait_for_turn("embedding")
        began = time.monotonic()
        try:
            vectors = await asyncio.get_running_loop().run_in_executor(None, self.adapter.embed, [chunk.text for chunk in chunks])
            await self.db.run(lambda tx: self._persist_ready(tx, chunks, vectors, round((time.monotonic() - began) * 1000)))
        except Exception as exc:
            await self.db.run(lambda tx: self._persist_failed(tx, chunks, exc))
            self._schedule_retry(meeting_id)
        else:
            self._failures.pop(meeting_id, None)

    def _schedule_retry(self, meeting_id: str) -> None:
        failures = self._failures[meeting_id] = self._failures.get(meeting_id, 0) + 1
        delay = min(self.config.retry_delay_s * 2 ** (failures - 1), self.config.retry_max_delay_s)

        async def retry():
            await asyncio.sleep(delay)
            self.enqueue_meeting(meeting_id)
        task = asyncio.create_task(retry(), name="rag-index-retry")
        self._retry_tasks.add(task)
        task.add_done_callback(self._retry_tasks.discard)

    @staticmethod
    def _save(tx, chunk: TranscriptChunk, **changes) -> None:
        if transcript_chunks.get(tx.conn, chunk.chunk_id) is None:
            transcript_chunks.insert(tx.conn, replace(chunk, **changes))
        else:
            transcript_chunks.update(tx.conn, chunk.chunk_id, text=chunk.text, utterance_id_start=chunk.utterance_id_start,
                                     utterance_id_end=chunk.utterance_id_end, is_closed=chunk.is_closed, **changes)

    def _persist_ready(self, tx, chunks, vectors, duration_ms: int) -> None:
        for chunk, vector in zip(chunks, vectors, strict=True):
            self._save(tx, chunk, status="pending", error_message=None)
            self.store.upsert(tx.conn, chunk.chunk_id, chunk.meeting_id, vector)
        # One execution row per embedding batch; related_id is the batch's first chunk (docs/data-model.md).
        model_executions.insert(tx.conn, ModelExecution(new_id(), "embedding", self.adapter.model_identifier,
                                self.adapter.runtime, duration_ms, chunks[0].chunk_id, utc_now()))

    def _persist_failed(self, tx, chunks, exc: Exception) -> None:
        message = f"{type(exc).__name__}: {exc}"[:500]
        for chunk in chunks:
            # The stored text and range are the current ones, so a retry re-embeds exactly what failed.
            self._save(tx, chunk, status="failed", error_message=message)
            emit(tx, "index_failed", "rag", {"utterance_id_start": chunk.utterance_id_start,
                 "utterance_id_end": chunk.utterance_id_end, "error": message}, meeting_id=chunk.meeting_id)
        emit(tx, "model_error", "models", {"resource_type": "embedding", "model_identifier": self.adapter.model_identifier,
             "device_id": None, "window_id": None, "related_id": chunks[0].chunk_id, "error": message},
             meeting_id=chunks[0].meeting_id)

    async def index_status(self, meeting_id: str) -> dict:
        def read(tx):
            source = utterances.list_for_meeting(tx.conn, meeting_id)
            rows = transcript_chunks.list_for_meeting(tx.conn, meeting_id)
            counts = {status: sum(row.status == status for row in rows) for status in ("ready", "pending", "failed")}
            covered = self._covered([row.utterance_id for row in source], rows, ready_only=True)
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
