# Emailing the minutes (ADR-33)

## Implementation — 2026-09-30

Branch `feature/email-report`, from `main` at `9c31e73`. Asked for after the first review round: an optional email on
the join page, and a post-meeting button that emails the DOCX minutes to those people through Resend (the user's
domain is verified there).

### Decisions (ADR-33)

- The address is stored in its own table, `ParticipantEmail` (migration 0011), not on `Participant`. Every participant
  view, dashboard push, `device_registered` payload and the `/database` inspector (the table is not in its list) stays
  free of it. Meeting delete erases it.
- `email` on `POST /api/meetings/{id}/devices`: optional, non-shared devices only, trimmed, loosely checked (one `@`, a
  dot in the domain, no spaces or `<>,;"`, at most 254 characters). Unlike the rest of an idempotent replay, a non-empty
  `email` on rejoin is stored. A changed address resets `last_sent_at`.
- Only the button sends (`POST /api/meetings/{id}/email`). Nothing sends when a meeting ends. It needs an ended
  meeting, a Resend configuration, at least one address, and a ready summary. The attachment is the same file
  `export.ensure()` gives the download, rendered first if needed.
- One message per recipient, so no one sees another person's address. Resend's batch endpoint takes no attachments.
  The sends are about 0.6 s apart (Resend's default limit is 2 per second). A 429 or 5xx is retried once after 1.5 s.
  One refused address does not stop the rest.
- Configured only by the environment: `RESEND_API_KEY` and `CONVENE_MAIL_FROM` in `mail.env` (gitignored by
  `*.env`; `mail.env.example` is tracked). `start_demo.sh` loads it next to `stt.env`. With a key and no valid sender,
  startup fails. With no key, the page says email is not set up. The key never enters `Settings`, logs,
  `repr`, or errors.
- Stdlib `urllib` like the Gemini connector, no new dependency. It sends an explicit `User-Agent`, because Resend's
  edge may refuse Python's default one.
- Audit `minutes_emailed` (`export_id`, `sent_participant_ids`, `failed_participant_ids`). It holds ids only, never
  an address.
- The post-meeting page shows masked addresses (`pr•••@example.com`), since it may be on the projector.
- Mascot voice: subject "Your words, delivered: <title>"; body "I sat in on this one and wrote everything down, so you
  didn't have to … I didn't put any words in your mouth. Promise. - Convene". The wording is in
  `server/mail/message.py`.

### What was built

- `server/migrations/0011_participant_email.sql`, `server/repositories/participant_emails.py`.
- `server/registry.py`: `email` on `register_device`, `normalize_email`, erase includes `ParticipantEmail`.
- `server/mail/resend.py` (connector and env config), `server/mail/message.py` (subject, bodies, attachment name),
  `server/mail/service.py` (status and send).
- Routes `GET`/`POST /api/meetings/{id}/email`; error codes `mail_not_configured`, `no_recipients`,
  `email_in_progress` (409). `meeting_not_ended` and `summary_not_ready` are reused.
- `create_app(..., mailer=)`, `Runtime.email`; `server.app.main` reads the environment and prints whether email is on.
- Join page: optional email field (browser check before the microphone prompt, remembered per meeting like the
  name, hidden when live). Post-meeting page: an "Email the minutes" section with recipients, a reason line when
  sending is not possible, Send / Send again (with a confirmation), and per-address results.
- Docs: `api.md`, `data-model.md` (table, event), `decisions.md` ADR-33, `export.md`, `frontend.md`, `requirements.md`,
  `feature-status.md`, `demo-features.md` (beat 9b and setup).

### Checks

```sh
.venv/bin/python -m pytest tests/test_email_minutes.py tests/test_join_page.py tests/test_api_contract_docs.py tests/test_audit_emit.py tests/test_db_migrations.py -q
.venv/bin/python -m pytest -q -m "not model"
```

- `tests/test_email_minutes.py`: 19 passed. They cover registration (stored apart, never returned or audited;
  invalid and shared rejected; rejoin add, change and keep; erase) and the connector against a fake `urlopen`
  (request shape, base64 attachment, one retry, the key absent from errors and `repr`, the no-internet message, env
  config). They also cover message escaping and the attachment name, and the API with a fake mailer: one sent and one
  refused, the attached DOCX opened with python-docx, the audit payload, and no address anywhere in `AuditEvent`. The
  refusals `meeting_not_ended`, `no_recipients`, `summary_not_ready`, `mail_not_configured` and 404 send nothing.
- Join-page harness: 2 new scenarios (email sent trimmed and remembered; an invalid email stops the join before the
  microphone prompt). The happy-path body now includes `email: null`.
- Full non-model suite on the development Mac: **820 passed**, 12 model tests deselected.
- Headless Chromium against a scratch server with seeded data and a fake mailer: the join page at 390 px, and the
  post-meeting section before and after a Send click ("Sent to 1 of 2", a refused row in red, the button becomes
  "Send again"). The email HTML was rendered too. No console errors. The first screenshot found a bug, fixed and
  checked again: the button needed an existing export row instead of a ready summary.

### Not run

| Check | Result |
|---|---|
| A real send through Resend with the verified domain | **Passed** (user, 2026-09-30, from `convene@aditya-sinha.xyz`); spam placement and opening the attachment in Word not recorded separately |
| Gmail / Outlook / Apple Mail rendering of the HTML body | Not run |
| Email field on real iOS Safari / Android Chrome (keyboard, autofill) | Not run |
| Sending with 10 recipients (timing, rate limit) | Not run |

### Fix after the first real send

`CONVENE_MAIL_FROM="<convene@aditya-sinha.xyz>"` (angle brackets, no name) passed the startup check but Resend
refused every message (`422 Invalid from field`). The check now accepts only `address` or `Name <address>`, so
startup fails and names the value. Test added. After the fix, with `Convene <convene@aditya-sinha.xyz>`, the user
reported that sending works.

### Handoff

- Put the key in `mail.env`, never in `stt.env`, `creds.env`, or a tracked file. Rotate the key if it is ever pasted into a
  chat or log.
- Bounces after Resend accepts a message are not tracked; "Sent" means Resend accepted it.

## Choose who gets which parts — 2026-09-30 (ADR-34)

Branch `feature/email-sections`, from `feature/email-report`. The user asked to choose recipients and, per person, the
parts they get (for example two people everything, the rest summary and action items).

### Decisions

- `POST …/email` takes optional `recipients: [{participant_id, sections}]`. Sections are a non-empty subset of
  `summary`, `action_items` and `transcript`. People not listed get nothing. Without the list, everyone gets
  everything (the ADR-33 behaviour). Bad input is `400 invalid_request` before anything is rendered or sent: an empty
  list, a duplicate, a participant without an address, or an empty or unknown section.
- The title block is always included. `docx_renderer.render(..., sections=)` keeps the template order, and the
  default output is unchanged.
- The full selection attaches the exported file itself (byte-identical, tested). Other selections are rendered in
  memory once per distinct set (`ExportService.render_sections`), with no `Export` row or file, and all of them before
  the first message.
- The email body names what is attached. `minutes_emailed` gains `sections` (participant id → parts).
- UI: a count line, a tick box per person with an address, and Summary / Action items / Transcript chips per person.
  Presets for the ticked people, and Tick all / Untick all. People without an address are listed but cannot be
  picked. "Send to n people". The choice is page state only.
- "Decisions" is not offered as a part: it is not a stored field (it sits inside the summary text).

### Checks

- `tests/test_email_minutes.py`: 23 passed (4 new: renderer subsets and invalid sections, the body lists only the
  attached parts, a mixed per-person send checked by each attachment's headings plus the audit `sections` and no
  extra `Export` row, bad selections refused with nothing sent). `tests/test_docx_renderer.py` passes unchanged.
- Full non-model suite: 824 passed, 2 failed. Both failures are in `tests/test_api_contract_docs.py` for the
  `GET /api/overview` section added in commits adad006/0b564ce (a `"…"` meeting_id example, and the section lacks the
  required parts). They fail the same on a clean checkout of that commit, without this change. Not touched here.
- Headless Chromium against a scratch server with a fake mailer: 4 participants, 3 with an address. Untick all, tick
  Maya, "Summary + action items", tick Priya and Sam, send. The attachments' headings: Priya and Sam had Summary,
  Action items and Transcript appendix, and Maya had Summary and Action items, with "attached: the summary and the
  action items." No console errors.

### Not run

- A real Resend send of a partial attachment, and the picker at phone width (the post-meeting page is a laptop page).
