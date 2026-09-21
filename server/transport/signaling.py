"""The phone signaling WebSocket: ``/ws/signal/{meeting_id}`` (message catalog in docs/transport.md).

One ``SignalingConnection`` per socket. Every error is scoped to the connection that caused it: it gets an
``error`` reply, and if the error is fatal its socket is closed (code 4400) and, if it had joined, that
device's peer is reset. Other devices and the process are never touched.
"""
import json
import logging

from starlette.websockets import WebSocket, WebSocketDisconnect

from ..audit import emit
from ..errors import StorageError
from ..ids import is_uuid4
from ..repositories import connections, devices, meetings, participants
from .sessions import DeviceSession, TransportError

log = logging.getLogger("convene.signaling")

FATAL_CLOSE_CODE = 4400


class SignalingConnection:
    def __init__(self, runtime, ws: WebSocket, meeting_id: str):
        self.runtime, self.ws, self.meeting_id = runtime, ws, meeting_id
        self.session: DeviceSession | None = None
        self.meeting_known = False
        self.remote_addr = ws.client.host if ws.client else None
        self.user_agent = ws.headers.get("user-agent")

    # -- main loop --------------------------------------------------------------------------

    async def run(self) -> None:
        try:
            await self._check_meeting()
            while True:
                message = await self.ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                text = message.get("text")
                if text is None:
                    raise TransportError("invalid_message", "binary frames are not accepted")
                try:
                    await self._dispatch(text)
                except TransportError as error:
                    if error.fatal:
                        raise
                    await self._send_error(error)
        except TransportError as error:
            await self._fail(error)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("signaling failure for meeting %s", self.meeting_id)
            await self._fail(TransportError("internal_error", "unexpected server error while handling your message"))
        finally:
            if self.session is not None:
                await self.runtime.peers.socket_closed(self.session, self.ws)

    async def _check_meeting(self) -> None:
        meeting = None
        if is_uuid4(self.meeting_id):
            meeting = await self.runtime.db.run(lambda tx: meetings.get(tx.conn, self.meeting_id))
        if meeting is None:
            raise TransportError("meeting_not_found", f"no meeting with id {self.meeting_id}")
        self.meeting_known = True

    async def _dispatch(self, text: str) -> None:
        if len(text.encode("utf-8")) > self.runtime.transport.max_message_bytes:
            raise TransportError("invalid_message", "message is too large")
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise TransportError("invalid_message", "message is not valid JSON") from exc
        if not isinstance(data, dict) or not isinstance(data.get("type"), str):
            raise TransportError("invalid_message", "message must be a JSON object with a string `type`")
        kind = data["type"]
        if kind == "join":
            await self._join(data)
        elif kind == "offer":
            await self._offer(data)
        elif kind == "leave":
            await self._leave()
        else:
            raise TransportError("unknown_message_type", f"unknown message type {kind!r}")

    # -- messages ---------------------------------------------------------------------------

    async def _join(self, data: dict) -> None:
        device_id = data.get("device_id")
        if not is_uuid4(device_id):
            raise TransportError("invalid_message", "`device_id` must be a UUID v4 string")
        if self.session is not None and self.session.device_id != device_id:
            raise TransportError("invalid_message", "this connection already joined as a different device")

        def read(tx):
            meeting = meetings.require(tx.conn, self.meeting_id)
            device = devices.get(tx.conn, device_id)
            if device is None or device.meeting_id != self.meeting_id:
                return meeting, None, [], False
            people = participants.list_for_device(tx.conn, device_id)
            return meeting, device, people, connections.has_connected(tx.conn, device_id)

        meeting, device, people, has_connected = await self.runtime.db.run(read)
        if meeting.status == "ended":
            raise TransportError("meeting_ended", "the meeting has ended")
        if device is None:
            raise TransportError("device_not_registered", f"device {device_id} is not registered for this meeting")
        session = self.runtime.peers.session_for(device_id, self.meeting_id)
        await self.runtime.peers.attach_socket(session, self.ws, self.remote_addr, self.user_agent)
        self.session = session
        await self._send({
            "type": "joined", "device_id": device_id,
            "participant_ids": [p.participant_id for p in people],
            "device_status": device.status,
            "reconnect_count": device.reconnect_count,  # as stored now; a reconnect is counted when the new peer connects
            "is_reconnect": has_connected,
            "peer_active": self.runtime.peers.peer_active(session),
        })

    async def _offer(self, data: dict) -> None:
        if self.session is None:
            raise TransportError("not_joined", "send `join` before `offer`")
        ice_restart = data.get("ice_restart", False)
        if not isinstance(ice_restart, bool):
            raise TransportError("invalid_message", "`ice_restart` must be a boolean")
        sdp = await self.runtime.peers.handle_offer(self.session, data.get("sdp"), ice_restart)
        await self._send({"type": "answer", "sdp": sdp})

    async def _leave(self) -> None:
        if self.session is None:
            raise TransportError("not_joined", "send `join` before `leave`")
        await self.runtime.peers.leave(self.session)
        await self._close(1000)

    # -- replies, errors --------------------------------------------------------------------

    async def _send(self, message: dict) -> None:
        try:
            await self.ws.send_json(message)
        except Exception:
            pass  # the phone is gone; the receive loop will notice

    async def _send_error(self, error: TransportError) -> None:
        await self._send({"type": "error", "code": error.code, "message": error.message, "fatal": error.fatal})

    async def _close(self, code: int) -> None:
        try:
            await self.ws.close(code=code)
        except Exception:
            pass

    async def _fail(self, error: TransportError) -> None:
        """A fatal error: reply, audit, reset this device's connection, close this socket."""
        await self._send_error(error)
        try:
            await self.runtime.db.run(lambda tx: emit(
                tx, "signaling_error", "transport",
                {"device_id": self.session.device_id if self.session else None, "code": error.code,
                 "remote_addr": self.remote_addr},
                meeting_id=self.meeting_id if self.meeting_known else None))
        except StorageError:
            log.exception("could not audit signaling error %s", error.code)
        if self.session is not None:
            await self.runtime.peers.reset_after_error(self.session, self.ws)
        await self._close(FATAL_CLOSE_CODE)


async def signaling_endpoint(websocket: WebSocket, meeting_id: str) -> None:
    await websocket.accept()
    await SignalingConnection(websocket.app.state.runtime, websocket, meeting_id).run()
