"""Dashboard fan-out: ``/ws/dashboard/{meeting_id}`` (event catalog in docs/api.md).

The hub is an ``AuditEvent`` subscriber, not a second log. After each commit that emitted events it turns
them into dashboard events for that meeting's sockets, each carrying the ``seq`` of the audit event that
caused it. Read-only: whatever a client sends is ignored. A client that cannot keep up is closed with 1013
and resynchronizes; one slow socket never delays another.

This task pushes ``meeting_status``, ``device_status``, ``connection_event`` and ``device_gauges``; the
tasks that produce the other event types add them here (CON-09: ``qa_query`` becomes ``qa_answer``; CON-10:
``summary_generated`` and ``summary_failed`` become ``summary_ready`` and ``summary_failed``).
"""
import asyncio
import logging
from dataclasses import asdict

from .audit import emit
from .attribution.views import utterance_view
from .rag.citations import query_view, resolve_citations
from .repositories import connections, devices, meetings, participants, qa_queries, summaries, utterances
from .repositories.models import AuditEvent
from .timeutil import utc_now
from .views import device_view, meeting_view

log = logging.getLogger("convene.hub")

_MEETING_EVENTS = {"meeting_started", "meeting_ended"}
_DEVICE_EVENTS = {"device_registered", "device_left"}
_CONNECTION_EVENTS = {"device_connected", "device_reconnected", "device_disconnected", "device_audio_resumed"}
_UTTERANCE_EVENTS = {"utterance_created": "utterance", "utterance_corrected": "utterance_updated"}
_SUMMARY_EVENTS = {"summary_generated", "summary_failed"}
OVERFLOW_CLOSE_CODE = 1013


class _Client:
    def __init__(self, ws, queue_size: int):
        self.ws = ws
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self.closing = False

    async def send_loop(self) -> None:
        try:
            while True:
                await self.ws.send_json(await self.queue.get())
        except Exception:
            pass  # the client went away; serve() notices on its receive side

    def overflow(self) -> None:
        if not self.closing:
            self.closing = True
            asyncio.get_running_loop().create_task(self._close())

    async def _close(self) -> None:
        try:
            await self.ws.close(code=OVERFLOW_CLOSE_CODE)
        except Exception:
            pass


class DashboardHub:
    def __init__(self, db, peers, *, low_confidence_threshold: float = 0.8,
                 gauge_interval_s: float = 1.0, client_queue: int = 256):
        self.db, self.peers = db, peers
        self.low_confidence_threshold = low_confidence_threshold
        self.gauge_interval_s, self.client_queue = gauge_interval_s, client_queue
        self._clients: dict[str, set[_Client]] = {}
        self._events: asyncio.Queue = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        self._tasks = [asyncio.create_task(self._run()), asyncio.create_task(self._gauge_loop())]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    def on_audit(self, event: AuditEvent) -> None:
        """``Database.subscribe`` callback: runs on the event loop after commit, so it only enqueues."""
        if event.meeting_id is not None and event.meeting_id in self._clients:
            self._events.put_nowait(event)

    # -- fan-out ----------------------------------------------------------------------------

    async def _run(self) -> None:
        while True:
            event = await self._events.get()
            try:
                for kind, payload in await self._translate(event):
                    self._broadcast(event.meeting_id, {"type": kind, "seq": event.seq,
                                                       "meeting_id": event.meeting_id, "at": utc_now(), **payload})
            except Exception:
                log.exception("could not forward %s to dashboards", event.event_type)
                try:
                    await self.db.run(lambda tx: emit(
                        tx, "hook_failed", "api",
                        {"hook": "dashboard.push", "error": f"could not translate {event.event_type}"},
                        meeting_id=event.meeting_id))
                except Exception:
                    log.exception("could not audit dashboard push failure")

    async def _translate(self, event: AuditEvent) -> list[tuple[str, dict]]:
        kind = event.event_type
        if kind not in (_MEETING_EVENTS | _DEVICE_EVENTS | _CONNECTION_EVENTS | set(_UTTERANCE_EVENTS) | _SUMMARY_EVENTS
                        | {"qa_query"}):
            return []

        def read(tx):
            out = []
            if kind == "qa_query":
                row = qa_queries.get(tx.conn, event.payload["query_id"])
                if row is not None:
                    view = query_view(row, event.payload["error_code"], event.payload["reason"])
                    out.append(("qa_answer", {"query": view, "citations": resolve_citations(
                        tx.conn, event.meeting_id, view["cited_chunk_ids"])}))
                return out
            if kind == "summary_generated":
                return [("summary_ready", {"summary_id": event.payload["summary_id"]})]
            if kind == "summary_failed":
                row = summaries.get(tx.conn, event.payload["summary_id"])
                return [("summary_failed", {"summary_id": event.payload["summary_id"],
                                            "error_message": row.error_message if row is not None else None})]
            if kind in _MEETING_EVENTS:
                out.append(("meeting_status", {"meeting": meeting_view(meetings.require(tx.conn, event.meeting_id))}))
            device_id = event.payload.get("device_id")
            if device_id is not None and kind != "device_audio_resumed" and kind not in _MEETING_EVENTS:
                device = devices.require(tx.conn, device_id)
                out.append(("device_status", {"device": device_view(
                    device, participants.list_for_device(tx.conn, device_id))}))
            if kind in _CONNECTION_EVENTS:
                row = connections.get(tx.conn, event.event_id)
                if row is not None:
                    out.append(("connection_event", {"event": asdict(row)}))
            if kind in _UTTERANCE_EVENTS:
                row = utterances.require(tx.conn, event.payload["utterance_id"])
                out.append((_UTTERANCE_EVENTS[kind], {"utterance": utterance_view(
                    tx.conn, row, self.low_confidence_threshold)}))
                if kind == "utterance_corrected" and event.payload["created_participant_id"] is not None:
                    device = devices.require(tx.conn, row.device_id)
                    out.append(("device_status", {"device": device_view(
                        device, participants.list_for_device(tx.conn, row.device_id))}))
            return out

        return await self.db.run(read)

    def _broadcast(self, meeting_id: str, message: dict) -> None:
        for client in list(self._clients.get(meeting_id, ())):
            try:
                client.queue.put_nowait(message)
            except asyncio.QueueFull:
                client.overflow()

    async def _gauge_loop(self) -> None:
        while True:
            await asyncio.sleep(self.gauge_interval_s)
            for meeting_id in list(self._clients):
                gauges = self.peers.gauges_for_meeting(meeting_id)
                if gauges:
                    self._broadcast(meeting_id, {"type": "device_gauges", "seq": None, "meeting_id": meeting_id,
                                                 "at": utc_now(), "gauges": gauges})

    # -- sockets ----------------------------------------------------------------------------

    async def serve(self, ws, meeting_id: str) -> None:
        """Serve one dashboard socket until it closes."""
        client = _Client(ws, self.client_queue)
        self._clients.setdefault(meeting_id, set()).add(client)
        sender = asyncio.create_task(client.send_loop())
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                # read-only feed: client frames are ignored
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            clients = self._clients.get(meeting_id)
            if clients is not None:
                clients.discard(client)
                if not clients:
                    del self._clients[meeting_id]
