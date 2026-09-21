"""Decoded audio frames and the per-device audio sink seam that CON-05 implements against.

The receive loop turns every frame into mono 16-bit PCM and hands it to an ``AudioSink``. The transport
does not interpret audio and never persists it (docs/transport.md).

Time base available to the sink: ``t_wall`` is the server's wall clock (UTC epoch seconds) when the frame
was received, so it includes network and jitter-buffer delay and is not sample-accurate. The frame's own
media time (RTP-derived ``pts``) is not passed on; CON-05 can ask for it if windowing needs it.
"""
from typing import Protocol

import numpy as np


def frame_to_mono_int16(frame) -> tuple[np.ndarray, int]:
    """Decode an ``av.AudioFrame`` to little-endian mono int16 samples and its sample rate.

    Handles planar and packed layouts, integer and float samples, and any channel count (channels are
    averaged). Kept from the DT-17 prototype, with planar-versus-packed decided from the frame format
    rather than inferred from array shape.
    """
    raw = frame.to_ndarray()
    channels = len(frame.layout.channels)
    if frame.format.is_planar:
        planes = raw.astype(np.float32)  # shape (channels, samples)
        mono = planes.mean(axis=0) if channels > 1 else planes.reshape(-1)
    else:
        flat = raw.reshape(-1).astype(np.float32)  # packed: samples interleaved, shape (1, samples * channels)
        mono = flat.reshape(-1, channels).mean(axis=1) if channels > 1 else flat
    if np.issubdtype(raw.dtype, np.floating):
        mono = mono * 32767
    return np.clip(mono, -32768, 32767).astype("<i2"), int(frame.sample_rate)


class AudioSink(Protocol):
    """Receives every decoded frame of every device.

    ``push`` is awaited from a per-device pump task fed by a bounded queue, never from the receive loop
    itself. A slow sink therefore fills that device's queue and frames are dropped and counted; it cannot
    delay receiving audio or affect another device. ``push`` must still not block the event loop (no
    synchronous heavy work): hand off to a thread or process.
    """

    async def push(self, device_id: str, pcm: np.ndarray, sample_rate: int, t_wall: float) -> None: ...


class CountingSink:
    """The default sink: counts samples per device and drops the audio."""

    def __init__(self) -> None:
        self.samples: dict[str, int] = {}
        self.frames: dict[str, int] = {}

    async def push(self, device_id: str, pcm: np.ndarray, sample_rate: int, t_wall: float) -> None:
        self.samples[device_id] = self.samples.get(device_id, 0) + len(pcm)
        self.frames[device_id] = self.frames.get(device_id, 0) + 1
