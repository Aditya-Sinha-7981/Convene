-- CON-09: every explicit question is stored with its outcome (docs/data-model.md, QAQuery).
-- The reason for no_grounding/failed lives in the qa_query audit payload, not in this row.
CREATE TABLE QAQuery (
    query_id         TEXT PRIMARY KEY CHECK (length(query_id) = 36),
    meeting_id       TEXT REFERENCES Meeting(meeting_id),
    mode             TEXT NOT NULL CHECK (mode IN ('live', 'history')),
    question         TEXT NOT NULL CHECK (length(question) > 0),
    answer           TEXT,
    cited_chunk_ids  TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(cited_chunk_ids) AND json_type(cited_chunk_ids) = 'array'),
    status           TEXT NOT NULL CHECK (status IN ('answered', 'no_grounding', 'failed')),
    created_at       TEXT NOT NULL CHECK (created_at GLOB '????-??-??T??:??:??.???Z'),
    CHECK ((status = 'answered') = (answer IS NOT NULL)),
    CHECK (mode = 'history' OR meeting_id IS NOT NULL)
);
CREATE INDEX idx_qa_query_meeting_created ON QAQuery (meeting_id, created_at);
