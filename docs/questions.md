# Convene — Judge and Audience Questions

Use the short answer first. Expand only when the questioner wants detail. Keep answers aligned with what has been rehearsed on the demo hardware and network.

## Product and differentiation

### What is Convene?

Convene is a local-first meeting intelligence system. Phones on the same Wi-Fi network become separate microphones; a laptop creates a live speaker-labeled transcript, supports grounded Q&A, and produces meeting minutes.

### Why not just use Otter, Fireflies, or a recorder?

Most systems begin with one mixed microphone and infer who spoke from audio. Convene associates each audio stream with a registered participant's phone, so the common-case speaker label is based on device identity. It also keeps the demo's meeting processing local rather than making cloud AI the critical path.

### What is the actual innovation?

The important design choice is moving speaker separation to the capture layer. Giving each person a separate phone stream turns common-case attribution from a difficult acoustic inference problem into a reliable identity-mapping problem.

### Why would people use phones instead of one conference microphone?

Phones are already available, require no dedicated microphone-array hardware, and give each participant a separate channel. That improves attribution and makes the setup portable for small in-room meetings.

### Is this an app people have to install?

No. The phone experience is a browser join page: scan the meeting QR code, grant microphone permission, and join.

## Technical design

### How does the audio get from phones to the laptop?

Each browser captures its microphone and streams it to the laptop over WebRTC on the local Wi-Fi network. WebSocket signaling manages the connection; the laptop runs the FastAPI and aiortc server.

### How do you know who said a line?

For the normal one-person-per-phone flow, the server attributes a transcribed segment to the participant registered for the device that supplied that audio. The UI can show low-confidence output and lets a user correct it; the original attribution is retained in the audit trail.

### What if one phone hears another participant nearby?

Separate channels improve attribution, but they do not prevent physical cross-device microphone bleed. That is a real limitation to measure on the venue setup. We mitigate it with per-device voice activity detection, phone placement, confidence/review controls, and manual correction; we do not claim it disappears automatically.

### What happens if two people share one phone?

The core demo is intentionally one phone per speaker. Shared-device enrollment and local speaker classification are the planned extension; until that has been validated, it should not be presented as a solved live-demo capability.

### Why not use ordinary speaker diarization?

Blind diarization on a mixed recording must infer and separate voices after the fact, which can confuse similar or overlapping speakers. Convene avoids that uncertainty for dedicated phones and reserves acoustic classification for the exceptional shared-device case.

### What happens if a phone disconnects?

The system is designed so that one device's connection issue does not stop other phones. A reconnect resumes under the same stored device identity, preserving the participant mapping. Demonstrate this only if it has been rehearsed with the phones and Wi-Fi in use.

### Why WebRTC rather than uploading audio files?

WebRTC is built for low-latency browser media transport. It supports the live transcript and in-meeting question flow without waiting for a meeting recording to upload first.

### How do you prevent the system from making up an answer?

Q&A retrieves relevant transcript chunks first, then asks the local language model to answer from that evidence and returns citations. If there is no sufficient grounding, the intended result is an honest no-grounding response, not an answer based on general model knowledge.

### Why do citations matter?

They make an answer inspectable: the user can see which speaker and moment support it. That is essential for a meeting record, where an unsupported answer can create false decisions or ownership.

### How do summary and DOCX export work?

The model produces validated structured summary and action-item data. Deterministic Python code then renders the DOCX from that stored data. Separating content generation from document formatting makes export more reliable and makes corrections flow into the final minutes.

### What is your data store?

The planned local store is SQLite with `sqlite-vec` for transcript retrieval. It keeps the hackathon deployment simple: one laptop and one local data file rather than several services.

### Can you swap models?

Yes. Capabilities request model resource types—such as speech-to-text, embedding, or reasoning—and configuration maps those types to concrete local models. That makes late performance tuning a configuration concern rather than a code rewrite.

## Privacy, offline operation, and reliability

