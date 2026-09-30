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
- Once registered with a microphone, the page switches to a live view: the name field, colour picker and Join button
  give way to an orb in the participant's colour, with their avatar in the middle, that swells and ripples with the
  microphone level (read locally from the stream being sent; never played back) and says "Hearing you" or
  "Listening…". Two controls stay: **Pause microphone** mutes the sent track (the phone keeps its connection and its
  place in the meeting, the server receives silence, and the orb says "Paused"; Resume turns it back on, and a paused
  phone stays paused across a reconnect), and **Leave meeting** sends `leave`, releases the microphone and returns
  to the form. Neither control is shown before joining.
- The microphone is requested with echo cancellation and noise suppression on and automatic gain control off, so a
  phone far from a speaker stays quieter than the near one (the bleed filter relies on it, ADR-32). Browsers treat
  these as hints.
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
- Action items are editable in place (CON-16). Each shows its owner, due date, status and note count. The owner is picked from this meeting's participants or "Unassigned"; the due date is a date field with a clear action; the status buttons (Open, Done, Cancelled) take one click. "Add note" appends a note from this meeting, and "Notes (n)" shows the existing ones. Every change is sent to the server, and the row redraws from the server's reply. A failed save shows the error and leaves the row showing the stored values; nothing is updated optimistically. The Regenerate and Summarize actions warn that edits to the current action items will not carry over to a new summary (ADR-28).
- Summary status (pending, ready, failed with its reason), a staleness notice with a regenerate action, and the transcript, readable whatever the summary's status. Served at `/meetings/{meeting_id}` for a live meeting too, as the "summarize now" entry point (the dashboard links to it and goes there after "End meeting"). CON-10 built a deliberately minimal version (`client/post_meeting.html`); the planned UI redesign replaces its look.
- Export/download button (DOCX).
- Q&A box remains available in history mode against this specific meeting. Implemented (CON-14): an "Ask about this
  meeting" panel labelled "History · This meeting only", open once the meeting has ended (while it runs, the panel
  points to the live dashboard instead). The page also lists the participants, and each transcript line has the
  anchor `#u-<utterance_id>` that history citations link to; landing on one highlights it.

### 4. Meeting history view

- List of past meetings (title, date, participant count).
- Entry point into cross-meeting Q&A (`mode: "history"`) — search across some or all past meetings.
- This is the required front door for the should-have cross-meeting RAG feature (`requirements.md`) — build them together, not history-search before there's a place to launch it from, and not the reverse either.
- Implemented (CON-14) at `/history` (linked from the home page nav and the post-meeting header;
  `client/history.html`, `history.js`, and `history_qa.js`, which the post-meeting page shares). Meetings are listed
  newest first with title, date, participant count, status, and Summary/DOCX badges. There's a title search and
  From/To date filters, 50 meetings per page with "Show more". An ended meeting's title opens its post-meeting view,
  and a running one opens its dashboard. Only ended meetings have a selection checkbox. The Q&A panel chooses "All
  ended meetings" or "Selected meetings", and a "History" tag with the scope in words ("Searching 3 meetings") is
  always visible above the question. Answer cards repeat that tag. Their three outcomes are styled like the live
  panel's. Each source names its meeting, date, speakers and time within that meeting, and links to that line in
  the meeting's transcript. Meetings that were only partly searched, or not searched at all, are listed under the
  answer. Answers are not restored after a reload.
- Each row also has **Rename** (an inline title editor) and **Delete**. Delete opens a dialog that stays disabled until
  the exact meeting title is typed, then erases the meeting permanently (ADR-27). The post-meeting view has the same
  two actions: Rename next to the title, and a "Delete meeting" section at the bottom that returns to `/history`
  afterwards. Shared code is in `client/meeting_actions.js`.

### Action items

- Implemented (CON-16) at `/action-items` (`client/action_items.html`, `action_items.js`, `action_items.css`), linked
  from the home page nav, `/history` and the post-meeting header. It lists action items across all meetings from
  `GET /api/action-items`. The server decides what appears: open items by default, sorted by due date (no due
  date last), 50 per page with "Show more". Filters: status (Open, Done, Cancelled, All), owner name, due from/to,
  "Overdue only". Sort: due date or recently changed. Each row shows the item, its owner, due date, status, note
  count and last change ("edited" or "from summary"), and the originating meeting's title and date, linking to
  `/meetings/{meeting_id}`.
- Rows have the same inline edits as the post-meeting view, but the owner picker lists the participants of the
  item's own meeting, read from that meeting. "Add note" asks which meeting the update came from, offering ended
  meetings newest first, and can mark the item done in the same request. Overdue is shown with the error colour
  and the word "Overdue", never with amber, which is reserved for "Needs review". The server computes `overdue`.

### Reports (CON-18)

- Implemented at `/reports` (`client/reports.html`, `reports.js`, `reports.css`), linked from the home page nav and the
  `/history` intro. From/To date inputs (the same UTC dates as the `/history` filter) and three presets (This month,
  Last month, This quarter) that compute plain dates in the browser; the server has no preset logic. A live preview
  line from `GET /api/reports/preview` reads, for example, "7 meetings (6 with summaries) · 2 not yet ended,
  excluded". The download button is a link to `GET /api/reports/download` and is disabled for an invalid range, an
  empty range, or a range over the cap. The page defaults to this month.

### Demo database inspector

- Implemented at `/database`: a laptop-only, read-only view over the same SQLite file that stores meetings. It shows
  application-table counts and bounded rows so a judge can see persisted meetings, transcript lines, audit events,
  summaries, and exports. It is not a SQL console and exposes no write action.

### Policy repository (CON-17)

- `/policies` lists local policy documents and accepts PDF/DOCX upload. `/policies/{policy_id}` shows every immutable version, its processing/failure state, extracted read-only text, and a download of the retained original. A pending upload never blocks a live meeting UI.
- History Q&A makes policy scope explicit. Meeting citations retain their transcript appearance; policy citations are labelled as a policy and identify the title/version rather than speakers and timestamps.

## What the frontend explicitly does not do

- No client-side attribution logic, no client-side model inference of any kind — it only displays what the server has already computed and calls existing endpoints for actions (corrections, questions, ending the meeting).
- No offline/local caching layer beyond what's needed for basic page reliability — this is a single-laptop LAN app, not a resilient distributed client.
- No remote fonts, CDN scripts, analytics, or external visual assets. `prefers-reduced-motion` removes decorative
  motion while retaining immediate functional state changes.
