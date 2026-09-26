"""Safe, idempotent DOCX generation from stored rows (CON-11)."""
from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from ..audit import emit
from ..attribution.labels import is_low_confidence, speaker_label
from ..attribution.staleness import artifact_is_stale
from ..errors import MeetingNotFoundError
from ..ids import new_id
from ..repositories import audit_events, devices, exports, meetings, participants, summaries, utterances
from ..timeutil import utc_now
from .docx_renderer import render

FAILED = "export_render_failed"
MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class ExportRenderError(Exception):
    pass


class ExportService:
    def __init__(self, db, exports_dir: Path, *, low_confidence_threshold: float):
        self.db, self.exports_dir, self.threshold = db, Path(exports_dir), low_confidence_threshold
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, meeting_id: str) -> asyncio.Lock:
        return self._locks.setdefault(meeting_id, asyncio.Lock())

    async def ensure(self, meeting_id: str):
        """Return a fresh ready export, rendering one when missing or stale."""
        async with self._lock(meeting_id):
            state = await self.db.run(lambda tx: self._state(tx.conn, meeting_id))
            if state["current"] is not None and not state["stale"] and self._path(state["current"]).is_file():
                return state["current"]
            return await self._render(meeting_id)

    async def on_summary_ready(self, meeting_id: str) -> None:
        """Best-effort automatic render after a successful summary; failure is persisted, never propagated."""
        try:
            await self.ensure(meeting_id)
        except Exception:
            pass

    def _path(self, row) -> Path:
        if row.storage_path is None:
            return self.exports_dir / "missing.docx"
        return self.exports_dir / Path(row.storage_path).name

    def _state(self, conn, meeting_id: str):
        meetings.require(conn, meeting_id)
        summary = summaries.current(conn, meeting_id)
        current = exports.current(conn, meeting_id)
        if summary is None:
            return {"summary": None, "current": current, "stale": False}
        stale = current is None
        if current is not None:
            export_seq = audit_events.export_created_seq(conn, current.export_id)
            summary_seq = audit_events.summary_generated_seq(conn, summary.summary_id)
            stale = (export_seq is None or artifact_is_stale(conn, meeting_id, export_seq) or
                     summary_seq is None or export_seq < summary_seq)
        return {"summary": summary, "current": current, "stale": stale}

    async def _render(self, meeting_id: str):
        export_id = new_id()
        created_at = utc_now()

        def snapshot(tx):
            state = self._state(tx.conn, meeting_id)
            if state["summary"] is None:
                return None
            pending = exports.Export(export_id, meeting_id, "docx", "pending", None, None, created_at)
            exports.insert_pending(tx.conn, pending)
            meeting = meetings.require(tx.conn, meeting_id)
            people = participants.list_for_meeting(tx.conn, meeting_id)
            ordinals = {device.device_id: n for n, device in enumerate(devices.list_for_meeting(tx.conn, meeting_id), 1)}
            people_by_id = {person.participant_id: person for person in people}
            lines = []
            for line in utterances.list_for_meeting(tx.conn, meeting_id):
                lines.append({"t_start": line.t_start, "text": line.text,
                              "speaker_label": speaker_label(line, people_by_id.get(line.participant_id), ordinals[line.device_id]),
                              "low_confidence": is_low_confidence(line, self.threshold)})
            items = []
            for item in summaries.action_items(tx.conn, state["summary"].summary_id):
                value = asdict(item)
                value["owner_display_name"] = people_by_id.get(item.owner_participant_id).display_name if item.owner_participant_id in people_by_id else None
                items.append(value)
            return meeting, people, state["summary"], items, lines

        data = await self.db.run(snapshot)
        if data is None:
            raise ExportRenderError("no ready summary exists for this meeting")
        target = self.exports_dir / f"{meeting_id}.docx"
        temporary = None
        try:
            self.exports_dir.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=f".{meeting_id}.", suffix=".tmp", dir=self.exports_dir)
            with os.fdopen(fd, "wb") as stream:
                render(*data).save(stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            temporary = None
            def success(tx):
                row = exports.finish(tx.conn, export_id, status="ready", storage_path=f"{meeting_id}.docx")
                input_as_of = audit_events.max_seq(tx.conn)
                emit(tx, "export_created", "export", {"export_id": export_id, "summary_id": data[2].summary_id,
                     "input_as_of_seq": input_as_of, "type": "docx"}, meeting_id=meeting_id)
                return row
            return await self.db.run(success)
        except Exception as exc:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
            message = f"{type(exc).__name__}: {exc}"[:300]
            def failure(tx):
                exports.finish(tx.conn, export_id, status="failed", error_message=message)
                emit(tx, "export_failed", "export", {"export_id": export_id, "error_code": FAILED}, meeting_id=meeting_id)
            await self.db.run(failure)
            raise ExportRenderError("the DOCX could not be rendered") from exc

    async def payload(self, meeting_id: str) -> dict:
        def read(tx):
            state = self._state(tx.conn, meeting_id)
            latest = exports.latest_attempt(tx.conn, meeting_id)
            return {"export": asdict(state["current"]) if state["current"] else None,
                    "latest_attempt": asdict(latest) if latest else None, "stale": state["stale"],
                    "as_of_seq": audit_events.max_seq(tx.conn)}
        return await self.db.run(read)
