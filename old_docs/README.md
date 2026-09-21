# DT-17 Local Audio Transport Test

## Purpose

This is a deliberately small proof-of-concept for one question:

> Can a participant's smartphone act as a live microphone and continuously deliver audio to one laptop over a private Wi-Fi network with **no Internet dependency**, at low latency and with automatic recovery from normal Wi-Fi interruptions?

This prototype is **not DT-17 itself**. Do not add meeting management, databases, authentication systems, dashboards, RAG, AI analysis, summaries, or production UI.

## Success criteria

The prototype is successful if we can demonstrate all of the following:

1. A phone joins a local Wi-Fi network.
2. The phone opens a DT-17 test page hosted by the laptop.
3. The browser receives microphone permission and captures audio.
4. Live audio reaches the laptop continuously.
5. The laptop can identify which participant produced each stream.
6. Audio can be converted into small processing windows on the laptop without waiting for a recording to finish.
7. A local STT process can transcribe those windows while the participant is still speaking.
8. Transport latency is measurable and normally stays low.
9. A temporary Wi-Fi interruption does not require restarting the meeting/test page.
10. Multiple phones can stream simultaneously without streams mixing together.
11. The entire experiment continues to work with the laptop disconnected from the Internet.

## Important architecture decision

### Primary transport: WebRTC MediaStream

Use:

- `navigator.mediaDevices.getUserMedia()` for microphone capture.
- `RTCPeerConnection` for the audio transport.
- WebSocket for signaling only.
- No STUN/TURN/cloud relay for this local-network test.
- The laptop is the central receiver.
- Each phone gets its own peer connection.

Do **not** use a WebRTC DataChannel for audio.

The reason is simple: this is live media, and WebRTC's media transport is designed for low-latency real-time audio.

### Application-level audio processing

Do not make the phone wait for one-second blobs before sending anything.

Treat WebRTC as a continuous audio stream.

On the laptop:

```text
WebRTC audio stream
        |
        v
continuous audio frames
        |
        +--> transport metrics
        |
        +--> short processing windows
                |
                v
               STT
```

For the initial STT experiment, use approximately **1-second processing windows** because they make the test easy to observe. This is a processing choice, not a network-chunk requirement.

## Why STT is included

STT is included only as an observable end-to-end sanity check.

It should NOT become part of the architecture being evaluated.

We need to distinguish:

- transport latency
- audio continuity
- reconnect behavior
- STT processing latency

The terminal should make these visible separately.

## Prototype phases

### Phase 0: environment and secure-context validation

Before implementing the full transport, prove that the target browsers can access the microphone from the laptop-hosted local site.

`getUserMedia()` requires a secure context in normal browsers. A phone visiting `http://192.168.x.x/...` is generally not a secure context.

Therefore the prototype must use a practical HTTPS setup for LAN access.

The recommended development approach is:

- generate a local development CA/certificate with `mkcert`
- create a certificate for the laptop's LAN hostname/IP as appropriate
- install/trust the CA on the test phone(s)
- serve the test page over HTTPS

Document exact platform-specific trust steps in `docs/HTTPS_LOCAL.md`.

Do not "solve" this by telling users to enable arbitrary browser security flags. That is not a viable hackathon architecture.

If the chosen HTTPS approach proves too cumbersome on a target phone, record that as a browser/platform constraint rather than silently weakening the security model.

### Phase 1: one phone, transport only

Phone:

```text
join Wi-Fi
  -> open test URL
  -> allow microphone
  -> start
```

Laptop terminal:

```text
PHONE CONNECTED
participant=p01
transport=webrtc
state=connected

audio received
participant=p01
elapsed=00:12
last_audio_age=42ms
```

The receiver must continuously report whether audio is arriving.

Do not wait two minutes and then inspect a recording.

### Phase 2: one phone + processing windows + STT

Take the live received stream and produce approximately 1-second processing windows locally.

For each window:

```text
participant=p01
window=184
audio_start=...
audio_end=...
received=...
stt_start=...
stt_end=...
text="..."
```

The terminal must show transport and STT separately enough that we can see where latency comes from.

### Phase 3: realistic movement

Move the phone around the room.

Test:

- 3 m
- 5 m
- 10 m
- farther if practical
- different phone orientations
- walking around people/furniture

Do not optimize yet. Measure.

### Phase 4: interruption/reconnection

While streaming:

1. Turn phone Wi-Fi off for ~2 seconds.
2. Turn it back on.
3. Verify the same participant identity reconnects.
4. Verify audio resumes without restarting the meeting/test server.

Repeat with ~10 seconds of interruption.

A short missing interval is acceptable. Permanent failure is not.

### Phase 5: multiple phones

Test:

- 2 phones
- 3 phones
- 5 phones
- 10 phones if available

Each phone should say a unique phrase such as:

```text
THIS IS PARTICIPANT A
THIS IS PARTICIPANT B
THIS IS PARTICIPANT C
```

Verify that the laptop keeps streams separate.

### Phase 6: screen-lock/lifecycle testing

Test Android Chrome and iPhone Safari separately.

Start streaming, then:

- lock screen
- wait 10 seconds
- wait 30 seconds
- unlock

Record whether audio continues, pauses, or dies.

This is a major browser risk and must be experimentally validated.

## What NOT to build

Do not add:

- user accounts
- OAuth
- JWT
- database
- cloud server
- Internet fallback
- STUN/TURN
- cloud storage
- meeting history
- RAG
- AI summaries
- polished dashboard
- participant profiles
- production authentication
- persistent recordings unless useful for debugging
- complex message queues
- Redis
- Kafka
- Kubernetes

This is a transport experiment, not a startup pitch deck.

## Runtime topology

```text
                    INTERNET
                       X
                       |
                ┌──────┴──────┐
                │   LAPTOP    │
                │ DT-17 test  │
                │ HTTPS       │
                │ signaling   │
                │ WebRTC RX   │
                └──────┬──────┘
                       |
                 LOCAL WI-FI
                       |
          ┌────────────┼────────────┐
          |            |            |
       Phone A      Phone B      Phone C
       WebRTC       WebRTC       WebRTC
```

For the first experiment, the Wi-Fi can be the laptop's own hotspot. A dedicated travel router is a later hardware experiment, not a prerequisite for proving the transport.

## Final principle

The experiment is successful when we can say:

> "With Internet completely unavailable, 5-10 normal smartphones can join a local DT-17 network, continuously stream microphone audio to one laptop, maintain participant separation, recover from temporary Wi-Fi loss, and feed fresh audio into local STT with low transport latency."

Anything beyond that is scope creep.
