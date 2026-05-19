"""
tests/test_database.py — pytest suite for GemaDatabase.

Coverage:
    1.  compute_hash — URL normalization (query params, fragment, trailing slash,
                       case-insensitive scheme+host)
    2.  is_seen      — False for new URL, True after mark_seen, TTL expiry re-eligibility
    3.  filter_new_urls — batch delta load, empty input edge case
    4.  mark_seen    — idempotency via INSERT OR IGNORE
    5.  mark_seen_batch — bulk insert
    6.  vault CRUD   — save, load, increment_usage, delete
    7.  get_registry_stats — total count and by_tier aggregation

No real disk paths are used — all tests run in pytest's tmp_path fixture.
"""

from datetime import datetime, timedelta, timezone

import pytest

from database import GemaDatabase
from models import JobResult, SearchConfig, SearchVaultEntry


# =============================================================================
# Helpers
# =============================================================================

def _make_job(url: str, title: str = "Backend Engineer", company: str = "TestCorp") -> JobResult:
    return JobResult(
        title=title,
        company=company,
        url=url,
        source_domain="example.com",
    )


def _make_search_config() -> SearchConfig:
    return SearchConfig(
        target_titles=["Backend Engineer"],
        target_domains=["himalayas.app"],
    )


@pytest.fixture
def db(tmp_path):
    """Returns a fresh GemaDatabase backed by a temp SQLite file."""
    return GemaDatabase(db_path=str(tmp_path / "test_gema.db"))


# =============================================================================
# 1. compute_hash — URL normalization
# =============================================================================

def test_compute_hash_strips_query_params():
    url_a = "https://example.com/job/123?ref=newsletter&utm_source=email"
    url_b = "https://example.com/job/123?ref=homepage"
    assert GemaDatabase.compute_hash(url_a) == GemaDatabase.compute_hash(url_b)


def test_compute_hash_strips_fragment():
    url_a = "https://example.com/job/123#details"
    url_b = "https://example.com/job/123"
    assert GemaDatabase.compute_hash(url_a) == GemaDatabase.compute_hash(url_b)


def test_compute_hash_normalizes_trailing_slash():
    url_a = "https://example.com/job/123/"
    url_b = "https://example.com/job/123"
    assert GemaDatabase.compute_hash(url_a) == GemaDatabase.compute_hash(url_b)


def test_compute_hash_normalizes_scheme_and_host_case():
    url_a = "HTTPS://EXAMPLE.COM/job/123"
    url_b = "https://example.com/job/123"
    assert GemaDatabase.compute_hash(url_a) == GemaDatabase.compute_hash(url_b)


def test_compute_hash_different_paths_produce_different_hashes():
    url_a = "https://example.com/job/123"
    url_b = "https://example.com/job/456"
    assert GemaDatabase.compute_hash(url_a) != GemaDatabase.compute_hash(url_b)


# =============================================================================
# 2. is_seen — single URL lookup
# =============================================================================

def test_is_seen_returns_false_for_new_url(db):
    seen, reason = db.is_seen("https://example.com/job/new")
    assert seen is False
    assert reason == ""


def test_is_seen_returns_true_after_mark_seen(db):
    job = _make_job("https://example.com/job/1")
    db.mark_seen(job)
    seen, reason = db.is_seen("https://example.com/job/1")
    assert seen is True
    assert reason == "permanent_registry"


def test_is_seen_ttl_within_window_returns_true(db):
    job = _make_job("https://example.com/job/ttl")
    db.mark_seen(job)
    seen, reason = db.is_seen("https://example.com/job/ttl", ttl_hours=24)
    assert seen is True
    assert "within_ttl" in reason


def test_is_seen_ttl_expired_returns_false(db, tmp_path):
    """A job seen 2 hours ago is re-eligible when TTL=1h."""
    old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    url = "https://example.com/job/old"
    job_hash = GemaDatabase.compute_hash(url)

    import sqlite3
    db_path = str(tmp_path / "test_gema.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_jobs_registry "
            "(job_hash, url, title, company, source_domain, scraped_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (job_hash, url, "Old Job", "OldCo", "example.com", old_time),
        )

    db2 = GemaDatabase(db_path=db_path)
    seen, reason = db2.is_seen(url, ttl_hours=1)
    assert seen is False
    assert reason == ""


# =============================================================================
# 3. filter_new_urls — batch Delta Load
# =============================================================================

def test_filter_new_urls_returns_all_when_none_seen(db):
    urls = ["https://example.com/job/1", "https://example.com/job/2"]
    new, skip_perm, skip_ttl = db.filter_new_urls(urls)
    assert set(new) == set(urls)
    assert skip_perm == 0
    assert skip_ttl == 0


def test_filter_new_urls_skips_already_seen(db):
    job = _make_job("https://example.com/job/seen")
    db.mark_seen(job)

    urls = ["https://example.com/job/seen", "https://example.com/job/new"]
    new, skip_perm, _ = db.filter_new_urls(urls)
    assert "https://example.com/job/new" in new
    assert "https://example.com/job/seen" not in new
    assert skip_perm == 1


def test_filter_new_urls_empty_input_never_crashes(db):
    new, skip_perm, skip_ttl = db.filter_new_urls([])
    assert new == []
    assert skip_perm == 0
    assert skip_ttl == 0


# =============================================================================
# 4. mark_seen — idempotency
# =============================================================================

def test_mark_seen_is_idempotent(db):
    job = _make_job("https://example.com/job/idem")
    db.mark_seen(job)
    db.mark_seen(job)  # second call must not raise
    seen, _ = db.is_seen("https://example.com/job/idem")
    assert seen is True


def test_mark_seen_batch_registers_all_jobs(db):
    jobs = [_make_job(f"https://example.com/job/{i}") for i in range(5)]
    db.mark_seen_batch(jobs)
    for job in jobs:
        seen, _ = db.is_seen(job.url)
        assert seen is True


# =============================================================================
# 5. Search Vault CRUD
# =============================================================================

def test_vault_save_and_load_roundtrip(db):
    entry = SearchVaultEntry(
        label="Senior SDET Remote",
        config=_make_search_config(),
    )
    db.save_to_vault(entry)
    loaded = db.load_vault()
    assert len(loaded) == 1
    assert loaded[0].label == "Senior SDET Remote"
    assert loaded[0].config.target_titles == ["Backend Engineer"]


def test_vault_increment_usage(db):
    entry = SearchVaultEntry(label="Test Search", config=_make_search_config())
    db.save_to_vault(entry)
    db.increment_vault_usage(entry.vault_id)
    db.increment_vault_usage(entry.vault_id)
    loaded = db.load_vault()
    assert loaded[0].times_used == 2


def test_vault_delete_entry(db):
    entry = SearchVaultEntry(label="Delete Me", config=_make_search_config())
    db.save_to_vault(entry)
    db.delete_vault_entry(entry.vault_id)
    loaded = db.load_vault()
    assert len(loaded) == 0


# =============================================================================
# 6. get_registry_stats
# =============================================================================

def test_get_registry_stats_counts_correctly(db):
    jobs = [_make_job(f"https://example.com/job/{i}") for i in range(3)]
    db.mark_seen_batch(jobs)
    db.update_tier(jobs[0].url, "Tier 1", 90)
    db.update_tier(jobs[1].url, "Tier 2", 60)

    stats = db.get_registry_stats()
    assert stats["total_seen"] == 3
    assert stats["by_tier"].get("Tier 1") == 1
    assert stats["by_tier"].get("Tier 2") == 1
