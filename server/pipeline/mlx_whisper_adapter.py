"""The real STT adapter: ``mlx-whisper`` on Apple Silicon, weights from the local cache only.

The model and revision come from ``[models.stt]`` in configuration (ADR-14); nothing here names a model. The
adapter resolves the pinned snapshot with ``local_files_only=True`` and hands the local *directory* to
mlx-whisper, so there is no code path that can contact the network: a missing file fails at load with an
instruction to run ``scripts/provision_models.py`` while online.

``stt_confidence`` (docs/data-model.md, docs/stt-pipeline.md): mlx-whisper returns, per decoded segment,
``avg_logprob`` (mean token log-probability), ``no_speech_prob`` and ``compression_ratio``. The score is

    p       = exp( duration-weighted mean of avg_logprob )     # geometric-mean token probability, in (0, 1]
    score   = p * (1 - duration-weighted mean of no_speech_prob)

clamped to [0, 1]. It is an uncalibrated score, not a probability of being correct: it ranks windows, it does
not say how often a 0.8 window is right. Empty speech is ``text == ""`` with score 0.
"""
import os
import time

import numpy as np

from ..config import SttModelConfig
from .adapter import ModelNotProvisionedError, SttResult


def summarize_segments(segments: list[dict], config: SttModelConfig) -> SttResult:
    """Text and ``stt_confidence`` from mlx-whisper's segments. Pure, so it is testable without a model."""
    kept = [s for s in segments if s.get("text", "").strip()]
    if not kept:
        return SttResult("", 0.0)
    weights = np.array([max(float(s.get("end", 0)) - float(s.get("start", 0)), 1e-3) for s in kept])
    logprob = float(np.average([float(s["avg_logprob"]) for s in kept], weights=weights))
    no_speech = float(np.average([float(s.get("no_speech_prob", 0.0)) for s in kept], weights=weights))
    score = float(np.clip(np.exp(logprob) * (1.0 - no_speech), 0.0, 1.0))
    text = " ".join(s["text"].strip() for s in kept).strip()
    if score < config.min_confidence:
        return SttResult("", 0.0)
    return SttResult(text, score)


def resolve_snapshot(model: str, revision: str) -> str:
    """The local directory of the pinned snapshot, or ``ModelNotProvisionedError``. Never touches the network."""
    if not model or not revision:
        raise ModelNotProvisionedError(
            "no STT model is pinned: set [models.stt] model and revision in config/convene.toml "
            "(scripts/provision_models.py shows what to pin)")
    os.environ["HF_HUB_OFFLINE"] = "1"  # set before huggingface_hub is used: it must not fetch anything
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import LocalEntryNotFoundError
    try:
        path = snapshot_download(repo_id=model, revision=revision, local_files_only=True)
    except (LocalEntryNotFoundError, OSError, ValueError) as exc:
        raise ModelNotProvisionedError(
            f"the STT model {model} (revision {revision}) is not in the local cache and the server never downloads "
            f"models. Run `.venv/bin/python scripts/provision_models.py` while online, once. ({type(exc).__name__})") from exc
    if not any(name.endswith((".safetensors", ".npz")) for name in os.listdir(path)):
        raise ModelNotProvisionedError(f"the cached snapshot {path} has no weight files; re-run scripts/provision_models.py")
    return path


class MlxWhisperAdapter:
    resource_type = "stt"
    runtime = "mlx"

    def __init__(self, config: SttModelConfig):
        self.config = config
        self.model_identifier = config.model
        self._path: str | None = None
        self.load_seconds: float | None = None

    def load(self) -> None:
        """Resolve the weights from the local cache and warm the model up. Raises loudly if it cannot."""
        started = time.perf_counter()
        self._path = resolve_snapshot(self.config.model, self.config.revision)
        self._transcribe(np.zeros(16000, dtype=np.float32))  # loads the weights and compiles the kernels now, not mid-meeting
        self.load_seconds = time.perf_counter() - started

    def close(self) -> None:
        self._path = None

    def _transcribe(self, audio: np.ndarray) -> dict:
        import mlx_whisper
        c = self.config
        # mlx-whisper uses None, not the string "auto", to invoke its multilingual language detector.
        language = None if c.language == "auto" else c.language
        return mlx_whisper.transcribe(
            audio, path_or_hf_repo=self._path, language=language, verbose=None, temperature=0.0,
            condition_on_previous_text=False, word_timestamps=False, no_speech_threshold=c.no_speech_threshold,
            logprob_threshold=c.logprob_threshold, compression_ratio_threshold=c.compression_ratio_threshold)

    def transcribe_window(self, device_id: str, window_id: int, audio: np.ndarray) -> SttResult:
        if self._path is None:
            raise RuntimeError("the STT model is not loaded")
        return summarize_segments(self._transcribe(np.ascontiguousarray(audio, dtype=np.float32))["segments"], self.config)


def build_adapter(config: SttModelConfig):
    """The adapter for ``[models.stt].runtime``. Cloud runtimes are documented but not wired (docs/models.md)."""
    if config.runtime == "mlx":
        return MlxWhisperAdapter(config)
    raise ValueError(f"STT runtime {config.runtime!r} is not available; only 'mlx' is wired. Cloud backends are "
                     "a manual, deliberate fallback and are not implemented (docs/models.md).")
