-- Old minimal schema for seen_jobs_registry (pre-migration snapshot)
PRAGMA foreign_keys = OFF;
BEGIN TRANSACTION;

CREATE TABLE seen_jobs_registry (
    job_hash TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT,
    company TEXT,
    source_domain TEXT,
    scraped_at TEXT
);

COMMIT;
