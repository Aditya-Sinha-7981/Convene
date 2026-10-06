# Convene

**Convene** turns smartphones on the same Wi-Fi network into individual meeting microphones. Each participant joins from their phone, while a laptop receives separate audio streams, transcribes them locally, attributes speech by device identity, and turns the conversation into searchable minutes, action items, and exportable documents.

Built by **Team The Mentalist** for the **SISTec Innovation Hackathon 4.0 (SIH 4.0)**.

## Achievement

- **Overall Winner:** SISTec Innovation Hackathon 4.0
- **Prize:** ₹40,000
- **Team:** The Mentalist
- **Event:** [SISTec Innovation Hackathon 4.0](https://www.sistecrsih.in/)

SIH 4.0 was a national-level, 24-hour innovation hackathon hosted by SISTec Ratibad, Bhopal. Convene was developed for its DT-17 problem statement and evolved through several iterations based on judge feedback.

## The problem

Traditional meeting transcription tools usually receive one mixed audio stream and try to guess who said what. That creates unreliable speaker labels, especially in rooms with overlapping speech or similar voices.

Convene takes a different approach:

> Give each participant a phone.
> Treat each phone as a dedicated microphone.
> Use device identity for reliable speaker attribution.

This makes the common case a reliable systems problem rather than a fragile speaker-diarization problem.

## What Convene does

- Connects 1–10 phones to one laptop over local Wi-Fi
- Captures and streams each phone's microphone audio through WebRTC
- Transcribes speech locally using Whisper-based STT
- Attributes transcript lines to participants through their registered devices
- Shows a live speaker-labelled transcript dashboard
- Flags low-confidence attribution and supports manual correction
- Answers questions grounded in the current or past meeting transcript
- Generates structured summaries and action items
- Exports meeting minutes as DOCX
- Supports meeting history, cross-meeting Q&A, action-item tracking, reports, and a local policy repository
- Keeps meeting traffic and default AI processing local

## Architecture

```text
Phones
  │
  ├── Browser microphone capture
  ├── WebRTC audio streams
  └── WebSocket signalling
          │
          ▼
Laptop Server
  ├── FastAPI + aiortc
  ├── Per-device VAD and audio segmentation
  ├── Local speech-to-text
  ├── Device-based speaker attribution
  ├── SQLite + sqlite-vec storage
  ├── Live dashboard updates
  ├── Local RAG-based Q&A
  ├── Structured summarization
  └── Deterministic DOCX export
```

## Tech stack

- **Backend:** Python, FastAPI, aiortc
- **Transport:** WebRTC and WebSockets
- **Speech-to-text:** MLX Whisper
- **LLM / summaries / Q&A:** Local MLX language models
- **Retrieval:** sentence-transformers + sqlite-vec
- **Database:** SQLite
- **Document export:** python-docx
- **Frontend:** Vanilla HTML, CSS, and JavaScript

## Quick start

### Requirements

- Python 3.11–3.13
- A Mac with Apple Silicon is recommended for the default MLX model stack
- Phones and laptop connected to the same local Wi-Fi network
- HTTPS for mobile browser microphone access

### Install

```sh
uv venv --python 3.13
uv pip install -r requirements-dev.txt
```

### Provision local models

Run this once while online:

```sh
.venv/bin/python scripts/provision_models.py
.venv/bin/python scripts/provision_models.py --resource embedding
.venv/bin/python scripts/provision_models.py --resource reasoning
```

### Run

```sh
.venv/bin/python -m server.app
```

For the full trusted-host, HTTPS, and hotspot setup, see:

- [Deployment guide](docs/deployment.md)
- [Network and HTTPS guide](docs/network-and-https.md)
- [Manual phone test checklist](docs/manual-tests.md)

## Privacy and local-first design

Convene is designed for local meetings:

- Audio stays on the local network.
- Default transcription, embeddings, retrieval, and reasoning run locally.
- Cloud services are optional and must be explicitly configured or triggered.
- Meeting data is stored locally in SQLite.
- No user account system is required for the demo workflow.

## Team

**The Mentalist**

- Aaditya Sinha — Team Lead, architecture, engineering, product direction, and presentation
- Add team members and contributions here

## Documentation

The project documentation covers the architecture, API contracts, model choices, deployment, testing, demo flow, and technical decisions.

Start here:

- [Project context](docs/project-context.md)
- [Architecture](docs/architecture.md)
- [API reference](docs/api.md)
- [Deployment](docs/deployment.md)
- [Testing](docs/testing.md)
- [Demo flow](docs/demo.md)

## A personal note

Convene is the project I worked on, built from the start with a small idea of how can I transfer audio chunks from a mic to my laptop with as little internet as possible(only DNS resolution) and wirelessly and then it just kept shaping up into what it is today. I wanted to build this because this is a problem many of us face and I decided to put the features I thought I found useful, turns out, the judges found them useful too

This is what the coding was all about, finding solutions to a problem that you face and I believe this is a biggest gift for a coder, the ability to find a solution for any of their problem. 

I know there are online polished alternatives like Otter or Firefly, but they are just that, online, meanwhile Convene with it's mascot(took me a lot of time to design) is offline first 

This project and the presentation planning proved to me that I actually have what it takes, from BGI hackathon 3rd place winner to Sistec 1st place overall winner, this is a milestone for me.

Sistec Innovation Hackathon 3.0 was my first ever hackathon, and now in 4.0, I am the winner of it. I have learned many things in that 1 year and proved that I have what it takes to thrive. 

The best of of it, I never felt the pressure during presentation or project questioning, since I knew how it was built, I knew what was done where because I did it, and I was confident in it. It was fun to even see the judges trying to come up with questions, if any at all. A wonderful time indeed.

To anyone who's reading this, this project is proof that a simple idea, you can make something great, it's not always about how complex your project it, but how useful and easy-to-use it is for the people around you. 

— **Aaditya Sinha**, Team Lead, The Mentalist
