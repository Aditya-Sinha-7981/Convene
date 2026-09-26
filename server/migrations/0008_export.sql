-- CON-11: deterministic DOCX export attempts.  A failed retry is kept as a
-- separate row, so it can never hide or overwrite an earlier ready file.
CREATE TABLE Export (
    export_id      TEXT PRIMARY KEY CHECK (length(export_id) = 36),
    meeting_id     TEXT NOT NULL REFERENCES Meeting (meeting_id),
    type           TEXT NOT NULL CHECK (type = 'docx'),
    status         TEXT NOT NULL CHECK (status IN ('pending', 'ready', 'failed')),
    storage_path   TEXT,
    error_message  TEXT,
    created_at     TEXT NOT NULL CHECK (created_at GLOB '????-??-??T??:??:??.???Z'),
    CHECK ((status = 'ready') = (storage_path IS NOT NULL)),
    CHECK ((status = 'failed') = (error_message IS NOT NULL))
);
CREATE INDEX idx_export_meeting_type ON Export (meeting_id, type, created_at);
