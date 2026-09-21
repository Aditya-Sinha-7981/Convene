# Testing

This extends the original DT-17 transport test plan (`transport.md`'s inherited layer already has its own validated test sequence) with the layers Convene adds on top. The step-by-step checklist for what needs real phones, Wi-Fi and speech (everything built so far) is [manual-tests.md](manual-tests.md).

Run transport tests first — a failure there invalidates everything above it, same principle as the original plan's "failed early check means later checks cannot validate yet" rule.

## New failure classification: F6 — Misattribution

Added alongside the original plan's F1 (browser), F2 (secure-context), F3 (Wi-Fi), F4 (transport), F5 (laptop processing) classes:

**F6: Misattribution** — an utterance's `participant_id` does not match who actually said it. Subclasses:
- F6a: cross-device bleed (Path 1 wrongly attributed due to a nearby phone picking up another speaker)
- F6b: shared-device misclassification (Path 2 enrolled-speaker confusion)
- F6c: enrollment failure cascading into unnecessary generic-label attribution

Do not "fix" an F6 failure by adding unrelated infrastructure — identify which subclass it is first (`speaker-attribution.md`).

## Test: STT pipeline (CON-05)

- Default run (`pytest -m "not model"`, no weights): VAD, windowing and time base, resampling, the scheduler (fairness, overload, isolation, drain), the priority hook, the adapter contract and confidence mapping, offline model resolution, and synthetic phones through the real server with a fake model (`tests/test_vad.py`, `test_windowing.py`, `test_scheduler.py`, `test_stt_adapter.py`, `test_stt_integration.py`).
- Real model (`pytest tests/test_stt_adapter.py -m model`, reference laptop, weights provisioned): synthetic speech fixtures transcribed within a recorded error rate, safe from worker threads, resolved and run **with any non-loopback socket or DNS lookup raising**, and two devices through the whole pipeline with each transcript staying with its own device.
- Measurements (`scripts/measure_stt.py models|pipeline`): model comparison and latency/queue behavior at 1, 2 and 5 synthetic devices. Recorded in `logs/stt.md`.
- None of these establish accuracy on real phone audio, cross-device bleed, or behavior with real phones. Synthetic speech is clean text-to-speech; real-phone STT and bleed checks are still required (see the single-device and cross-device tests below).

## Test: single-device attribution baseline

- One phone, one speaker, 5 minutes of continuous speech.
- Every utterance should be `attribution_method = "device"`, `attribution_confidence` high.
- Pass: zero misattributions with no other phones present (isolates F6a from any real cross-device effect).

## Test: cross-device bleed (F6a)

- Two phones placed close together (worst case for bleed), each with a different speaker, alternating speech.
- Record how often either phone's transcript contains a stray line attributable to the other speaker.
- This is a measurement, not strictly pass/fail — record the rate, and use it to tune the VAD threshold (`stt-pipeline.md`) if it's higher than acceptable.

## Test: shared-device enrollment and classification (F6b, F6c)

- One phone declared shared, 2–3 people enrolled.
- Each person speaks distinguishable phrases in a randomized order.
- Verify classification matches the correct enrolled participant; record the confusion rate.
- Test degraded enrollment deliberately: one person provides a deliberately short/noisy sample — verify the system falls back to `quality_flag = "low_confidence_sample"` rather than silently treating it as reliable.
- Test a completely unenrolled 4th person joining mid-meeting on that device — verify they fall into `generic_unresolved`, not misattributed to one of the enrolled participants.

## Test: manual correction round-trip

- Deliberately misattribute (or use a real F6 case), correct it via the dashboard, verify:
  - `attribution_method` becomes `manual_correction`, `original_participant_id` preserved.
  - Downstream chunking/RAG/summary/export all reflect the corrected value, never the original.

## Test: RAG honesty (live and history mode)

- Ask a question about something that was genuinely discussed → verify a grounded answer with correct citations.
- Ask a question about something that was never discussed → verify `status = "no_grounding"` and an honest "don't know" response, **not** a fabricated answer drawn from the model's general knowledge (`rag-and-qa.md`). This is the single most important test in this document — a fabricated-but-confident RAG answer is a worse failure than the feature not existing at all.
- Ask a live-mode question referencing something said *after* the question was asked (should be impossible by construction) — verify it isn't somehow answered anyway, confirming the point-in-time scoping is real.

## Test: summarization structured-output reliability

- Run summarization across several real transcripts of varying length/messiness.
- Verify the output always parses as valid structured data (`summarization.md`) or fails cleanly after one retry — never a malformed partial result silently accepted.

## Test: export reliability

- Generate an export for a meeting with corrections, low-confidence lines, and a null-owner action item all present.
- Verify the DOCX reflects corrected attribution (never original), visually flags low-confidence lines, and shows "Unassigned" rather than blank for a null owner.

## Test: local-only operation (inherited, re-verify at every layer)

- Disconnect the laptop from the internet entirely.
- Run a full meeting end-to-end: join → live transcript → live Q&A → end → summarize → export.
- Pass: every step completes with zero network calls to anything outside the local Wi-Fi. This must be re-verified whenever a new dependency is added anywhere in the stack, not just once at the start.

## Test: resource contention under load

- 5+ phones streaming continuously while a Q&A question and a summarization call are both triggered.
- Verify STT does not visibly stall (per the scheduling priority in `stt-pipeline.md`) and verify memory stays within the budget in `models.md`. If it doesn't, that's a signal to revisit model choice or scheduling, not to silently accept degraded live transcription.

## Metrics to record (extends the original plan's table)

Add columns for: misattribution count/rate by F6 subclass, RAG honesty pass/fail, summarization parse-failure rate, export failure rate — alongside the original transport metrics (disconnects, reconnects, audio gaps, latency).
