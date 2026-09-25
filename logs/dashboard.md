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
