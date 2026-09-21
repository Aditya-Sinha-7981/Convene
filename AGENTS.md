# AGENTS.md — Convene

Read this before changing code or documentation in this repository. It is the instruction file for Convene work.

## Project and current state

Convene turns phones on a local Wi-Fi network into separate meeting microphones. A laptop transcribes each stream, attributes speech primarily by device identity, serves live Q&A over the meeting transcript, and produces a summary and DOCX minutes. The target is a reliable offline hackathon demo on one laptop with 1–10 phones.

The checked-in `server/` and `client/` are the earlier DT-17 WebRTC transport prototype with optional command-driven STT. The full Convene system described in `docs/` is planned, not yet implemented. Do not claim a feature works because it has an architecture document.

## Read before work

1. Read `docs/00-AI-CONTEXT.md` and `docs/project-context.md` for the product and reading order.
2. Read `docs/requirements.md`, `docs/architecture.md`, and the topic documents linked from `docs/README.md` that govern the task.
3. Read relevant decisions in `docs/decisions.md` before altering a locked choice.
4. Inspect existing code and tests. For transport work, read the original baseline in `old_docs/TEST_PLAN.md` and `old_docs/HTTPS_LOCAL.md`.
5. If working from a task, read its work order in `convene-tasks/` when available. That folder is local planning material and is ignored by Git; the architecture in `docs/` remains authoritative.

`bulwark_docs/` and `tasks/` describe Bulwark. They are reference material only and do not define Convene behavior.

## Architectural rules

- Keep one laptop server with FastAPI and aiortc, using WebRTC for audio and WebSocket for signaling and live dashboard updates.
- Attribute non-shared device audio through its registered participant. Use acoustic speaker classification only for declared shared devices.
- Store confidence on each utterance and support manual correction with the original attribution preserved in the audit record.
- Default to local models. A cloud backend may be used only through a deliberate user action or configuration, never as an automatic fallback.
- Resolve models by resource type. Keep concrete model identifiers in configuration, not scattered through application logic.
- Run retrieval only for an explicit question. Ground Q&A in indexed meeting content and show citations; return an honest no-grounding result when evidence is insufficient.
- Generate summaries as validated structured data. Render DOCX with deterministic code over stored data.
- Keep SQLite and `sqlite-vec` as the planned local stores, and one audit/event stream as the record of what happened.
- Preserve device isolation and reconnect identity. One phone's error must not stop another phone's audio or transcript.

The detailed contracts live in `docs/data-model.md`, `docs/api.md`, `docs/transport.md`, `docs/stt-pipeline.md`, `docs/speaker-attribution.md`, `docs/rag-and-qa.md`, `docs/summarization.md`, and `docs/export.md`. If a contract is incomplete, resolve and document it before dependent code relies on a guessed shape. If a requested change conflicts with a locked architecture decision, explain the conflict and ask the user before changing that decision.

## Working procedure

1. Check `git status` and inspect the files that will be affected. Respect existing user changes.
2. Identify the smallest coherent change and its dependencies. Follow `docs/implementation-plan.md` for build order.
3. Implement the change and update the authoritative document when an API, schema, event, or behavior contract changes. Record the rationale in `docs/decisions.md` if an architectural decision changes.
4. Run focused automated checks and the manual checks that matter for the change. Transport, microphone access, speaker attribution, offline operation, and model performance require real device or target-hardware validation; a unit test alone cannot establish those claims.
5. Review the diff and report what changed, what was verified, and what remains untested or unresolved.

Keep a concise work log in `logs/<workstream>.md` for substantial, multi-session implementation work. Record decisions, test commands/results, and handoff issues. Update it when meaningful work completes; small documentation edits do not require a log. The log is tracked project history, so do not put secrets or private audio/transcripts in it.

## Git and commit authority

The AI may inspect Git state and prepare changes without separate permission. It may ask the user for permission to commit a completed, reviewable change. **The AI must not commit unless the user explicitly allows that commit.** Permission to commit one change does not imply permission for later commits. If permission is granted, review the staged diff and commit only the intended files.

When the AI makes an authorized commit, use an ordinary project commit message. Do not add an AI name, AI-generated label, `Co-authored-by` trailer, or any other AI attribution to the commit message. Do not modify Git author identity to impersonate a person; use the repository's existing Git identity configuration. Do not push, open a PR, or merge unless the user separately authorizes that action.

`tasks/`, `convene-tasks/`, and `bulwark_docs/` are ignored in `.gitignore`; do not force-add them without an explicit request.

## Scope and verification

The demo-critical path is transport → meeting/device registry → local STT → device attribution and correction → dashboard → live RAG Q&A → summary → DOCX export → offline end-to-end test. Shared-device classification and cross-meeting history follow after the core demo is stable. See `docs/requirements.md` for priority and out-of-scope items.

Use `docs/testing.md` and `docs/demo.md` for acceptance. State clearly when a check could not run because phones, certificates, local models, or the reference laptop were unavailable. Never treat a mock or a planned feature as proof that the offline end-to-end demo passes.
