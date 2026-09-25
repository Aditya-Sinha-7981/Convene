-- CON-08: retain one mutable meeting tail; completed chunks keep stable citation IDs.
ALTER TABLE TranscriptChunk ADD COLUMN is_closed INTEGER NOT NULL DEFAULT 0 CHECK (is_closed IN (0, 1));
