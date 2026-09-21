# DT-17 Audio Transport Test Architecture

## 1. Goal

Build the smallest possible local-network audio transport test.

The laptop is the server and central receiver.

Phones are microphone nodes.

```text
Phone
  |
  | microphone
  v
getUserMedia()
  |
  v
WebRTC MediaStream
  |
  | local Wi-Fi
  v
Laptop WebRTC receiver
  |
  +--> metrics
  |
  +--> short audio processing windows
           |
           v
          STT
```

## 2. Why WebRTC is the primary candidate

WebRTC is designed for real-time media.

The important property for this experiment is not perfect delivery of every network packet. It is:

> fresh, continuous, intelligible audio arriving with low latency.

Small packet loss can be preferable to transport mechanisms that wait for missing data and increase latency.

WebRTC also provides:

- real-time media transport
- jitter handling
- packet-loss concealment
- browser-native microphone integration
- connection-state information
- built-in reconnection primitives at the connection level

All of that makes it a strong candidate for live speech.

## 3. Why WebSocket is not the primary transport

WebSocket is still a strong fallback.

A WebSocket design would look like:

```text
getUserMedia()
  |
MediaRecorder
  |
100-150ms blobs
  |
WebSocket
  |
Laptop
```

Advantages:

- simple mental model
- reliable ordered TCP delivery
- easy server implementation

Disadvantages for this experiment:

- MediaRecorder chunk timing is less precise
- browser codec/container behavior varies
- TCP head-of-line blocking can add latency during loss
- chunk boundaries become part of the transport design

Do not build the WebSocket transport unless WebRTC proves problematic or a comparison is explicitly needed.

## 4. WebRTC topology

Use one peer connection per participant.

```text
                Laptop
       ┌─────────┼─────────┐
       |         |         |
     Peer A    Peer B    Peer C
       |         |         |
    Phone A   Phone B   Phone C
```

Do not build peer-to-peer phone meshes.

Do not send phone audio through another phone.

Do not use a cloud SFU.

For 3-10 phones, direct phone-to-laptop connections are sufficient for this experiment.

## 5. Signaling

WebRTC requires signaling, but signaling is not the audio transport.

Use a WebSocket connection to exchange:

- participant ID
- meeting/test ID
- SDP offer
- SDP answer
- ICE candidates
- connection/reconnect control messages

Example:

```json
{
  "type": "join",
  "meetingId": "TEST-123",
  "participantId": "p01"
}
```

Keep signaling state in memory.

No database is needed.

## 6. ICE/STUN/TURN

Because all devices are intentionally on one local Wi-Fi network:

- no public STUN server is required
- no TURN server is required
- no cloud signaling is required

Prefer local host candidates.

If WebRTC somehow requires a specific local ICE configuration on a target browser, document it rather than introducing Internet infrastructure.

The test must remain functional with Internet disconnected.

## 7. Participant identity

For the prototype, create an ephemeral participant ID when the participant opens the join page.

Example:

```text
p01
p02
p03
```

The server maps:

```text
peer connection -> participant ID
```

Every audio event and processing window must carry that participant ID.

On reconnect, the phone should attempt to reuse the same participant ID rather than becoming a new participant.

A random session token can be included in the join URL if needed to prevent accidental cross-connection.

Do not build accounts.

## 8. Join URL

For the first prototype, use a direct URL.

Example:

```text
https://<laptop-local-host>:<port>/join/<test-id>
```

Later the URL can be encoded into a QR code.

The QR code itself is not part of the transport architecture.

## 9. Network addressing

The laptop should bind the server to the LAN interface, not only to `127.0.0.1`.

The server must listen on something equivalent to:

```text
0.0.0.0:<port>
```

while the join URL uses the laptop's actual local address/hostname.

Example:

```text
https://192.168.50.10:8443/join/TEST-123
```

Do not hard-code a permanent IP.

At startup, detect and display the current LAN address.

For the dedicated-router version later, DHCP reservation can make the laptop's address stable.

## 10. Microphone permission and HTTPS

This is a major constraint.

Normal mobile browsers require a secure context for `getUserMedia()`.

Therefore:

```text
http://192.168.50.10:8443
```

must not be assumed to work.

The prototype should serve HTTPS.

For development/testing, use a local CA such as `mkcert` and establish trust on the test devices.

The test documentation must clearly state:

