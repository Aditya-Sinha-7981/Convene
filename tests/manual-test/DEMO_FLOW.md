# Convene demo-day flow

Use this as the operator card. It deliberately contains no passwords, private transcripts, or backup-video path.
For the detailed physical checks, use [PHYSICAL_PHONE_TESTS.md](PHYSICAL_PHONE_TESTS.md).

## Before leaving for the venue

1. Confirm all three model resources are cached while online:

   ```sh
   .venv/bin/python scripts/provision_models.py --check
   .venv/bin/python scripts/provision_models.py --resource embedding --check
   .venv/bin/python scripts/provision_models.py --resource reasoning --check
   ```

2. Bring the exact network and certificate arrangement rehearsed with. Do not switch networks on stage.
3. Keep this repository, the virtual environment, certificate and private key on the laptop. Keep the backup video outside Git.

## Start the demo

1. Connect the laptop and phones to the rehearsal Wi-Fi. For an offline claim, remove its Internet uplink first.
2. Set the real certificate values; do not put the key in a shell history file or this document.

   ```sh
   export CONVENE_CERT=/absolute/path/to/certificate.pem
   export CONVENE_KEY=/absolute/path/to/private-key.pem
   export CONVENE_ADVERTISE_IP=192.168.50.10
   ./scripts/start_demo.sh
   ```

   For the trusted public-host arrangement instead, additionally set `CONVENE_PUBLIC_HOST`. That route requires its documented DNS preflight and may need weak Internet for first hostname lookup; do not describe it as a fully offline join path.

3. Wait for `Certificate OK`, `STT model ready`, and `Reasoning model ready`. If any is absent, stop—do not start a half-working meeting.
4. On the laptop, open the URL printed by the server and choose **New meeting**. Put the dashboard on the display.
5. In another terminal, record the server PID and start connection sampling:

   ```sh
   .venv/bin/python scripts/verify_local_only.py --pid <server-pid> --samples 24 --interval 5
   ```

   A passing result samples only TCP connections; it is not packet-level proof. A non-local peer is a failed offline run.

## Rehearsed presentation beat

1. Each participant scans the QR code, enters their own name, allows the microphone, and appears as connected.
2. Priya says: **“We should ship the beta on Friday.”**
3. Marcus says: **“I will send the release notes tomorrow.”**
4. Wait at least 15 seconds for indexing. Ask: **“What did we decide about the beta launch date?”** Confirm the answer cites Priya's line.
5. Ask: **“Who is responsible for the marketing budget?”** Confirm the honest not-discussed result.
6. Turn one phone's Wi-Fi off for about 10 seconds, then on. Confirm the same participant returns while the other phone continues.
7. Correct one transcript row, then end the meeting.
8. Wait for the summary and action items. Download the DOCX and open it locally; confirm the correction and action item appear.

## If something fails

- Certificate warning or unavailable microphone: stop and use the already trusted certificate/network; do not use browser security flags.
- A phone cannot connect: verify it is on the right Wi-Fi, the laptop IP has not changed, and the certificate covers the QR host.
- A model fails to load: do not download during the demo. Use the backup video or the pre-provisioned laptop setup.
- STT backlog grows or lines stop: keep the meeting ending path available; do not claim a successful live run after a server restart.
- Wi-Fi fails: move to the backup video within seconds rather than improvising an online tunnel.

## After each run

Save the local-only sampler output, metrics, timing observations, and pass/fail result in `logs/e2e-demo.md`. Keep audio, transcripts, exports, and video outside Git.
