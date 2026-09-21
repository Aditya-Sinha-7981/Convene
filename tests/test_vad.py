"""The per-device VAD gate: silence and noise are gated, speech passes, devices never share state, thresholds are
configuration. Speech is synthetic (text-to-speech); the noise conditions are synthetic too. These pin the gate's
behavior; they do not show how it behaves on real phones (see logs/stt.md)."""
import numpy as np
import pytest

from server.pipeline.vad import EnergyVad, VadConfig
from server.pipeline.windowing import Windower
from tests.support.speech import RATE, at_level, hum, pink, rms_db, speech_stream, white


def run(audio: np.ndarray, config: VadConfig = VadConfig(), min_fraction: float = 0.2):
    """Feed a stream in 20 ms chunks through a Windower; return (passed window ids, windows seen)."""
    w = Windower("d", "m", RATE, 1000, config, min_fraction)
    passed = []
    for i in range(0, len(audio) - 319, 320):
        windows, _ = w.feed(audio[i:i + 320], 1000 + (i + 320) / RATE)
        passed += [x.window_id for x in windows]
    windows, _ = w.flush()
    passed += [x.window_id for x in windows]
    return set(passed), w.windows_seen


def speech_windows(clean: np.ndarray) -> list[bool]:
    """Ground truth: does the clean speech occupy at least 20% of this 1 s window?"""
    out = []
    for i in range(0, len(clean) - RATE + 1, RATE):
        frames = clean[i:i + RATE].reshape(-1, 320)
        out.append(bool(np.mean(20 * np.log10(np.sqrt((frames ** 2).mean(1)) + 1e-10) > -45) >= 0.2))
    return out


STREAM = speech_stream()


@pytest.mark.parametrize("name,noise", [
    ("digital silence", lambda n: np.zeros(n, np.float32)),
    ("very quiet white", lambda n: white(n, -60)),
    ("white -45", lambda n: white(n, -45)),
    ("white -35", lambda n: white(n, -35)),
    ("loud white fan -25", lambda n: white(n, -25)),
    ("50 Hz hum -35", lambda n: hum(n, -35)),
])
def test_silence_and_steady_noise_are_gated(name, noise):
    """Steady noise is the case a floor that never rises gets wrong: it must be learned as background."""
    passed, seen = run(noise(RATE * 40))
    assert seen >= 39 and len(passed) == 0, f"{name}: {len(passed)}/{seen} windows passed"


@pytest.mark.parametrize("level", [-18, -28, -38, -46])
def test_speech_passes_at_normal_to_very_quiet_levels(level):
    clean = at_level(STREAM, level)
    passed, _ = run(clean)
    truth = speech_windows(clean)
    assert sum(truth) >= 12
    missed = [i for i, v in enumerate(truth, 1) if v and i not in passed]
    assert not missed, f"speech windows missed at {level} dBFS: {missed}"


@pytest.mark.parametrize("snr", [20, 10])
def test_speech_in_background_noise_mostly_passes_and_pauses_mostly_do_not(snr):
    clean = at_level(STREAM, -28)
    noisy = clean + pink(len(clean), -28 - snr)
    passed, _ = run(noisy)
    truth = speech_windows(clean)
    speech_hit = sum(1 for i, v in enumerate(truth, 1) if v and i in passed) / sum(truth)
    pause_pass = sum(1 for i, v in enumerate(truth, 1) if not v and i in passed) / max(sum(1 for v in truth if not v), 1)
    assert speech_hit >= 0.85, f"{snr} dB SNR: only {speech_hit:.0%} of speech windows passed"
    assert pause_pass <= 0.25, f"{snr} dB SNR: {pause_pass:.0%} of pause windows passed"


def test_the_background_level_is_learned_per_device_and_devices_share_nothing():
    quiet, noisy = EnergyVad(RATE, VadConfig()), EnergyVad(RATE, VadConfig())
    assert quiet is not noisy and quiet._levels is not noisy._levels
    for i in range(0, RATE * 6, 320):
        quiet.process(white(320, -70, seed=i))
        noisy.process(white(320, -35, seed=i + 1))
    assert noisy.noise_db > quiet.noise_db + 25  # each learned its own background
    before = quiet.noise_db
    noisy.process(white(RATE, -10))  # a loud event on one device...
    assert quiet.noise_db == before  # ...changes nothing on the other


def test_the_same_speech_on_two_devices_is_judged_against_each_devices_own_background():
    clean = at_level(STREAM, -30)
    quiet_passed, _ = run(clean + white(len(clean), -70))
    loud_passed, _ = run(clean + white(len(clean), -32))  # background almost as loud as the speech
    truth = speech_windows(clean)
    assert sum(1 for i, v in enumerate(truth, 1) if v and i in quiet_passed) == sum(truth)
    assert len(loud_passed) < len(quiet_passed)  # speech barely above the room is not treated as speech


def test_frames_before_the_background_is_known_are_never_called_speech():
    vad = EnergyVad(RATE, VadConfig())
    decisions = vad.process(white(RATE, -20))  # loud from the very first sample
    assert not any(decisions[:EnergyVad.MIN_FRAMES_FOR_ESTIMATE - 1])


def test_hangover_bridges_a_brief_pause_inside_speech():
    vad = EnergyVad(RATE, VadConfig(hangover_frames=5))
    vad.process(white(RATE, -60))  # learn a quiet background
    talk = white(3200, -20, seed=5)  # 200 ms of loud: 10 frames
    gap = np.zeros(1600, np.float32)  # 100 ms of silence: exactly 5 frames
    voiced = np.array(vad.process(np.concatenate([talk, gap, np.zeros(3200, np.float32)])))
    assert voiced[:10].all() and voiced[10:15].all()  # the 5 hangover frames stay open
    assert not voiced[-5:].any()  # and it does close afterward


def test_thresholds_are_configuration():
    clean = at_level(STREAM, -40) + pink(len(STREAM), -52)  # speech about 12 dB over the background
    lax, _ = run(clean, VadConfig(margin_db=6.0))
    strict, _ = run(clean, VadConfig(margin_db=16.0))
    assert len(strict) < len(lax)
    quiet = at_level(STREAM, -58)
    default, _ = run(quiet)
    raised_floor, _ = run(quiet, VadConfig(min_db=-70.0))
    assert len(default) == 0 < len(raised_floor)  # the absolute floor is configurable too
    loud = at_level(STREAM, -28)
    assert len(run(loud, min_fraction=0.99)[0]) < len(run(loud, min_fraction=0.2)[0])  # and the share of a window


def test_pipeline_config_maps_onto_the_gate():
    from server.config import PipelineConfig
    from server.pipeline.pipeline import vad_config
    cfg = vad_config(PipelineConfig(vad_margin_db=12.0, vad_min_db=-45.0, vad_noise_window_s=8.0, vad_hangover_frames=3))
    assert (cfg.margin_db, cfg.min_db, cfg.noise_window_s, cfg.hangover_frames) == (12.0, -45.0, 8.0, 3)
    assert rms_db(np.ones(10, np.float32)) == pytest.approx(0.0, abs=0.01)