- how to create the certificate
- which hostname/IP it covers
- how to trust it on Android
- how to trust it on iOS
- how to remove the test CA afterward

Do not depend on browser security flags.

## 11. Audio capture

Use:

```javascript
navigator.mediaDevices.getUserMedia({
  audio: true,
  video: false
})
```

Start with the browser's native microphone processing.

Do not initially request exotic constraints.

The first goal is reliable speech capture, not perfect studio audio.

Record the actual negotiated WebRTC codec and browser behavior in diagnostics.

## 12. Audio format

Do not force a custom PCM-over-WebSocket design for the first test.

With WebRTC, allow the browser/WebRTC stack to negotiate a normal speech codec.

The receiver should decode to a form usable for local STT.

If the local STT process needs PCM/WAV-like input, perform that conversion on the laptop after receipt.

Keep transport codec and STT input format separate.

## 13. Audio processing windows

The transport is continuous.

Create approximately 1-second processing windows on the laptop for the first STT experiment.

Example:

```text
continuous audio
|----1s----|----1s----|----1s----|
    W0          W1          W2
```

Do not make the phone wait for one second before sending.

The 1-second window exists only because it makes STT testing simple.

Later experiments can compare 250ms, 500ms, and 1s windows.

## 14. Metrics

Collect at minimum:

### Connection

- participant ID
- connection state
- connection start time
- reconnect count
- disconnect duration

### Audio

- audio received/not received
- last received audio timestamp
- frames/bytes received where available
- audio gaps
- total received duration

### Latency

Use timestamps wherever possible.

Display:

```text
last_audio_age_ms
```

and, when a proper source timestamp is available:

```text
transport_latency_ms
```

Do not pretend that a local receive timestamp alone proves true one-way latency.

For a stronger measurement, use a known audio marker or synchronized application timestamps.

## 15. STT metrics

For every processing window:

```text
participant
window_id
audio_start
audio_end
stt_start
stt_end
stt_duration
text
```

This lets us separate:

```text
transport
+
windowing delay
+
STT processing
```

## 16. Terminal output

Keep transport and STT visibly separate.

Recommended terminal A:

```text
=== DT-17 AUDIO TRANSPORT ===

[p01] CONNECTED
[p01] audio=RECEIVING age=43ms
[p02] CONNECTED
[p02] audio=RECEIVING age=51ms

[p01] reconnect #1
[p01] RESUMED after 1.8s
```

Recommended terminal B:

```text
=== DT-17 LIVE STT ===

[p01][W184]  "This is participant one..."
[p02][W184]  "This is participant two..."
[p01][W185]  "Now we are testing..."
```

If running two terminals is inconvenient, a single terminal with two clearly labeled sections is acceptable.

The purpose is observability, not aesthetics.

## 17. Reconnection behavior

Monitor WebRTC connection state.

Important states:

```text
new
connecting
connected
disconnected
failed
closed
```

On a failed/disconnected connection:

1. Keep participant identity.
2. Attempt ICE restart/renegotiation where appropriate.
3. If necessary, create a fresh peer connection.
4. Reattach the microphone track.
5. Re-establish the stream.
6. Continue processing.

Do not restart the entire server or meeting.

A short audio gap is acceptable.

## 18. Multiple participants

Each participant must have:

- independent peer connection
- independent participant ID
- independent audio pipeline
- independent reconnect state
- independent processing windows

One broken phone must not stop other participants.

Test this explicitly by disconnecting one phone while others continue speaking.

## 19. Failure isolation

Required behavior:

```text
Phone A fails
     |
     X
     |
Phone B ------------------> continues
Phone C ------------------> continues
Phone D ------------------> continues
```

Never use a single global connection state to represent all participants.

## 20. Server responsibilities

The server should only do:

- serve HTTPS web page
- provide signaling
- accept WebRTC peer connections
- receive audio
- identify streams
- expose metrics
- produce short processing windows
- invoke the local STT test adapter

Keep these components separate enough that STT can be removed without changing the transport.

## 21. Do not prematurely optimize

At 3-10 phones, the expected speech bandwidth is small.

The primary risks are:

1. browser microphone restrictions
2. HTTPS/local secure-context setup
3. mobile browser lifecycle behavior
4. Wi-Fi stability
5. reconnection behavior
6. real-world RF congestion

Bandwidth and laptop CPU are not expected to be the first bottleneck.

Measure rather than speculate.
