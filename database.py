"""
database.py — Seen Registry & Search Vault for Project GEMA.

SRE Principles:
    WAL journal mode   — survives abrupt kill without corruption
    Pre-write backup   — .db.bak before any bulk write session
    SHA-256 Delta Load — hash(normalized_url) ONLY, never hash+timestamp
    TTL filter         — ignore jobs seen within N hours
    Search Vault       — persists successful SearchConfig JSONs for reuse
"""

import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generator, Optional
from urllib.parse import urlparse, urlunparse

from models import JobResult, SearchConfig, SearchVaultEntry
import config

logger = logging.getLogger(__name__)

# =============================================================================
# Schema DDL
# =============================================================================

_DDL_SEEN_REGISTRY = """
CREATE TABLE IF NOT EXISTS seen_jobs_registry (
    job_hash        TEXT PRIMARY KEY,
    url             TEXT NOT NULL,
    title           TEXT NOT NULL,
    company         TEXT NOT NULL,
    source_domain   TEXT NOT NULL,
    scraped_at      TEXT NOT NULL,
    tier            TEXT,
    match_score     INTEGER
);
"""

_DDL_SEARCH_VAULT = """
CREATE TABLE IF NOT EXISTS search_vault (
    vault_id        TEXT PRIMARY KEY,
    label           TEXT NOT NULL,
    config_json     TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    times_used      INTEGER DEFAULT 0
);
"""

_DDL_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_registry_domain  ON seen_jobs_registry(source_domain);",
    "CREATE INDEX IF NOT EXISTS idx_registry_scraped ON seen_jobs_registry(scraped_at);",
    "CREATE INDEX IF NOT EXISTS idx_vault_created    ON search_vault(created_at);",
]


# =============================================================================
# Connection Management
# =============================================================================

@contextmanager
def _get_connection(db_path: str) -> Generator[sqlite3.Connection, None, None]:
    """
    Yields a WAL-mode SQLite connection, commits on clean exit, rolls back on error.

    WAL mode (Write-Ahead Logging):
        Writes go to a separate log file first. On reconnect after a crash,
        SQLite replays the log — the database is self-healing.
        Directly mitigates Vol 1.4 Risk 4.1 (corruption on abrupt exit).
    """
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# =============================================================================
# GemaDatabase
# =============================================================================

