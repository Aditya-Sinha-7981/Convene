"""In-process synthetic phone: an aiortc client that plays a tone or a WAV as its microphone.

A regression harness, not a substitute for real phones. It runs over loopback with aiortc as the client, so it
cannot reveal browser, Wi-Fi, secure-context, or screen-lock behavior.

All protocol handling lives in ``SyntheticPhone``. Retargeted (CON-04) to the Convene contract:
``POST /api/meetings/{id}/devices`` registers the device, then over ``/ws/signal/{id}``
``join{device_id}`` -> ``joined``, ``offer{sdp}`` -> ``answer{sdp}``, ``leave``; errors arrive as
``error{code, message, fatal}``. ICE is non-trickle and there is no ICE restart.
"""
import asyncio
import fractions
import time
import wave
from pathlib import Path

import numpy as np
from aiohttp import ClientSession, WSMsgType
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import AUDIO_PTIME, AudioStreamTrack, MediaStreamError
from av import AudioFrame

from server.ids import new_id

SAMPLE_RATE = 48000


def _load_wav(path: Path) -> np.ndarray:
    """Read a 16-bit PCM WAV as mono int16 at 48 kHz (linear resample; test use only)."""
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise ValueError("synthetic phone WAV input must be 16-bit PCM")
        channels, rate = wav.getnchannels(), wav.getframerate()
        data = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    if rate != SAMPLE_RATE and len(data):
        target = int(len(data) * SAMPLE_RATE / rate)
        data = np.interp(np.linspace(0, len(data) - 1, target), np.arange(len(data)), data)
    return np.asarray(data, dtype="<i2")


class PcmTrack(AudioStreamTrack):
    """Paced 20 ms mono frames from a sample buffer. Loops the buffer; a tone by default."""

    def __init__(self, samples: np.ndarray):
        super().__init__()
        if not len(samples):
            raise ValueError("no samples to play")
        self._samples = np.asarray(samples, dtype="<i2")
        self._position = 0
        self.frames_sent = 0

    async def recv(self) -> AudioFrame:
        if self.readyState != "live":
            raise MediaStreamError
        count = int(AUDIO_PTIME * SAMPLE_RATE)
        if hasattr(self, "_timestamp"):
            self._timestamp += count
            await asyncio.sleep(max(0.0, self._start + self._timestamp / SAMPLE_RATE - time.time()))
        else:
            self._start = time.time()
            self._timestamp = 0
        index = (self._position + np.arange(count)) % len(self._samples)
        self._position = (self._position + count) % len(self._samples)
        frame = AudioFrame(format="s16", layout="mono", samples=count)
        frame.planes[0].update(self._samples[index].tobytes())
        frame.pts = self._timestamp
        frame.sample_rate = SAMPLE_RATE
        frame.time_base = fractions.Fraction(1, SAMPLE_RATE)
        self.frames_sent += 1
        return frame


def tone(frequency: float = 440.0, seconds: float = 1.0, amplitude: float = 0.3) -> np.ndarray:
    t = np.arange(int(SAMPLE_RATE * seconds)) / SAMPLE_RATE
    return (np.sin(2 * np.pi * frequency * t) * amplitude * 32767).astype("<i2")


