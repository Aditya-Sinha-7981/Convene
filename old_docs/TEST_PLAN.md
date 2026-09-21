# DT-17 Audio Transport Stress Test Plan

## Objective

Determine whether the proposed phone-as-wireless-microphone architecture is reliable enough for a hackathon.

The experiment must be performed with the laptop disconnected from the Internet.

## Test environment

### Initial network

Use the laptop's built-in hotspot first.

```text
MacBook hotspot
     |
     +--- test phone
```

No Internet connection.

Later, repeat the exact same tests with a small dedicated travel router/AP if one is purchased.

### Devices

Minimum:

- 1 laptop
- 1 Android phone
- 1 iPhone if available

Preferred:

- 5+ phones
- 10 phones for final stress test

## Test 0: Internet independence

1. Connect laptop and phone to the same local Wi-Fi.
2. Disconnect laptop from Internet completely.
3. Verify the laptop can still serve the DT-17 page.
4. Verify phone can open the page.
5. Verify microphone permission works.
6. Verify WebRTC audio reaches the laptop.

### Pass

Audio works with no Internet.

### Fail

Any required dependency contacts the public Internet.

---

# Test 1: One phone, five minutes

Phone:

- join local Wi-Fi
- open join URL
- allow microphone
- speak continuously for five minutes

Use varied speech:

```text
normal speech
quiet speech
loud speech
silence
numbers
fast speech
slow speech
```

Laptop records:

- connection state
- audio arrival
- last-audio age
- gaps
- transport diagnostics

### Pass

Continuous audio for the full test without manual intervention.

---

# Test 2: Low-latency observation

Use a repeated known phrase:

```text
ONE
TWO
THREE
FOUR
FIVE
```

The laptop should show fresh audio arriving while the phone is still speaking.

Do not wait until the test ends.

Record:

- apparent transport latency
- processing-window delay
- STT delay

Do not combine all three into one number.

Target for transport:

> Preferably under ~150 ms under normal local-network conditions.

This is a target, not a hard protocol guarantee.

---

# Test 3: One-second STT windows

Use approximately 1-second windows on the laptop.

Example:

```text
Phone speaks continuously

Laptop:
W001 -> STT
W002 -> STT
W003 -> STT
W004 -> STT
```

Verify that STT results appear during the speech session.

The purpose is to validate that audio is arriving continuously enough to feed downstream processing.

Do not treat 1 second as the final DT-17 latency target.

---

# Test 4: Distance

Repeat the test at approximate distances:

```text
3m
5m
10m
15m
```

Use the actual room available.

At every distance:

- speak continuously
- walk slowly
- rotate phone
- put phone in normal carrying positions

Record:

- disconnects
- gaps
- latency
- reconnects

Do not assume a theoretical Wi-Fi range is representative.

---

# Test 5: Movement

Start near the laptop.

Walk around the room.

Test:

- front of laptop
- side of laptop
- behind laptop
- behind people
- different phone orientation
- walking continuously

Run for at least 10 minutes.

### Important observation

We care more about:

> Does the stream remain usable?

than:

> Was every packet perfect?

---

# Test 6: Wi-Fi interruption

While streaming:

### Short interruption

1. Disable Wi-Fi on phone.
2. Wait 2 seconds.
3. Re-enable Wi-Fi.
4. Wait for reconnection.

### Long interruption

Repeat with approximately 10 seconds.

### Pass condition

- Same participant identity returns.
- Other participants are unaffected.
- Audio resumes automatically.
- Server does not restart.
- Meeting does not need to restart.

A short missing audio section is acceptable.

---

# Test 7: Phone screen lock

Perform separately on Android Chrome and iPhone Safari.

1. Start streaming.
2. Lock phone.
3. Wait 10 seconds.
4. Unlock.
5. Check stream.
6. Repeat for 30 seconds.
7. Repeat for 1 minute.

Record:

```text
continued
paused
disconnected
browser killed
requires user interaction
```

This test is extremely important.

If a mobile browser suspends microphone capture while locked, the architecture must explicitly account for that limitation.

---

# Test 8: Backgrounding

Open another app briefly.

Return to DT-17.

Test:

- 5 seconds
- 30 seconds
- 1 minute

Record whether the stream survives.

Do not assume Android and iOS behave identically.

---

# Test 9: Multiple phones

