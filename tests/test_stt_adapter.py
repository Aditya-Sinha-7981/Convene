"""The STT adapter contract, the confidence mapping, offline model resolution, and (marked ``model``) the real
mlx-whisper model. The default run needs no weights; ``pytest -m model`` needs the pinned model in the local cache
(scripts/provision_models.py) and runs on the reference laptop. Synthetic speech: it shows the adapter works and
how models compare, not accuracy on real phone audio."""
import inspect
import os
import re
import subprocess
import sys
import textwrap
import types
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from server.config import SttModelConfig, load_settings
from server.pipeline.adapter import FakeAdapter, ModelNotProvisionedError, SttAdapter, SttResult
from server.pipeline.mlx_whisper_adapter import (MlxWhisperAdapter, build_adapter, resolve_snapshot,
                                                 summarize_segments)
from tests.support.speech import RATE, clip, manifest, speech_stream, white

ROOT = Path(__file__).resolve().parents[1]
CONFIG = SttModelConfig(model="org/model", revision="rev")


def seg(text="hello", start=0.0, end=1.0, logprob=-0.2, no_speech=0.0):
    return {"text": f" {text}", "start": start, "end": end, "avg_logprob": logprob, "no_speech_prob": no_speech,
            "compression_ratio": 1.0}


# --- the fixed contract ---------------------------------------------------------------------


def test_the_adapter_method_is_exactly_the_documented_contract():
    """transcribe_window(device_id, window_id, audio) -> {text, stt_confidence}; the same on every adapter."""
    for cls in (FakeAdapter, MlxWhisperAdapter):
        params = list(inspect.signature(cls.transcribe_window).parameters)
        assert params == ["self", "device_id", "window_id", "audio"], cls
    assert set(SttResult.__dataclass_fields__) == {"text", "stt_confidence"}
    for cls in (FakeAdapter, MlxWhisperAdapter):
        assert cls.resource_type == "stt" and cls.runtime in ("mlx", "groq", "gemini")  # docs/data-model.md enum


def test_the_fake_adapter_returns_the_documented_shape_and_is_deterministic():
    fake = FakeAdapter(confidence=0.8)
    fake.load()
    first = fake.transcribe_window("aaaaaaaa-1111", 3, np.zeros(16000, np.float32))
    assert isinstance(first, SttResult) and first.text == "fake aaaaaaaa 3" and first.stt_confidence == 0.8
    assert first == fake.transcribe_window("aaaaaaaa-1111", 3, np.ones(16000, np.float32))
    assert fake.calls[0] == ("aaaaaaaa-1111", 3, 16000)


def test_confidence_must_be_between_zero_and_one():
    for bad in (-0.01, 1.01, 5.0):
        with pytest.raises(ValueError):
            SttResult("x", bad)
    SttResult("x", 0.0)
    SttResult("x", 1.0)


def test_empty_speech_is_empty_text_with_zero_confidence():
    result = FakeAdapter(silent_when=lambda d, w: True).transcribe_window("d", 1, np.zeros(16000, np.float32))
    assert (result.text, result.stt_confidence) == ("", 0.0)
    assert summarize_segments([], CONFIG) == SttResult("", 0.0)
    assert summarize_segments([seg("   ")], CONFIG) == SttResult("", 0.0)


def test_the_fake_can_inject_delay_and_failure():
    boom = FakeAdapter(fail_when=lambda d, w: w == 2)
    boom.transcribe_window("d", 1, np.zeros(10, np.float32))
    with pytest.raises(RuntimeError, match="injected failure"):
        boom.transcribe_window("d", 2, np.zeros(10, np.float32))


# --- stt_confidence ---------------------------------------------------------------------------


def test_confidence_is_exp_of_the_mean_log_probability_times_the_speech_probability():
    result = summarize_segments([seg("we ship friday", logprob=-0.2, no_speech=0.0)], CONFIG)
    assert result.text == "we ship friday" and result.stt_confidence == pytest.approx(np.exp(-0.2))
    discounted = summarize_segments([seg(logprob=-0.2, no_speech=0.5)], CONFIG)
    assert discounted.stt_confidence == pytest.approx(np.exp(-0.2) * 0.5)


