CREATE TABLE PolicyDocument (
    policy_id TEXT PRIMARY KEY CHECK (length(policy_id) = 36),
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    tags TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL CHECK (created_at GLOB '????-??-??T??:??:??.???Z')
);
CREATE TABLE PolicyVersion (
    policy_version_id TEXT PRIMARY KEY CHECK (length(policy_version_id) = 36),
    policy_id TEXT NOT NULL REFERENCES PolicyDocument(policy_id),
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    storage_path TEXT NOT NULL,
    content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
    original_filename TEXT NOT NULL,
    media_type TEXT NOT NULL CHECK (media_type IN ('application/pdf', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')),
    byte_size INTEGER NOT NULL CHECK (byte_size > 0),
    extracted_text TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'ready', 'failed')),
    error_code TEXT,
    error_message TEXT,
    uploaded_at TEXT NOT NULL CHECK (uploaded_at GLOB '????-??-??T??:??:??.???Z'),
    UNIQUE(policy_id, version_number),
    CHECK ((status = 'ready') = (extracted_text IS NOT NULL)),
    CHECK ((status != 'failed') OR error_code IS NOT NULL)
);
CREATE INDEX idx_policy_version_policy ON PolicyVersion(policy_id, version_number DESC);
CREATE TABLE PolicyChunk (
    policy_chunk_id TEXT PRIMARY KEY CHECK (length(policy_chunk_id) = 36),
    policy_version_id TEXT NOT NULL REFERENCES PolicyVersion(policy_version_id),
    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
    text TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'ready', 'failed')),
    error_message TEXT,
    created_at TEXT NOT NULL CHECK (created_at GLOB '????-??-??T??:??:??.???Z'),
    UNIQUE(policy_version_id, chunk_index)
);
CREATE VIRTUAL TABLE PolicyChunkVector USING vec0(policy_chunk_id TEXT PRIMARY KEY, policy_version_id TEXT PARTITION KEY, embedding float[384] distance_metric=cosine);