class SyntheticPhone:
    """One synthetic phone. ``base_url`` is ``http://host:port``; ``meeting_id`` is the meeting to join."""

    def __init__(self, base_url: str, meeting_id: str, name: str = "Synthetic", device_id: str | None = None,
                 samples: np.ndarray | None = None, wav_path: Path | None = None,
                 user_agent: str = "SyntheticPhone/1.0"):
        if samples is None:
            samples = _load_wav(wav_path) if wav_path else tone()
        self.base_url = base_url.rstrip("/")
        self.meeting_id = meeting_id
        self.name = name
        self.device_id = device_id or new_id()
        self.user_agent = user_agent
        self.track = PcmTrack(samples)
        self.joined: dict | None = None
        self.errors: list[dict] = []
        self.peer: RTCPeerConnection | None = None
        self._abandoned: list[RTCPeerConnection] = []
        self._session: ClientSession | None = None
        self._ws = None

    @property
    def signaling_url(self) -> str:
        return self.base_url.replace("http", "ws", 1) + f"/ws/signal/{self.meeting_id}"

    async def _http(self) -> ClientSession:
        if self._session is None:
            self._session = ClientSession(headers={"User-Agent": self.user_agent})
        return self._session

    async def register(self, **body) -> tuple[int, dict]:
        """``POST /api/meetings/{id}/devices``. Returns (status, JSON body)."""
        payload = {"device_id": self.device_id, "display_name": self.name, "is_shared": False, **body}
        session = await self._http()
        async with session.post(f"{self.base_url}/api/meetings/{self.meeting_id}/devices", json=payload) as response:
            return response.status, await response.json()

    async def open(self):
        """Open the signaling socket only (no join)."""
        session = await self._http()
        self._ws = await session.ws_connect(self.signaling_url)
        return self._ws

    async def send(self, message: dict) -> None:
        await self._ws.send_json(message)

    async def receive(self, timeout: float = 5.0) -> dict:
        """Next JSON message from the server; ``error`` messages are also recorded in ``errors``."""
        message = await asyncio.wait_for(self._ws.receive(), timeout)
        if message.type != WSMsgType.TEXT:
            raise ConnectionError(f"signaling socket ended: {message.type.name} {self._ws.close_code}")
        data = message.json()
        if data.get("type") == "error":
            self.errors.append(data)
        return data

    async def join(self) -> dict:
        await self.send({"type": "join", "device_id": self.device_id})
        reply = await self.receive()
        if reply.get("type") == "joined":
            self.joined = reply
        return reply

    async def offer(self, **extra) -> dict:
        """Create a non-trickle offer (aiortc gathers inside setLocalDescription) and apply the answer."""
        self.peer = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))
        self.peer.addTrack(self.track)
        await self.peer.setLocalDescription(await self.peer.createOffer())
        await self.send({"type": "offer", "sdp": self.peer.localDescription.sdp, **extra})
        reply = await self.receive(timeout=15.0)
        if reply.get("type") == "answer":
            await self.peer.setRemoteDescription(RTCSessionDescription(sdp=reply["sdp"], type="answer"))
        return reply

    async def connect(self, register: bool = True) -> dict:
        """register + open + join + offer/answer. Returns the answer message."""
        if register:
            status, body = await self.register()
            if status not in (200, 201):
                raise RuntimeError(f"registration failed: {status} {body}")
        await self.open()
        joined = await self.join()
        if joined.get("type") != "joined":
            raise RuntimeError(f"join failed: {joined}")
        return await self.offer()

    async def reconnect(self, close_old_peer: bool = True) -> dict:
        """What the join page does after a drop: a new socket, `join`, and a fresh peer (no ICE restart).

        ``close_old_peer=True`` is the page's own cleanup (it closes its RTCPeerConnection first, so the server
        sees the old peer close). ``False`` is a silent Wi-Fi drop: the old peer is simply abandoned and the
        server only learns of it when the new offer replaces it.
        """
        await self.drop_signaling()
        if self.peer is not None:
            if close_old_peer:
                await self.peer.close()
            else:
                self._abandoned.append(self.peer)
            self.peer = None
        self.track = PcmTrack(self.track._samples)
        await self.open()
        joined = await self.join()
        if joined.get("type") != "joined":
            raise RuntimeError(f"join failed: {joined}")
        return await self.offer()

    async def wait_connected(self, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while self.peer.connectionState != "connected":
            if time.monotonic() > deadline:
                raise TimeoutError(f"peer state {self.peer.connectionState}")
            await asyncio.sleep(0.05)

    async def drop_signaling(self) -> None:
        """Close only the signaling socket (as a lost Wi-Fi link would), leaving the local peer object alone."""
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()

    async def close(self) -> None:
        for old in self._abandoned:
            await old.close()
        self._abandoned.clear()
        if self.peer is not None:
            await self.peer.close()
            self.peer = None
        self.track.stop()
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._session is not None:
            await self._session.close()
            self._session = None
