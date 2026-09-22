# logs/attribution.md

## CON-06 — Device attribution and manual correction

### Summary (2026-09-23)

- Successful STT speech segments are persisted as one `Utterance` each without blocking the STT callback.
- Non-shared devices use their sole registered participant, method `device`, confidence `0.95`.
- Shared devices use the safe pre-CON-13 stub: null participant, `generic_unresolved`, confidence `0.2`.
- Manual correction is atomic, preserves the first original participant, records complete from/to state in
  `utterance_corrected`, and supports naming an unresolved speaker.
- Same-participant correction confirms once at confidence `1.0`; an identical replay is a no-op.
- The server owns the `0.8` attribution threshold. STT and attribution scores stay separate.
- Transcript and correction REST endpoints and `utterance` / `utterance_updated` dashboard translation are wired.
- Post-write and correction hook registries are non-blocking; failures are isolated and audited as `hook_failed`.
- Summary/export staleness can be derived from the latest `utterance_created` or `utterance_corrected` sequence.

### Decisions

The project lead approved proceeding with the recommended defaults: device confidence `0.95`, manual correction
confidence `1.0`, first-original preservation, same-participant confirmation, generic labels by device join order,
and one utterance per successful speech segment. Unresolved confidence is `0.2`, matching the API examples; the
initial low-confidence threshold is `0.8`. Thresholds remain provisional until CON-15 real-phone measurement.

### Verification

- Focused unit/service tests: `10 passed` before API/e2e additions.
- Focused synthetic WebRTC test: `1 passed`; loopback and a fake adapter, not evidence about real phones.
- Final focused contract/CON-06 suite: `52 passed`.
- Final default repository suite: `460 passed, 6 deselected` in 93.41 seconds.
- Provisioned real-model suite: `6 passed, 460 deselected` in 41.63 seconds. Its WebRTC clients remain synthetic;
  this validates model integration on the reference laptop, not physical phones or room acoustics.

### Hardware checks

- Single real phone, five minutes: **Not run — physical testing deferred by the project lead.**
- Two real phones with distinct speakers/phrases: **Not run — physical testing deferred.**
- Correction round-trip on a real transcript: **Not run — no real transcript yet.**
- Failed synthetic STT window followed by later windows: covered by the existing automated STT suite; this does
  not prove microphone or phone behavior.

### Handoff

- CON-07 consumes complete `utterance` and `utterance_updated` views and must use `low_confidence` rather than
  comparing confidence numbers in JavaScript.
- CON-08 can register post-write and correction hooks on `runtime.attribution`.
- CON-10/11 use `artifact_is_stale` with their stored `input_as_of_seq`.
- CON-13 replaces only the shared-device unresolved branch.
