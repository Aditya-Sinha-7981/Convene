-- CON-16: action-item lifecycle (docs/data-model.md, ActionItem and ActionItemNote; ADR-28).
-- SQLite cannot alter a CHECK in place, so ActionItem is rebuilt (the 0003 ModelExecution pattern) to widen
-- `status` and add `due_date`. Nothing references ActionItem yet; the note table is created after the rebuild.
ALTER TABLE ActionItem RENAME TO ActionItem_old;
CREATE TABLE ActionItem (
    action_item_id        TEXT PRIMARY KEY CHECK (length(action_item_id) = 36),
    summary_id            TEXT NOT NULL REFERENCES Summary (summary_id),
    meeting_id            TEXT NOT NULL REFERENCES Meeting (meeting_id),
    text                  TEXT NOT NULL CHECK (length(text) > 0),
    owner_participant_id  TEXT REFERENCES Participant (participant_id),
    status                TEXT NOT NULL CHECK (status IN ('open', 'done', 'cancelled')),
    -- a calendar date only; date() normalizes an impossible day such as 2026-02-30, so it fails the equality
    due_date              TEXT CHECK (due_date IS NULL OR (due_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
                                                           AND date(due_date) = due_date))
);
INSERT INTO ActionItem (action_item_id, summary_id, meeting_id, text, owner_participant_id, status)
    SELECT action_item_id, summary_id, meeting_id, text, owner_participant_id, status FROM ActionItem_old ORDER BY rowid;
DROP TABLE ActionItem_old;
CREATE INDEX idx_action_item_summary ON ActionItem (summary_id);
-- The global list: open items across meetings, ordered by due date.
CREATE INDEX idx_action_item_status_due ON ActionItem (status, due_date);

-- Append-only manual updates attached to an existing item, from the meeting where they were mentioned.
CREATE TABLE ActionItemNote (
    note_id            TEXT PRIMARY KEY CHECK (length(note_id) = 36),
    action_item_id     TEXT NOT NULL REFERENCES ActionItem (action_item_id),
    source_meeting_id  TEXT NOT NULL REFERENCES Meeting (meeting_id),
    text               TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 2000),
    created_at         TEXT NOT NULL CHECK (created_at GLOB '????-??-??T??:??:??.???Z')
);
CREATE INDEX idx_action_item_note_item ON ActionItemNote (action_item_id, created_at);
CREATE INDEX idx_action_item_note_source ON ActionItemNote (source_meeting_id);
