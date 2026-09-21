# Convene implementation plan

## Purpose and current baseline

This is a dependency-ordered route from the existing DT-17 transport prototype to the [must-have demo](requirements.md). It is a plan, not an implementation status report. `server/app.py` and `client/` already handle local WebRTC audio; `server/stt.py` supports optional command-driven STT. Verify their behavior against [transport](transport.md) before changing them. Each stage is complete only when its acceptance checks pass on the target laptop and phones.

## Demo-critical stages

| Stage | Depends on | Build | Acceptance check |
|---|---|---|---|
| 1. Transport baseline | Existing prototype | Preserve phone join, signaling, audio isolation, reconnect, and local HTTPS; record known gaps against the target contract. | Re-run the DT-17 transport checks in [`old_docs/TEST_PLAN.md`](../old_docs/TEST_PLAN.md) with at least two phones. |
| 2. Meeting and participant records | 1 | Add the meeting, device, participant, utterance, and audit persistence defined in [data model](data-model.md); retain stable identity across reconnects. | A reconnect keeps the same participant; an utterance and its source device survive server restart. |
| 3. Live transcription and attribution | 2 | Add per-device VAD/windowing and local STT adapter per [STT pipeline](stt-pipeline.md). Apply device-based attribution with confidence and correction records per [speaker attribution](speaker-attribution.md). | Two phones produce correctly labeled live lines; a failed window does not stop later windows; a manual correction persists. |
| 4. Live dashboard and API | 2–3 | Expose the [API](api.md) and build the live [frontend](frontend.md) for device health, transcript, low-confidence markers, and correction. | A browser sees incoming lines and connection changes without refreshing; correction is visible after reload. |
| 5. Live Q&A | 3–4 | Chunk and embed persisted utterances, add `sqlite-vec`, and invoke retrieval only for an explicit question per [RAG and Q&A](rag-and-qa.md). | A discussed fact returns an answer with speaker/time citation; an absent fact returns `no_grounding`. |
| 6. Summary and DOCX | 3–4 | Generate structured summary/action items and render them with deterministic code per [summarization](summarization.md) and [export](export.md). | End a meeting and download a DOCX reflecting corrected labels and action owners. |
| 7. Offline end-to-end rehearsal | 1–6 | Prepare local models, certificates, and network per [deployment](deployment.md); run [testing](testing.md) and the [demo](demo.md). | With internet unavailable, phones join, transcript appears, Q&A cites a line, and summary/DOCX complete. |

## After the core demo is stable

Implement shared-device enrollment/classification, then meeting history and cross-meeting search, then tracked action items. These are the [should-have requirements](requirements.md). Keep low-confidence and generic-speaker fallbacks visible throughout the shared-device path. Optional cloud fallback and extra export formats remain lower priority.

## Change discipline

Use [00-AI-CONTEXT.md](00-AI-CONTEXT.md) and the topic document for each stage before coding. Keep API shapes and persisted fields aligned with their authoritative contracts. Update those documents if implementation reveals a necessary contract change, and record the reason in [decisions](decisions.md). Do not mark a stage complete based solely on a component starting; use its acceptance check and the relevant cases in [testing](testing.md).
