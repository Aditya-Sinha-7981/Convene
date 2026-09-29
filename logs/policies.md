# Policy repository (CON-17)

## 2026-09-28 to 2026-09-30 — implementation and completion work

- Project lead approved the CON-17 defaults and explicitly allowed work before the CON-12 stability gate. CON-12 remains required before any demo claim.
- ADR-29 records: PDF + DOCX multipart upload; pure-local `pypdf`/`python-docx` extraction; SHA-256-addressed retained originals; separate policy chunk/vector tables in the same SQLite file; and current = newest fully indexed version.
- Upload responds `202` while a background task extracts and indexes. A failed scan/no text layer is retained with `failed` state and no OCR. Earlier ready versions remain the current/searchable selection until a replacement is fully ready.
- The upload write path now retains the binary atomically before inserting the version/audit rows; a database failure removes that file. The policy service serializes version allocation, resumes pending originals at startup, replaces only derived chunks on a retry, records a batch `ModelExecution`, and backs off retryable indexing failures.
- History Q&A now accepts explicit `sources: meetings | policies | both`; the default is `meetings`. Policy-only and mixed citations are resolved from stored policy rows, and the History page exposes the source choice. Mixed retrieval applies each index's threshold/cap before alternating retained evidence.
- The policy list filters by title and the detail page has a new-version upload action, read-only text for every successful version, and exact-original downloads. No version edit/delete, OCR, approval workflow, or live-meeting policy retrieval was added.

### Checks

```sh
.venv/bin/python -m compileall -q server
.venv/bin/python -m pytest tests/test_policy_extract.py tests/test_policy_service.py tests/test_policy_qa.py tests/test_history_qa.py tests/test_retrieval.py tests/test_db_migrations.py tests/test_audit_emit.py tests/test_config.py tests/test_api_contract_docs.py -q -m 'not model'
```

- Passed: compile and JavaScript syntax checks; focused policy extraction/service/Q&A plus RAG/schema/contract checks (**155 passed** on 2026-09-30). These use generated fixture documents and fake local adapters; they do not make a model-quality claim.
- Full non-model suite: **553 passed, 18 failed, 158 errors, 12 model tests deselected**. The errors are the pre-existing sandbox restriction against binding `127.0.0.1`; HTTP/transport tests consequently cannot start Uvicorn. The run is not evidence of a policy regression until repeated on a machine that permits loopback. The failures need triage there as well.
- Not run: real multi-page PDF/DOCX extraction, policy embedding/retrieval calibration with the reference model, browser/manual upload, offline physical-network check, and live-STT interference measurement. Those require the reference laptop/models and are recorded as not run, not inferred. CON-12 remains unmet.

### Handoff / remaining physical validation

- Manual: upload text PDF/DOCX, upload a replacement while the first is current, verify original hashes/downloads, upload image-only PDF, ask Policies and Both questions, and confirm policy citation links. The work order's seven manual steps remain the acceptance rehearsal.
- Hardware/model: measure a 20-page policy index while synthetic/live STT runs; run policy question calibration and honesty checks with local cached models; run `scripts/verify_local_only.py` with networking disabled. Do not call the overall demo stable until CON-12 passes.

### Disposable demo fixture

- `scripts/seed_con17_demo.py` creates `data/con17-policy-demo.db` with eight synthetic ended meetings, a manifest, and three synthetic DOCX policy uploads (including travel v1/v2). The files are ignored by Git.
- `scripts/remove_con17_demo.py` reads only that manifest and removes exactly the recorded meetings plus the generated DOCX inputs. A temporary-database seed → remove round-trip passed with zero remaining meetings and no input directory.

## 2026-09-30 — policy UI refinement

- Rebuilt the policy list and version detail pages from the browser-default prototype into responsive Convene surfaces: shared navigation/theme support, searchable policy cards, tags, clear ready/processing/failed state badges, contextual upload panels, original-download links, and retry feedback.
- History policy citations receive a distinct source treatment while keeping the established meeting citation layout and links.
- Verified with `node --check client/policies.js`, `node --check client/policy.js`, `node --check client/history_qa.js`, and `git diff --check`. Browser visual review remains to be performed locally.
- Tightened the answer instruction to request short, complete sentences and extended the defensive cleanup to remove a model's literal `[speaker, time since that meeting started]` source placeholder. Citation cards remain the readable, linked source presentation.