Run:

```text
2 phones
3 phones
5 phones
10 phones if possible
```

Each phone gets a unique phrase.

Example:

```text
Phone A:
THIS IS PARTICIPANT A

Phone B:
THIS IS PARTICIPANT B

Phone C:
THIS IS PARTICIPANT C
```

The laptop should display separate streams.

### Pass

No cross-contamination.

One participant's disconnect does not terminate another participant's audio.

---

# Test 10: Simultaneous speech

Have 3-5 participants speak simultaneously.

This is not primarily an STT accuracy test.

We are testing:

- stream separation
- transport stability
- CPU behavior
- memory behavior
- Wi-Fi stability

The receiver should still show each participant's audio stream independently.

---

# Test 11: One phone fails while others continue

Start 5 phones.

Disconnect Phone C.

Phones A, B, D, E must continue.

Reconnect Phone C.

Phone C should return under the same participant identity.

---

# Test 12: RF stress

Create a realistic noisy environment.

If possible:

- several phones nearby
- multiple Wi-Fi networks nearby
- Bluetooth devices
- normal hackathon-like crowd
- people moving between devices

Run 30-60 minutes.

This is more valuable than a perfect isolated-room test.

---

# Test 13: Long-duration soak

Run 5 phones continuously for:

> 30-60 minutes

Record:

- disconnect count
- reconnect count
- audio gaps
- increasing latency
- CPU
- memory
- server crashes
- browser crashes
- STT backlog

The system must not gradually degrade.

---

# Metrics to record

Create a simple table after each experiment:

| Test | Phones | Duration | Distance | Disconnects | Reconnects | Audio gaps | Avg latency | Worst latency | Result |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 1 phone | 1 | 5m | 3m | | | | | | |
| Distance | 1 | | 10m | | | | | | |
| Multi-phone | 5 | 30m | | | | | | | |
| Stress | 10 | 60m | | | | | | | |

Also record:

- phone model
- OS version
- browser/version
- Wi-Fi band
- laptop model
- access point used

This will matter when something inevitably behaves like it was designed by a committee.

---

# Failure classification

Classify failures as:

## F1: Browser limitation

Examples:

- microphone unavailable
- permission unavailable
- stream stops when backgrounded
- iOS/Android-specific behavior

## F2: Secure-context problem

Examples:

- microphone permission unavailable over LAN HTTP
- certificate/trust problem

## F3: Wi-Fi problem

Examples:

- connection drops at distance
- RF congestion
- poor roaming behavior

## F4: Transport problem

Examples:

- WebRTC connection fails
- audio stops while Wi-Fi remains connected
- reconnection does not work

## F5: Laptop processing problem

Examples:

- CPU saturation
- memory growth
- STT backlog

Do not "fix" an F1 failure by adding unrelated infrastructure.

Identify the actual layer first.

---

# Decision gates

## Gate A

If one phone cannot reliably stream:

**Stop.**

Fix the fundamental transport/browser problem before scaling.

## Gate B

If 1 phone works but 3 phones do not:

Investigate:

- peer connection handling
- per-participant isolation
- laptop resource usage
- Wi-Fi behavior

## Gate C

If 5 phones work for 30-60 minutes:

The architecture is promising.

Proceed to 10-phone testing.

## Gate D

If 10 phones work under realistic conditions:

The transport is sufficiently validated for the hackathon prototype.

Then test the dedicated portable AP.

---

# Dedicated AP comparison

Only after the laptop-hotspot version works, compare:

```text
A:
Mac hotspot

B:
dedicated portable AP
```

Run the same tests.

Compare:

- range
- disconnect frequency
- reconnect behavior
- latency
- setup time
- phone compatibility
- stability under 5-10 clients

The dedicated AP is valuable if it reduces variability.

It is not required to prove the basic architecture.

---

# Final recommendation threshold

For a hackathon, aim for:

- 5 phones stable for 30-60 minutes
- 10 phones stable in shorter stress tests
- automatic reconnect after short Wi-Fi interruptions
- no participant stream interference
- transport latency generally below ~150 ms on a healthy LAN
- no Internet dependency
- participant setup no more than:
  - join Wi-Fi
  - open link
  - allow microphone

If these conditions hold, stop adding networking complexity.

The objective is not theoretical perfection.

The objective is a boring system that keeps delivering audio.
