# AI Context — Read This First

This is the entry point to Convene's architecture documentation. If you are an agent (or a person) about to write code, tasks, or tests against this system, read this file fully, then read the documents below in the listed order before writing anything. Every document here is authoritative for its own topic — if two documents appear to disagree, `decisions.md` wins for *why* a choice was made, and the topic-specific document wins for *how* it is implemented; if that disagreement can't be resolved by that rule, it's a documentation bug, flag it rather than guessing.

**Implementation status:** The checked-in system implements the demo path through CON-07: WebRTC transport, meeting/device registry, local STT, dedicated-device attribution, manual correction, and the live dashboard. Shared-device enrollment/classification, live Q&A, summary, DOCX export, and history remain planned. Real-phone, Wi-Fi, and offline behavior are not yet validated; see [manual-tests.md](manual-tests.md). See [documentation index](README.md) for navigation and [implementation plan](implementation-plan.md) for the build sequence.

## What Convene is, in one paragraph

Convene turns a group of smartphones into a distributed, correctly-attributed meeting microphone array. Each phone streams audio live over local Wi-Fi via WebRTC to one laptop, which transcribes it, attributes every line to the correct speaker (using device identity as the primary mechanism, not error-prone acoustic diarization), answers questions about the conversation while it's still happening, and produces a speaker-labeled summary and DOCX export at the end. Models and meeting traffic run locally; the trusted-host hotspot join path needs weak internet only for DNS preflight and initial name resolution (ADR-20). Cloud AI remains a manually-triggered fallback, never a dependency. It is being built to demo at a hackathon, and every design choice in this doc set is shaped by that fact.

## The one idea to understand before anything else

Read `project-context.md`'s "core insight" section. Everything about how speaker attribution, confidence scoring, and correction work (`speaker-attribution.md`, ADR-02 through ADR-04 in `decisions.md`) flows from this single idea: a phone-per-speaker system gets speaker separation for free from *which device the audio arrived on*, not from analyzing the audio itself. The only place real diarization is needed is the shared-device exception, and even there it's a smaller, better-conditioned version of the problem, handled by classifying against known enrolled voices rather than guessing blind.

## Reading order

1. **`project-context.md`** — the pitch, the core insight, why this is being built and for what (a hackathon demo, not a production system). Read first, always.
2. **`requirements.md`** — success criteria and the locked must-have / should-have / nice-to-have / out-of-scope split. Read second — it tells you what's actually worth building and in what order.
3. **`architecture.md`** — the full component diagram, responsibilities and boundaries, lifecycles, and failure boundaries. The structural map of the whole system.
4. **`decisions.md`** — the ADR set. Every non-obvious choice referenced anywhere else in this doc set is explained here, with alternatives considered and tradeoffs stated honestly. Read before questioning or changing any locked decision.
5. **`data-model.md`** — authoritative schema for every persisted entity. Other docs reference these shapes, never redefine them.
6. **`transport.md`** — the WebRTC/signaling layer (inherited from the already-validated DT-17 prototype).
7. **`stt-pipeline.md`** — VAD, windowing, the STT adapter contract, worker scheduling.
8. **`speaker-attribution.md`** — the full attribution state machine: device-based path, shared-device enrollment/classification path, confidence scoring, manual correction. The most important document after `project-context.md` and `decisions.md`, given how central this problem is to the product.
9. **`models.md`** — resource-type → concrete model mapping, the M4 Pro memory budget, and the model-swap procedure.
10. **`rag-and-qa.md`** — chunking, retrieval, live vs. history mode, citation and honesty requirements.
11. **`summarization.md`** — end-of-meeting summary and action-item generation, structured-output contract.
12. **`export.md`** — deterministic DOCX rendering from structured summary data.
13. **`api.md`** — the full REST/WebSocket surface tying every component above together.
14. **`frontend.md`** — the four UI surfaces (join page, live dashboard, post-meeting view, history view) and what each does and does not own.
15. **`deployment.md`** — how this actually gets run: network setup, HTTPS/mkcert, model pre-caching, startup sequence.
16. **`testing.md`** — extends the original DT-17 transport test plan with the new failure class (F6: misattribution) and test cases for every new layer.
17. **`demo.md`** — the exact rehearsed demo script, anticipated questions, and the failure-recovery plan.

## Locked decisions — do not relitigate without reading `decisions.md` first

- Device-per-speaker attribution is the primary mechanism; acoustic diarization is scoped only to the shared-device exception (ADR-02, ADR-03).
- Every utterance carries a confidence score and is manually correctable, always (ADR-04).
- Local models are the default execution path for everything; cloud is opt-in, manual, never automatic (ADR-06).
- One Python process (FastAPI + aiortc), not a split Node/Python architecture (ADR-01).
- SQLite + `sqlite-vec`, one file, no separate database or vector store process (ADR-07, ADR-08).
- Retrieval only ever runs on an explicit user question, never automatically (ADR-11).
- Export rendering is deterministic code over structured model output — the model never authors the document directly (ADR-12).
- Model references are always indirect, through a resource-type mapping (ADR-14, `models.md`).

## What is explicitly out of scope

See `requirements.md`'s "explicitly out of scope" and "non-goals" sections. In short: no accounts/auth, no cloud deployment, no third-party integrations, no translation or multilingual product workflows, nothing requiring a paid API tier. Local STT may auto-detect spoken-language segments; that narrow capability does not broaden the product into a translation feature. If a task or a piece of code starts to require any of these, stop and check `requirements.md` — it likely means the task has drifted outside what this system is meant to be.

## If you are generating a tasks/build plan from this document set

Order work by the must-have list in `requirements.md`, in roughly this sequence: transport (already validated) → device/participant registry → STT pipeline with VAD → device-based attribution (the non-shared path, which needs no ML) → live dashboard → chunking/embedding/RAG for live Q&A → summarization → deterministic export → shared-device enrollment/classification (should-have) → cross-meeting history (should-have). This ordering exists because the must-have list defines the demo-critical path, and the should-have items are additive on top of a working core, not prerequisites for it — see `demo.md` for exactly what the rehearsed script depends on.

## Style this doc set follows, if extending it

Every document: states what it's authoritative for, cross-references rather than duplicates other documents' content, includes a "what this deliberately does not do" section where relevant, and states tradeoffs honestly rather than only benefits. Decisions are ADRs with rationale, alternatives considered, and tradeoffs — not bare assertions. Keep new documents in this same shape.
