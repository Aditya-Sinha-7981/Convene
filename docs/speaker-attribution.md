# Speaker Attribution

This document is authoritative for how a transcribed window becomes an attributed `Utterance` (ADR-02, ADR-03, ADR-04). Read `project-context.md`'s "core insight" section first if you haven't — this document assumes that context.

## The two paths

### Path 1: non-shared device (the common case)

If `Device.is_shared = false`, attribution requires no model at all. Every transcribed window from that device is attributed directly to the device's single `Participant`.

```text
audio window → device_id → participant_id (1:1, from Device/Participant tables)
```

- `attribution_method = "device"`
- `attribution_confidence` = a fixed high value (this is a structural guarantee, not a model output — there is no classification step to be uncertain about)
- This path still allows manual correction (ADR-04) — cross-device audio bleed can, rarely, cause a wrong attribution even here; see "Residual risk" below.

### Path 2: shared device (2–3 people, one phone)

If `Device.is_shared = true`, the device declares `declared_speaker_count` at join, and each of those people must complete enrollment before the meeting is treated as `live` for that device.

#### Enrollment flow

1. Each declared speaker on the device records a short sample (target: 5–10 seconds of clear speech, e.g. reading a prompt sentence).
2. The sample is embedded via the `speaker_embedding` resource type (see `models.md`).
3. If the resulting embedding passes a minimum quality check (sample long/clean enough to produce a stable embedding), it's stored as a `SpeakerEnrollment` row, `quality_flag = "ok"`, and the `Participant.enrollment_status` moves to `enrolled`.
4. If the sample is too short or too noisy to embed reliably, `quality_flag = "low_confidence_sample"` and the participant is still marked `enrolled` but flagged — later classification against this participant should be treated as inherently less reliable, reflected in the confidence score at runtime.
5. If enrollment is skipped entirely (declined or timed out), `Participant.enrollment_status = "failed"` and that speaker has no reference embedding — any of their speech on that device falls into the generic-label path (below) for the whole meeting.

#### Runtime classification

For every transcribed window from a shared device:

1. Compute a speaker embedding for the window's audio.
2. Compare (cosine similarity) against every enrolled `SpeakerEnrollment` embedding for that device's participants.
3. If the best match clears a similarity threshold:
   - `participant_id` = that enrolled participant
   - `attribution_method = "enrolled"`
   - `attribution_confidence` = a value derived from the similarity score (higher similarity → higher confidence, not a flat constant)
4. If no enrolled embedding clears the threshold (unknown/unenrolled speaker, or a genuinely ambiguous window):
   - `participant_id = null`
   - `attribution_method = "generic_unresolved"`
   - `attribution_confidence` = low (fixed low value, since there is no reference to derive a score from)
   - The dashboard displays this as e.g. "Speaker on Phone 2" rather than inventing a name

This is nearest-centroid classification against known references, not unsupervised clustering — deliberately, per ADR-03, because classifying against a known reference degrades more predictably than blind clustering does.

## Confidence and correction (applies to both paths)

Every `Utterance`, regardless of path, carries `attribution_confidence`. The dashboard visually flags anything below a "trust this without checking" threshold — the exact threshold is a tuning parameter, not an architectural one, and should be set from real testing data (`testing.md`), not guessed.

Manual correction is a first-class action, not a special case:

1. A user clicks a transcript line in the dashboard and reassigns it to a different participant (or names an unresolved generic speaker).
2. The API sets `attribution_method = "manual_correction"`, `attribution_confidence = 1.0`, `corrected = true`, and preserves the previous `participant_id` in `original_participant_id`.
3. An `utterance_corrected` AuditEvent is fired, so the correction is part of the permanent record, not an invisible overwrite.
4. Corrected utterances feed downstream (chunking, summarization, export) using the corrected attribution — the original is retained for audit purposes only, never surfaced as if it were still current.

## Residual risk: cross-device audio bleed

Device-based attribution (Path 1) assumes a device's audio predominantly reflects its own owner. In practice, a nearby phone can pick up another speaker at reduced volume. This is mitigated, not eliminated, by the VAD energy-gating in `transport.md`/`stt-pipeline.md` (a distant, quieter voice is less likely to clear a device's own gate than its actual owner). Where it does slip through, the result is a stray line attributed to the wrong (but real, connected) participant — not a fabricated name — and it remains manually correctable like any other utterance. This is a documented, accepted limitation, not a silent gap: state it plainly if asked, rather than implying the system is bleed-proof.

## What this document deliberately does not do

- It does not attempt full acoustic diarization across the whole meeting — that's the problem this architecture exists to avoid (ADR-02).
- It does not treat a low-confidence label as equivalent to a high-confidence one anywhere downstream (summarization, export, RAG citations all carry the confidence/method forward — see `data-model.md`'s `Utterance` fields).
