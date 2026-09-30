"""The cross-device bleed filter (ADR-32) on synthetic speech: a quieter copy of another phone's speech is dropped,
everything else is kept. Real phones in a real room are what the B7 manual test is for."""
import asyncio
from dataclasses import replace

import numpy as np
import pytest

from server.config import PipelineConfig
from server.pipeline.adapter import FakeAdapter
from server.pipeline.bleed import BleedConfig, LevelHistory, judge
from server.pipeline.pipeline import SttPipeline
from server.pipeline.windowing import Window, iso_utc
from tests.support.speech import RATE, clip
from tests.test_windowing import T0, to_int16

RNG = np.random.default_rng(7)


def room(*tracks: tuple[np.ndarray, float, float], seconds: float) -> np.ndarray:
    """A phone's microphone: each (clip, gain, delay in s) mixed in, over a faint noise floor."""
    out = (RNG.standard_normal(int(RATE * seconds)) * 10 ** (-62 / 20)).astype(np.float32)
    for audio, gain, delay in tracks:
        start = int(RATE * delay)
        out[start:start + len(audio)] += audio * gain
    return out


async def run(mics: dict[str, np.ndarray], *, config=PipelineConfig(), meetings=None):
    """Feed every phone's audio frame by frame, interleaved in time, and return what reached STT per device."""
    got = []
    pipeline = SttPipeline(FakeAdapter(), config, on_window=got.append)
    pipeline.start()
    try:
        for device in mics:
            pipeline.bind_device(device, (meetings or {}).get(device, "m"))
        length = max(len(a) for a in mics.values())
        for i in range(0, length - 319, 320):
            t = T0 + (i + 320) / RATE
            for device, audio in mics.items():
                await pipeline.push(device, to_int16(audio[i:i + 320]), RATE, t)
        for device in mics:
            pipeline.stream_ended(device)
        for meeting in set((meetings or {}).values()) or {"m"}:
            await pipeline.drain(meeting, 5)
        await asyncio.sleep(0.05)
        stats = pipeline.scheduler.stats()
    finally:
        await pipeline.stop()
    ok = {d: sum(1 for w in got if w.device_id == d and w.status == "ok") for d in mics}
    bleed = {d: stats.get(d, {}).get("bleed", 0) for d in mics}
    return ok, bleed, got


async def test_a_quieter_copy_of_another_phones_speech_is_dropped_before_stt():
    speech = clip("ship_beta")
    seconds = 1.0 + len(speech) / RATE + 2.0
    near = room((speech, 1.0, 1.0), seconds=seconds)
    far = room((speech, 0.25, 1.04), seconds=seconds)            # 12 dB quieter, arriving 40 ms later
    ok, bleed, got = await run({"near": near, "far": far})
    assert ok == {"near": 1, "far": 0} and bleed == {"near": 0, "far": 1}
    dropped = [w for w in got if w.device_id == "far"]
    assert dropped and dropped[0].status == "dropped" and "louder" in dropped[0].error


async def test_two_people_talking_at_once_are_both_kept():
    a, b = clip("ship_beta"), clip("budget")
    seconds = 1.0 + max(len(a), len(b)) / RATE + 2.0
    phone_a = room((a, 1.0, 1.0), (b, 0.25, 1.0), seconds=seconds)   # each phone hears the other person faintly
    phone_b = room((b, 1.0, 1.0), (a, 0.25, 1.0), seconds=seconds)
    ok, bleed, _ = await run({"a": phone_a, "b": phone_b})
    assert ok == {"a": 1, "b": 1} and bleed == {"a": 0, "b": 0}


async def test_equal_levels_are_kept():
    """Phones side by side (or gain control evening them out): no clear source, so nothing is dropped."""
    speech = clip("ship_beta")
    seconds = 1.0 + len(speech) / RATE + 2.0
    ok, bleed, _ = await run({"x": room((speech, 1.0, 1.0), seconds=seconds),
                              "y": room((speech, 0.8, 1.02), seconds=seconds)})
    assert ok == {"x": 1, "y": 1} and bleed == {"x": 0, "y": 0}


async def test_other_meetings_and_the_off_switch_are_respected():
    speech = clip("ship_beta")
    seconds = 1.0 + len(speech) / RATE + 2.0
    mics = {"near": room((speech, 1.0, 1.0), seconds=seconds), "far": room((speech, 0.25, 1.04), seconds=seconds)}
    ok, _, _ = await run(mics, meetings={"near": "m1", "far": "m2"})
    assert ok == {"near": 1, "far": 1}
    ok, _, _ = await run(mics, config=replace(PipelineConfig(), bleed_filter=False))
    assert ok == {"near": 1, "far": 1}


def history(times, levels, voiced=True) -> np.ndarray:
    h = LevelHistory()
    for t, level in zip(times, levels):
        h.append(t, level, voiced)
    return h.snapshot()


def window(t0, t1) -> Window:
    return Window("me", "m", 1, np.zeros(10, np.float32), RATE, iso_utc(t0), iso_utc(t1), 1.0, 0.0)


def test_judge_keeps_when_in_doubt():
    times = T0 + np.arange(100) * 0.02
    envelope = -30 + 10 * np.sin(np.arange(100) / 4)
    own = history(times, envelope - 10)
    config = BleedConfig()
    assert judge(window(times[0], times[-1]), own, {"other": history(times, envelope)}, config).drop is True
    assert judge(window(times[0], times[-1]), own, {}, config).drop is False                         # nobody else
    assert judge(window(times[0], times[-1]), own, {"other": history(times[:20], envelope[:20])}, config).drop is False
    assert judge(window(times[0], times[3]), own, {"other": history(times, envelope)}, config).drop is False  # too short
    unvoiced = history(times, envelope - 10, voiced=False)
    assert judge(window(times[0], times[-1]), unvoiced, {"other": history(times, envelope)}, config).drop is False


@pytest.mark.parametrize("bad", [{"bleed_min_correlation": 0.0}, {"bleed_min_level_gap_db": -1.0},
                                 {"bleed_max_lag_ms": 5000}])
def test_config_is_validated(tmp_path, bad):
    from server.config import ConfigError, load_settings
    key, value = next(iter(bad.items()))
    (tmp_path / "c.toml").write_text(f"[pipeline]\n{key} = {value}\n")
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "c.toml", root=tmp_path)
