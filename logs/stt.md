# logs/stt.md

> Workstream: local STT adapter, VAD, windowing and scheduler (CON-05)
> Status: implemented; tested with a fake model and with the real model on the reference laptop. A first real-phone run on 2026-09-25 produced local, attributed live transcript rows; real-phone quality, duration, noise, bleed, and language coverage remain unmeasured. **One decision is waiting on the project lead: the window size (see "Decisions needed").**

Tracked project history. No secrets, private audio or transcripts. The speech used here is synthetic text-to-speech.

---

## Summary (2026-09-22)

| Item | Result |
|---|---|
| Model and runtime chosen from measurement, pinned in `config/convene.toml`, `docs/models.md` updated | **Done**: `mlx-whisper` with `mlx-community/whisper-large-v3-turbo`, revision `a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb` |
| Resampling, per-device VAD gate, windowing with UTC time base | Done |
| Bounded fair scheduler, drop-oldest overload policy with counters and audit, backlog gauges, `drain` | Done |
| Compute-priority hook for reasoning and embedding | Done (mechanism and tests; real interference is CON-15's) |
| Adapter contract, fake adapter, real adapter, `stt_confidence` definition | Done |
| `ModelExecution` migration (`0002`) and one row per invocation; `model_error` and `model_load` audit | Done |
| Startup model load that fails loudly; offline-only resolution; `scripts/provision_models.py` | Done, verified with the network blocked |
| `scripts/measure_stt.py` (models and pipeline at 1, 2, 5 devices) | Done, results below |
| Automated tests | **412 default tests pass** (110 new; 302 before), plus **6 tests marked `model` pass** on the reference laptop |
| Mutation check | 23 deliberate breakages of the STT code were each caught (one was missed at first, see Findings) |
| Real process, real model, network blocked | Passed (below) |
| **One real phone: real speech through the trusted-host path** | **Passed (2026-09-25):** local STT produced attributed live dashboard transcript rows. Device/browser, duration, and quantitative accuracy were not recorded. |
| **Distinct phrases per device; bleed with two phones close together** | **Not run** |
| Window size ~1 s | **Not changed. Measurements say it is a problem; needs the project lead** |

The initial real-phone result establishes that real microphone audio reaches the STT pipeline and dashboard. The synthetic work still does not establish real-phone accuracy, isolation, noise handling, or bleed; those require the remaining manual checks.

## Commands

```bash
.venv/bin/python -m pytest tests/test_vad.py tests/test_windowing.py tests/test_scheduler.py tests/test_stt_adapter.py -q -m "not model"   # the work order's command
.venv/bin/python -m pytest tests -q                                       # everything default: 412 passed, about 90 s
.venv/bin/python -m pytest tests -m model -q                              # needs the model provisioned: 6 passed, about 26 s
.venv/bin/python scripts/provision_models.py [--check]                    # download once while online / verify offline
.venv/bin/python scripts/make_speech_fixtures.py                          # regenerate tests/fixtures/audio with say + afconvert
.venv/bin/python scripts/measure_stt.py models                            # candidate comparison
.venv/bin/python scripts/measure_stt.py pipeline --devices 1 2 5 --window-ms 1000
```

## Environment, dependencies (please confirm)

Reference laptop: MacBook Pro, Apple M4 Pro, 12 cores, 24 GB, macOS (Darwin 25.6.0), Python 3.14.7.

Added to `requirements.txt`: `mlx-whisper>=0.4,<0.5` (tested 0.4.3) and `huggingface-hub>=0.24` (used directly; 1.32.0 installed). `mlx-whisper` brings `mlx` 0.32.2, **`torch` 2.14.0**, `numba` 0.67.0, `scipy` 1.18.1 and `tiktoken` 0.14.0 as dependencies; the virtual environment grew to about 1.1 GB. All of it works offline once installed. No VAD library was added (the gate is dependency-free). `server/stt.py` (the DT-17 command adapter) was removed; nothing referenced it.

Model weights in the Hugging Face cache (`~/.cache/huggingface/hub/models--mlx-community--*`), downloaded for the comparison: turbo 1.61 GB (**pinned, needed**), distil-large-v3 1.51 GB, whisper-small 0.48 GB, whisper-base 0.14 GB. The last three are only for the comparison and can be deleted.

## Decisions and deviations

1. **Turbo, not distil.** The docs held distil in reserve for "if turbo can't keep up". Measured, distil is neither faster (both about 0.5 s per call) nor more accurate, so it would not help.
2. **One worker.** Inference is GPU-bound: 1, 2, 3 and 4 threads gave 1.94, 2.03, 2.05, 2.06 windows/s. More workers only lengthen each call. The pool supports N (tested with 3) but the default is 1.
3. **Whisper's own confidence cannot detect non-speech.** On silence, digital noise, steady noise, hum and clicks the model returned "Thank you." with `stt_confidence` 0.60 to 0.79 and `no_speech_prob` 0.00, the same range as real speech (0.73 for a real window). So the VAD gate is the defense, and the hallucination blocklist is keyed on the VAD's speech fraction, not on confidence (it drops "thank you" and similar only when the window was mostly non-speech; the same words in a speech window are kept). The work order proposed suppressing with a no-speech threshold; that cannot work with this runtime.
4. **VAD is an adaptive energy gate.** The first design only learned its noise floor from frames it had gated out, so steady noise louder than the threshold opened the gate forever (measured: 26 of 26 windows passed for white noise at -45, -35, -25 dBFS, for pink noise and hum). It was replaced with a rolling low percentile of the last 5 s of frame levels. Measured after the fix: silence and steady noise are gated; speech passes.
5. **Confidence mapping** (`docs/data-model.md`, `docs/stt-pipeline.md`): `exp(duration-weighted mean avg_logprob) × (1 − mean no_speech_prob)`, clamped; documented as uncalibrated. Since `no_speech_prob` reads 0.00 here, in practice it is `exp(mean avg_logprob)`.
6. **`ModelExecution.related_id` for `stt` is `<device_id>/<window_id>`** (a window has no utterance yet); a row is written for every invocation including failed ones (the `model_error` event says which). Documented in `docs/data-model.md`. The `runtime` enum (`mlx`) needed no change. **Row volume:** one row per window that reaches the model; at the measured 5-device load that is about 2 rows per second, which SQLite handled with no lag (event-loop lag stayed at or below 8 ms).
7. **Audio sink registration:** the transport calls an optional `bind_device(device_id, meeting_id)` on the sink when it creates a device session (the pipeline needs the meeting for `drain`), and merges the pipeline's `stt_backlog` and `stt_dropped_windows` into the device gauges. Both in `server/transport/peers.py`.
8. **`--no-stt` flag** on the server runs the transport only, so the phone checks (R1 to R9 in `logs/transport.md`) can be done without loading the model.
9. **Overload policy is drop oldest** (the work order's recommendation). Confirm.
10. **Hallucination blocklist** default: "thank you", "thanks for watching", "thank you for watching", "you", "bye", "thanks", applied only below a speech fraction of 0.5. Chosen from the one stock phrase actually observed ("Thank you."); the others are Whisper's commonly reported ones and are **unverified here**.
11. **Time base:** `t_wall` at frame receipt (UTC). Accuracy is bounded by network and jitter-buffer delay and, after a resync, by 0.25 s. The RTP media time (`pts`) is available but not passed on. A gap flushes the partial window so none straddles it. Retained audio for CON-13's speaker embeddings: **windows are not retained after transcription**; CON-13 would need to tap the sink itself.
12. **Language mode:** the default is now `auto`, mapped to `None` for MLX Whisper's built-in multilingual detector.
    Explicit language codes remain supported. This is an implementation/configuration change only; Hindi/Hinglish
    quality is pending the B1a physical test in `docs/manual-tests.md`. Focused configuration/adapter tests passed
    (48 non-model tests); the local MLX model suite also passed with direct Metal access on the reference laptop.

## Findings

* **The window size is the central problem** (numbers below). One turbo call costs about 0.5 s whether the window is 1 s or 3 s, so one worker sustains about 2 windows/s.
* **Windows of 1 s cut words in half**: word error rate 0.094 for turbo (0.000 for whole clips), with fragments like "very tough. height." for "very tight." and a spurious trailing "$10,000".
* **The event loop is not the bottleneck**: with 5 synthetic phones the loop lag stayed at or below 8 ms in every run; the model is.
* **Bugs found in my own code by measurement and tests:** the never-adapting noise floor (above); a partial window buffered before a reconnect gap would have merged with audio after it (fixed, tested); an internal worker error leaked the in-flight count and the lost window vanished silently (fixed: now reported failed and audited, in-flight accounting is exception-safe, and `drain` returning means the callbacks have run); `Runtime.stop()` closes the adapter, so a measurement script reusing one adapter across servers got 80 of 80 windows failing (script fixed; the failure path did report and audit them, which was a useful accidental test).
* **One mutation was missed at first:** breaking `_process`'s adapter-error handling still produced correct results because the worker's outer safety net caught it, so an ordinary model failure would have been counted as a worker restart. The test now asserts `worker_restarts == 0` for an adapter failure.
* **A test-isolation trap:** `huggingface_hub` reads `HF_HUB_CACHE` once at import, so `monkeypatch.setenv` only worked when the test imported it first (order-dependent, and one assertion was vacuous against the real cache). The tests patch the library's constant instead.
* `whisper-small` and `whisper-base` cost 143 to 239 ms and 37 to 46 ms per call: about 5 and 25 windows/s on this machine.

## Measurements (reference laptop, synthetic phones and synthetic speech)

### Model comparison (`scripts/measure_stt.py models`, whole fixtures cut into fixed windows)

| Model | Load (cached) | Peak RSS | Peak GPU | Latency/call | WER whole | WER 3 s | WER 2 s | WER 1 s |
|---|---|---|---|---|---|---|---|---|
| whisper-large-v3-turbo (**pinned**) | 1.3 s | 1.77 GB | 2.36 GB | 490–525 ms | 0.000 | 0.000 | 0.062 | 0.094 |
| distil-whisper-large-v3 | 1.4 s | 1.96 GB | 2.26 GB | 508–548 ms | 0.000 | 0.000 | 0.062 | 0.094 |
| whisper-small-mlx | first load incl. download | 1.30 GB | 1.38 GB | 143–239 ms | 0.000 | 0.047 | not run | 0.125 |
| whisper-base-mlx | first load incl. download | 0.55 GB | 0.52 GB | 37–46 ms | 0.000 | 0.031 | not run | 0.109 |

Load times of 129 s to 414 s in the first run included the download and are not load times. Eight clips of 2 to 3 s, no digits in the references (a first run with "fifty thousand dollars" inflated WER through number formatting and was redone).

### What Whisper returns on non-speech (VAD bypassed, 2 s of audio, turbo)

Digital silence, white noise at -60, -45 and -30 dBFS, pink noise, 50 Hz hum and keyboard-like clicks all returned "Thank you." with confidence 0.60 to 0.79 and `no_speech_prob` 0.00.

### VAD (`tests/test_vad.py`; 20 ms frames, margin 9 dB, 5 s window, 10th percentile; synthetic)

| Condition | Result |
|---|---|
| digital silence, white -60/-45/-35/-25 dBFS, hum -35 | 0 of 46 windows pass |
| speech at -18, -28, -38, -46 dBFS with silent pauses | every speech window passes |
| speech (-28) over pink noise at 20 / 10 / 5 dB SNR | 26/26, 24/26, 21/26 speech windows pass (pause windows in that noise pass 2/19, 3/19, 3/19) |
| pink noise only, -35 / -25 dBFS | 4 and 3 of 46 windows pass (the hallucination filter catches these) |
| margin 12 dB (rejected) | cleaner on noise but only 10/26 speech windows at 5 dB SNR: exactly the cross-device bleed regime |

### Pipeline (`scripts/measure_stt.py pipeline`, real model, worker 1, queue 4)

"Post-window latency" is the time from a window's last sample arriving to its transcript; add up to the window length for the delay to the first word in it. "Continuous" means every phone talks all the time (worst case); "turns" means each phone is active about 34% of the time with staggered starts (still overlapping more than a real conversation).

| Window | Scenario | Devices | Median / p95 post-window latency | Queue depth (max) | Windows dropped | Notes |
|---|---|---|---|---|---|---|
| 1 s | continuous | 1 | 522 / 538 ms | 1 | 0 | |
| 1 s | continuous | 2 | 805 / 1098 ms | 2 | 0 | saturating |
| 1 s | continuous | 5 | 3935 / 4472 ms | 21 | 274 (about 55%) | overloaded; drain timed out |
| 1 s | turns | 1 | 511 / 532 ms | 1 | 0 | 15 of 40 windows gated |
| 1 s | turns | 2 | 1227 / 3200 ms | 6 | 0 | |
| 1 s | turns | 5 | 3819 / 7432 ms (max 10.5 s) | 21 | 46 | |
| 2 s | continuous | 5 | 6718 / 8286 ms | 21 | 54 | still over capacity (2.5 windows/s) |
| 2 s | turns | 5 | 1484 / 3390 ms | 6 | 0 | |
| **3 s** | continuous | 5 | 1892 / 3236 ms | 6 | 2 | about at capacity (1.7 windows/s) |
| **3 s** | turns | 5 | 1032 / 2139 ms | 4 | 0 | |

Queues stayed bounded in every run and every drop was counted and matched by an audit event (274 drops = the sum of per-device counters in the 1 s, 5-device run). Peak memory over the runs: 2.0 GB resident, 2.5 GB GPU. In the overloaded runs the drain step timed out while the phones kept streaming, so per-device raw counts in those rows include that extra period; the latency figures cover the 40 s test window only. The phones ran in the same process as the server (Opus encoding on the same CPU); event-loop lag stayed at or below 8 ms.

### Real process with the network blocked

`python -m server.app` with the real model under a guard that raises on any non-loopback connection or DNS lookup: loaded the pinned model in 1.3 s, wrote `model_load` to the audit stream, served HTTPS (`POST /api/meetings` returned 201), exited 0 on SIGINT, and recorded **zero network attempts**. With a revision that is not in the cache it printed "the STT model … is not in the local cache and the server never downloads models. Run `scripts/provision_models.py` while online, once." and never started. Whole-machine "networking disabled" (Wi-Fi off) was **not** done; the guard covers this process only.

## Thresholds and where they live (all in `config/convene.toml`; all from synthetic measurement)

| Key | Value | Evidence | Used by |
|---|---|---|---|
| `[pipeline] window_ms` | 1000 | the docs fix ~1 s; **measurements argue for longer** | `Windower` |
| `min_speech_fraction` | 0.2 | speech windows pass, silence and steady noise do not | `Windower` |
| `vad_margin_db` | 9.0 | 12 dB lost speech at low SNR; 6 dB passed pink noise | `EnergyVad` |
| `vad_min_db` / `vad_noise_window_s` / `vad_noise_percentile` / `vad_hangover_frames` | -50 / 5 / 10 / 5 | first estimate; noise floor design above | `EnergyVad` |
| `workers` | 1 | GPU-bound (throughput did not scale) | `SttScheduler` |
| `queue_max` | 4 | bounds staleness; **provisional**, tune with real load | `SttScheduler` |
| `window_timeout_s` | 20 | a call that long is broken | `SttScheduler` |
| `priority_high_backlog_windows` / `priority_max_wait_s` | 4 / 3.0 | **provisional**; CON-15 measures interference | `ComputePriority` |
| `hallucination_blocklist`, `hallucination_max_speech_fraction` | see decision 10 / 0.5 | one observed phrase | `SttScheduler` |
| `[models.stt] no_speech_threshold` / `logprob_threshold` / `compression_ratio_threshold` | 0.6 / -1.0 / 2.4 | Whisper's own defaults, passed through | `MlxWhisperAdapter` |

## Decisions needed from the project lead

1. **Window size: resolved.** The project lead chose option B (speech segments, ADR-19); it is built, is the default, and fixed windows remain a one-line fallback (`segmentation = "fixed"`, with `window_ms = 3000` for five phones). See "Addendum: speech segments" below.
2. **Confirm the added dependencies** (`mlx-whisper` and its `torch`/`numba`/`scipy`/`tiktoken`, `huggingface-hub`).
3. **Confirm** drop-oldest as the overload policy, and one `ModelExecution` row per invocation including failures.

## Open items and Not run

* **Partially run:** one phone produced attributed real-speech transcript rows through the trusted-host path on 2026-09-25. **Still not run:** distinct phrases staying with the right device on real audio; two phones close together (bleed) and the gate's behavior there; the 30 to 60 minute soak; five real phones; screen lock effects on the stream; real background noise; and a measured Hindi/Hinglish pass. VAD thresholds remain unvalidated in those conditions.
* **Not run:** contention with a reasoning or embedding job (the mechanism exists and is tested; CON-15 measures the interference); the full memory budget with the other models loaded (only `stt` is measured); a whole-machine offline start (Wi-Fi off).
* Browser default audio processing (echo cancellation, noise suppression, AGC) changes what the gate and the model see; unmeasured.
* A window that times out leaves its model thread busy until it returns (MLX calls cannot be interrupted).

## Handoff

* **CON-06:** in the default segment mode one "window" is one stretch of speech (up to 8 s), so create one `Utterance` per `ok` outcome. Register `runtime.on_transcribed_window(callback)`; it receives every outcome (`ok`, `empty`, `failed`, `dropped`) with `device_id`, `meeting_id`, `window_id`, `text`, `stt_confidence`, `t_start`, `t_end`. Create an `Utterance` only for `ok`. Non-empty `text` is already trimmed. Windows are not retained after transcription.
* **CON-07:** `stt_backlog` and `stt_dropped_windows` are in `device_gauges` and the meeting snapshot.
* **CON-08, CON-09:** `await runtime.pipeline.priority.wait_for_turn("embedding" | "reasoning")` before starting a job.
* **CON-10:** `await runtime.pipeline.drain(meeting_id, timeout)` before reading the transcript (flushes each device's open segment, then waits for every callback).
* **CON-13:** needs per-window audio for speaker embeddings; it is discarded after transcription, so tap `AudioSink.push` or extend the pipeline.
* **CON-15:** tune the thresholds above from real recordings; list is in this file.

## Addendum: speech segments (ADR-19), the default

**What.** Each device's audio is now cut by its own VAD into one segment per stretch of speech instead of every ~1 s (`server/pipeline/segmenting.py`). A segment starts at the first voiced frame (200 ms pre-roll), ends after 600 ms of silence (200 ms tail kept), and is capped at 8 s: a capped stretch is cut at the quietest frame in its last 1.5 s and the rest starts the next segment. Voiced audio shorter than 300 ms (or under 20% voiced) is counted as gated, not transcribed. A gap in the stream (reconnect), stream end, or `drain` sends the open segment first. Timestamps use the same `StreamClock` as before.

**Config** (`[pipeline]`): `segmentation = "segments"` (or `"fixed"`), `segment_end_silence_ms = 600`, `segment_max_ms = 8000`, `segment_min_ms = 300`, `segment_pre_roll_ms = 200`, `segment_tail_ms = 200`, `segment_cut_search_ms = 1500`, `hallucination_min_strength_db = 10.0`, `log_transcripts = true`. `window_ms` applies to fixed mode only.

**Hallucination filter in segment mode.** A stock phrase ("Thank you.") is suppressed when the segment is weak: either its voiced fraction is low, or its `strength_db` (mean margin of its voiced frames over the device's noise floor) is under 10 dB. Real speech measured well above that on the synthetic clips; the threshold is **not validated on real phones**.

**Measurement** (`scripts/measure_stt.py pipeline --segmentation segments`, synthetic speech through the real pipeline and the real model, 40 s each, this laptop):

| Scenario | Devices | Segments | Dropped | Latency after segment end (median / p95) | Queue wait median / p95 | Max queue depth |
|---|---|---|---|---|---|---|
| continuous talking | 1 | 6 | 0 | 0.83 / 1.19 s | 0 / 0 ms | 1 |
| continuous talking | 2 | 12 | 0 | 1.07 / 2.17 s | 0 / 521 ms | 2 |
| continuous talking | 5 | 30 | 0 | 1.95 / 2.74 s | 603 / 1614 ms | 4 |
| turn-taking | 1 | 4 | 0 | 1.25 / 1.96 s | 0 / 0 ms | 1 |
| turn-taking | 2 | 8 | 0 | 1.24 / 2.00 s | 0 / 0 ms | 1 |
| turn-taking | 5 | 19 | 0 | 1.22 / 1.97 s | 0 / 225 ms | 2 |

Model call about 0.55 to 0.62 s median throughout; peak RSS 2.08 GB, peak MLX GPU 2.7 GB; event loop lag p95 1 ms; every run drained. Against ~1 s windows (55% dropped at five talking phones), five phones now fit with zero drops. These are synthetic clips (text-to-speech, mixed with synthetic noise); accuracy on real speech and the lag with real overlapping conversation are **Not run** (`docs/manual-tests.md`, Part B).

**Tests.** `tests/test_segmenting.py` (23), scheduler strength suppression (2 new), config validation and defaults (5 new), console transcript lines and `log_transcripts = false` (2), `/metrics` `stt` block (1). Ten mutations of the segmenter (end-silence, cap, cut choice, tail, min-speech gate, pre-roll, gap flush, trailing counter, strength, cut index) were each caught. Full default suite: 445 passed, 6 deselected (`model`). `-m model`: 6 passed.

**Known limits.** Surging noise (8 to 20 dB above the floor, for example music with strong beats or a nearby conversation) looks like speech to an energy VAD and can produce short false segments; pink noise leaked about 11 short segments per 120 s in the synthetic test. Speech through a noise-suppressing phone browser may be lower or shaped differently than the clips. Whisper still hallucinates on some of these; the blocklist only removes known stock phrases. A monologue without a 600 ms pause is cut every 8 s at the quietest point, which can still land inside a word if there is no dip. Each segment costs one full model call regardless of length, so many very short segments (people interjecting) cost more than their audio.
