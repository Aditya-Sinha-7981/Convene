-- ADR-33: an optional email address a participant typed on the join page, used only when someone on the laptop sends
-- the minutes by email. Kept out of Participant so no participant view, dashboard push, audit payload or the
-- /database inspector ever carries it. At most one address per participant; erased with the meeting.
CREATE TABLE ParticipantEmail (
    participant_id TEXT PRIMARY KEY REFERENCES Participant (participant_id),
    meeting_id     TEXT NOT NULL REFERENCES Meeting (meeting_id),
    email          TEXT NOT NULL CHECK (length(email) BETWEEN 3 AND 254),
    created_at     TEXT NOT NULL CHECK (created_at GLOB '????-??-??T??:??:??.???Z'),
    last_sent_at   TEXT CHECK (last_sent_at IS NULL OR last_sent_at GLOB '????-??-??T??:??:??.???Z')
);
CREATE INDEX idx_participant_email_meeting ON ParticipantEmail (meeting_id);