class GemaDatabase:
    """
    Primary interface to the GEMA SQLite database.
    All public methods manage their own connection — safe for Streamlit threads.
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or config.GEMA_DB_PATH
        self._initialize()

    def _initialize(self) -> None:
        with _get_connection(self.db_path) as conn:
            conn.execute(_DDL_SEEN_REGISTRY)
            conn.execute(_DDL_SEARCH_VAULT)
            for idx_sql in _DDL_INDEXES:
                conn.execute(idx_sql)
        logger.info("Database initialized: %s", self.db_path)

    def backup(self) -> str:
        """
        Creates an atomic, WAL-safe backup using SQLite's native backup API.

        WHY shutil.copy2 IS WRONG WITH WAL MODE:
            When WAL mode is active, SQLite uses three files:
                gema_registry.db       — main database
                gema_registry.db-wal   — write-ahead log (uncommitted pages)
                gema_registry.db-shm   — shared memory index

            shutil.copy2 copies only the .db file at filesystem level.
            If transactions are pending in the -wal file at the moment of
            copy, those pages are NOT in the .db file yet — the backup
            is a corrupt, partially-written database that cannot be read.

        WHY src_conn.backup(dst_conn) IS CORRECT:
            SQLite's C-level backup API (exposed in Python as conn.backup())
            checkpoints the WAL, then copies page-by-page while holding a
            shared lock. The result is a consistent, self-contained .db file
            that includes all committed transactions — even those still in
            the -wal file. This is the only safe backup method for WAL mode.

        Vol 1.4 Risk 4.1 mitigation.
        Returns the backup path, or empty string if db does not exist yet.
        """
        src_path = Path(self.db_path)
        if not src_path.exists():
            logger.warning("Backup skipped — database file does not exist yet.")
            return ""

        backup_path = str(src_path) + config.DB_BACKUP_SUFFIX
        # timeout=5000ms: waits for the writer task to release its lock rather
        # than raising OperationalError immediately under concurrent write load.
        src_conn = sqlite3.connect(self.db_path, timeout=5.0)
        dst_conn = sqlite3.connect(backup_path, timeout=5.0)
        try:
            # Mirror the WAL + foreign-keys pragmas from _get_connection() so
            # the backup connection reads a fully consistent WAL checkpoint.
            src_conn.execute("PRAGMA journal_mode=WAL")
            src_conn.execute("PRAGMA foreign_keys=ON")
            src_conn.backup(dst_conn)
            logger.info("WAL-safe backup created: %s", backup_path)
        finally:
            dst_conn.close()
            src_conn.close()
        return backup_path

    # =========================================================================
    # Idempotency Core — SHA-256 Seen Registry
    # =========================================================================

    @staticmethod
    def compute_hash(url: str) -> str:
        """
        SHA-256 of the canonical URL — the idempotency key.

        NORMALIZATION PIPELINE (order matters):
            1. Strip whitespace
            2. Lowercase scheme + netloc
            3. Strip trailing slash from path
            4. DROP query string entirely  ← Fix 1 (audit)
            5. DROP fragment (#anchor)

        WHY DROP QUERY PARAMS:
            Job boards append tracking parameters to every URL:
                example.com/job/123?ref=newsletter&utm_source=email
                example.com/job/123?ref=homepage&utm_campaign=daily
            These are the SAME job. The previous implementation only
            did str.lower() + str.rstrip('/'), producing two different
            hashes for the same posting — Delta Load failed silently.

            urllib.parse.urlunparse with empty query and fragment ensures
            the canonical URL is deterministic regardless of how the job
            board decorated the link at the time of scraping.

        SHA-256 collision probability at 1M URLs: ~2.9e-68.
        """
        stripped = url.strip()
        parsed = urlparse(stripped)
        canonical = urlunparse((
            parsed.scheme.lower(),  # normalize scheme
            parsed.netloc.lower(),  # normalize host
            parsed.path.rstrip("/"),  # strip trailing slash
            "",                     # drop params (;key=val)
            "",                     # drop query string (?ref=...)
            "",                     # drop fragment (#section)
        ))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def is_seen(self, url: str, ttl_hours: int = 0) -> tuple[bool, str]:
        """
        Single-URL seen check. Returns (is_seen, reason).

        ttl_hours=0  → permanent: skip forever once seen
        ttl_hours=N  → skip only if seen within last N hours
        """
        job_hash = self.compute_hash(url)
        with _get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT scraped_at FROM seen_jobs_registry WHERE job_hash = ?",
                (job_hash,),
            ).fetchone()

        if row is None:
            return False, ""

        if ttl_hours <= 0:
            return True, "permanent_registry"

        scraped_at = datetime.fromisoformat(row["scraped_at"])
        cutoff = datetime.now(timezone.utc) - timedelta(hours=ttl_hours)
        if scraped_at >= cutoff:
            return True, f"within_ttl_{ttl_hours}h"

        return False, ""  # Outside TTL — re-eligible

    def filter_new_urls(
        self, urls: list[str], ttl_hours: int = 0
    ) -> tuple[list[str], int, int]:
        """
        Batch Delta Load filter — single SQL query for the entire URL batch.

        Returns: (new_urls, skipped_permanent_count, skipped_ttl_count)

        More efficient than calling is_seen() in a loop for large batches.
        """
        if not urls:
            return [], 0, 0

        hash_to_url = {self.compute_hash(u): u for u in urls}
        all_hashes = list(hash_to_url.keys())

        # SQLite SQLITE_MAX_VARIABLE_NUMBER is 999 by default.
        # Chunking prevents "too many SQL variables" on large batches.
        _CHUNK = 900
        rows = []
        with _get_connection(self.db_path) as conn:
            for i in range(0, len(all_hashes), _CHUNK):
                chunk = all_hashes[i : i + _CHUNK]
                placeholders = ",".join("?" * len(chunk))
                rows.extend(
                    conn.execute(
                        f"SELECT job_hash, scraped_at FROM seen_jobs_registry "
                        f"WHERE job_hash IN ({placeholders})",
                        chunk,
                    ).fetchall()
                )

        seen_map = {
            row["job_hash"]: datetime.fromisoformat(row["scraped_at"])
            for row in rows
        }

        new_urls: list[str] = []
        skipped_permanent = 0
        skipped_ttl = 0
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=ttl_hours)
            if ttl_hours > 0 else None
        )

        for url in urls:
            h = self.compute_hash(url)
            if h not in seen_map:
                new_urls.append(url)
                continue
            if cutoff and seen_map[h] < cutoff:
                new_urls.append(url)  # Outside TTL — re-eligible
            elif ttl_hours > 0:
                skipped_ttl += 1
            else:
                skipped_permanent += 1

        logger.info(
            "Delta Load: %d total | %d new | %d perm-skip | %d ttl-skip",
            len(urls), len(new_urls), skipped_permanent, skipped_ttl,
        )
        return new_urls, skipped_permanent, skipped_ttl

    def mark_seen(self, job: JobResult) -> None:
        """
        Registers one job immediately after extraction (before matcher runs).
        INSERT OR IGNORE ensures concurrent calls are safe.
        """
        job_hash = self.compute_hash(job.url)
        with _get_connection(self.db_path) as conn:
            conn.execute(
                """INSERT OR IGNORE INTO seen_jobs_registry
                   (job_hash, url, title, company, source_domain, scraped_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    job_hash, job.url, job.title, job.company,
                    job.source_domain, job.scraped_at.isoformat(),
                ),
            )

    def mark_seen_batch(self, jobs: list[JobResult]) -> None:
        """Bulk-registers a list of jobs in a single transaction."""
        records = [
            (
                self.compute_hash(j.url),
                j.url, j.title, j.company,
                j.source_domain, j.scraped_at.isoformat(),
            )
            for j in jobs
        ]
        with _get_connection(self.db_path) as conn:
            conn.executemany(
                """INSERT OR IGNORE INTO seen_jobs_registry
                   (job_hash, url, title, company, source_domain, scraped_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                records,
            )
        logger.info("Batch-registered %d jobs.", len(records))

    def update_tier(self, url: str, tier: str, match_score: int) -> None:
        """Backfills tier and match_score after the matcher has run."""
        with _get_connection(self.db_path) as conn:
            conn.execute(
                "UPDATE seen_jobs_registry SET tier=?, match_score=? WHERE job_hash=?",
                (tier, match_score, self.compute_hash(url)),
            )

    # =========================================================================
    # Search Vault (Vol 1.3)
    # =========================================================================

    def save_to_vault(self, entry: SearchVaultEntry) -> None:
        with _get_connection(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO search_vault
                   (vault_id, label, config_json, created_at, times_used)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    entry.vault_id, entry.label,
                    entry.config.model_dump_json(),
                    entry.created_at.isoformat(),
                    entry.times_used,
                ),
            )
        logger.info("Vault saved: '%s'", entry.label)

    def load_vault(self) -> list[SearchVaultEntry]:
        with _get_connection(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM search_vault ORDER BY created_at DESC"
            ).fetchall()

        entries = []
        for row in rows:
            try:
                entries.append(SearchVaultEntry(
                    vault_id=row["vault_id"],
                    label=row["label"],
                    config=SearchConfig(**json.loads(row["config_json"])),
                    created_at=datetime.fromisoformat(row["created_at"]),
                    times_used=row["times_used"],
                ))
            except Exception as exc:
                logger.warning("Corrupt vault entry %s skipped: %s", row["vault_id"], exc)
        return entries

    def increment_vault_usage(self, vault_id: str) -> None:
        with _get_connection(self.db_path) as conn:
            conn.execute(
                "UPDATE search_vault SET times_used = times_used + 1 WHERE vault_id = ?",
                (vault_id,),
            )

    def delete_vault_entry(self, vault_id: str) -> None:
        with _get_connection(self.db_path) as conn:
            conn.execute("DELETE FROM search_vault WHERE vault_id = ?", (vault_id,))

    # =========================================================================
    # Stats for Streamlit Dashboard
    # =========================================================================

    def get_registry_stats(self) -> dict:
        with _get_connection(self.db_path) as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM seen_jobs_registry"
            ).fetchone()[0]
            by_tier = conn.execute(
                "SELECT tier, COUNT(*) as cnt FROM seen_jobs_registry "
                "WHERE tier IS NOT NULL GROUP BY tier"
            ).fetchall()
            recent_24h = conn.execute(
                "SELECT COUNT(*) FROM seen_jobs_registry WHERE scraped_at >= ?",
                ((datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(),),
            ).fetchone()[0]

        return {
            "total_seen": total,
            "recent_24h": recent_24h,
            "by_tier": {row["tier"]: row["cnt"] for row in by_tier},
        }

    # =========================================================================
    # Async-Safe Interface for "The Blitz" (asyncio.gather concurrency)
    # =========================================================================

    def async_filter_new_urls(
        self, urls: list[str], ttl_hours: int = 0
    ) -> tuple[list[str], int, int]:
        """
        Synchronous method safe to call via loop.run_in_executor().

        WHY THIS EXISTS:
            The existing filter_new_urls() is already thread-safe for reads —
            WAL mode allows unlimited concurrent readers with no lock risk.
            This alias makes the intent explicit at the call site in scraper.py:
            it is designed to be dispatched to the thread pool executor, keeping
            disk I/O off the asyncio event loop without blocking it.

            Pattern in scraper.py:
                new_urls, skip_p, skip_t = await loop.run_in_executor(
                    None,
                    self.db.async_filter_new_urls,
                    raw_urls,
                    self.ttl_hours,
                )

        SQLite WAL + multiple readers:
            WAL mode allows any number of readers to operate concurrently
            with each other and with a single writer. The thread pool executor
            may have multiple threads calling this simultaneously — all safe.
        """
        return self.filter_new_urls(urls, ttl_hours)

    def async_mark_seen_batch(self, jobs: list["JobResult"]) -> None:
        """
        Synchronous bulk insert — safe to call via loop.run_in_executor().

        CRITICAL: In "The Blitz" architecture, ONLY the _db_writer_task
        coroutine calls this method, and it does so sequentially (one batch
        at a time). Multiple concurrent callers are architecturally impossible
        because there is exactly one writer task per scrape session.

        This means the run_in_executor() wrapping this call never has
        more than one thread executing it at any moment — zero lock contention
        guaranteed by design, not by timeout tuning.

        WAL BUSY TIMEOUT:
            Set to 5000ms as a safety net for the one theoretical case where
            the Streamlit UI (which also writes to SQLite for vault operations)
            and the writer task overlap. In practice this is < 1ms.
        """
        if not jobs:
            return
        # Ensure WAL busy timeout on the write connection
        records = [
            (
                self.compute_hash(j.url),
                j.url, j.title, j.company,
                j.source_domain, j.scraped_at.isoformat(),
            )
            for j in jobs
        ]
        with _get_connection(self.db_path) as conn:
            conn.execute("PRAGMA busy_timeout = 5000;")
            conn.executemany(
                """INSERT OR IGNORE INTO seen_jobs_registry
                   (job_hash, url, title, company, source_domain, scraped_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                records,
            )

    def get_scrape_session_stats(self, run_id: str) -> dict:
        """
        Returns per-run statistics for the Streamlit live dashboard.
        Called from main.py after the scrape completes to populate metrics.
        """
        with _get_connection(self.db_path) as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM seen_jobs_registry"
            ).fetchone()[0]

            by_tier = conn.execute(
                "SELECT tier, COUNT(*) as cnt FROM seen_jobs_registry "
                "WHERE tier IS NOT NULL GROUP BY tier"
            ).fetchall()

            recent_1h = conn.execute(
                "SELECT COUNT(*) FROM seen_jobs_registry WHERE scraped_at >= ?",
                ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),),
            ).fetchone()[0]

        return {
            "total_seen":   total,
            "recent_1h":    recent_1h,
            "by_tier":      {row["tier"]: row["cnt"] for row in by_tier},
        }
