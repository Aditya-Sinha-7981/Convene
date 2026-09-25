-- CON-08: durable transcript chunks and their sqlite-vec embeddings.
-- `sentence_transformers` is the local CPU runtime selected for the initial embedding model.
ALTER TABLE ModelExecution RENAME TO ModelExecution_old;
CREATE TABLE ModelExecution (
    model_execution_id  TEXT PRIMARY KEY CHECK (length(model_execution_id) = 36),
    resource_type       TEXT NOT NULL CHECK (resource_type IN ('stt', 'embedding', 'speaker_embedding', 'reasoning')),
    model_identifier    TEXT NOT NULL CHECK (length(model_identifier) > 0),
    runtime             TEXT NOT NULL CHECK (runtime IN ('mlx', 'sentence_transformers', 'groq', 'gemini')),
    duration_ms         INTEGER NOT NULL CHECK (duration_ms >= 0),
    related_id          TEXT,
    created_at          TEXT NOT NULL CHECK (created_at GLOB '????-??-??T??:??:??.???Z')
);
INSERT INTO ModelExecution SELECT * FROM ModelExecution_old;
DROP TABLE ModelExecution_old;
CREATE INDEX idx_model_execution_type_created ON ModelExecution (resource_type, created_at);

CREATE TABLE TranscriptChunk (
    chunk_id             TEXT PRIMARY KEY CHECK (length(chunk_id) = 36),
    meeting_id           TEXT NOT NULL REFERENCES Meeting(meeting_id),
    utterance_id_start   TEXT NOT NULL REFERENCES Utterance(utterance_id),
    utterance_id_end     TEXT NOT NULL REFERENCES Utterance(utterance_id),
    text                 TEXT NOT NULL,
    chunk_index          INTEGER NOT NULL CHECK (chunk_index >= 0),
    status               TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'ready', 'failed')),
    error_message        TEXT,
    created_at           TEXT NOT NULL,
    UNIQUE (meeting_id, chunk_index)
);
CREATE INDEX idx_transcript_chunk_meeting_status ON TranscriptChunk(meeting_id, status, chunk_index);

-- A one-row guard prevents silently mixing embeddings from different vector spaces in this SQLite file.
CREATE TABLE TranscriptIndexMeta (
    singleton            INTEGER PRIMARY KEY CHECK (singleton = 1),
    model_identifier     TEXT NOT NULL,
    dimension            INTEGER NOT NULL CHECK (dimension > 0),
    distance_metric      TEXT NOT NULL CHECK (distance_metric = 'cosine'),
    created_at           TEXT NOT NULL
);

-- sqlite-vec supports both stable TEXT primary keys and a meeting partition key (CON-03 spike).
CREATE VIRTUAL TABLE TranscriptChunkVector USING vec0(
    chunk_id TEXT PRIMARY KEY,
    meeting_id TEXT PARTITION KEY,
    embedding float[384] distance_metric=cosine
);
