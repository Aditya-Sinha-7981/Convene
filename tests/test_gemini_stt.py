"""The demo-only Gemini STT connector (ADR-31) and its environment switch. No network: HTTP is faked."""
import io
import json
import urllib.error
import wave
from dataclasses import replace

import numpy as np
import pytest

from server.config import ConfigError, SttModelConfig, apply_stt_environment, load_settings
from server.pipeline.adapter import SttResult
from server.pipeline.gemini_adapter import GeminiSttAdapter, GeminiSttError, parse_reply, wav_bytes

CONFIG = SttModelConfig(runtime="gemini", model="gemini-test-model")
SECRET = "sk-test-SECRET-key"
AUDIO = (np.sin(np.linspace(0, 400, 16000)) * 0.3).astype(np.float32)


def reply(text, logprob=None):
    candidate = {"content": {"parts": [{"text": json.dumps({"text": text})}]}}
    if logprob is not None:
        candidate["avgLogprobs"] = logprob
    return {"candidates": [candidate]}


class FakeHttp:
    """Records every request; answers from a list of replies (dicts) or HTTP status codes (ints)."""

    def __init__(self, *answers):
        self.answers, self.requests = list(answers), []

    def __call__(self, request, timeout):
        self.requests.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, int):
            body = io.BytesIO(json.dumps({"error": {"message": f"thinking budget is not supported ({answer})"}}).encode())
            raise urllib.error.HTTPError(request.full_url, answer, "error", {}, body)
        return io.BytesIO(json.dumps(answer).encode())


def adapter(*answers):
    http = FakeHttp(*answers)
    return GeminiSttAdapter(CONFIG, api_key=SECRET, opener=http), http


def test_wav_is_16_bit_mono_at_the_pipeline_rate():
    with wave.open(io.BytesIO(wav_bytes(AUDIO)), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()) == (1, 2, 16000, 16000)


def test_parse_reply():
    assert parse_reply(reply("  hello   there ")) == SttResult("hello there", 0.9)
    assert parse_reply(reply("haan theek hai", logprob=-0.1)).stt_confidence == pytest.approx(0.9048, abs=1e-3)
    assert parse_reply(reply("")) == SttResult("", 0.0)
    assert parse_reply({"candidates": []}) == SttResult("", 0.0)
    assert parse_reply({"candidates": [{"content": {"parts": [{"text": "plain words"}]}}]}).text == "plain words"
    with pytest.raises(GeminiSttError, match="blocked"):
        parse_reply({"promptFeedback": {"blockReason": "SAFETY"}})


def test_request_carries_audio_and_key_in_a_header_only():
    stt, http = adapter(reply(""), reply("we ship friday"))
    stt.load()
    assert stt.transcribe_window("device", 1, AUDIO) == SttResult("we ship friday", 0.9)
    request = http.requests[-1]
    assert request.get_header("X-goog-api-key") == SECRET and SECRET not in request.full_url
    assert request.full_url.endswith("/models/gemini-test-model:generateContent")
    body = json.loads(request.data)
    part = body["contents"][0]["parts"][0]["inlineData"]
    assert part["mimeType"] == "audio/wav" and len(part["data"]) > 40000
    assert body["generationConfig"]["temperature"] == 0.0 and "Latin letters" in body["systemInstruction"]["parts"][0]["text"]
    assert SECRET not in repr(stt)


def test_english_only_prompt():
    stt = GeminiSttAdapter(replace(CONFIG, languages=("en",)), api_key=SECRET, opener=FakeHttp(reply("")))
    assert "Hindi" not in stt.payload(AUDIO)["systemInstruction"]["parts"][0]["text"]


def test_not_loaded_is_an_error():
    stt, _ = adapter(reply("x"))
    with pytest.raises(RuntimeError, match="not loaded"):
        stt.transcribe_window("device", 1, AUDIO)


def test_load_drops_the_thinking_budget_when_the_model_refuses_it():
    stt, http = adapter(400, reply(""))
    stt.load()
    assert "thinkingConfig" in json.loads(http.requests[0].data)["generationConfig"]
    assert "thinkingConfig" not in json.loads(http.requests[1].data)["generationConfig"]
    assert stt.load_seconds is not None


def test_rate_limit_is_retried_once_then_fails_without_the_key(monkeypatch):
    monkeypatch.setattr("server.pipeline.gemini_adapter.RETRY_DELAY_S", 0)
    stt, http = adapter(reply(""), 429, reply("after retry"))
    stt.load()
    assert stt.transcribe_window("d", 1, AUDIO).text == "after retry"
    stt, http = adapter(reply(""), 429)
    stt.load()
    with pytest.raises(GeminiSttError, match="HTTP 429") as caught:
        stt.transcribe_window("d", 1, AUDIO)
    assert SECRET not in str(caught.value)


def test_environment_switch(tmp_path):
    settings = load_settings(root=tmp_path)
    assert apply_stt_environment(settings, {}) is settings
    assert apply_stt_environment(settings, {"CONVENE_STT": "local", "GEMINI_API_KEY": "k"}) is settings
    chosen = apply_stt_environment(settings, {"CONVENE_STT": "gemini", "GEMINI_API_KEY": SECRET,
                                              "GEMINI_STT_MODEL": "gemini-2.5-flash"})
    assert (chosen.stt.runtime, chosen.stt.model, chosen.pipeline.workers) == ("gemini", "gemini-2.5-flash", 4)
    assert SECRET not in repr(chosen)
    assert apply_stt_environment(settings, {"CONVENE_STT": "gemini", "GEMINI_API_KEY": "k", "GEMINI_STT_MODEL": "m",
                                            "CONVENE_STT_WORKERS": "2"}).pipeline.workers == 2
    for bad in ({"CONVENE_STT": "groq"}, {"CONVENE_STT": "gemini", "GEMINI_API_KEY": "k"},
                {"CONVENE_STT": "gemini", "GEMINI_STT_MODEL": "m"},
                {"CONVENE_STT": "gemini", "GEMINI_API_KEY": "k", "GEMINI_STT_MODEL": "m", "CONVENE_STT_WORKERS": "0"}):
        with pytest.raises(ConfigError):
            apply_stt_environment(settings, bad)
