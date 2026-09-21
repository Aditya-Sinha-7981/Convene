# What counts as a successful test?

Run the checks in order. Record the phone model, browser, Wi-Fi setup, duration, and result for each run. A failed early check means later checks cannot validate the transport yet.

## 1. Basic transport pass (one phone)

- Mac and phone are on the same hotspot/local Wi-Fi. Disable Internet access without disconnecting that local network.
- The phone opens the Mac's local HTTPS join URL, accepts the trusted certificate, and grants microphone access.
- Phone shows `connected`; terminal shows the same participant ID and `state=connected`.
- Speak for 5 minutes. The terminal prints a new `AUDIO` window about every second, `audio=RECEIVING`, and increasing `duration_s`. There is no permanent audio stop or manual restart.
- Speak a distinctive phrase and confirm it through local STT, if configured. Without STT, arriving audio frames demonstrate transport but do not prove speech intelligibility.

If all five checks pass, **the one-phone transport test passes**. This does not establish the full architecture's reliability.

## 2. Recovery pass

- Keep the same page open, turn the phone's Wi-Fi off for about 2 seconds, then back on. Repeat with about 10 seconds.
- The phone reconnects automatically; the terminal resumes `AUDIO` windows under the **same participant ID**. `reconnects` increases.
- No server restart or new meeting is needed.

When testing on the phone that provides the hotspot, turning off its Wi-Fi may also turn off the hotspot. Use a second phone as the microphone for this interruption test.

## 3. Multiple-phone pass

- Start 2, then 3, then 5 phones. Each says a unique phrase. Confirm separate participant IDs and separate audio windows; use local STT or another audio inspection method to confirm the phrases stay with the right phone.
- Disconnect one phone. The others keep receiving audio. Reconnect that phone under its old ID.
- Run five phones for 30–60 minutes without a crash, permanent audio loss, or accumulating STT backlog.

## 4. Latency and browser limits

The preferred transport target in `TEST_PLAN.md` is around 150 ms on a healthy LAN. **The current `last_audio_age_ms` metric cannot measure one-way transport latency.** Do not declare this target met from that number. A source timestamp with synchronized clocks, or a known audio marker measured at both ends, is needed for a defensible result.

Run screen-lock and background tests separately on Android Chrome and iPhone Safari. Record whether audio continues, pauses, or needs user interaction. Those results are browser behavior observations, not an automatic transport pass.

The full experiment is successful only after offline operation, recovery, participant separation, sustained multi-phone operation, and a valid latency measurement have all passed. See `TEST_PLAN.md` for the full stress sequence.
