# Project Context

## One-paragraph summary

Convene turns any group of smartphones into a distributed, correctly-attributed meeting microphone array. Each phone is a dedicated mic for one (or a declared few) participant(s), streamed live over local Wi-Fi via WebRTC to one laptop, which transcribes, attributes, and stores every utterance, answers questions about the conversation while it is still happening, and produces a speaker-labeled summary and exportable minutes at the end — all running fully offline on a single machine, with cloud AI as an optional, manually-triggered upgrade, never a dependency.

## The core insight (read this before anything else)

Most meeting-transcription products diarize speakers from one mixed microphone, which is inherently a guessing problem — voices get confused, and a wrong label silently corrupts the record. Convene sidesteps that: because each phone is already a separate audio channel tied to a known participant, **speaker attribution is mostly a bookkeeping problem, not a machine-learning problem.** The only place ML-based diarization is needed at all is the shared-device case (2-3 people on one phone), which is a smaller, better-conditioned version of the same problem everyone else has to solve for the whole meeting. Every architectural decision downstream of this (see `decisions.md`, ADR-02) exists to preserve this property and to make the exception (shared devices) safe rather than silent.

## What Convene is being built for

A hackathon demo. This shapes real decisions across every doc in this set:

- **Reliability under judge scrutiny beats theoretical completeness.** A feature that's 80% solid and might fail live is worse than a smaller feature that never fails. See `requirements.md` for the explicit must-have/nice-to-have split this produces.
- **Explainability matters as much as function.** Every non-obvious design choice should have a one-sentence defensible answer ready, because it will be asked about. `decisions.md` exists specifically so that answer is already written down, not improvised on stage.
- **Offline-first is a product claim, not just an engineering constraint.** "Works with the Wi-Fi unplugged, zero per-meeting API cost" is a pitch line, not an implementation detail — see ADR-06 in `decisions.md`.
- **Scale target is small and known**: 1–10 phones, one meeting at a time, one laptop, one demo session. Nothing here should be designed for production multi-tenant scale — see `requirements.md` non-goals.

## The pitch, in one breath

"Most transcription tools give you a wall of text you never read again. Convene turns every meeting into a queryable, correctly-attributed knowledge base — ask it a question mid-meeting and get an answer sourced from what was just said, or ask it weeks later what your team decided and who owns it. And it works with zero internet dependency, because we solved speaker attribution at the hardware layer instead of guessing from audio."

## Relationship to DT-17

DT-17 is the codename of the transport-only prototype that came first (see `transport.md`) — it proved phone-to-laptop WebRTC audio delivery works reliably on a local network, with reconnect handling and participant isolation. Convene is the full product built on top of that validated transport layer. Nothing in the transport layer is being redesigned; everything else in this doc set is new.

## Document map

See `00-AI-CONTEXT.md` for the full reading order and a summary of every document in this set. Start there if this is your first pass through the project.

## Non-negotiable framing for anyone extending this system

If you are about to add a feature, a dependency, or a code path, ask: does this make the core demo (phones join → live correctly-labeled transcript → live Q&A → summary/export) more likely to work reliably on stage, or does it just make the system bigger? If the honest answer is the latter, it belongs in `requirements.md`'s nice-to-have list, not in the build.
