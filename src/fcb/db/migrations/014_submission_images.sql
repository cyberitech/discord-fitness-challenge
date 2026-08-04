-- One row per attached image; N rows per submission for multi-image sessions.
--
-- The perceptual hash (imagehash.phash) is used to detect duplicate
-- resubmissions. New incoming images are compared against every hash
-- in this table; if a match exists on an APPROVED submission, the new
-- image is rejected. Backfilled rows from before this migration have
-- an empty hash and will not match — historic data is untouched by
-- duplicate detection.
--
-- Note: 013_clarification_message_id existed briefly and its column
-- lives on submissions already. That migration file is gone from the
-- tree but the schema_migrations row remains harmless. This migration
-- picks up at 014 and only handles submission_images plus the index
-- on the existing clarification_message_id column.

CREATE TABLE submission_images (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id  INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    image_path     TEXT NOT NULL,
    image_hash     TEXT NOT NULL DEFAULT '',
    position       INTEGER NOT NULL,
    UNIQUE(submission_id, position)
);

CREATE INDEX idx_submission_images_hash ON submission_images(image_hash);
CREATE INDEX idx_submission_images_sub  ON submission_images(submission_id);

-- Backfill: every existing submission with an image becomes one row at
-- position 0 with an empty hash. Empty hashes never match new incoming
-- hashes, so historic data can't accidentally trigger duplicate rejects.
INSERT INTO submission_images (submission_id, image_path, image_hash, position)
SELECT id, image_path, '', 0
  FROM submissions
 WHERE image_path IS NOT NULL AND image_path != '';

-- Index for looking up pending submissions by their clarification message.
-- The column itself was added by the earlier 013 migration.
CREATE INDEX IF NOT EXISTS idx_submissions_clarification_msg
    ON submissions(clarification_message_id)
 WHERE clarification_message_id IS NOT NULL;
