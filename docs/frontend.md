# Frontend

## Principle

Functional and legible beats visually elaborate (`requirements.md`). The frontend renders state and calls existing APIs — it holds no business logic of its own (`architecture.md` responsibilities table).

## Surfaces

### 1. Join page (phone)

- Meeting ID/QR entry (or pre-filled from the join link).
- Name entry.
- Optional colour picker (ADR-25): 12 swatches, each the mascot's head in a palette colour; colours already used in the
  meeting are greyed out and cannot be chosen. Skipping it lets the server assign an unused colour. Tapping the picked
  swatch again clears it. After joining, the picker collapses to the person's own colour, which a rejoin keeps.
- "Are you the only person using this phone?" toggle → if no, "how many people?" and a per-person enrollment flow (record a short sample each) before the page proceeds to `connected` state — see `speaker-attribution.md`.
- A one-line consent notice ("this session's audio is being transcribed") — cheap, avoids an awkward question, per `project-context.md`'s "what else is missing" discussion.
- Connection-state indicator, reconnect happens automatically and silently unless it fails repeatedly, in which case show a clear retry action.
- A guide (brand mascot and a one-line message) follows the connection status. While connecting or reconnecting the
  mascot walks across its track; once live it shows the participant's own avatar. It never covers the controls.
- The phone surface is deliberately light: locally served system fonts and small local SVGs, but no remote assets,
  video, heavy canvas effect, or library may delay microphone setup. Respect `prefers-reduced-motion` (the mascot
  stands still).

### 2. Live dashboard (laptop, shown during the meeting)

- Rolling transcript, auto-scrolling, laid out like a group chat: each speaker's avatar (the mascot's head in their
  colour, ADR-25) and name open a bubble, and their consecutive lines continue it. Grouping is display-only; every
  utterance keeps its own row, timestamp, correction action and citation target. A new bubble starts when the speaker
  changes, after 2 minutes of silence from them, or when a line's `low_confidence` differs, so an uncertain line is
  never merged into a confident run.
- Low-confidence / generic-label lines visually distinguished (not hidden — visible but flagged, per `speaker-attribution.md`).
- Click-to-correct on any line (opens a small participant picker, calls the correction endpoint, `api.md`).
- Connection-health panel: per-device state, last-audio-age, reconnect count — carried directly from `transport.md`'s metrics.
- Q&A input box: ask a question, see the answer plus which speaker(s)/timestamp(s) it was grounded in (`rag-and-qa.md`). This is the panel that carries the live-Q&A demo moment (`demo.md`) — it should be prominent, not buried. Implemented (CON-09) in the right-hand "Ask the room" panel: Enter or **Ask** submits; one question at a time with a pending card and elapsed seconds; results newest first, deduplicated by `query_id` between the HTTP response and the `qa_answer` push. `answered` shows the answer as plain text, with its sources behind a collapsed **Sources (n)** toggle: one button per citation (speakers and time) that scrolls to and highlights the cited transcript lines (ADR-26); `no_grounding` says "Not discussed in this meeting so far." with the server's reason in words; `failed` is styled as an error, names the system problem, and offers **Try again**. The panel only displays server outcomes; it has no answer logic. After the meeting ends the input is disabled. A reload does not restore earlier answers (ADR-15, G18).
- "End meeting" action, which triggers summarization and moves the meeting to `ended`.
- Keep the live transcript visually dominant. The light theme uses the brand indigo for Convene's own actions and
  state (live, connected), participant colours only for identity, amber only for server-provided `low_confidence`
  ("Needs review"), and red only for connection or system problems; it must never make uncertainty resemble a
  confirmed attribution. Arriving lines, gauges and timers update without animation.
- The mascot appears only where it does not compete with live data: the empty Q&A panel (full mascot) and a pending
  question (its head "reading the conversation", no walking). The walking mascot is reserved for longer processing
  (joining, writing the summary).
- Now and then the mascot pops in at the bottom-right corner with one light line ("No more 'wait, who said that?'")
  and leaves after a few seconds or on a click: first after about a minute, then every 4 to 7 minutes, and only while
  the meeting is live and nothing needs attention (no pending question, no open correction, connection in sync). The
  homepage shows at most three; on the phone the guide's line changes for a few seconds instead. Decorative only:
  hidden from assistive technology.

### 3. Post-meeting view

- Summary text, action items (with owner if inferred), all speaker-attributed and confidence-aware exactly as the live view was.
- Summary status (pending, ready, failed with its reason), a staleness notice with a regenerate action, and the transcript, readable whatever the summary's status. Served at `/meetings/{meeting_id}` for a live meeting too, as the "summarize now" entry point (the dashboard links to it and goes there after "End meeting"). CON-10 built a deliberately minimal version (`client/post_meeting.html`); the planned UI redesign replaces its look.
- Export/download button (DOCX).
- Q&A box remains available in history mode against this specific meeting.

### 4. Meeting history view

- List of past meetings (title, date, participant count).
- Entry point into cross-meeting Q&A (`mode: "history"`) — search across some or all past meetings.
- This is the required front door for the should-have cross-meeting RAG feature (`requirements.md`) — build them together, not history-search before there's a place to launch it from, and not the reverse either.

## What the frontend explicitly does not do

- No client-side attribution logic, no client-side model inference of any kind — it only displays what the server has already computed and calls existing endpoints for actions (corrections, questions, ending the meeting).
- No offline/local caching layer beyond what's needed for basic page reliability — this is a single-laptop LAN app, not a resilient distributed client.
- No remote fonts, CDN scripts, analytics, or external visual assets. `prefers-reduced-motion` removes decorative
  motion while retaining immediate functional state changes.