def test_segments_are_weighted_by_their_duration_and_texts_are_joined():
    result = summarize_segments([seg("first", 0, 3, logprob=-0.1), seg("second", 3, 4, logprob=-0.9)], CONFIG)
    mean = (0.1 * 3 + 0.9 * 1) / 4
    assert result.text == "first second" and result.stt_confidence == pytest.approx(np.exp(-mean))


@pytest.mark.parametrize("logprob,expected", [(0.0, 1.0), (-1.0, np.exp(-1.0)), (-5.0, np.exp(-5.0)), (0.5, 1.0)])
def test_confidence_is_always_in_zero_one(logprob, expected):
    result = summarize_segments([seg(logprob=logprob)], CONFIG)
    assert 0.0 <= result.stt_confidence <= 1.0 and result.stt_confidence == pytest.approx(min(expected, 1.0))


def test_min_confidence_returns_unsure_windows_as_empty():
    unsure = [seg("maybe", logprob=-1.5)]
    assert summarize_segments(unsure, CONFIG).text == "maybe"
    assert summarize_segments(unsure, replace(CONFIG, min_confidence=0.4)) == SttResult("", 0.0)


# --- offline resolution: no download path -----------------------------------------------------


@pytest.mark.parametrize("model,revision", [("", ""), ("org/model", ""), ("", "abc")])
def test_an_unpinned_model_is_an_error_with_instructions(model, revision):
    with pytest.raises(ModelNotProvisionedError, match="no STT model is pinned"):
        resolve_snapshot(model, revision)


def use_cache(monkeypatch, directory: Path) -> None:
    """Point huggingface_hub at ``directory``. Its cache path is read once at import, so the constant is patched
    (an environment variable set now would only work if this test happened to import the library first)."""
    import huggingface_hub.constants as constants
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(directory))
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)  # resolve_snapshot sets it; restored on teardown


def test_a_model_that_is_not_cached_fails_loudly_instead_of_downloading(tmp_path, monkeypatch):
    use_cache(monkeypatch, tmp_path / "hub")
    with pytest.raises(ModelNotProvisionedError) as caught:
        resolve_snapshot("nobody/never-downloaded", "0" * 40)
    assert "not in the local cache" in str(caught.value) and "provision_models.py" in str(caught.value)
    assert not (tmp_path / "hub").exists() or not any((tmp_path / "hub").iterdir())  # and nothing was fetched
    assert os.environ["HF_HUB_OFFLINE"] == "1"


def test_a_cached_snapshot_without_weights_is_rejected(tmp_path, monkeypatch):
    revision = "a" * 40
    hub = tmp_path / "hub"
    snapshot = hub / "models--org--model" / "snapshots" / revision
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}")
    use_cache(monkeypatch, hub)
    with pytest.raises(ModelNotProvisionedError, match="no weight files"):
        resolve_snapshot("org/model", revision)
    (snapshot / "weights.safetensors").write_bytes(b"\0" * 16)  # with weights present it resolves to that directory
    assert Path(resolve_snapshot("org/model", revision)) == snapshot


def test_load_of_an_unprovisioned_adapter_raises_and_transcribing_before_load_is_an_error(tmp_path, monkeypatch):
    use_cache(monkeypatch, tmp_path / "hub")
    adapter = MlxWhisperAdapter(CONFIG)
    with pytest.raises(RuntimeError, match="not loaded"):
        adapter.transcribe_window("d", 1, np.zeros(16000, np.float32))
    with pytest.raises(ModelNotProvisionedError):
        adapter.load()


def test_build_adapter_selects_by_configured_runtime_and_rejects_cloud_runtimes():
    adapter = build_adapter(replace(CONFIG, runtime="mlx"))
    assert isinstance(adapter, MlxWhisperAdapter) and adapter.model_identifier == "org/model"
    for cloud in ("groq", "gemini", "whatever"):
        with pytest.raises(ValueError, match="not available"):
            build_adapter(replace(CONFIG, runtime=cloud))


@pytest.mark.parametrize(("languages", "options"), [
    (("en", "hi"), {"language": "en", "initial_prompt": CONFIG.hindi_prompt}),
    (("en",), {"language": "en"})])
