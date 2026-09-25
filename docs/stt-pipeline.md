# STT Pipeline

## Scope

Everything between "continuous per-device audio frames arriving from the Transport Layer" and "a transcribed window handed to the Attribution Layer." Does not include attribution itself (`speaker-attribution.md`) or model selection detail (`models.md`).

**Implementation status (through CON-07):** implemented and covered by automated tests with a fake model and, on the reference laptop, with the real model (`pytest -m model`). Measured on the reference laptop with synthetic phones and synthetic speech (`logs/stt.md`). A first real-phone run on 2026-09-25 produced attributed live transcript rows on the dashboard; duration, noise, multi-device isolation, and language quality remain unmeasured. The default `language = "auto"` asks MLX Whisper to detect each segment's language; explicit codes such as `en` remain available for a deliberately monolingual run. Its output callback (`Runtime.on_transcribed_window`) now feeds CON-06's `Utterance` attribution service and the CON-07 live dashboard.

## Pipeline stages

```text
per-device audio frames (transport.md, the AudioSink seam)
        |
        v
  resample to 16 kHz mono (per device, stateful)
        |
        v
   VAD gate  ──── audio the VAD calls non-speech never reaches STT
        |
        v
  segmenting (per device): one segment per stretch of speech, ended by a pause (capped)
        |
        v
   STT queue (per device, bounded)
        |
        v
  worker pool (N workers, shared across devices, round robin)
        |
        v
  {device_id, window_id, text, stt_confidence, t_start, t_end}   (window_id numbers a device's segments)
        |
        v
  Attribution Layer (speaker-attribution.md)
```

The transport hands each decoded frame to the pipeline through the `AudioSink` seam (`transport.md`); everything up to the queue is cheap numpy work on the event loop and never waits on a model.

## Audio preparation

The transport delivers mono 16-bit PCM at the received rate (48 kHz for browsers). Each device has its own `Resampler` (PyAV, which ships with aiortc) that converts to float32 at the model rate, 16 kHz, keeping filter state across chunks. A change of the incoming rate mid-stream starts a new converter instead of mixing rates. Empty and very short chunks are safe. Nothing is written to disk: windows live in memory until transcribed and there is no table that stores samples.

## VAD gating

Each device maintains its own rolling VAD state, independent of every other device (no shared mutable state — failure/backlog on one device's gate cannot affect another's). A window or segment is only enqueued for STT if the device's own signal is voiced for at least `min_speech_fraction` of its frames. This serves two purposes (ADR-05): avoiding wasted STT compute on silence, and acting as the primary (not sole) defense against cross-device audio bleed.

**The method is an adaptive energy gate with no dependency** (`server/pipeline/vad.py`). Each 20 ms frame's level (dBFS RMS) is compared with the larger of an absolute floor (`vad_min_db`) and the device's own background level plus a margin (`vad_margin_db`). The background level is the `vad_noise_percentile` (10th) percentile of the frame levels of the last `vad_noise_window_s` (5 s): speech has pauses, steady noise does not, so a low percentile tracks the room. A short hangover bridges brief pauses inside speech, and until 0.2 s of audio has been seen no frame is called speech.

Measured behavior on synthetic material (`logs/stt.md`, `tests/test_vad.py`): digital silence and steady noise (white, hum, loud fan) are gated, speech from about -18 to -46 dBFS passes, and speech over pink noise at 10 to 20 dB SNR mostly passes (about 90%) while a small share of pause windows in that noise still passes. A design that only learned its floor from frames it had already gated never adapted to steady noise (every window passed); that was found by measurement and fixed. The margin is a real trade-off: a larger margin gates more noise and loses quiet or distant speech, which is the cross-device bleed case, so it is left for real bleed data (CON-15). No thresholds have been tuned on real phone audio.

## Segmenting and the time base

**Default: speech segments (ADR-19).** The pipeline follows each device's own VAD and sends one *segment* per stretch of speech (`server/pipeline/segmenting.py`), not one window per second. A segment starts when speech starts, with a short pre-roll (`segment_pre_roll_ms`, 200 ms) so the first word is not clipped; it ends after a pause of `segment_end_silence_ms` (600 ms), with a short tail (`segment_tail_ms`); and it is capped at `segment_max_ms` (8 s). A stretch that reaches the cap is cut at the quietest frame in its last `segment_cut_search_ms` (1.5 s), which is usually a gap between words, and the remainder starts the next segment, so a long monologue still produces text on time and no audio is lost or repeated at the cut. A voiced burst shorter than `segment_min_ms` (300 ms), such as a click or a cough, is counted as gated and not transcribed. A gap in the stream (a reconnect) ends the open segment first; no segment spans it. When a device's stream ends (its peer closes) or a meeting is drained, the open segment is sent.

