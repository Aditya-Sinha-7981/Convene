# Requirements

## Success criteria (demo-day definition of done)

Convene is successful if, live and without a network connection to the internet required:

1. 2–10 phones join a local Wi-Fi network and connect to the laptop server.
2. Each phone streams live audio continuously; the laptop shows connection state and audio activity per phone.
3. A live transcript appears on a dashboard, with each line correctly attributed to the speaking participant, within a few seconds of being spoken.
4. A temporary Wi-Fi interruption on any phone recovers automatically under the same participant identity, without restarting the meeting.
5. A person can ask a question about the conversation while it is still happening and get an answer grounded in what has actually been said so far, with a citation back to who said it and when.
6. At meeting end, a summary and action-item list are generated and can be exported as a DOCX file.
7. A person can later open a past meeting and ask questions across it (or across multiple meetings).
8. If two or three people share one phone, the system still produces per-person attribution for that device — imperfectly is acceptable, silently wrong is not; low-confidence attributions are visibly flagged and correctable.

## Must-have (the demo depends on these; build and stabilize first)

| Feature | Why it's must-have |
|---|---|
| WebRTC phone→laptop transport with reconnect | Nothing else exists without this (inherited from DT-17, already validated) |
| Per-device participant identity | This is the mechanism that makes attribution reliable without ML in the common case |
| Live transcription (local STT) | The core visible output of the whole system |
| Live transcript dashboard | The judge needs to *see* the thing working, not take it on faith |
| Live in-meeting Q&A (RAG over the current meeting) | This is the single strongest demo moment — see `demo.md` |
| Confidence scoring + manual correction on utterances | Directly answers the "what if a label is wrong" objection, and is required for the shared-device case to be honest rather than silently wrong |
| End-of-meeting summary + action items | Closes the pitch loop — "record it" becomes "record it and act on it" |
| DOCX export | The tangible artifact a judge can hold; cheap once summary data exists |
| Fully offline operation | The core differentiating claim in the pitch |

## Should-have (build only after must-haves are demo-stable)

| Feature | Why it's second-tier |
|---|---|
| Shared-device speaker enrollment + diarization | Real and useful, but only needed when phones < people; most groups will have 1:1 |
| Cross-meeting history search / RAG across past meetings | Strengthens the "knowledge base" pitch, but a single strong in-meeting Q&A demo already proves the concept |
| Action items as a tracked checklist (owner, status) | Nice polish on top of already-extracted action items; not required for the core loop |
| Meeting history dashboard (list/search past meetings) | Needed as the front door to cross-meeting RAG; build alongside it, not before |

## Nice-to-have (only if there is meaningfully spare time)

| Feature | Why it's deferred |
|---|---|
| Export as PDF/Markdown in addition to DOCX | One format is enough to prove the concept |
| Speaker-correction UI polish inside the exported document, not just the live view | Cosmetic once the underlying data already carries corrections |
| Cloud STT/LLM fallback wired in and tested | Local-first is the plan; only worth wiring if local proves genuinely insufficient in testing |
| Dedicated portable Wi-Fi AP comparison | Directly inherited from DT-17's own deferred item — not required to prove the product |

## Explicitly out of scope (do not build)

- User accounts, login, or any auth system — a meeting ID is the access boundary.
- Multi-tenant / cloud deployment of any kind.
- A production-grade polished frontend — functional and legible beats visually elaborate.
- Calendar, Slack, email, or any third-party integration.
- Mobile native apps — the phone side is a browser page, nothing more.
- Real-time translation / multi-language support.
- Anything requiring a paid API tier of any kind.
- General-purpose meeting management (scheduling, invites, recurring meetings).

## Non-goals of this document set

These docs describe a hackathon-scale system for one laptop and up to ~10 phones in one room. They deliberately do not address: multi-organization deployment, horizontal scaling, production security hardening, or long-term data retention policy. Where a decision trades those away for simplicity, `decisions.md` says so explicitly rather than leaving it implicit.