def test_segments_are_decoded_in_english_mode_with_the_hinglish_prompt_when_hindi_is_allowed(languages, options, monkeypatch):
    """English-mode decoding keeps every other language out; the prompt makes Hindi come out romanized (ADR-24)."""
    calls = []
    module = types.SimpleNamespace(transcribe=lambda audio, **kwargs: calls.append(kwargs) or {"segments": []})
    monkeypatch.setitem(sys.modules, "mlx_whisper", module)
    adapter = MlxWhisperAdapter(replace(CONFIG, languages=languages))
    adapter._path = "/local/model"
    assert adapter._transcribe(np.zeros(10, np.float32)) == {"segments": []}
    assert calls == [{"path_or_hf_repo": "/local/model", **options, "verbose": None,
                      "temperature": 0.0, "condition_on_previous_text": False, "word_timestamps": False,
                      "no_speech_threshold": CONFIG.no_speech_threshold,
                      "logprob_threshold": CONFIG.logprob_threshold,
                      "compression_ratio_threshold": CONFIG.compression_ratio_threshold}]


def test_the_runtime_source_has_no_download_call():
    """The only code allowed to download is scripts/provision_models.py."""
    for path in (ROOT / "server").rglob("*.py"):
        text = path.read_text()
        assert "hf_hub_download" not in text and "from_pretrained" not in text, path
        if "snapshot_download" in text:
            assert "local_files_only=True" in text, f"{path} calls snapshot_download without local_files_only"


def test_no_model_identifier_is_hard_coded_outside_configuration():
    """ADR-14: the model is named in config/convene.toml only."""
    literal = re.compile(r"""["'][\w.-]+/[\w.-]*(whisper|distil|parakeet|wav2vec|moonshine)[\w.-]*["']""", re.I)
    offenders = []
    for path in [*(ROOT / "server").rglob("*.py"), *(ROOT / "scripts").glob("*.py")]:
        if path.name == "measure_stt.py":  # the comparison tool lists its candidates on purpose
            continue
        for match in literal.finditer(path.read_text()):
            offenders.append(f"{path.relative_to(ROOT)}: {match.group(0)}")
    assert offenders == []
    settings = load_settings()
    assert settings.stt.model and settings.stt.revision and re.fullmatch(r"[0-9a-f]{40}", settings.stt.revision)


def test_the_model_swap_is_a_config_change_only(tmp_path):
    config = tmp_path / "c.toml"
    config.write_text('[models.stt]\nmodel = "some/other-model"\nrevision = "abc"\n')
    assert load_settings(config, root=tmp_path).stt.model == "some/other-model"
    assert build_adapter(load_settings(config, root=tmp_path).stt).model_identifier == "some/other-model"


# --- the real model (pytest -m model) ---------------------------------------------------------


def _real_adapter() -> MlxWhisperAdapter:
    adapter = build_adapter(load_settings().stt)
    try:
        adapter.load()
    except ModelNotProvisionedError as exc:
        pytest.skip(f"model not provisioned: {exc}")
    return adapter


@pytest.fixture(scope="module")
def real():
    adapter = _real_adapter()
    yield adapter
    adapter.close()


def wer(reference: str, hypothesis: str) -> float:
    norm = lambda t: re.sub(r"[^a-z0-9' ]+", " ", t.lower()).split()  # noqa: E731
    ref, hyp = norm(reference), norm(hypothesis)
    row = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, row[0] = row[0], i
        for j, h in enumerate(hyp, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (r != h))
    return row[len(hyp)] / max(len(ref), 1)


@pytest.mark.model
def test_real_model_transcribes_the_speech_fixtures_within_the_recorded_error_rate(real):
    errors = words = 0
    for entry in manifest():
        result = real.transcribe_window("d", 1, clip(entry["file"][:-4]))
        assert isinstance(result, SttResult) and 0.0 < result.stt_confidence <= 1.0
        ref, hyp = re.sub(r"[^a-z ]+", " ", entry["text"].lower()).split(), re.sub(r"[^a-z ]+", " ", result.text.lower()).split()
        words += len(ref)
        errors += wer(entry["text"], result.text) * len(ref)
    assert errors / words <= 0.05, f"whole-clip word error rate {errors / words:.3f} (recorded in logs/stt.md: 0.000)"


