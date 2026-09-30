"""Demo-only cloud STT: Google Gemini behind the same ``transcribe_window`` contract (ADR-06, ADR-31).

Selected only when the operator starts the server with ``CONVENE_STT=gemini`` (``config.apply_stt_environment``);
the default stays the local mlx-whisper adapter and nothing ever falls back to the cloud automatically. Each
speech segment the VAD passes becomes one ``generateContent`` request carrying the segment as a 16-bit WAV. The
reply is constrained to JSON ``{"text": ...}``.

Only English and Latin-script Hindi may come out (ADR-24). The prompt says so with right and wrong examples, and
because a model can still answer in Devanagari, every reply is passed through ``romanize`` (Devanagari to Hinglish
Latin letters) before it is returned. Translation into English cannot be caught mechanically; the prompt is the only
defence against it. ``stt_confidence`` is ``exp(avgLogprobs)`` when the API reports it, else a fixed
``DEFAULT_CONFIDENCE``: like Whisper's score it ranks segments and is not a calibrated probability.

The API key travels only in the ``x-goog-api-key`` header. It is never logged, stored, or put in an error message.
Stdlib HTTP only, so the connector adds no dependency.
"""
import base64
import io
import json
import math
import time
import urllib.error
import urllib.request
import wave

import numpy as np

from ..config import SttModelConfig
from .adapter import SttResult
from .romanize import romanize

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_CONFIDENCE = 0.9
REQUEST_TIMEOUT_S = 15.0       # under [pipeline].window_timeout_s, so the scheduler sees our error, not its timeout
RETRY_STATUSES = (429, 500, 503)
RETRY_DELAY_S = 1.0


class GeminiSttError(RuntimeError):
    """A request failed. The message names the HTTP status and Google's reason, never the key."""


def wav_bytes(audio: np.ndarray, sample_rate: int = 16000) -> bytes:
    """Float32 mono in [-1, 1] as a 16-bit PCM WAV file."""
    pcm = (np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0) * 32767.0).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(pcm.tobytes())
    return buffer.getvalue()


def instruction(config: SttModelConfig) -> str:
    rules = ["You are a speech-to-text engine for a meeting. Write down exactly the words that are spoken, in the "
             "order they are spoken. You are a transcriber, not a translator.",
             "Output only Latin (English) letters. Never output Devanagari or any other script.",
             "Do not translate, summarize, answer, correct, or add commentary, speaker names, or timestamps.",
             'If there is no intelligible speech (silence, noise, music), return {"text": ""}.']
    if "hi" in config.languages:
        rules += ["Speakers mix Hindi and English (Hinglish). Keep every word in the language it was spoken in: "
                  "English words stay English, and Hindi words are written phonetically in Latin letters, the way "
                  "people type Hindi in chat messages.",
                  'Example. Spoken: "kya kar rahe ho, meeting kab start hogi?" '
                  'Correct: {"text": "kya kar rahe ho, meeting kab start hogi?"} '
                  'Wrong (translated): {"text": "what are you doing, when will the meeting start?"} '
                  'Wrong (Devanagari): {"text": "क्या कर रहे हो, मीटिंग कब स्टार्ट होगी?"}',
                  'Example. Spoken: "haan theek hai, main kal deploy kar dunga" '
                  'Correct: {"text": "haan theek hai, main kal deploy kar dunga"}']
    else:
        rules.append("Speech is English. Write it in English.")
    return " ".join(rules)


USER_PROMPT = ("Transcribe this audio word for word. Hindi stays Hindi, written in Latin letters (Hinglish); "
               "English stays English. Do not translate. No Devanagari.")


def parse_reply(body: dict) -> SttResult:
    """Text and confidence from a ``generateContent`` reply. Pure, so it is testable without the network."""
    candidates = body.get("candidates") or []
    if not candidates:
        reason = (body.get("promptFeedback") or {}).get("blockReason")
        if reason:
            raise GeminiSttError(f"Gemini returned no transcript (blocked: {reason})")
        return SttResult("", 0.0)
    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    raw = "".join(part.get("text", "") for part in parts if not part.get("thought")).strip()
    if not raw:
        return SttResult("", 0.0)
    try:
        value = json.loads(raw)
        text = value.get("text", "") if isinstance(value, dict) else ""
    except ValueError:
        text = raw  # the schema was ignored; the plain reply is still the transcript
    text = " ".join(romanize(str(text)).split())  # ADR-24 backstop: a model that still returns Devanagari
    if not text:
        return SttResult("", 0.0)
    logprob = candidate.get("avgLogprobs")
    confidence = DEFAULT_CONFIDENCE
    if isinstance(logprob, (int, float)) and not math.isnan(logprob):
        confidence = float(min(1.0, max(0.0, math.exp(logprob))))
    return SttResult(text, confidence)


