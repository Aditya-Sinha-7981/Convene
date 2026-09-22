# Convene — Standalone AI Project Brief

> Give this entire file to an AI assistant when it cannot inspect the repository. It is a self-contained product,
> architecture, status, and working brief—not a replacement for the repository's authoritative contracts.
>
> **Snapshot date:** 2026-09-23. **Implemented through:** CON-06. If the AI can access the repository, it must read
> `AGENTS.md`, check Git state, inspect the relevant code and tests, and use the topic documents under `docs/` as the
> final authority. Later code and work logs may supersede this snapshot.

## The product

Convene turns phones on the same local Wi-Fi network into separate meeting microphones. Each participant normally
uses one phone. The phones stream microphone audio over WebRTC to one laptop, which transcribes every stream,
attributes speech primarily from the source device, displays a live speaker-labeled transcript, answers explicit
questions using the meeting transcript, and produces a structured summary, action items, and DOCX minutes.

The intended demo is entirely local after one-time setup: one laptop, 1–10 phones, no internet dependency during
the meeting, and no paid API requirement. A cloud model may eventually be offered only through an explicit user
choice or configuration; it must never become an automatic fallback.

## Why it exists

Most meeting transcription systems listen to one mixed microphone and then guess which voice belongs to which
person. That makes speaker attribution an uncertain diarization problem, and a wrong label silently corrupts the
record.

Convene's central insight is that a phone-per-speaker setup already provides separated audio channels. For the
normal case, speaker attribution is bookkeeping:

```text
audio stream -> device_id -> registered participant
```

Acoustic speaker classification is needed only when two or three people explicitly share one phone. Even there,
the planned approach compares speech against enrolled voices and falls back to an honest generic, low-confidence
label. It does not silently invent a speaker.

The project is being built for a hackathon. Reliable, explainable demo behavior is more important than production
scale or broad feature coverage.

## Demo-day goal

A successful offline demo should show this complete loop:

1. Two or more phones join a meeting from a local HTTPS page or QR code.
2. Each phone streams microphone audio independently to the laptop.
3. The laptop shows device connection and audio health.
4. Speech appears within a few seconds as a live transcript labeled with the correct participant.
5. A temporary phone/network interruption reconnects under the same device and participant identity.
6. A user asks a question about what was said and receives a grounded answer with speaker/time citations.
7. An unsupported question returns an honest `no_grounding` result.
8. Ending the meeting generates a validated summary and action items.
9. Deterministic application code renders those stored results into a downloadable DOCX.
10. Low-confidence attribution is visible and any line can be manually corrected without losing its history.

Past-meeting Q&A and shared-device voice classification are valuable, but they follow only after this core demo is
stable.

## Non-negotiable architecture

- One laptop server, one Python process: FastAPI + aiortc.
- WebRTC carries phone audio. WebSockets carry signaling and live dashboard updates.
- No STUN, TURN, public signaling service, account system, or cloud deployment.
- Device identity is the primary speaker-attribution mechanism.
- Acoustic classification is restricted to declared shared devices.
- Every utterance stores STT confidence, attribution method, attribution confidence, and correction state.
- Manual correction preserves the first original participant on the row and every transition in the audit stream.
- Local models are the default. Runtime model downloads are forbidden; models are provisioned beforehand.
- Concrete model identifiers live in configuration and are resolved by resource type.
- Retrieval happens only for an explicit question—never automatically.
- Q&A must use indexed meeting evidence, cite stored content, and return `no_grounding` when evidence is inadequate.
- Summaries are validated structured data. A model never authors a DOCX directly.
- SQLite is the application store; `sqlite-vec` is the planned vector store.
- One ordered audit/event stream is the durable record of what happened.
- A failure on one device must not stop another device's transport, transcription, or transcript.
- Reconnecting a known phone must preserve its device and participant identity.

## System shape

```text
Phone browser
  getUserMedia -> RTCPeerConnection -> WebRTC audio
                                      |
                                      v
Laptop FastAPI/aiortc server
  signaling + peer isolation + device registry
                                      |
                                      v
  per-device audio preparation -> adaptive energy VAD -> speech segmentation
                                      |
                                      v
  bounded fair STT scheduler -> local mlx-whisper
                                      |
                                      v
  device attribution -> Utterance in SQLite -> audit event
                                      |
                 +--------------------+--------------------+
                 v                    v                    v
          live dashboard       chunk/embed/index    summary/action items
                                      |                    |
                                      v                    v
                              explicit live RAG       deterministic DOCX
```

The current STT input is VAD-bounded speech segments, not fixed one-second windows. A segment ends after a pause,
is capped at eight seconds, and carries UTC start/end timestamps. Fixed windows remain a configuration fallback.

## Current implementation status

The repository is implemented through CON-06.

### Implemented

