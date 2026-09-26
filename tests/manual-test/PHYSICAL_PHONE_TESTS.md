# Convene physical-phone test checklist

Run these tomorrow with the reference laptop, the actual demo network, real speech, and at least two phones (one Android and one iPhone if available). Mark every row **Passed**, **Failed**, or **Not run — reason** in `logs/e2e-demo.md`. Do not copy private transcripts or recordings into the log.

## Setup record (once per session)

- Date / operator:
- Laptop and macOS version:
- Wi-Fi method and whether its Internet uplink was disabled:
- Certificate path/mode (mkcert LAN or trusted hostname):
- Phone model, OS, browser/version for every phone:
- STT, embedding, and reasoning model revisions:

## Preflight

- [ ] All three `scripts/provision_models.py --check` commands pass.
- [ ] Cold offline start prints certificate, STT, and reasoning readiness.
- [ ] `verify_local_only.py` runs during the meeting without a non-local peer.
- [ ] Each phone opens the QR URL without a certificate warning and can grant microphone access.

## Run this full flow three times

For each run, record observed transcript delay (speech end to visible row), Q&A latency, CPU/memory, reconnect time/count, summary/export time, manual interventions, and any F1–F6 failure.

- [ ] Two or more phones join and remain separately connected.
- [ ] Each speaker says a distinct sentence; every row has the correct device participant label.
- [ ] Priya says “We should ship the beta on Friday.” Wait 15 seconds, then ask the planned beta-date question; confirm the cited speaker and time.
- [ ] Ask the absent marketing-budget question; confirm `no_grounding`, never an invented answer.
- [ ] Turn one phone's Wi-Fi off for about 10 seconds; other phones keep working and the first returns with the same identity.
- [ ] Correct one row; reload the dashboard and confirm the correction persists.
- [ ] End the meeting; summary and action items finish without restart.
- [ ] Download and open the DOCX in the viewer intended for demo day; confirm corrected attribution and action items.

## Additional transport/browser observations

- [ ] One phone streams for five minutes with no restart.
- [ ] Close and reopen the phone tab; record whether its identity is retained.
- [ ] Lock/background each phone for 10, 30, and 60 seconds; record continued, paused, disconnected, or interaction-needed behavior.
- [ ] With two phones, disconnect one; confirm the other continues.

## Record failure class before fixing

- **F1** browser/microphone behavior
- **F2** HTTPS/certificate trust
- **F3** Wi-Fi/access-point behavior
- **F4** WebRTC transport/reconnect
- **F5** laptop/model processing/backlog
- **F6** wrong attribution or cross-device bleed

## Stability gate

CON-12 is stable only after **three consecutive** complete offline runs: no manual server restart, no data loss, successful grounded and absent-fact Q&A, same-identity reconnect, summary, and opened DOCX. A second person must follow `DEMO_FLOW.md` from a cold start once. Record the backup video location outside the repository.
