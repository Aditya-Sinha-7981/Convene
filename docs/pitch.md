# Convene — Demo Pitch and Flow

## The one-line pitch

**Convene turns the phones already in the room into separate meeting microphones, giving teams a live, speaker-attributed, queryable meeting record that stays on the local network.**

## The problem

Most meeting tools start with one mixed audio stream and then try to infer who spoke. When that guess is wrong, the meeting record loses the context that makes it useful: who made a decision, who accepted an action, and what evidence supports an answer.

Even when transcription is accurate, teams are often left with a long transcript they never revisit.

## The insight

Convene changes the input, rather than relying entirely on a harder AI problem. Each participant joins with their own phone; the audio arrives on a separate channel already tied to that participant. In the common case, attribution comes from device identity, not acoustic speaker guessing.

That makes the record more explainable: a transcript line is labeled because it arrived from that participant's registered phone. Every line still has a confidence signal and can be manually corrected, so the system does not present uncertain output as unquestionable fact.

## What Convene does

1. Participants scan one QR code and use their browser as a microphone.
2. Phones stream audio over local Wi-Fi to one laptop using WebRTC.
3. The laptop creates a live, speaker-labeled transcript.
4. During the meeting, a user can ask a question and receive an answer grounded in the recorded discussion, with citations to the speaker and time.
5. At the end, Convene produces a structured summary, action items, and downloadable DOCX minutes.

The design is local-first: meeting traffic and AI processing are intended to run on the laptop, avoiding a per-meeting cloud-AI dependency.

## 90-second spoken pitch

> Meetings generate decisions, owners, and context—but most transcription tools give us a wall of text and then try to guess who said each line from one mixed microphone. That is exactly where trust breaks.
>
> Convene uses a simpler idea: every participant's phone becomes their own microphone. Because each audio stream is already associated with a registered participant, speaker attribution in the normal case comes from device identity rather than from trying to distinguish voices after they have been mixed together.
>
> On this dashboard, you can see phones connect, watch a live labeled transcript appear, and ask a question about the conversation while it is still happening. The answer is grounded in the meeting record and points back to the relevant speaker and moment.
>
> When the meeting ends, Convene turns that record into a summary, action items, and DOCX minutes. It is a meeting memory system built for privacy, reliability, and accountability—not just another transcript.

## Demo flow

Use two or three phones and rehearse the exact words and question below. The Q&A moment is the centerpiece; do not dilute it by touring every screen.

| Moment | What to show | What to say |
|---|---|---|
| 1. Problem | Start on the empty dashboard. | “Most tools hear one mixed stream and have to guess who spoke. A wrong label makes the record less useful.” |
| 2. Join | Scan the QR code from two or three phones; show connected states. | “There is no app to install. Each phone joins through its browser and becomes a dedicated microphone.” |
| 3. Live record | Have each person state a prepared decision and an owner. Wait for labeled transcript rows. | “These labels come from the device that sent the audio, not from blind diarization of one mixed recording.” |
| 4. Ask | Ask a prepared question. Show the answer and its citations. | “Now the meeting is queryable while it is still happening. The answer is grounded in what was said, and it shows the evidence.” |
| 5. Close | End the meeting; show summary, action items, and DOCX. | “Instead of leaving with a transcript, the team leaves with usable minutes and accountable next steps.” |
| 6. Finish | Return to the transcript or export. | “Convene makes meeting memory attributable, searchable, and local-first.” |

## Rehearsal script for the speakers

Use unmistakable names and facts so the transcript, Q&A, and action items have clear evidence.

- **Asha:** “We will launch the pilot on Friday, October ninth. I will prepare the onboarding guide by Wednesday.”
- **Rohan:** “I will test the Android join flow and share the results by Thursday.”
- **Asha:** “The decision is to keep all meeting processing on the local laptop for the demo.”

Ask: **“Who owns the Android join-flow testing, and when will they share results?”**

Expected evidence: Rohan and Thursday. If this question is not answered cleanly in rehearsal, change the spoken wording—not the live question—to make the evidence more explicit.

## Credible claims and careful wording

Say what you have rehearsed and verified on the actual demo setup. This is particularly important because real-phone, Wi-Fi, and model behavior need device-level validation.

| Topic | Safe presentation wording | Do not overclaim |
|---|---|---|
| Attribution | “For one participant per phone, labels come from registered device identity.” | Do not imply perfect attribution when phones pick up nearby speakers. |
| Shared phones | “Multiple-speaker shared-phone attribution is a planned extension; the core demo uses one phone per person.” | Do not present it as production-ready unless it has been tested successfully. |
| Corrections | “Uncertain or wrong transcript labels can be reviewed and corrected, while preserving the original attribution in the audit record.” | Do not call a correction proof that the original model output was right. |
| Q&A | “Answers are grounded in retrieved meeting content and include citations; when there is insufficient evidence, the correct response is that it does not know.” | Do not portray it as a general web-search assistant. |
| Privacy / local-first | “Audio, transcription, and meeting intelligence are designed to stay on the local laptop and Wi-Fi.” | With the trusted-host join path, do not claim a completely internet-free phone join: it needs a small DNS preflight and initial hostname lookup. |
| Scale | “The hackathon target is one laptop, one meeting, and roughly 1–10 phones.” | Do not claim multi-tenant, enterprise-scale, or hardened production deployment. |
| Languages | “The demo is scoped to English and Hindi/Hinglish transcription testing.” | Do not claim translation or broad multilingual workflow support. |

## Before stepping on stage

- Rehearse the exact speaker lines and Q&A question on the same laptop, Wi-Fi, phones, browser versions, certificates, and model configuration that will be used live.
- Verify the intended end-to-end path: join, live transcript, Q&A, summary, and DOCX export.
- Keep a short pre-recorded successful run ready. If phone or Wi-Fi setup fails, switch quickly and narrate the flow rather than troubleshooting on stage.
- If a feature has not been verified in this setup, describe it as planned or omit it from the live flow.

## Closing line

**Convene does not just record a meeting. It preserves who said what, lets the team ask the meeting for evidence, and turns the result into action—without making the cloud the critical path.**
