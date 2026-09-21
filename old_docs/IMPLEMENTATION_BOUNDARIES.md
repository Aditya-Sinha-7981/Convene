# DT-17 Prototype Implementation Boundaries

## Build this

### Laptop server

- HTTPS server
- WebSocket signaling endpoint
- WebRTC receiver
- participant registry in memory
- connection state monitoring
- audio reception
- audio metrics
- short audio-window generation
- simple local STT adapter
- terminal logging

### Phone web client

- join page
- microphone permission
- `getUserMedia()`
- WebRTC peer connection
- signaling client
- connection-state display
- reconnect behavior
- participant/session identity

### Diagnostics

At minimum show:

```text
participant
connection state
audio received
last audio age
reconnect count
audio duration
```

STT terminal:

```text
participant
window number
STT processing duration
transcribed text
```

## Do not build

- database
- login
- account system
- cloud infrastructure
- production deployment
- public DNS
- TURN server
- public STUN dependency
- message broker
- Redis
- Kafka
- Kubernetes
- authentication framework
- recording management
- meeting dashboard
- polished frontend
- AI summaries
- RAG
- document processing
- analytics

## Code organization

Keep these conceptual boundaries:

```text
server/
  signaling
  webrtc
  participants
  audio
  metrics
  stt

client/
  join
  microphone
  webrtc
  signaling
  reconnect

docs/
  architecture
  testing
  HTTPS setup
```

The exact language/framework is flexible.

Use the team's existing stack if practical.

Do not introduce a new framework solely because it is fashionable.

## STT adapter

STT should be an adapter behind a simple interface.

Conceptually:

```text
processAudioWindow(
    participantId,
    windowId,
    audio
) -> transcription
```

This makes it possible to replace the STT engine without changing the audio transport.

For the prototype, local STT is preferred so the test remains Internet-independent.

## Audio persistence

Do not persist all audio by default.

If debugging requires recordings, provide an explicit test/debug option.

Avoid turning the prototype into an audio storage system.

## Error handling

Every participant must be isolated.

An error for:

```text
p03
```

must not crash:

```text
p01
p02
p04
p05
```

The server must survive:

- phone closing the page
- Wi-Fi loss
- browser reconnect
- malformed signaling message
- WebRTC connection failure

## Reconnection

The client should automatically attempt recovery.

Preserve:

```text
meeting/test ID
participant ID
```

A reconnect should not create a second logical participant.

## Logging

Logs should be timestamped.

Example:

```text
[12:42:10.123] [p03] connected
[12:42:10.241] [p03] audio started
[12:43:01.921] [p03] connection disconnected
[12:43:03.412] [p03] reconnecting
[12:43:04.008] [p03] audio resumed
```

Avoid dumping every low-level WebRTC event forever.

Provide useful summaries plus an optional debug mode.

## Definition of done

The implementation is done when:

1. One phone can stream live audio.
2. The laptop receives it continuously.
3. Audio processing windows can be created while streaming.
4. Local STT can process those windows.
5. Transport and STT timing are visible.
6. Multiple phones remain separated.
7. Temporary disconnects recover.
8. The entire system works without Internet.
9. The test plan can be executed repeatedly.

After that, stop coding and torture-test it.
