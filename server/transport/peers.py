"""Peer connections, receive loops and connection-state handling for every device.

Isolation: each device has its own ``DeviceSession`` (peer, receive task, queue, pump task, counters, lock).
An exception in one device's receive loop is caught, audited, and turned into a peer failure for that device
only. The registry and the audit stream are the only things shared.

Socket versus peer lifetime (docs/transport.md): closing the signaling WebSocket does not by itself close a
connected peer; device status follows the peer connection state.
"""
import asyncio
import logging
import time
from typing import Awaitable

from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError
from aiortc.sdp import SessionDescription

from .. import registry
from ..audit import emit, record_connection_event
from ..db import Database
from ..errors import MeetingEndedError, StorageError
from ..repositories import devices
from .audio import AudioSink, frame_to_mono_int16
from .sessions import DeviceSession, TransportError

log = logging.getLogger("convene.transport")

_LOST_REASONS = {"disconnected": "peer_disconnected", "failed": "peer_failed", "closed": "peer_closed"}


class PeerManager:
    def __init__(self, db: Database, sink: AudioSink, config):
        self.db, self.sink, self.config = db, sink, config
        self.sessions: dict[str, DeviceSession] = {}
        self.ended_meetings: set[str] = set()
        self._background: set[asyncio.Task] = set()

    # -- helpers ----------------------------------------------------------------------------

    def _spawn(self, coro: Awaitable) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._background.add(task)

        def done(t: asyncio.Task) -> None:
            self._background.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log.error("background transport task failed", exc_info=t.exception())

        task.add_done_callback(done)
        return task

    def session_for(self, device_id: str, meeting_id: str) -> DeviceSession:
        session = self.sessions.get(device_id)
        if session is None:
            session = DeviceSession(device_id, meeting_id, self.config.audio_queue_frames)
            session.pump_task = asyncio.create_task(self._pump(session))
            self.sessions[device_id] = session
            bind = getattr(self.sink, "bind_device", None)  # optional part of the sink seam (CON-05)
            if bind is not None:
                bind(device_id, meeting_id)
        return session

    @staticmethod
    def peer_active(session: DeviceSession) -> bool:
        return session.peer is not None and session.peer.connectionState in ("connected", "connecting")

    # -- signaling-side operations ----------------------------------------------------------

    async def attach_socket(self, session: DeviceSession, ws, remote_addr: str | None, user_agent: str | None) -> None:
        """Make ``ws`` the device's signaling socket. A previous socket for the same device is closed."""
        old = session.ws
        session.ws, session.remote_addr, session.user_agent = ws, remote_addr, user_agent
        if old is not None and old is not ws:
            try:
                await old.close(code=1000, reason="replaced by a newer connection")
            except Exception:
                pass

    async def handle_offer(self, session: DeviceSession, sdp: str, ice_restart: bool) -> str:
        """Answer a phone's offer with a fresh peer connection, replacing any previous one."""
        if session.closing or session.meeting_id in self.ended_meetings:
            raise TransportError("meeting_ended", "the meeting has ended")
        if ice_restart:
            if not self.peer_active(session):
                raise TransportError("no_active_peer", "there is no usable peer to restart; send a plain offer", fatal=False)
            # aiortc accepts a re-offer but keeps the old ICE connection and the new one fails (verified
            # in CON-04), so restarting is refused and the phone builds a fresh peer instead.
            raise TransportError("renegotiation_failed",
                                 "this server cannot restart ICE; send a plain offer to build a new peer", fatal=False)
        if not isinstance(sdp, str) or len(sdp) > self.config.max_sdp_chars:
            raise TransportError("invalid_sdp", f"sdp must be a string of at most {self.config.max_sdp_chars} characters")
        try:
            parsed = SessionDescription.parse(sdp)
        except Exception as exc:
            raise TransportError("invalid_sdp", f"sdp does not parse: {exc}") from exc
        if not any(media.kind == "audio" for media in parsed.media):
            raise TransportError("invalid_sdp", "sdp has no audio section")

        async with session.lock:
            await self._drop_peer(session)  # the old peer is replaced silently; this is not a disconnect
            peer = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))
            session.peer = peer
            self._watch(session, peer)
            try:
                await peer.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
                await peer.setLocalDescription(await peer.createAnswer())
            except (ValueError, TypeError) as exc:
                await self._drop_peer(session)
                raise TransportError("invalid_sdp", f"sdp was rejected: {exc}") from exc
            except Exception as exc:
                await self._drop_peer(session)
                raise TransportError("internal_error", f"could not negotiate: {exc}") from exc
            if session.closing or session.meeting_id in self.ended_meetings:  # the meeting ended mid-negotiation
                await self._drop_peer(session)
                raise TransportError("meeting_ended", "the meeting has ended")
            return peer.localDescription.sdp

    async def leave(self, session: DeviceSession) -> None:
        """The phone pressed Stop: close its peer and record ``left``."""
        async with session.lock:
            await self._drop_peer(session)
        try:
            await self.db.run(lambda tx: registry.record_device_left(tx, session.device_id, "client_leave"))
        except StorageError:
            log.exception("could not record device_left for %s", session.device_id)

    async def socket_closed(self, session: DeviceSession, ws) -> None:
        """The signaling socket went away. A connected peer is kept (media may still flow); anything else is released."""
        if session.ws is not ws:
            return
        session.ws = None
        if session.closing or (session.peer is not None and session.peer.connectionState == "connected"):
            return
        async with session.lock:
            await self._drop_peer(session)
        await self._record_disconnect(session, "peer_closed")

    async def reset_after_error(self, session: DeviceSession, ws) -> None:
        """A fatal signaling error: reset only this device's connection."""
        if session.ws is not ws:
            return
        session.ws = None
        async with session.lock:
            await self._drop_peer(session)
        await self._record_disconnect(session, "peer_closed")

    # -- peer lifecycle ---------------------------------------------------------------------

    def _watch(self, session: DeviceSession, peer: RTCPeerConnection) -> None:
        @peer.on("connectionstatechange")
        async def _changed() -> None:
            try:
                if session.peer is not peer or session.closing:
                    return  # a replaced or torn-down peer says nothing about the device
                state = peer.connectionState
                log.info("TRANSPORT [%s] state=%s", session.device_id[:8], state)
                if state == "connected":
                    await self._on_connected(session, peer)
                elif state in _LOST_REASONS:
                    await self._on_lost(session, peer, state)
            except Exception:
                log.exception("connection-state handler failed for %s", session.device_id)

        @peer.on("track")
        def _track(track) -> None:
            if track.kind == "audio" and session.peer is peer and not session.closing:
                self._start_receive(session, track)

    async def _on_connected(self, session: DeviceSession, peer: RTCPeerConnection) -> None:
        try:
            await self.db.run(lambda tx: registry.record_device_connected(
                tx, session.device_id, via="new_peer", remote_addr=session.remote_addr,
                user_agent=session.user_agent))
        except MeetingEndedError:
            self._spawn(self._teardown(session))
        except StorageError:
            log.exception("could not record the connection of %s", session.device_id)

    async def _on_lost(self, session: DeviceSession, peer: RTCPeerConnection, state: str) -> None:
        await self._record_disconnect(session, _LOST_REASONS[state])
        if state in ("failed", "closed") and session.peer is peer:
            async with session.lock:
                if session.peer is peer:
                    await self._drop_peer(session)

    async def _record_disconnect(self, session: DeviceSession, reason: str) -> None:
        def op(tx):
            device = devices.get(tx.conn, session.device_id)
            if device is None or device.status != "connected":
                return None  # nothing to record: it never connected, already disconnected, or left
            return registry.record_device_disconnected(tx, session.device_id, reason)

        try:
            await self.db.run(op)
        except StorageError:
            log.exception("could not record the disconnect of %s", session.device_id)

    async def _drop_peer(self, session: DeviceSession) -> None:
        """Release the device's peer and receive loop. Records nothing; callers record what happened."""
        peer, task = session.peer, session.receive_task
        session.peer, session.receive_task = None, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if peer is not None:
            try:
                await peer.close()
            except Exception:
                log.exception("closing a peer failed for %s", session.device_id)

    async def _teardown(self, session: DeviceSession) -> None:
        session.closing = True
        ws, session.ws = session.ws, None
        async with session.lock:
            await self._drop_peer(session)
        if session.pump_task is not None:
            session.pump_task.cancel()
            await asyncio.gather(session.pump_task, return_exceptions=True)
        if ws is not None:
            try:
                await ws.send_json({"type": "meeting_ended"})
                await ws.close(code=1000)
            except Exception:
                pass

    async def end_meeting(self, meeting_id: str) -> None:
        """After the meeting has been marked ended: close every device's socket and peer, and refuse new offers."""
        self.ended_meetings.add(meeting_id)
        doomed = [s for s in self.sessions.values() if s.meeting_id == meeting_id]
        await asyncio.gather(*(self._teardown(s) for s in doomed), return_exceptions=True)
        for session in doomed:
            self.sessions.pop(session.device_id, None)

    async def shutdown(self) -> None:
        """Server stopping: close everything without recording anything (startup reconciliation records it)."""
        for session in list(self.sessions.values()):
            session.closing = True
        await asyncio.gather(*(self._teardown(s) for s in list(self.sessions.values())), return_exceptions=True)
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        self.sessions.clear()

    # -- audio ------------------------------------------------------------------------------

    def _start_receive(self, session: DeviceSession, track) -> None:
        if session.receive_task is not None:
            session.receive_task.cancel()
        session.previous_frame_at = None
        session.receive_task = asyncio.create_task(self._receive_loop(session, track))

    async def _receive_loop(self, session: DeviceSession, track) -> None:
        """Decode frames and queue them. Never awaits anything slow: the queue is bounded and full means drop."""
        stats = session.stats
        try:
            while True:
                frame = await track.recv()
                now, wall = time.monotonic(), time.time()
                pcm, rate = frame_to_mono_int16(frame)
                previous = session.previous_frame_at
                session.previous_frame_at = now
                stats.frames += 1
                stats.seconds += len(pcm) / rate
                stats.sample_rate = rate
                stats.last_audio_at, stats.last_audio_wall = now, wall
                if previous is not None and now - previous > self.config.audio_gap_s:
                    stats.gaps += 1
                    self._spawn(self._record_audio_resumed(session, now - previous))
                try:
                    session.queue.put_nowait((pcm, rate, wall))
                except asyncio.QueueFull:
                    stats.dropped_frames += 1
        except asyncio.CancelledError:
            raise
        except MediaStreamError:
            log.info("AUDIO [%s] track ended", session.device_id[:8])
        except Exception as exc:
            log.exception("receive loop failed for %s", session.device_id)
            self._spawn(self._receive_failed(session, exc))
        finally:
            ended = getattr(self.sink, "stream_ended", None)  # optional part of the sink seam (CON-05)
            if ended is not None:
                try:
                    ended(session.device_id)
                except Exception:
                    log.exception("the audio sink failed to close the stream of %s", session.device_id)

    async def _receive_failed(self, session: DeviceSession, exc: Exception) -> None:
        """One device's receive loop died: audit it, and treat it as that device's peer failing."""
        def op(tx):
            return emit(tx, "signaling_error", "transport",
                        {"device_id": session.device_id, "code": "receive_failed", "remote_addr": session.remote_addr},
                        meeting_id=session.meeting_id)
        try:
            await self.db.run(op)
        except StorageError:
            log.exception("could not audit the receive failure of %s", session.device_id)
        peer = session.peer
        if peer is not None:
            await self._on_lost(session, peer, "failed")

    async def _record_audio_resumed(self, session: DeviceSession, gap_s: float) -> None:
        def op(tx):
            device = devices.get(tx.conn, session.device_id)
            if device is None or device.status != "connected":
                return None
            return record_connection_event(tx, device_id=session.device_id, event_type="audio_resumed",
                                           payload={"gap_ms": round(gap_s * 1000)})
        try:
            await self.db.run(op)
        except StorageError:
            log.exception("could not record audio_resumed for %s", session.device_id)

    async def _pump(self, session: DeviceSession) -> None:
        """Feed this device's queued frames to the sink. A slow or failing sink only ever affects this device."""
        while True:
            pcm, rate, wall = await session.queue.get()
            try:
                await self.sink.push(session.device_id, pcm, rate, wall)
            except asyncio.CancelledError:
                raise
            except Exception:
                session.stats.sink_errors += 1
                log.exception("audio sink failed for %s", session.device_id)

    # -- reads ------------------------------------------------------------------------------

    def _with_stt(self, session: DeviceSession, gauges: dict) -> dict:
        """Replace the placeholder STT gauges with the pipeline's, when a pipeline is the sink."""
        stt = getattr(self.sink, "gauges", None)
        if stt is not None and gauges["last_audio_age_ms"] is not None:
            gauges = {**gauges, **stt(session.device_id)}
        return gauges

    def gauges_for_device(self, device_id: str) -> dict:
        session = self.sessions.get(device_id)
        if session is None:
            return {k: None for k in ("last_audio_age_ms", "audio_duration_s", "stt_backlog", "stt_dropped_windows")}
        return self._with_stt(session, session.gauges(time.monotonic()))

    def gauges_for_meeting(self, meeting_id: str) -> list[dict]:
        """One entry per device of the meeting that has streamed audio (docs/api.md, ``device_gauges``)."""
        now = time.monotonic()
        rows = []
        for session in self.sessions.values():
            if session.meeting_id == meeting_id and session.stats.last_audio_at is not None:
                rows.append({"device_id": session.device_id, **self._with_stt(session, session.gauges(now))})
        return rows

    def diagnostics(self) -> list[dict]:
        """Read-only per-device counters for ``GET /metrics`` and the periodic log line."""
        now = time.monotonic()
        rows = []
        for session in self.sessions.values():
            stats = session.stats
            rows.append({
                "device_id": session.device_id, "meeting_id": session.meeting_id, "state": session.peer_state,
                "audio_received": stats.last_audio_at is not None, **self._with_stt(session, session.gauges(now)),
                "frames": stats.frames, "dropped_frames": stats.dropped_frames,
                "sink_errors": stats.sink_errors, "audio_gaps": stats.gaps,
            })
            device_stats = getattr(self.sink, "device_stats", None)
            if device_stats is not None:
                rows[-1]["stt"] = device_stats().get(session.device_id)
        return rows
