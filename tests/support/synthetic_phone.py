"""In-process synthetic phone: an aiortc client that plays a tone or a WAV as its microphone.

This is a regression harness, not a substitute for real phones. It runs over the
loopback/host network with aiortc as the client, so it cannot reveal browser,
Wi-Fi, secure-context, or screen-lock behavior.

All signaling protocol handling lives in ``SyntheticPhone`` so it can be retargeted
to a new signaling contract (CON-04) by editing one class. The DT-17 prototype
protocol is: ``join{token}`` -> ``joined{participantId, reconnects}``, then
``offer{sdp}`` -> ``answer{sdp}``, with no ICE candidate messages (non-trickle).
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
    """One synthetic phone. ``url`` is the signaling WebSocket URL (``ws://host:port/ws/<meeting>``)."""

    def __init__(self, url: str, token: str, samples: np.ndarray | None = None,
                 wav_path: Path | None = None):
        if samples is None:
            samples = _load_wav(wav_path) if wav_path else tone()
        self.url = url
        self.token = token
        self.track = PcmTrack(samples)
        self.participant_id: str | None = None
        self.reconnects: int | None = None
        self.errors: list[str] = []
        self.peer: RTCPeerConnection | None = None
        self._session: ClientSession | None = None
        self._ws = None

    async def open(self):
        """Open the signaling socket only (no join)."""
        self._session = ClientSession()
        self._ws = await self._session.ws_connect(self.url)
        return self._ws

    async def send(self, message: dict) -> None:
        await self._ws.send_json(message)

    async def receive(self, timeout: float = 5.0) -> dict:
        """Next JSON message from the server; error messages are also recorded in ``errors``."""
        message = await asyncio.wait_for(self._ws.receive(), timeout)
        if message.type != WSMsgType.TEXT:
            raise ConnectionError(f"signaling socket ended: {message.type.name}")
        data = message.json()
        if data.get("type") == "error":
            self.errors.append(data.get("message", ""))
        return data

    async def join(self) -> dict:
        await self.send({"type": "join", "token": self.token})
        reply = await self.receive()
        if reply.get("type") == "joined":
            self.participant_id = reply["participantId"]
            self.reconnects = reply["reconnects"]
        return reply

    async def offer(self) -> dict:
        """Create a non-trickle offer (aiortc gathers inside setLocalDescription) and apply the answer."""
        self.peer = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))
        self.peer.addTrack(self.track)
        await self.peer.setLocalDescription(await self.peer.createOffer())
        await self.send({"type": "offer", "sdp": self.peer.localDescription.sdp})
        reply = await self.receive(timeout=15.0)
        if reply.get("type") == "answer":
            await self.peer.setRemoteDescription(RTCSessionDescription(sdp=reply["sdp"], type="answer"))
        return reply

    async def connect(self) -> dict:
        """open + join + offer/answer. Returns the answer message."""
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
        if self.peer is not None:
            await self.peer.close()
            self.peer = None
        self.track.stop()
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._session is not None:
            await self._session.close()
            self._session = None
