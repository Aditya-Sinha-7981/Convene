# Frontend

## Principle

Functional and legible beats visually elaborate (`requirements.md`). The frontend renders state and calls existing APIs — it holds no business logic of its own (`architecture.md` responsibilities table).

## Surfaces

### 1. Join page (phone)

- Meeting ID/QR entry (or pre-filled from the join link).
- Name entry.
- "Are you the only person using this phone?" toggle → if no, "how many people?" and a per-person enrollment flow (record a short sample each) before the page proceeds to `connected` state — see `speaker-attribution.md`.
- A one-line consent notice ("this session's audio is being transcribed") — cheap, avoids an awkward question, per `project-context.md`'s "what else is missing" discussion.
- Connection-state indicator, reconnect happens automatically and silently unless it fails repeatedly, in which case show a clear retry action.

### 2. Live dashboard (laptop, shown during the meeting)

- Rolling transcript, grouped/labeled by speaker, auto-scrolling.
- Low-confidence / generic-label lines visually distinguished (not hidden — visible but flagged, per `speaker-attribution.md`).
- Click-to-correct on any line (opens a small participant picker, calls the correction endpoint, `api.md`).
- Connection-health panel: per-device state, last-audio-age, reconnect count — carried directly from `transport.md`'s metrics.
- Q&A input box: ask a question, see the answer plus which speaker(s)/timestamp(s) it was grounded in (`rag-and-qa.md`). This is the panel that carries the live-Q&A demo moment (`demo.md`) — it should be prominent, not buried.
- "End meeting" action, which triggers summarization and moves the meeting to `ended`.

### 3. Post-meeting view

- Summary text, action items (with owner if inferred), all speaker-attributed and confidence-aware exactly as the live view was.
- Export/download button (DOCX).
- Q&A box remains available in history mode against this specific meeting.

### 4. Meeting history view

- List of past meetings (title, date, participant count).
- Entry point into cross-meeting Q&A (`mode: "history"`) — search across some or all past meetings.
- This is the required front door for the should-have cross-meeting RAG feature (`requirements.md`) — build them together, not history-search before there's a place to launch it from, and not the reverse either.

## What the frontend explicitly does not do

- No client-side attribution logic, no client-side model inference of any kind — it only displays what the server has already computed and calls existing endpoints for actions (corrections, questions, ending the meeting).
- No offline/local caching layer beyond what's needed for basic page reliability — this is a single-laptop LAN app, not a resilient distributed client.
