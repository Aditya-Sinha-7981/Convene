# logs/dashboard.md

## CON-07 — Live dashboard and correction UI

### Summary (2026-09-23)

- Replaced the CON-04 raw dashboard event log with a static, offline-first dark dashboard.
- Added a pure ES-module reducer for snapshot/event reconciliation, transcript ordering, de-duplication, and
  complete-row replacement after a correction.
- Added device health, live transcript, low-confidence/server-label presentation, correction dialog, stale-feed
  banner, QR/join card, End Meeting action, and reserved Q&A panel.
- Reworked the phone join page visually without changing its registration, microphone, signaling, or reconnect code.
- All fonts, CSS, and JavaScript are locally served. No package manager, framework, CDN, remote asset, or server/API
  contract was introduced.

### Motion and accessibility

- Entrances and transcript additions use transform/opacity only; gauges do not animate.
- Cursor glow is desktop-only and skipped for touch and reduced-motion users.
- `prefers-reduced-motion` collapses decorative animation while leaving state changes immediate.
- Low confidence uses the server boolean only; the client contains no confidence threshold.

### Automated verification

- `node --test tests/js/dashboard_state.test.mjs`: reducer ordering, dedupe, update, snapshot race, gauges, and
  1,500-line ordering checks.
- `.venv/bin/python -m pytest tests/test_dashboard_feed.py tests/test_join_page.py -q`: **20 passed**;
  dashboard feed, correction event, snapshot cursor, meeting isolation, and phone-page regression checks.
- `.venv/bin/python -m pytest tests -q`: full offline suite passed (exit 0) after the UI changes.

### Hardware checks

- **Passed (2026-09-25, one real participant phone):** the laptop dashboard received and displayed attributed local
  STT rows after QR join and microphone permission through the trusted hostname.
- Two real phones/dashboard, Wi-Fi interruption, correction reload, restart resync, projector legibility, reduced
  motion/touch behavior, and offline laptop page load: **Not run.**

### Handoff

- CON-09 fills the reserved Q&A panel and adds `qa_answer` rendering.
- CON-10/11 add post-meeting summary/export UI; no placeholder behavior was invented here.
- Projector contrast and cursor-glow frame cost must be judged on the demo laptop. Disable cursor glow if it costs
  transcript legibility or update responsiveness.

## 2026-09-26 — Brand, participant colours, chat-style transcript (branch UI-testing)

**Changed.** Traced the monogram PNG to SVG (`client/brand/logo.svg`, `logo-mark.svg`); the logo and core mascot use
the brand indigo, which is excluded from participant colours. Participant colours (ADR-25): 12-key palette, join-page
picker with taken colours greyed, server assignment when skipped, kept on rejoin. Dashboard transcript grouped into
chat bubbles per speaker run with mascot-head avatars (display-only grouping; rows, corrections and citations stay
per utterance). Walking mascot while a phone connects and while the summary is written; a static "reading the
conversation" avatar for a pending question. Post-meeting page moved onto the shared theme (CSS only). Arriving lines
no longer animate. STT limited to English and romanized Hindi (ADR-24, `logs/stt.md`).

**Verified.** Full suite 594 passed (11 `model` deselected); `-m model` STT tests 6 passed; dashboard reducer 7
passed; join-page harness 19 scenarios. Screenshots in headless Chromium against mocked API/WebSocket data at 1440,
1280, 1024 and 390 px: grouping, low-confidence bubbles, correction dialog with avatars, Q&A pending and answered,
colour picker (tap-to-clear, taken colours unselectable), walking and reduced-motion states, summary pending. No
console errors or failed requests.

**Not run.** Real phones (picker on iOS Safari and Android Chrome, colour kept across a real rejoin), projector
legibility of 12 colours, real Hindi speech, the full demo on the reference laptop.

## 2026-09-26 — Colour fix, calmer Q&A answers, mascot pop-ins

**Colour not applied (real Android phone).** The stored registration had a server-assigned colour: the phone ran a
heuristically cached `app.js` from before the `color` field, because `/static` had no `Cache-Control`. Static files
now send `Cache-Control: no-cache` (ETag revalidation, 304 when unchanged; test added). A phone that already joined a
meeting now loads with its picker locked to its colour, and says so if a different colour was tapped.

**Q&A (ADR-26).** Prompt asks for plain answers without times or brackets; `clean_answer` strips leftover inline
references; sources sit behind a collapsed "Sources (n)" toggle that survives the pending-question redraw. A longer
style prompt was tried first and broke the real-model honesty test (non-answers instead of `NO_GROUNDING`); the final
one-sentence change passes it (`-m model`: 2 passed).

**Pop-ins.** Mascot corner pop-ins on the dashboard (only when live, synced, no pending question or open dialog;
first after 45–75 s, then every 4–7 min) and the homepage (max 3); on the phone the guide line changes briefly.

**Verified.** Full suite 600 passed; screenshots of collapsed/open sources, pop-in, locked picker and phone quirk line,
no console errors. **Not run:** the colour fix on the real phone (needs a server restart and one fresh join).