class GeminiSttAdapter:
    resource_type = "stt"
    runtime = "gemini"

    def __init__(self, config: SttModelConfig, *, api_key: str, sample_rate: int = 16000, opener=None):
        if not api_key:
            raise ValueError("the Gemini STT connector needs GEMINI_API_KEY")
        self.config = config
        self.model_identifier = config.model
        self.sample_rate = sample_rate
        self._key = api_key
        self._open = opener or urllib.request.urlopen
        self._thinking_off = True   # 2.5 Flash models accept a zero thinking budget; dropped if a model refuses it
        self._ready = False
        self.load_seconds: float | None = None

    def __repr__(self) -> str:  # never show the key
        return f"GeminiSttAdapter(model={self.model_identifier!r})"

    def load(self) -> None:
        """One real request on a second of silence: fails startup loudly on a bad key, model, or network."""
        started = time.perf_counter()
        try:
            self._request(np.zeros(self.sample_rate // 2, dtype=np.float32))
        except GeminiSttError as exc:
            if not self._thinking_off or "thinking" not in str(exc).lower():
                raise
            self._thinking_off = False
            self._request(np.zeros(self.sample_rate // 2, dtype=np.float32))
        self._ready = True
        self.load_seconds = time.perf_counter() - started

    def close(self) -> None:
        self._ready = False

    def payload(self, audio: np.ndarray) -> dict:
        generation = {"temperature": 0.0, "responseMimeType": "application/json",
                      "responseSchema": {"type": "OBJECT", "properties": {"text": {"type": "STRING"}},
                                         "required": ["text"]}}
        if self._thinking_off:
            generation["thinkingConfig"] = {"thinkingBudget": 0}
        return {
            "systemInstruction": {"parts": [{"text": instruction(self.config)}]},
            "contents": [{"role": "user", "parts": [
                {"inlineData": {"mimeType": "audio/wav",
                                "data": base64.b64encode(wav_bytes(audio, self.sample_rate)).decode("ascii")}},
                {"text": USER_PROMPT}]}],
            "generationConfig": generation,
        }

    def _request(self, audio: np.ndarray) -> SttResult:
        body = json.dumps(self.payload(audio)).encode("utf-8")
        url = ENDPOINT.format(model=urllib.request.quote(self.model_identifier, safe="-._"))
        for attempt in (1, 2):
            request = urllib.request.Request(url, data=body, method="POST", headers={
                "Content-Type": "application/json", "x-goog-api-key": self._key})
            try:
                with self._open(request, timeout=REQUEST_TIMEOUT_S) as response:
                    return parse_reply(json.loads(response.read().decode("utf-8")))
            except urllib.error.HTTPError as exc:
                if exc.code in RETRY_STATUSES and attempt == 1:
                    time.sleep(RETRY_DELAY_S)
                    continue
                raise GeminiSttError(f"Gemini STT HTTP {exc.code}: {_reason(exc)}") from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise GeminiSttError(f"Gemini STT request failed: {type(exc).__name__}: "
                                     f"{getattr(exc, 'reason', exc)}") from None
        raise GeminiSttError("Gemini STT request failed")  # unreachable: the loop returns or raises

    def transcribe_window(self, device_id: str, window_id: int, audio: np.ndarray) -> SttResult:
        if not self._ready:
            raise RuntimeError("the Gemini STT connector is not loaded")
        return self._request(np.ascontiguousarray(audio, dtype=np.float32))


def _reason(exc: urllib.error.HTTPError) -> str:
    try:
        detail = json.loads(exc.read().decode("utf-8")).get("error", {}).get("message", "")
    except Exception:
        detail = ""
    return (detail or exc.reason or "no detail")[:300]
