-- CON-10: summaries and action items (docs/data-model.md, Summary and ActionItem; ADR-18 failure state).
-- A row is inserted as `pending` when an attempt starts and becomes `ready` or `failed` exactly once.
-- Attempt order is insertion order (rowid): at most one attempt per meeting runs at a time.
CREATE TABLE Summary (
    summary_id        TEXT PRIMARY KEY CHECK (length(summary_id) = 36),
    meeting_id        TEXT NOT NULL REFERENCES Meeting (meeting_id),
    status            TEXT NOT NULL CHECK (status IN ('pending', 'ready', 'failed')),
    summary_text      TEXT,
    error_message     TEXT,
    model_identifier  TEXT NOT NULL CHECK (length(model_identifier) > 0),
    generated_at      TEXT CHECK (generated_at IS NULL OR generated_at GLOB '????-??-??T??:??:??.???Z'),
    -- malformed model output can never be stored as a result: text exists only on a ready row
    CHECK ((status = 'ready') = (summary_text IS NOT NULL AND length(summary_text) > 0)),
    CHECK ((status = 'failed') = (error_message IS NOT NULL)),
    CHECK ((status = 'pending') = (generated_at IS NULL))
);
CREATE INDEX idx_summary_meeting ON Summary (meeting_id);

CREATE TABLE ActionItem (
    action_item_id        TEXT PRIMARY KEY CHECK (length(action_item_id) = 36),
    summary_id            TEXT NOT NULL REFERENCES Summary (summary_id),
    meeting_id            TEXT NOT NULL REFERENCES Meeting (meeting_id),
    text                  TEXT NOT NULL CHECK (length(text) > 0),
    owner_participant_id  TEXT REFERENCES Participant (participant_id),
    status                TEXT NOT NULL CHECK (status IN ('open', 'done'))
);
CREATE INDEX idx_action_item_summary ON ActionItem (summary_id);
