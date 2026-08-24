-- Audit log for the "Ask the Data" page: one row per question asked.
-- The app creates this automatically on first use; kept here for reference
-- and for provisioning the table ahead of time.

CREATE TABLE IF NOT EXISTS query_log (
    id            BIGSERIAL PRIMARY KEY,
    asked_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    question      TEXT NOT NULL,
    generated_sql TEXT,
    attempts      SMALLINT,          -- 1, or 2 when the first query was retried
    status        TEXT NOT NULL,     -- 'ok' | 'error'
    detail        TEXT,              -- rejection reason or database error
    row_count     INTEGER,
    duration_ms   INTEGER
);

CREATE INDEX IF NOT EXISTS idx_query_log_asked_at ON query_log (asked_at DESC);