- FastAPI + aiortc server and local HTTPS startup checks.
- Meeting creation, retrieval, ending, join URLs, and QR payloads.
- Persistent meeting, device, participant, utterance, model-execution, connection-event, and audit records.
- SQLite migrations, WAL configuration, serialized database worker, and post-commit audit subscribers.
- Phone registration, stable `device_id`, participant registration, reconnect counting, and restart reconciliation.
- Per-device WebRTC isolation and malformed-client isolation.
- Join page with name entry, consent text, persisted device identity, and reconnect behavior.
- Dashboard WebSocket infrastructure for durable events and ephemeral device gauges.
- Local `mlx-whisper` adapter with an exactly pinned model revision and offline-only cache resolution.
- Audio conversion/resampling, adaptive per-device energy VAD, and speech segmentation.
- Bounded fair STT scheduling, drop-oldest overload behavior, backlog/drop gauges, and compute-priority hooks.
- Model invocation auditing, failure isolation, pipeline draining, and server-console transcription.
- Device-based attribution for non-shared phones.
- Safe `generic_unresolved` attribution for shared devices until speaker classification is built.
- Persisted utterances, server-derived labels, confidence flags, transcript API, and dashboard utterance events.
- Atomic manual correction by participant ID or display name, including correction after meeting end.
- Non-blocking post-write/correction hooks and audit-derived summary/export staleness helpers.

### Attribution defaults currently locked

- Device attribution confidence: `0.95`.
- Unresolved shared-device confidence: `0.2`.
- Manual correction confidence: `1.0`.
- Server-owned low-attribution-confidence threshold: `0.8`.
- `generic_unresolved` is always low confidence.
- STT confidence and attribution confidence remain separate concepts.
- One successful speech segment creates one utterance.
- Generic labels use stable device join order, such as `Speaker on Phone 2`.
- The first original participant is preserved across repeated corrections.
- Correcting to the current participant confirms the label once; repeating the confirmed state is a no-op.

### Not implemented yet

- CON-07: functional live transcript dashboard, low-confidence presentation, correction UI, resynchronization UI,
  device-health panel, and End Meeting control.
- CON-08: deterministic transcript chunking and local embeddings stored in `sqlite-vec`.
- CON-09: live, grounded Q&A and its dashboard panel.
- CON-10: validated structured summary and action-item generation.
- CON-11: deterministic DOCX renderer, export persistence, and download control.
- CON-12: offline end-to-end rehearsal and demo stability gate.
- CON-13: shared-device enrollment and acoustic speaker classification.
- CON-14: meeting history and cross-meeting Q&A.
- CON-15: five-phone load, real attribution validation, and evidence-based threshold tuning.

The next dependency-ordered task is **CON-07, Live dashboard and correction UI**. CON-08 may follow or proceed in a
carefully isolated parallel workstream after the CON-06 interfaces are understood.

## What automated evidence exists

At the CON-06 checkpoint:

```text
Default repository suite: 460 passed, 6 deselected
Provisioned real-model suite: 6 passed, 460 deselected
Focused contract/CON-06 suite: 52 passed
```

The tests cover database behavior, contracts, signaling, loopback WebRTC, synthetic phones, audio processing,
VAD, segmentation, scheduling, offline model resolution, attribution, correction, API behavior, and a synthetic
two-phone end-to-end attribution path. Real model tests run the provisioned local model on the reference laptop.

This evidence does **not** establish that real phones or real-room audio work. Synthetic clients and text-to-speech
fixtures validate plumbing and regression behavior, not browser microphones, Wi-Fi conditions, human accents,
room noise, acoustic bleed, or screen-lock behavior.

## Manual validation debt

Physical/manual testing has been deliberately deferred until the project owner has time. The following remain
`Not run`, not passed:

- Trusted local HTTPS and microphone permission on Android Chrome and iPhone Safari.
- One real phone speaking continuously for five minutes.
- Two real phones saying distinct phrases with correct labels.
- Wi-Fi loss for roughly 2 and 10 seconds followed by identity-preserving recovery.
- Tab close/reopen, backgrounding, and screen locking on each browser.
- Nearby-phone acoustic bleed and behavior in real room noise.
- A 30–60 minute soak and a five-real-phone run.
- Correction through the UI on a real transcript followed by reload/restart.
- Whole-machine offline operation with internet unavailable.
- Full memory and latency behavior with STT, embedding, and reasoning models loaded together.

Continue building with synthetic tests, but keep these claims explicitly unverified. A strong manual checkpoint is
after CON-07, because the dashboard will expose transport, STT, attribution, confidence, correction, and reconnect
behavior together. Do not claim demo readiness before CON-12's offline rehearsals.

## Key runtime and data behavior

- A meeting moves `created -> live -> ended`.
- A non-shared registration creates exactly one participant for its device.
- A shared registration creates no named participant until enrollment; current speech becomes unresolved.
- Successful STT results become utterances; empty, failed, or dropped results do not.
- Transcript order is `t_start`, then `utterance_id`, not arrival order.
- Dashboard durable events carry the global audit `seq`; clients must not interpret per-meeting gaps as loss.
- Dashboard recovery is subscribe, buffer, fetch snapshot with `as_of_seq`, discard buffered events at or below that
  sequence, then apply the rest as upserts.
