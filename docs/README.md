# Convene documentation

Start with [00-AI-CONTEXT.md](00-AI-CONTEXT.md) for the product, architectural rules, and full reading order. [Implementation plan](implementation-plan.md) turns those contracts into a dependency-ordered build sequence.

## Status

Convene is implemented through the live dashboard: phone-to-laptop WebRTC transport, meeting/device persistence, local STT, dedicated-device attribution, manual correction, and live device/transcript views run in the repository. Trusted-host hotspot setup is implemented but unvalidated on real phones. RAG, summary, export, shared-device enrollment/classification, and history remain planned. Treat remaining contracts as plans, not claims that their features run.

The operational instructions for the current prototype are in the [root README](../README.md). Its original test and HTTPS notes are in [`old_docs/`](../old_docs/README.md). `bulwark_docs/` describes a different project; its documentation organization informed this index and plan, but its product decisions do not apply to Convene.

## Find a contract

| Need | Read |
|---|---|
| Goal, scope, priorities | [Project context](project-context.md), [requirements](requirements.md) |
| Components and decisions | [Architecture](architecture.md), [decisions](decisions.md) |
| Stored records and endpoints | [Data model](data-model.md), [API](api.md) |
| Audio and speaker identity | [Transport](transport.md), [STT pipeline](stt-pipeline.md), [speaker attribution](speaker-attribution.md) |
| Models and meeting intelligence | [Models](models.md), [RAG and Q&A](rag-and-qa.md), [summarization](summarization.md) |
| UI and output | [Frontend](frontend.md), [export](export.md) |
| Running and verifying | [Deployment](deployment.md), [network and HTTPS](network-and-https.md), [domain setup](domain-setup.md), [testing](testing.md), [manual tests](manual-tests.md), [demo](demo.md) |

When a plan and a topic contract differ, follow the topic contract and update the plan. For rationale, consult `decisions.md`.
