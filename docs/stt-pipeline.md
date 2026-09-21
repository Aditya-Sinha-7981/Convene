# STT Pipeline

## Scope

Everything between "continuous per-device audio frames arriving from the Transport Layer" and "a transcribed window handed to the Attribution Layer." Does not include attribution itself (`speaker-attribution.md`) or model selection detail (`models.md`).

## Pipeline stages

```text
per-device audio frames (transport.md)
        |
        v
   VAD gate  ──── window dropped if below threshold, never reaches STT
        |
        v
  windowing (~1s, per device)
        |
        v
   STT queue (per device)
        |
        v
  worker pool (N workers, shared across devices)
        |
        v
  {device_id, window_id, text, stt_confidence, t_start, t_end}
        |
        v
  Attribution Layer (speaker-attribution.md)
```

## VAD gating

Each device maintains its own rolling VAD state, independent of every other device (no shared mutable state — failure/backlog on one device's gate cannot affect another's). A window is only enqueued for STT if the device's own signal clears the VAD confidence threshold for a meaningful portion of that window. This serves two purposes (ADR-05): avoiding wasted STT compute on silence, and acting as the primary (not sole) defense against cross-device audio bleed.

## Windowing

Target ~1 second per window, per device — chosen for STT testability and responsiveness, not because it's a transport requirement (the underlying audio stream is continuous; windowing is a processing choice made entirely on the laptop). Each window carries `device_id`, `window_id` (monotonic per device), `t_start`, `t_end`.

## STT adapter contract

```text
transcribe_window(device_id, window_id, audio) -> { text, stt_confidence }
```

This interface is fixed regardless of which model backs it (ADR-14) — swapping the underlying STT model (see `models.md`) never requires touching any code upstream (VAD/windowing) or downstream (attribution) of this contract.

## Worker pool and scheduling

A single-process worker pool (not one STT instance per device) pulls from a shared queue, prioritized round-robin across devices so no one device can starve another under load. This is a deliberate choice over "one STT process per phone," which would not scale CPU/GPU contention gracefully at 5–10 concurrent devices on one laptop (see `models.md` for the resource budget this assumes).

Priority rule: STT always has scheduling priority over summarization/RAG LLM calls (ADR-06's local-first choice means all of these share the same machine's compute) — a live transcript falling behind is more visible and more damaging to the demo than a summarization request taking an extra second.

## Failure handling

- A single window failing to transcribe (model error, corrupt audio) is marked `failed`, logged as a `model_error` AuditEvent, and does not block subsequent windows for that device or any other.
- If the STT queue for a device grows unboundedly (worker pool falling behind), that is a load signal, not a silent failure — surfaced on the dashboard's connection-health panel as backlog, per `frontend.md`.

## What this pipeline deliberately does not do

- It does not decide who is speaking — that is entirely the Attribution Layer's job, using this stage's output plus (for shared devices) a separate speaker-embedding classification, per `speaker-attribution.md`.
- It does not persist raw audio by default — only text and metadata cross into the `Utterance` table (`data-model.md`).