- Manual correction is one transaction containing the row update, optional participant creation, and audit event.
- A summary/export becomes stale when a later `utterance_created` or `utterance_corrected` sequence exceeds the
  artifact's input high-water mark. No mutable `stale` column exists.
- Private transcript or audio content must never be placed in Git-tracked logs.
- Raw audio is not persisted by default.

## Model and performance facts

- STT runtime: `mlx-whisper` on Apple Silicon.
- Configured STT model: `mlx-community/whisper-large-v3-turbo`, pinned by revision in `config/convene.toml`.
- Default STT workers: one; measured inference is GPU-bound and more worker threads did not improve throughput.
- Speech segmentation replaced fixed one-second windows because short windows split words and overloaded at five
  continuous synthetic devices.
- In synthetic five-device measurements, segment mode completed without dropped segments and typically produced
  roughly one-to-three-second post-segment latency. This is not real-phone evidence.
- The adaptive energy VAD and hallucination suppression are measured on synthetic speech/noise and still need
  tuning from real recordings.

## Repository map

```text
AGENTS.md                 mandatory repository working rules
README.md                 operator-facing setup/run overview
config/convene.toml       paths, STT, pipeline, and attribution configuration
server/app.py             application construction and CLI entry point
server/runtime.py         process lifecycle and component wiring
server/transport/         signaling, peer sessions, audio sink
server/pipeline/          VAD, segmentation, scheduler, STT adapters
server/attribution/       attribution, correction, labels, views, staleness
server/repositories/      SQLite repositories
server/migrations/        ordered SQL migrations
client/                   phone page and current dashboard assets
tests/                    unit, integration, synthetic phone, model tests
scripts/                  model provisioning and STT measurements
docs/                     authoritative product and behavior contracts
logs/                     tracked implementation evidence and handoffs
convene-tasks/            local ignored work orders, CON-01 through CON-15
old_docs/                 original DT-17 transport test and HTTPS notes
data/                     private runtime database/exports; ignored by Git
```

`bulwark_docs/` and `tasks/` belong to another project and are reference material only. They do not define Convene.

## Scope boundaries

Do not add these unless the project owner explicitly changes scope:

- Accounts, login, authorization, or multi-tenancy.
- Cloud hosting or production deployment architecture.
- Native mobile applications.
- Calendar, email, Slack, or other third-party integrations.
- Translation or multilingual support.
- A paid API dependency.
- General meeting scheduling or invite management.
- Automatic retrieval or unsolicited model-generated commentary.
- Production-scale distributed infrastructure.

## How an AI should work on this project

If repository access is available:

1. Read `AGENTS.md` before changing anything.
2. Check `git status` and preserve unrelated user changes.
3. Read the relevant numbered work order in `convene-tasks/` and its authoritative topic documents.
4. Inspect actual code and tests. Documentation is not proof that a feature exists.
5. Respect dependency order and make the smallest coherent change.
6. Update the authoritative contract if an API, schema, event, or behavior changes.
7. Record substantial work, decisions, commands, results, and untested checks in the appropriate `logs/*.md` file.
8. Run focused tests, then the full suite when appropriate.
9. Report hardware/model checks as `Passed`, `Failed`, or `Not run (reason)`.
10. Never equate a unit test, mock, or synthetic phone with physical-phone validation.
11. Do not commit unless the user explicitly authorizes that specific commit. Never add AI attribution or
    `Co-authored-by` trailers. Do not push, open a PR, or merge without separate authorization.

When a requested change conflicts with a locked architectural decision, explain the conflict and ask before
changing it. When a contract is incomplete, resolve and document it before dependent code guesses a shape.

## Authoritative documents when deeper detail is needed

- Product and priorities: `docs/project-context.md`, `docs/requirements.md`
- Architecture and rationale: `docs/architecture.md`, `docs/decisions.md`
- Stored entities and audit catalog: `docs/data-model.md`
- REST and WebSocket contracts: `docs/api.md`
- WebRTC and reconnect behavior: `docs/transport.md`
- Audio/STT behavior: `docs/stt-pipeline.md`, `docs/models.md`
- Attribution and correction: `docs/speaker-attribution.md`
- RAG/Q&A: `docs/rag-and-qa.md`
- Summary and action items: `docs/summarization.md`
- DOCX generation: `docs/export.md`
- UI ownership and behavior: `docs/frontend.md`
- Running and validating: `docs/deployment.md`, `docs/testing.md`, `docs/manual-tests.md`, `docs/demo.md`
- Dependency order: `docs/implementation-plan.md`, `convene-tasks/ALL-TASKS.md`

If this standalone brief and a topic document disagree, the current code plus the topic document and applicable ADR
win. Flag the discrepancy instead of silently choosing whichever version is more convenient.
