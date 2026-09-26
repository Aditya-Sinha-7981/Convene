-- ADR-25: each participant's colour key from the fixed palette (server/colors.py). Null only for rows created before
-- this migration; the client gives those a stable colour from the participant id.
ALTER TABLE Participant ADD COLUMN color TEXT CHECK (color IS NULL OR length(color) BETWEEN 1 AND 20);
