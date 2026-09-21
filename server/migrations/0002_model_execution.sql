-- 0002_model_execution: one row per model invocation (docs/data-model.md, ModelExecution).
-- `runtime` values are the documented ones; `related_id` is a window reference for `stt`
-- ("<device_id>/<window_id>"), because an STT window exists before (and often without) an Utterance.
-- The runner wraps this file in a transaction and sets PRAGMA user_version; do not add BEGIN/COMMIT.

CREATE TABLE ModelExecution (
    model_execution_id  TEXT PRIMARY KEY CHECK (length(model_execution_id) = 36),
    resource_type       TEXT NOT NULL CHECK (resource_type IN ('stt', 'embedding', 'speaker_embedding', 'reasoning')),
    model_identifier    TEXT NOT NULL CHECK (length(model_identifier) > 0),
    runtime             TEXT NOT NULL CHECK (runtime IN ('mlx', 'groq', 'gemini')),
    duration_ms         INTEGER NOT NULL CHECK (duration_ms >= 0),
    related_id          TEXT,
    created_at          TEXT NOT NULL CHECK (created_at GLOB '????-??-??T??:??:??.???Z')
);
CREATE INDEX idx_model_execution_type_created ON ModelExecution (resource_type, created_at);
