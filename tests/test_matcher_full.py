"""
tests/test_matcher_full.py — Complete pytest suite for matcher.py.

Coverage:
    1. _score_title           — exact, contains, all-words, partial, no-match
    2. _score_salary          — no filter, meets min, below min, not published
    3. _score_must_include    — all present, partial, empty list
    4. _apply_exclusion_penalty — hit, clean, empty list
    5. score_job              — Tier 4 bypass, Tier 1, Tier 3, score=115 (regression fix)
    6. score_job_inline       — returns tier string
    7. bucket_jobs            — 4-tier distribution, db update_tier called

No external API calls or disk access.
"""

from unittest.mock import MagicMock

import pytest

from matcher import (
    _apply_exclusion_penalty,
    _score_must_include,
    _score_salary,
    _score_title,
    bucket_jobs,
    score_job,
    score_job_inline,
)
from models import JobResult, SearchConfig, TieredJob


# =============================================================================
# Shared fixtures
# =============================================================================

def _job(title: str, company: str = "TestCorp", salary_raw: str = None, url_id: int = 1) -> JobResult:
    return JobResult(
        title=title,
        company=company,
        salary_raw=salary_raw,
        url=f"https://example.com/job/{url_id}",
        source_domain="example.com",
    )


def _config(
    titles=None,
    min_salary=None,
    must_include=None,
    must_exclude=None,
) -> SearchConfig:
    return SearchConfig(
        target_titles=titles or ["Backend Engineer"],
        min_salary=min_salary,
        must_include=must_include or [],
        must_exclude=must_exclude or [],
        target_domains=["example.com"],
    )


FULL_PROFILE = {
    "core_skills": ["Python", "FastAPI", "Docker"],
    "audit_signals": ["SRE", "idempotency"],
}


# =============================================================================
# 1. _score_title
# =============================================================================

def test_score_title_exact_match():
    score, match, miss = _score_title("Backend Engineer", ["Backend Engineer"])
    assert score == 50
    assert any("Exact" in r for r in match)


def test_score_title_contains_match():
    score, match, miss = _score_title("Senior Backend Engineer", ["Backend Engineer"])
    assert score == 40
    assert any("contains" in r.lower() for r in match)


def test_score_title_all_words_match():
    score, match, miss = _score_title("Engineer Backend Senior", ["Senior Backend Engineer"])
    assert score == 35


def test_score_title_partial_match():
    score, match, miss = _score_title("Engineering Lead", ["Backend Engineer"])
    assert score == 15
    assert any("Partial" in r for r in match)


def test_score_title_no_match():
    score, match, miss = _score_title("Product Designer", ["Backend Engineer"])
    assert score == 0
    assert len(miss) > 0


def test_score_title_caps_at_50():
    """Even with many matching targets, max is 50."""
    score, _, _ = _score_title(
        "Backend Engineer",
        ["Backend Engineer", "Backend Engineer", "Backend Engineer"],
    )
    assert score <= 50


# =============================================================================
# 2. _score_salary
# =============================================================================

def test_score_salary_no_filter_returns_25():
    job = _job("Backend Engineer", salary_raw="$120,000")
    score, match, miss = _score_salary(job, min_salary=None)
    assert score == 25
    assert any("No salary filter" in r for r in match)


def test_score_salary_meets_minimum():
    job = _job("Backend Engineer", salary_raw="$150k")
    score, match, miss = _score_salary(job, min_salary=100_000)
    assert score == 25
    assert any("meets" in r.lower() for r in match)


def test_score_salary_below_minimum():
    job = _job("Backend Engineer", salary_raw="$60k")
    score, match, miss = _score_salary(job, min_salary=100_000)
    assert score == 0
    assert any("below" in r.lower() for r in miss)


def test_score_salary_not_published_returns_12():
    job = _job("Backend Engineer", salary_raw=None)
    score, _, miss = _score_salary(job, min_salary=100_000)
    assert score == 12
    assert any("manual review" in r.lower() for r in miss)


# =============================================================================
# 3. _score_must_include
# =============================================================================

def test_score_must_include_all_present():
    job = _job("Remote Backend Engineer Python", salary_raw="$120k Remote")
    score, match, miss = _score_must_include(job, ["Remote", "Python"])
    assert score == 15
    assert miss == []


def test_score_must_include_partial():
    job = _job("Backend Engineer Python")
    score, match, miss = _score_must_include(job, ["Python", "Remote"])
    assert 0 < score < 15
    assert any("Python" in r for r in match)
    assert any("Remote" in r for r in miss)


def test_score_must_include_empty_list():
    job = _job("Backend Engineer")
    score, match, miss = _score_must_include(job, [])
    assert score == 15
    assert any("No must-include" in r for r in match)


# =============================================================================
# 4. _apply_exclusion_penalty
# =============================================================================

def test_exclusion_penalty_hit():
    job = _job("Contract Backend Engineer")
    score, match, miss = _apply_exclusion_penalty(job, ["Contract"])
    assert score == -10
    assert any("Contract" in r for r in miss)


def test_exclusion_penalty_clean():
    job = _job("Remote Backend Engineer")
    score, match, miss = _apply_exclusion_penalty(job, ["Contract"])
    assert score == 10
    assert any("clean" in r.lower() for r in match)


