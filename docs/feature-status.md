# Convene feature status

## What Convene does today

Convene turns phones on the same Wi-Fi network into separate microphones for a meeting. The laptop receives the
audio, creates a speaker-labelled transcript, saves the meeting locally, and helps turn the discussion into useful
notes.

### Starting and joining a meeting

- Start a meeting from the laptop and give it a title.
- Show a QR code so people can join from their phone browser.
- Each person enters their name and allows microphone access.
- A returning phone keeps its identity if it reconnects during the same meeting.

### Live meeting view

- The laptop dashboard shows connected phones, their audio activity, reconnects, and transcription backlog.
- Speech appears as a live transcript, labelled with the participant who joined from that phone.
- Lines that need a human check are visibly marked and can be corrected from the dashboard.
- The dashboard keeps working through its own reconnects and can resync from the saved meeting data.

### Questions during and after a meeting

- Ask a question about the current conversation and receive an answer based on the saved transcript.
- Answers include links back to the relevant speaker and moment in the meeting.
- If the meeting did not contain enough evidence, Convene says so instead of inventing an answer.
- Past meetings can be listed, searched by title and date, and queried individually or together after they end.

### Meeting wrap-up

- Ending a meeting creates a structured summary with decisions and action items.
- The summary can be downloaded as a DOCX minutes document.
- People who add an email when they join can be sent the DOCX minutes from the post-meeting page, through Resend,
  when the laptop has a Resend key (ADR-33). Nothing is emailed unless someone presses Send.
- Meeting titles can be renamed, and inactive meetings can be deleted after a deliberate confirmation.

### Saved local record

- Meetings, participants, transcript lines, corrections, questions, summaries, exports, and audit events are saved
  in a SQLite database on the laptop.
- The new `/database` screen is a read-only viewer for this record: it shows table counts and the latest saved rows,
  making the persistence visible during a demo.
- Raw microphone audio is not stored by default; Convene stores the transcript and its supporting metadata.

## Current boundaries

- The normal path uses one phone per person. Shared-phone speaker classification is planned, not implemented.
- Local transcription, embeddings, and question answering are the intended default. Cloud STT or LLM providers are
  documented as a possible deliberate demo fallback, but are not connected in the current code.
- The software has automated tests and some single-phone validation, but a full demo rehearsal with several real
  phones, the target Wi-Fi, and the target laptop still needs to be completed. See `manual-tests.md` and `demo.md`.
- This is a hackathon demo for one laptop and a small meeting, not an account-based or cloud-hosted product.
