# Summarization & Action Items

## Scope

Producing a meeting summary and action-item list from a completed (or in-progress, if manually triggered early) transcript. Rendering that data into a downloadable file is a separate, deterministic step — see `export.md`.

## Trigger

Runs once, on an explicit action: the meeting-end action, or a manual "summarize now" button in the dashboard. It never runs continuously or per-utterance — summarizing the whole transcript at once is both cheaper and produces a more coherent result than incremental summarization.

## Input

The full attributed transcript for the meeting, in order, using the *current* (possibly corrected) attribution for every utterance — never the original pre-correction attribution (`speaker-attribution.md`). Utterances with `attribution_method = "generic_unresolved"` are included using their generic label as-is; the summary should not invent a name for an unresolved speaker.

## Output contract

The `reasoning` resource type is invoked with a constrained-output instruction to produce structured data only:

```json
{
  "summary": "string — a few paragraphs, plain text",
  "action_items": [
    { "text": "string", "owner": "string or null" }
  ]
}
```

This structured output is what gets validated and persisted (`Summary`, `ActionItem` rows — `data-model.md`) — the model never produces the final document's formatting or markup directly (ADR-12). If the model's output doesn't parse as valid structured data, the summarization attempt is retried once with a stricter instruction, then marked `failed` rather than falling back to using malformed output.

## Owner inference

If the model can reasonably infer an action item's owner from the transcript context (e.g. someone explicitly says they'll do something), it sets `owner`; otherwise `owner` is left null rather than guessed. A null owner is a normal, expected outcome, not a failure — the dashboard should let a user assign one manually afterward (should-have feature, `requirements.md`).

## Quality expectations

Local-model summarization quality is good enough for meeting minutes at this scale, but is not held to the same bar as the largest cloud models (ADR-06's accepted tradeoff). If summary quality genuinely disappoints in testing, the first fix is prompt iteration, the second is trying a slightly larger local model within the memory budget (`models.md`), and only as a last resort is the cloud `reasoning` fallback activated — deliberately, not silently.

## Failure handling

A summarization failure (model error, parse failure after retry) is marked `failed` and surfaced to the dashboard; the transcript itself remains fully intact and viewable — a summarization failure must never appear to affect or invalidate the underlying transcript data.

## Relationship to RAG

Summarization and Q&A (`rag-and-qa.md`) both use the `reasoning` resource type but are otherwise independent pipelines — summarization reads the full transcript directly (no retrieval step needed, since the whole meeting is small enough to fit in context at this scale), while Q&A always goes through chunk retrieval first. Do not conflate the two or try to unify them into one pipeline; they have different inputs and different failure semantics.