@pytest.mark.model
def test_real_model_returns_the_documented_shape_for_speech_and_for_silence(real):
    speech = real.transcribe_window("d", 1, clip("ship_beta"))
    assert speech.text and "beta" in speech.text.lower() and 0.0 < speech.stt_confidence <= 1.0
    silence = real.transcribe_window("d", 2, np.zeros(16000, np.float32))
    assert isinstance(silence, SttResult)  # Whisper says something on silence: the VAD gate exists for this reason
    assert 0.0 <= silence.stt_confidence <= 1.0


@pytest.mark.model
def test_real_model_runs_from_a_worker_thread_and_concurrent_calls_are_safe(real):
    from concurrent.futures import ThreadPoolExecutor
    audio = clip("release_notes")
    with ThreadPoolExecutor(3) as pool:
        results = list(pool.map(lambda n: real.transcribe_window("d", n, audio), range(6)))
    assert all("release" in r.text.lower() for r in results)


@pytest.mark.model
def test_real_model_loads_and_transcribes_with_the_network_blocked():
    """No socket to a non-loopback address may be opened while resolving, loading, or transcribing."""
    code = textwrap.dedent(f"""
        import socket, sys
        sys.path.insert(0, {str(ROOT)!r})
        real_connect = socket.socket.connect
        attempts = []
        def guarded(self, address, *a, **k):
            host = address[0] if isinstance(address, tuple) else address
            if isinstance(host, str) and host not in ("127.0.0.1", "::1", "localhost") and not host.startswith("/"):
                attempts.append(host); raise OSError("network is disabled for this test")
            return real_connect(self, address, *a, **k)
        socket.socket.connect = guarded
        socket.getaddrinfo = lambda *a, **k: (_ for _ in ()).throw(OSError("DNS is disabled for this test"))
        from server.config import load_settings
        from server.pipeline.mlx_whisper_adapter import build_adapter
        from tests.support.speech import clip
        adapter = build_adapter(load_settings().stt); adapter.load()
        text = adapter.transcribe_window("d", 1, clip("ship_beta")).text
        print("TEXT", text); print("ATTEMPTS", attempts)
    """)
    env = {**os.environ, "HF_HUB_OFFLINE": "1"}
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=180, env=env, cwd=ROOT)
    if "not in the local cache" in proc.stderr:
        pytest.skip("model not provisioned")
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert "beta" in proc.stdout.lower() and "ATTEMPTS []" in proc.stdout


@pytest.mark.model
async def test_real_model_through_the_whole_pipeline_keeps_each_devices_text_with_that_device(real):
    """Two devices speak different fixtures at once; each device's transcripts come back under its own id."""
    from server.config import PipelineConfig
    from server.pipeline.pipeline import SttPipeline
    got = []
    pipeline = SttPipeline(real, replace(PipelineConfig(), queue_max=8), on_window=got.append)  # the default: speech segments
    pipeline.start()
    try:
        pipeline.bind_device("dev-a", "m")
        pipeline.bind_device("dev-b", "m")
        a = speech_stream(["ship_beta", "budget"], lead_s=0.5, pause_s=1.0)
        b = speech_stream(["release_notes", "objection"], lead_s=0.5, pause_s=1.0)
        t = 1_800_000_000.0
        for i in range(0, min(len(a), len(b)) - 319, 320):
            t += 0.02
            for device, audio in (("dev-a", a), ("dev-b", b)):
                pcm = np.clip(audio[i:i + 320] * 32767, -32768, 32767).astype("<i2")
                await pipeline.push(device, pcm, RATE, t)
        assert (await pipeline.drain("m", 60)).drained
    finally:
        await pipeline.stop()
    text = {d: " ".join(o.text for o in got if o.device_id == d and o.status == "ok").lower() for d in ("dev-a", "dev-b")}
    assert "beta" in text["dev-a"] and "release" not in text["dev-a"]
    assert "release" in text["dev-b"] and "beta" not in text["dev-b"]
