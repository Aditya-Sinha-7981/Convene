# Policy repository (CON-17)

## 2026-09-28 — implementation start

- Project lead approved the CON-17 defaults and explicitly allowed work before the CON-12 stability gate. CON-12 remains required before any demo claim.
- ADR-29 records: PDF + DOCX multipart upload; pure-local `pypdf`/`python-docx` extraction; SHA-256-addressed retained originals; separate policy chunk/vector tables in the same SQLite file; and current = newest fully indexed version.
- Upload responds `202` while a background task extracts and indexes. A failed scan/no text layer is retained with `failed` state and no OCR. Earlier ready versions remain the current/searchable selection until a replacement is fully ready.

### Checks

```sh
.venv/bin/python -m compileall -q server
.venv/bin/python -m pytest tests/test_policy_service.py tests/test_history_qa.py tests/test_retrieval.py tests/test_db_migrations.py tests/test_audit_emit.py tests/test_config.py tests/test_api_contract_docs.py -q -m 'not model'
```

- Passed: compile check; 144 focused tests.
- Not run: realistic PDF/DOCX extraction, policy embedding/retrieval calibration, browser/manual upload, offline physical-network check, and live-STT interference measurement. CON-12 remains unmet.