### Is it private?

The product is designed so meeting audio, transcription, retrieval, and reasoning run locally on the laptop and local network. It has no accounts or cloud deployment in its hackathon scope. Access is appropriate only for a trusted meeting LAN; this is not positioned as production-grade enterprise security.

### Does it work offline?

The processing path is local-first, and models are pre-cached locally. Be precise about setup: the trusted-host QR join flow needs weak internet for an operator DNS preflight and the phone's initial hostname lookup. After joining, WebRTC, signaling, page assets, and AI processing stay local. Only claim a fully offline run if a local-DNS setup has been separately validated.

### Does it send meeting audio to an API?

Not in the intended default path. Local models are the default; a cloud backend, if configured at all, must be deliberately selected rather than used as an automatic fallback.

### What happens if the AI model fails?

The transcript path and the Q&A/summary path are isolated. A Q&A or summary failure is surfaced clearly while the stored transcript remains available; one device or one model failure should not take down other audio streams.

### How accurate is the transcription?

Accuracy depends on the local model, room noise, microphone placement, language, and speaking style. We verify it on the actual phones and network before presenting it; we show confidence/review controls instead of claiming perfect recognition.

## Scope and roadmap

### Can it scale to a company-wide deployment?

Not yet by design. The hackathon target is one laptop, one active meeting, and roughly 1–10 phones. Multi-tenant deployment, accounts, production security hardening, and horizontal scaling are deliberately out of scope for this version.

### What is next after the demo?

First, validate the core path across more real phones, browsers, noisy rooms, reconnects, and multi-device load. Then improve shared-device enrollment/classification, cross-meeting retrieval, action-item tracking, and deployment options without sacrificing the local-first design.

### Why not add Slack, calendar, or email integrations now?

They are useful later, but they do not prove the core value. The demo prioritizes a reliable chain from capture to attributed record to grounded answer to usable minutes.

### What languages are supported?

The current scope is English and Hindi/Hinglish transcription testing. Convene is not a translation product and does not claim broad multilingual workflow support.

### What are the biggest risks?

The live-demo risks are real-device networking and microphone behavior, transcription quality in noisy rooms, and cross-device bleed. That is why the stage flow uses dedicated phones, pre-cached local models, a rehearsed question, and a backup recording.

## Tough questions

### Isn't a phone-per-person setup inconvenient?

It is a tradeoff. For a small meeting, the phones are already in the room and browser joining keeps setup light. In return, the system gains a separate audio channel per person and more trustworthy attribution than a single mixed microphone can provide.

### What if someone deliberately joins as another participant?

The current hackathon design assumes a trusted room and LAN; it has no account or strong device-authentication system. This is an explicit scope limitation, not a claim of production-grade access control.

### Can a user correct a wrong label without losing provenance?

Yes. A correction updates the displayed/current attribution while preserving the original attribution and correction event in the audit record. Downstream Q&A, summaries, and exports use the corrected attribution.

### Is the summary generated continuously?

No. It runs on an explicit action, normally at the end of the meeting, using the attributed transcript. This avoids unnecessary computation and produces a more coherent meeting-level result.

### Why is the DOCX renderer deterministic?

Language models are useful for extracting summary and action-item content, but they are not reliable document-formatting engines. A fixed renderer ensures the final minutes have stable structure and cannot be malformed by model formatting.

### How do you measure success?

For the demo: phones join and reconnect, labeled transcript lines arrive within a few seconds, Q&A cites actual discussion and declines unsupported questions, and the meeting produces a summary and DOCX. Real-device checks are required to support claims about Wi-Fi, microphone access, accuracy, or offline operation.

## A concise final answer

“Convene turns meeting conversation into a trustworthy local knowledge base. The difference is that it captures separate participant audio streams from the start, so speaker attribution is explainable, answers are grounded in cited discussion, and the team leaves with actionable minutes rather than a wall of text.”
