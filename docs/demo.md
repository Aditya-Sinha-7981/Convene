# Demo Script

## Principle

One clean, rehearsed flow beats showing every feature (`project-context.md`). This script is the thing to practice until it's boring to run, not a feature checklist.

## Pre-demo checklist

- All local models pre-loaded and verified working with internet disconnected (`deployment.md`).
- Phones pre-connected to the demo Wi-Fi, join links/QR ready.
- A backup video of a full successful run, recorded in advance, ready to play if live phones/Wi-Fi fail on stage (`project-context.md`'s stated biggest demo risk).
- A rehearsed one-line answer ready for "why not just use Otter.ai/Fireflies" (`project-context.md`'s pitch, restated: hardware-based attribution instead of guessing, local meeting processing, queryable memory).

## Script

1. **Open with the problem, briefly.** Meeting transcription tools guess who's talking from one mixed mic and get it wrong; the record you never read again isn't useful.
2. **Show phones joining.** 2–3 phones connect live, dashboard shows connection state — fast, visibly working, don't linger.
3. **Speak, show the live transcript.** Each line appears correctly labeled by speaker within a few seconds. State plainly, in one sentence, *why* the labels are reliable (device-per-speaker, not audio guessing) — this is the core technical story, say it once, clearly.
4. **The key moment: live Q&A.** Ask the dashboard a question about something said a few minutes earlier in this same conversation. Get an answer, grounded, with a citation back to who said it and when. This is the strongest, most differentiated beat in the whole demo — give it the most time and the clearest framing.
5. **End the meeting, show the summary and action items.** Fast — this is expected functionality, not the differentiator, don't over-linger.
6. **Download the export.** Show the DOCX opening, briefly. Tangible artifact, closes the loop.
7. **Close with the local-processing claim.** State that audio, transcription, and meeting intelligence stayed on the local laptop/network; the trusted join hostname used a small DNS lookup before joining. Do not claim a fully offline run unless a separately validated local-DNS setup was used.

## What not to do in the demo

- Don't attempt the shared-device (multiple people, one phone) flow live unless it has been tested extensively and is fully reliable — it's a should-have feature (`requirements.md`), not worth the risk of showing a rough edge live.
- Don't try to show every feature — cross-meeting history search, manual correction UI, etc. are fine to mention verbally or show only if a judge specifically asks, not part of the core script.
- Don't leave the RAG question to chance — know in advance exactly what was said and exactly what question you'll ask, so the demo moment is guaranteed to land.

## Anticipated questions and honest answers

| Likely question | Answer |
|---|---|
| "Why not just use Otter/Fireflies?" | They guess speaker identity from one mixed mic; we get it from which phone the audio came from, which is inherently more reliable, and audio/transcription stay local with zero per-meeting AI cost. |
| "What if two people share one phone?" | We support enrollment-based classification for that case, with low-confidence flagging and one-click manual correction — see `speaker-attribution.md`. Say this honestly as a real, tested feature; don't oversell its accuracy. |
| "What happens if it mishears something?" | Every line carries a confidence score and is correctable in one click — wrong output is visible and fixable, never silently authoritative. |
| "Does this scale beyond a hackathon?" | Today it's one laptop, one meeting at a time by design (`requirements.md` non-goals) — the architecture (adapters, structured data model) is built to extend, but scaling to multi-tenant/cloud is a deliberately separate, later problem. |

## Failure recovery, live

If a phone fails to connect on stage: don't troubleshoot live for more than a few seconds — fall back immediately to the pre-recorded backup video and narrate over it. A confident recovery reads better to judges than a visibly stuck demo.
