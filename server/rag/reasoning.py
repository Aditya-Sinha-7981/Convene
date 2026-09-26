"""The ``reasoning`` resource type: a local LLM behind one ``generate(...)`` call (CON-09; reused by CON-10).

    generate(messages, max_tokens, temperature, timeout) -> Generation(text, prompt_tokens, completion_tokens,
                                                                       duration_s, finish_reason)

``messages`` are chat messages (``{"role": "system" | "user", "content": str}``). The call is synchronous and
runs on a worker thread; callers ``await priority.wait_for_turn("reasoning")`` first so STT keeps priority.
MLX inference cannot be preempted, so ``timeout`` is enforced between generated tokens: the deadline stops
decoding and raises ``GenerationTimeout``. One generation runs at a time (a lock), so a second question waits;
the deadline starts when the call begins, so time spent waiting for the lock counts. A single step that overruns
(a slow prompt prefill) cannot be interrupted, but the next caller gives up at its own deadline instead of
queueing behind it indefinitely.

The model and revision come from ``[models.reasoning]`` (ADR-14). Weights resolve from the local cache only;
a missing snapshot fails ``load()`` with an instruction to provision it while online.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol, Sequence

from ..config import ReasoningModelConfig
from ..pipeline.adapter import ModelNotProvisionedError


@dataclass(frozen=True)
class Generation:
    text: str
    prompt_tokens: int
    completion_tokens: int
    duration_s: float
    finish_reason: str          # "stop" (the model ended its answer) or "length" (max_tokens reached)


class GenerationTimeout(TimeoutError):
    """The deadline passed while generating; the partial output is discarded."""


class ReasoningAdapter(Protocol):
    resource_type: str
    model_identifier: str
    runtime: str

    def load(self) -> None: ...
    def generate(self, messages: Sequence[dict], max_tokens: int, temperature: float, timeout: float) -> Generation: ...
    def count_tokens(self, messages: Sequence[dict]) -> int: ...
    def close(self) -> None: ...


class FakeReasoningAdapter:
    """Deterministic stand-in. ``respond(messages) -> str`` builds the answer; ``fail``/``delay_s`` inject faults."""
    resource_type = "reasoning"
    runtime = "mlx"

    def __init__(self, respond: Callable[[Sequence[dict]], str] | None = None, *, delay_s: float = 0.0,
                 fail: Exception | None = None, model_identifier: str = "fake/reasoning"):
        self.respond = respond or (lambda messages: "The excerpts answer this.")
        self.delay_s, self.fail, self.model_identifier = delay_s, fail, model_identifier
        self.calls: list[list[dict]] = []
        self.loaded = False
        self._lock = threading.Lock()

    def load(self) -> None:
        self.loaded = True

    def close(self) -> None:
        self.loaded = False

    def count_tokens(self, messages) -> int:
        return sum(len(message["content"].split()) for message in messages)

    def generate(self, messages, max_tokens, temperature, timeout) -> Generation:
        with self._lock:
            began = time.monotonic()
            self.calls.append([dict(message) for message in messages])
            if self.delay_s:
                time.sleep(min(self.delay_s, timeout))
                if self.delay_s >= timeout:
                    raise GenerationTimeout(f"no answer within {timeout:.1f} s")
            if self.fail is not None:
                raise self.fail
            text = self.respond(messages)
            return Generation(text, self.count_tokens(messages), len(text.split()),
                              time.monotonic() - began, "stop")


def resolve_snapshot(config: ReasoningModelConfig) -> str:
    """The local directory of the pinned snapshot. Never touches the network."""
    if not config.model or not config.revision:
        raise ModelNotProvisionedError(
            "no reasoning model is pinned: set [models.reasoning] model and revision in config/convene.toml "
            "(scripts/provision_models.py --resource reasoning shows what to pin)")
    os.environ["HF_HUB_OFFLINE"] = "1"
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import LocalEntryNotFoundError
    try:
        path = snapshot_download(repo_id=config.model, revision=config.revision, local_files_only=True)
    except (LocalEntryNotFoundError, OSError, ValueError) as exc:
        raise ModelNotProvisionedError(
            f"the reasoning model {config.model} (revision {config.revision}) is not in the local cache and the "
            "server never downloads models. Run `.venv/bin/python scripts/provision_models.py --resource reasoning` "
            f"while online, once. ({type(exc).__name__})") from exc
    if not any(name.endswith(".safetensors") for name in os.listdir(path)):
        raise ModelNotProvisionedError(f"the cached snapshot {path} has no weight files; provision it again")
    return path


class MlxLmAdapter:
    """``mlx-lm`` on Apple Silicon. The model stays resident after ``load()`` (CON-09: no cold load mid-demo)."""
    resource_type = "reasoning"
    runtime = "mlx"

    def __init__(self, config: ReasoningModelConfig):
        self.config = config
        self.model_identifier = config.model
        self._model = self._tokenizer = None
        self._lock = threading.Lock()
        self.load_seconds: float | None = None

    def load(self) -> None:
        if self._model is not None:
            return  # already resident
        started = time.perf_counter()
        path = resolve_snapshot(self.config)
        from mlx_lm import load
        self._model, self._tokenizer = load(path)
        # Warm up: compile kernels and page the weights in now, not at the first question.
        self.generate([{"role": "user", "content": "Reply with OK."}], max_tokens=2, temperature=0.0, timeout=120)
        self.load_seconds = time.perf_counter() - started

    def close(self) -> None:
        self._model = self._tokenizer = None

    def count_tokens(self, messages) -> int:
        """Prompt length in model tokens, with the chat template applied exactly as ``generate`` does."""
        if self._tokenizer is None:
            raise RuntimeError("the reasoning model is not loaded")
        return len(self._tokenizer.apply_chat_template(list(messages), add_generation_prompt=True))

    def generate(self, messages, max_tokens, temperature, timeout) -> Generation:
        if self._model is None:
            raise RuntimeError("the reasoning model is not loaded")
        from mlx_lm import stream_generate
        from mlx_lm.sample_utils import make_sampler
        began = time.monotonic()
        deadline = began + timeout
        if not self._lock.acquire(timeout=timeout):
            raise GenerationTimeout(f"an earlier answer was still generating after {timeout:.1f} s")
        try:
            prompt = self._tokenizer.apply_chat_template(list(messages), add_generation_prompt=True)
            parts, last = [], None
            for response in stream_generate(self._model, self._tokenizer, prompt, max_tokens=max_tokens,
                                            sampler=make_sampler(temp=temperature)):
                parts.append(response.text)
                last = response
                if time.monotonic() > deadline:
                    raise GenerationTimeout(f"no complete answer within {timeout:.1f} s")
            return Generation("".join(parts), last.prompt_tokens if last else len(prompt),
                              last.generation_tokens if last else 0, time.monotonic() - began,
                              (last.finish_reason if last else None) or "stop")
        finally:
            self._lock.release()


def build_reasoning_adapter(config: ReasoningModelConfig):
    if config.runtime == "mlx":
        return MlxLmAdapter(config)
    raise ValueError(f"reasoning runtime {config.runtime!r} is not available; only 'mlx' is wired. Cloud backends "
                     "are a manual, deliberate fallback and are not implemented (docs/models.md, ADR-06).")
