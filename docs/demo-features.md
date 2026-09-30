# Convene features and demo flow

The presenter's guide: what Convene does, in the order to show it. It is compiled from the work logs in `logs/`.
The status column states what the logs actually record. "Automated" means covered by tests with synthetic phones
or fake models; "real phone" means someone checked it on a physical device. For the operator's startup card,
see `tests/manual-test/DEMO_FLOW.md`. For the talk track and judge Q&A, see `demo.md`.

## Before the demo

- Pick the STT backend in `stt.env` (see `stt.env.example`). Use `CONVENE_STT=local` for the offline story or
  `CONVENE_STT=gemini` for better transcripts. With Gemini, do **not** claim offline or "audio never leaves the
  laptop"; say "speech-to-text uses Gemini for this demo, everything else runs on the laptop".
- For the email beat, put `RESEND_API_KEY` and `CONVENE_MAIL_FROM` in `mail.env` (see `mail.env.example`). The
  startup log says whether email is set up. Sending needs internet.
- `./scripts/start_demo.sh` with `CONVENE_CERT`/`CONVENE_KEY`. Wait for `Certificate OK`, `STT model ready` (or the
  Gemini check), and `Reasoning model ready`.
- For the reports beat, have two or three **ended** meetings with summaries and a few action items from earlier
  rehearsals. Mark at least one action item done, so "Closed in this period" is not zero.
- For the policies beat, upload one or two short policy PDFs/DOCX files in advance, and wait for them to show Ready.

## Demo flow

| # | Beat | What to do and say | Where | Status in the logs |
|---|---|---|---|---|
| 1 | **Start a meeting** | Home page → "Start a meeting", then give it a title. The dashboard shows a join QR code. | `/` → `/dashboard/{id}` | Automated |
| 2 | **Phones join as microphones** | Each person scans the QR code, types their name, picks a colour, and allows the microphone. The phone shows a live mic orb ("Hearing you"). | phone `/join/{id}` | **Real phone: passed** (one phone, trusted hostname, 2026-09-25). Multi-phone not yet run |
| 3 | **Who said what comes from the device, not guesswork** | Speak. Lines appear as chat bubbles in each person's colour within a few seconds. Say it once: *each phone is one person, so the label comes from the hardware*. | dashboard | **Real phone: passed** (one phone). Automated for several |
| 4 | **English + Hinglish** | Say a mixed line ("haan, main kal dekh lunga"). It is written in Latin letters, not translated. | dashboard | Automated with synthetic speech; real Hindi speech not run |
| 5 | **Live gauges and resilience** | Point at device health: audio age, backlog. Turn one phone's Wi-Fi off for ~10 s and back on. The same person reconnects, and the other phone never stops. | dashboard | Automated (synthetic phones); real Wi-Fi drop not run |
| 6 | **Honest confidence + one-click correction** | Low-confidence or unassigned lines are marked "Needs review" (amber). Click a line and reassign the speaker. The original is kept in the audit trail. | dashboard | Automated; not exercised on a real transcript |
| 7 | **Live Q&A: the key moment** | Ask about something said minutes ago ("What did we decide about the beta launch date?"). The answer cites who said it, under "Sources". Then ask something never discussed ("Who owns the marketing budget?"). It honestly says it was not discussed instead of inventing an answer. | dashboard "Ask the room" | Automated + real-model honesty check passed (0/36 fabricated on fixtures); not run on real phones |
| 8 | **End meeting → summary + action items** | Press End meeting. The post-meeting page shows the summary and action items with owners, written by the local LLM as validated structured data. | `/meetings/{id}` | Real model measured on fixtures; real meeting not run |
| 9 | **DOCX minutes** | Download the minutes: title, participants, summary, action-item table, and the full transcript with corrected speakers, low-confidence lines italicized. Deterministic code, not the model, lays out the file. | post-meeting page | Automated; opening in Word/Pages not recorded |
| 9b | **Minutes in everyone's inbox (new)** | Phones that typed an email at join are listed under "Email the minutes" (addresses masked). Untick one person, give them "Summary + action items", and leave the rest on "Everything". Press **Send to n people**: each person gets "Your words, delivered: <title>" with only their parts attached. Say it is the one deliberate internet action, and nothing is sent unless someone presses it. | post-meeting page | Automated with a fake mailer; a real Resend send not yet recorded |
| 10 | **Action items are tracked, not just listed** | Set an owner, a due date and a status (open / done / cancelled). Add a note "from" a later meeting. The overdue items show in red. | post-meeting page, `/action-items` | Automated; browser click-through not run |
| 11 | **Meeting history + cross-meeting Q&A** | Open Past meetings, search or filter by date, and ask one question across all ended meetings. Each source names its meeting and links to the exact transcript line. Rename or delete a meeting (delete needs the exact title typed). | `/history` | Automated; HTTP smoke check with fake models |
| 11b | **Summaries across meetings (new)** | On Past meetings, tick three meetings and open the **Summaries** tab: each title with its summary, oldest first. A meeting without one gets "Summarize now". **Download DOCX** saves them as one document, and **Copy all** puts them on the clipboard. | `/history` | Headless browser with a fake model; not run on real meetings |
| 12 | **Policy repository** | Show the uploaded policies and their versions (older versions stay downloadable). In History Q&A, choose "Policies" or "Both" and ask a policy question. Policy sources are labelled as policies. | `/policies`, `/history` | Automated with generated documents; real PDFs not run |
| 13 | **Periodic reports (new)** | Reports → "This month". The preview line says how many meetings match ("3 meetings (3 with summaries) · 1 not yet ended, excluded"). Download a single DOCX with every meeting's summary and action items, plus opened / closed / open-now counts. No AI call is made, so there is no new failure mode. | `/reports` | Automated (32 tests); browser and Word check not run |
| 14 | **Everything is on record** | Open the read-only database viewer: meetings, utterances, the audit trail, summaries, exports. | `/database` | Automated |
| 15 | **Close** | Local mode: audio, transcription and AI ran on this laptop, with no per-meeting cost. Gemini mode: only speech-to-text went to the cloud; Q&A, summaries, reports and storage stayed local. | — | Offline laptop run with Wi-Fi off: **not yet run** |

## Keep off stage

- Shared-device speaker enrollment (several people on one phone) is not built. Answer it verbally if a judge asks.
- Do not claim a fully offline run unless the Wi-Fi-off rehearsal with `scripts/verify_local_only.py` passed and STT
  was local.
- If a phone fails to connect, switch to the backup video within seconds.