def test_exclusion_penalty_empty_list():
    job = _job("Remote Backend Engineer")
    score, match, miss = _apply_exclusion_penalty(job, [])
    assert score == 0
    assert any("No exclusion" in r for r in match)


# =============================================================================
# 5. score_job
# =============================================================================

def test_score_job_tier4_bypass_on_text_salary():
    """Non-numeric salary_raw triggers Tier 4 bypass; math scoring skipped."""
    job = _job("Backend Engineer", salary_raw="Competitive + equity")
    config = _config(titles=["Backend Engineer"])
    tiered = score_job(job, config)
    assert tiered.tier == "Tier 4"
    assert tiered.match_score == -1
    assert any("text" in r.lower() or "bypassed" in r.lower() for r in tiered.match_reasons)


def test_score_job_tier1_high_score():
    """Exact title + salary met + all keywords present = Tier 1."""
    job = _job("Backend Engineer", salary_raw="$120k", url_id=10)
    config = _config(titles=["Backend Engineer"], min_salary=100_000)
    tiered = score_job(job, config)
    assert tiered.tier == "Tier 1"
    assert tiered.match_score >= 80


def test_score_job_tier3_low_score():
    """No title match + salary below minimum = Tier 3."""
    job = _job("Product Designer", salary_raw="$40k", url_id=20)
    config = _config(titles=["Backend Engineer"], min_salary=100_000)
    tiered = score_job(job, config)
    assert tiered.tier == "Tier 3"
    assert tiered.match_score < 50


def test_score_job_returns_tiered_job_instance():
    job = _job("Backend Engineer", url_id=30)
    config = _config()
    result = score_job(job, config)
    assert isinstance(result, TieredJob)
    assert result.tier in {"Tier 1", "Tier 2", "Tier 3", "Tier 4"}


def test_score_job_accepts_score_above_100_regression():
    """
    Regression test for the le=100 bug.

    Before the fix: TieredJob(match_score=115) raised pydantic.ValidationError,
    silently dropping all high-quality Tier 1 results when a CV was loaded.
    After the fix (le=115): this test must pass.

    Trigger conditions:
        - title_exact(50) + salary_ok(25) + include_full(15) + no_exclude(+10) = 100
        - skill_overlap bonus (+15) pushes to 115
    """
    job = _job(
        title="Python SRE Backend Engineer FastAPI Docker",
        salary_raw="$150k",
        url_id=99,
    )
    config = _config(
        titles=["Python SRE Backend Engineer FastAPI Docker"],
        min_salary=100_000,
        must_include=["Python"],
    )
    tiered = score_job(job, config, profile=FULL_PROFILE)

    assert tiered.match_score >= 100
    assert tiered.tier == "Tier 1"


def test_score_job_with_exclusion_found():
    """Exclusion keyword in title applies -10 penalty."""
    job = _job("Contract Backend Engineer", salary_raw="$120k", url_id=40)
    config = _config(titles=["Backend Engineer"], must_exclude=["Contract"])
    tiered = score_job(job, config)
    assert any("Contract" in r for r in tiered.miss_reasons)


# =============================================================================
# 6. score_job_inline
# =============================================================================

def test_score_job_inline_returns_tier_string():
    job = _job("Backend Engineer", salary_raw="$120k", url_id=50)
    config = _config(titles=["Backend Engineer"])
    tier = score_job_inline(job, config)
    assert tier in {"Tier 1", "Tier 2", "Tier 3", "Tier 4"}


def test_score_job_inline_tier4_on_text_salary():
    job = _job("Backend Engineer", salary_raw="DOE", url_id=51)
    config = _config()
    assert score_job_inline(job, config) == "Tier 4"


# =============================================================================
# 7. bucket_jobs
# =============================================================================

def test_bucket_jobs_distributes_into_four_tiers():
    jobs = [
        _job("Backend Engineer", salary_raw="$150k", url_id=100),        # Tier 1
        _job("Backend Engineer", salary_raw="$80k", url_id=101),          # Tier 2
        _job("Product Designer", salary_raw="$40k", url_id=102),          # Tier 3
        _job("SRE Lead", salary_raw="Competitive + equity", url_id=103),  # Tier 4
    ]
    config = _config(titles=["Backend Engineer"], min_salary=130_000)
    t1, t2, t3, t4 = bucket_jobs(jobs, config)

    assert len(t4) >= 1
    total = len(t1) + len(t2) + len(t3) + len(t4)
    assert total == len(jobs)


def test_bucket_jobs_calls_db_update_tier_per_job():
    """When a db object is provided, update_tier must be called for every job."""
    mock_db = MagicMock()
    jobs = [_job("Backend Engineer", salary_raw="$150k", url_id=200)]
    config = _config(titles=["Backend Engineer"])
    bucket_jobs(jobs, config, db=mock_db)
    mock_db.update_tier.assert_called_once()


def test_bucket_jobs_empty_input_returns_four_empty_lists():
    config = _config()
    t1, t2, t3, t4 = bucket_jobs([], config)
    assert t1 == t2 == t3 == t4 == []
