-- 0001_core: Meeting, Device, Participant, Utterance, ConnectionEvent, AuditEvent.
-- Names, types, enums and indexes follow docs/data-model.md. The runner wraps this file in a
-- transaction and sets PRAGMA user_version; do not add BEGIN/COMMIT here.
--
-- Timestamps: ISO 8601 UTC with millisecond precision and a Z suffix (24 characters). The GLOB checks
-- pin that shape so lexical order is chronological order. IDs are 36-character UUID text; the
-- repositories check the UUID v4 format.
-- AuditEvent.event_type and .component are validated by the single emit path against the catalog,
-- not by a CHECK, so adding an event type never needs a table rebuild.

CREATE TABLE Meeting (
    meeting_id  TEXT PRIMARY KEY CHECK (length(meeting_id) = 36),
    title       TEXT,
    status      TEXT NOT NULL CHECK (status IN ('created', 'live', 'ended')),
    created_at  TEXT NOT NULL CHECK (created_at GLOB '????-??-??T??:??:??.???Z'),
    started_at  TEXT CHECK (started_at IS NULL OR started_at GLOB '????-??-??T??:??:??.???Z'),
    ended_at    TEXT CHECK (ended_at IS NULL OR ended_at GLOB '????-??-??T??:??:??.???Z')
);

CREATE TABLE Device (
    device_id               TEXT PRIMARY KEY CHECK (length(device_id) = 36),
    meeting_id              TEXT NOT NULL REFERENCES Meeting (meeting_id),
    joined_at               TEXT NOT NULL CHECK (joined_at GLOB '????-??-??T??:??:??.???Z'),
    status                  TEXT NOT NULL
                            CHECK (status IN ('joining', 'enrolling', 'connected', 'disconnected', 'left')),
    is_shared               INTEGER NOT NULL CHECK (is_shared IN (0, 1)),
    declared_speaker_count  INTEGER NOT NULL CHECK (declared_speaker_count >= 1),
    reconnect_count         INTEGER NOT NULL DEFAULT 0 CHECK (reconnect_count >= 0),
    user_agent              TEXT,
    CHECK (is_shared = 1 OR declared_speaker_count = 1)
);

CREATE TABLE Participant (
    participant_id     TEXT PRIMARY KEY CHECK (length(participant_id) = 36),
    meeting_id         TEXT NOT NULL REFERENCES Meeting (meeting_id),
    device_id          TEXT NOT NULL REFERENCES Device (device_id),
    display_name       TEXT NOT NULL CHECK (length(display_name) > 0),
    enrollment_status  TEXT NOT NULL
                       CHECK (enrollment_status IN ('not_required', 'pending', 'enrolled', 'failed'))
);

CREATE TABLE Utterance (
    utterance_id             TEXT PRIMARY KEY CHECK (length(utterance_id) = 36),
    meeting_id               TEXT NOT NULL REFERENCES Meeting (meeting_id),
    device_id                TEXT NOT NULL REFERENCES Device (device_id),
    participant_id           TEXT REFERENCES Participant (participant_id),
    text                     TEXT NOT NULL,
    t_start                  TEXT NOT NULL CHECK (t_start GLOB '????-??-??T??:??:??.???Z'),
    t_end                    TEXT NOT NULL CHECK (t_end GLOB '????-??-??T??:??:??.???Z'),
    stt_confidence           REAL NOT NULL CHECK (stt_confidence BETWEEN 0 AND 1),
    attribution_method       TEXT NOT NULL
                             CHECK (attribution_method IN ('device', 'enrolled', 'generic_unresolved', 'manual_correction')),
    attribution_confidence   REAL NOT NULL CHECK (attribution_confidence BETWEEN 0 AND 1),
    corrected                INTEGER NOT NULL DEFAULT 0 CHECK (corrected IN (0, 1)),
    original_participant_id  TEXT REFERENCES Participant (participant_id),
    created_at               TEXT NOT NULL CHECK (created_at GLOB '????-??-??T??:??:??.???Z'),
    CHECK (t_end >= t_start),
    -- "null only for an unresolved generic-label case"
    CHECK (participant_id IS NOT NULL OR attribution_method = 'generic_unresolved'),
    -- "set only if corrected = 1"
    CHECK (corrected = 1 OR original_participant_id IS NULL)
);
CREATE INDEX idx_utterance_meeting_t_start ON Utterance (meeting_id, t_start);
CREATE INDEX idx_utterance_meeting_participant ON Utterance (meeting_id, participant_id);

CREATE TABLE ConnectionEvent (
    event_id    TEXT PRIMARY KEY CHECK (length(event_id) = 36),
    device_id   TEXT NOT NULL REFERENCES Device (device_id),
    meeting_id  TEXT NOT NULL REFERENCES Meeting (meeting_id),
    event_type  TEXT NOT NULL CHECK (event_type IN ('connected', 'disconnected', 'reconnected', 'audio_resumed')),
    timestamp   TEXT NOT NULL CHECK (timestamp GLOB '????-??-??T??:??:??.???Z')
);

CREATE TABLE AuditEvent (
    event_id    TEXT PRIMARY KEY CHECK (length(event_id) = 36),
    seq         INTEGER NOT NULL UNIQUE CHECK (seq >= 1),
    meeting_id  TEXT REFERENCES Meeting (meeting_id),
    event_type  TEXT NOT NULL CHECK (length(event_type) > 0),
    component   TEXT NOT NULL CHECK (length(component) > 0),
    timestamp   TEXT NOT NULL CHECK (timestamp GLOB '????-??-??T??:??:??.???Z'),
    payload     TEXT NOT NULL CHECK (json_valid(payload) AND json_type(payload) = 'object')
);
CREATE INDEX idx_audit_meeting_timestamp ON AuditEvent (meeting_id, timestamp);
CREATE INDEX idx_audit_type_timestamp ON AuditEvent (event_type, timestamp);
