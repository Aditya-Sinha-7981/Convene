# Offline integration and demo rehearsal (CON-12)

## Automation setup — 2026-09-26

- Added `scripts/start_demo.sh`: one reviewed startup command; it requires certificate/key environment variables and sets local-only Hugging Face/Transformers mode.
- Added `scripts/verify_local_only.py` and parser tests. It samples established TCP peers with `lsof`, permits loopback/private/link-local peers, and fails public or hostname peers. It is sampling evidence only, not proof of every packet.
- Added demo-day and physical-phone operator checklists under `tests/manual-test/`.
- Physical offline rehearsal, Android/iOS certificate trust, multi-phone behavior, backup video, second-person runbook validation, and the three-run stability gate remain **Not run — require target hardware and people**.

### Verification

- `tests/test_verify_local_only.py` and `tests/test_api_contract_docs.py`: **41 passed**.
- `tests/e2e/test_offline_e2e.py -m model`: **collected, not run** in this session; it requires the cached STT, embedding, and reasoning models and an available loopback server.
- The full non-model suite was attempted outside the workspace loopback restriction. It reaches an existing baseline failure in `tests/test_db_migrations.py::test_creates_the_schema_from_an_empty_file`: migration code reports schema version 8 while the test still expects 7. This task does not change migrations, so the mismatch is not modified here.

No private audio, transcript, export, credentials, or video path is recorded here.
