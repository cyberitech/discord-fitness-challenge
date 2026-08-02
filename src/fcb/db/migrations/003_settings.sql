-- Key/value settings for user-tunable dashboard config.
--
-- Current keys (all optional; defaults live in code):
--   voice.base_persona
--   voice.acknowledge_instruction
--   voice.describe_instruction
--   voice.nag_instruction
--
-- New keys are added by admins over time; the schema is deliberately
-- flat so no migration is needed to introduce more.

CREATE TABLE settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_by  TEXT
);
