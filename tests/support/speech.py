"""Speech and noise builders for the STT tests. Speech is the synthetic (text-to-speech) fixtures: regression
material, not evidence about real phone audio."""
import json
import wave
from pathlib import Path

import numpy as np

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "audio"
RATE = 16000


def db(level: float) -> float:
    return 10 ** (level / 20)


def rms_db(x: np.ndarray) -> float:
    return float(20 * np.log10(np.sqrt(np.mean(x * x)) + 1e-10))


def manifest() -> list[dict]:
    return json.loads((FIXTURES / "manifest.json").read_text())


def clip(name: str) -> np.ndarray:
    """A fixture as float32 mono at 16 kHz."""
    with wave.open(str(FIXTURES / f"{name}.wav"), "rb") as w:
        assert w.getframerate() == RATE and w.getnchannels() == 1
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0


def speech_stream(names: list[str] | None = None, lead_s: float = 1.0, pause_s: float = 2.0) -> np.ndarray:
    """Clips separated by silence, normalized so the *active* speech is at -16 dBFS RMS."""
    names = names or [m["file"][:-4] for m in manifest()]
    parts = [np.zeros(int(RATE * lead_s), np.float32)]
    for n in names:
        parts += [clip(n), np.zeros(int(RATE * pause_s), np.float32)]
    stream = np.concatenate(parts)
    active = stream[np.abs(stream) > 0.01]
    return stream * db(-16 - rms_db(active))


def at_level(stream: np.ndarray, level_db: float) -> np.ndarray:
    active = stream[np.abs(stream) > 0.003]
    return stream * db(level_db - rms_db(active))


def white(n: int, level_db: float, seed: int = 1) -> np.ndarray:
    return (np.random.default_rng(seed).standard_normal(n) * db(level_db)).astype(np.float32)


def pink(n: int, level_db: float, seed: int = 2) -> np.ndarray:
    f = np.fft.rfft(np.random.default_rng(seed).standard_normal(n))
    f /= np.sqrt(np.maximum(np.arange(len(f)), 1))
    x = np.fft.irfft(f, n)
    return (x / x.std() * db(level_db)).astype(np.float32)


def hum(n: int, level_db: float) -> np.ndarray:
    t = np.arange(n) / RATE
    return ((np.sin(2 * np.pi * 50 * t) + 0.5 * np.sin(2 * np.pi * 100 * t)) * db(level_db) * 0.8).astype(np.float32) \
        + white(n, level_db - 15)


def sine(freq: float, seconds: float, level_db: float = -20, rate: int = RATE) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return (np.sin(2 * np.pi * freq * t) * db(level_db) * np.sqrt(2)).astype(np.float32)