Why: a model call costs about the same for 1 s of audio as for 8 s, so fixed 1 s windows overloaded the model at five phones and cut words in half. Measured on the same audio with the real model (`logs/stt.md`): segments gave one line per sentence, word error rate 0.000 on all three test streams against 0.156 to 0.219 for 1 s windows and 0.031 to 0.078 for 3 s windows, with 3 to 6 times fewer model calls than 1 s windows. The cost is delay: a segment is sent about `segment_end_silence_ms` (plus the gate's 100 ms hangover) after the speaker stops, plus the model's ~0.5 s, so a line appears roughly 1.2 s after someone finishes a sentence, instead of streaming word by word.

**Fallback: fixed windows.** Setting `segmentation = "fixed"` in `[pipeline]` restores the earlier design: a window of `window_ms` (~1 s) per device, each gated by the VAD (`server/pipeline/windowing.py`). Nothing else changes; it needs no code change. Window ids are monotonic per device, and windows or segments the gate drops still consume an id, so a hole in the numbering shows where the gate closed.

Each unit carries `device_id`, `window_id`, `t_start`, `t_end`, the sample rate and sample count, plus `speech_fraction` (the share of its 20 ms frames the VAD called speech) and, for segments, `strength_db` (the mean margin of its voiced frames over the device's background).

**Time base:** `t_start` is the server wall clock at frame receipt (UTC, ISO 8601, millisecond precision) plus the number of samples since the anchor. The anchor is re-taken whenever the derived time drifts from the wall clock by more than `resync_s` (0.25 s). This is the time the server *received* the audio, not when it was spoken: it includes network and jitter-buffer delay. Stated accuracy: within the receipt delay of a frame and, after a resync, within 0.25 s of the wall clock. Cross-device ordering by `t_start` relies on this shared server clock and is only as accurate as those two bounds.

## STT adapter contract

```text
transcribe_window(device_id, window_id, audio) -> { text, stt_confidence }
```

This interface is fixed regardless of which model backs it (ADR-14) — swapping the underlying STT model (see `models.md`) never requires touching any code upstream (VAD/windowing) or downstream (attribution) of this contract. `audio` is float32 mono at 16 kHz. The method is synchronous and runs on a scheduler worker thread, because inference blocks. Empty speech is `text = ""` with `stt_confidence = 0.0`. The adapter also has `load()` (local cache only; never downloads) and `close()`, and reports its `model_identifier` and `runtime` from configuration.

**`stt_confidence`** is an uncalibrated score in [0, 1], not a probability. For the `mlx` runtime it is `exp(duration-weighted mean of the segments' avg_logprob) × (1 − duration-weighted mean of no_speech_prob)`, clamped to [0, 1]. It ranks how certain the model was of the tokens it chose. **It cannot tell speech from non-speech**: measured, Whisper returns "Thank you." on silence, steady noise, hum and clicks with a confidence of 0.6 to 0.8 and a `no_speech_prob` of 0.00, indistinguishable from a real window. The VAD gate is the defense, not this score.

**Hallucination handling.** Because the score cannot catch it, a stock phrase (`hallucination_blocklist`, for example "thank you", "thanks for watching", "bye") is dropped only when the VAD barely accepted the audio: a fixed window that was mostly non-speech (speech fraction below `hallucination_max_speech_fraction`), or a segment whose voiced frames were only just above the background (`strength_db` below `hallucination_min_strength_db`, 10 dB; measured: steady noise that slips through the gate sits at 5 to 9 dB, normal speech at 11 to 28 dB). The same words said clearly are kept; a genuine, quiet "thank you" in a noisy room can be dropped, which is the price of the filter. **Known limit:** noise that surges (fans, traffic) produced segments at 8 to 20 dB, indistinguishable from speech by energy alone, so such noise can still yield false lines; a model-based VAD would be the next step if real-phone testing shows it matters. Suppressed windows are counted (`suppressed`). Decoding runs at temperature 0 with `condition_on_previous_text` off, so latency is bounded and windows do not contaminate each other.

## Worker pool and scheduling

A single-process worker pool (not one STT instance per device) pulls from bounded per-device queues, served round robin across devices so no one device can starve another under load. This is a deliberate choice over "one STT process per phone," which would not scale CPU/GPU contention gracefully at 5–10 concurrent devices on one laptop (see `models.md` for the resource budget this assumes).

- **Fairness:** devices with queued windows sit in a ring; each turn takes one window from the next device, so a device that floods gets one turn per rotation, exactly like a quiet one (tested).
- **Load:** with segments the unit of work is a stretch of speech, so five phones talking produce roughly one call per sentence instead of one per second; the 2-calls-per-second ceiling is far less likely to be reached.
- **Workers:** `workers` is 1 by default. Measured on the reference laptop, inference is GPU-bound: 2 to 4 workers gave no extra throughput (1.94, 2.03, 2.05 windows/s) and only longer per-window latency.
- **Overload policy: drop oldest.** When a device's queue holds `queue_max` windows, the oldest queued window is dropped so the live view stays fresh. Every drop increments that device's counter, emits a `stt_window_dropped` audit event, and is reported to the window callback with status `dropped`. Nothing is dropped silently.
- **Backlog** is published per device: `stt_backlog` (queued plus in-flight windows) and `stt_dropped_windows` in the live gauges (`data-model.md`), and queue depth with the age of the oldest window from the scheduler.
- **`drain(meeting_id, timeout)`** flushes the meeting's partial final windows, then waits until every queued and in-flight window of the meeting has finished and its callback has run, or the timeout passes (summarization uses it before reading the transcript).
- **Timeouts and worker faults:** a window that takes longer than `window_timeout_s` is marked failed. (A blocked model call cannot be interrupted, so its thread stays busy until it returns.) A worker that hits an unexpected internal error is restarted and the window it held is reported as failed.

Priority rule: STT always has scheduling priority over summarization/RAG LLM calls (ADR-06's local-first choice means all of these share the same machine's compute) — a live transcript falling behind is more visible and more damaging to the demo than a summarization request taking an extra second. MLX inference is not preemptible, so priority is cooperative and applies before a job starts: the reasoning adapter (CON-09) and the embedding worker (CON-08) call `await priority.wait_for_turn("reasoning")` first. While the STT backlog is above `priority_high_backlog_windows` they wait, but never longer than `priority_max_wait_s`; a job already running is not interrupted. This reduces interference and cannot remove it (CON-15 measures it).

## Failure handling

- A single window failing to transcribe (model error, timeout, corrupt audio) is marked `failed`, recorded as a `model_error` AuditEvent (component, model, device, window, reason), and does not block subsequent windows for that device or any other. A `ModelExecution` row is written for every invocation, failed or not.
- If the STT queue for a device grows unboundedly (worker pool falling behind), that is a load signal, not a silent failure — bounded by the overload policy above, counted, audited, and surfaced on the dashboard's connection-health panel as backlog, per `frontend.md`.
- A model that fails to load stops the server at startup with a message; the server never starts half-working, and it never downloads a model.

## Decision: segments instead of ~1 s windows (ADR-19)

This document used to fix ~1 s windows. Measurement on the reference laptop showed that one worker sustains about 2 model calls per second regardless of audio length, that 1 s windows overloaded the model at five phones (55% of windows dropped when all five talked), and that cutting speech every second breaks words (word error rate 0.094 to 0.219 for 1 s windows against 0.000 for whole sentences, with fragments such as "very tough. height." for "very tight."). The project lead approved replacing them with speech segments (ADR-19; `logs/stt.md` has the numbers). Fixed windows remain as the `segmentation = "fixed"` fallback. All figures are from synthetic speech and synthetic phones; how the segmenter's pause detection behaves on real phone audio is checked by the manual tests in `manual-tests.md`.

## What this pipeline deliberately does not do

- It does not decide who is speaking — that is entirely the Attribution Layer's job, using this stage's output plus (for shared devices) a separate speaker-embedding classification, per `speaker-attribution.md`.
- It does not persist raw audio by default — only text and metadata cross into the `Utterance` table (`data-model.md`). There is no debug flag that saves audio.
- It does not call a cloud model. Cloud STT is a documented manual fallback behind the same interface (`models.md`) and is not wired.
