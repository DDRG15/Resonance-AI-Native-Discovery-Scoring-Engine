"""
tests/test_models.py — pytest suite for Pydantic data contracts in models.py.

Coverage:
    1. SearchConfig.strip_and_deduplicate — deduplication preserves insertion order
    2. SearchConfig.normalize_domains     — strips https://, http://, trailing slash
    3. SearchConfig.validate_no_overlap   — raises ValueError on include∩exclude conflict
    4. JobResult.parse_salary_usd         — dollar prefix, usd suffix, range, rejects noise
    5. TieredJob.validate_tier            — rejects unknown tier labels
    6. TieredJob.match_score              — accepts 115 (regression test for le=100 bug)

No external calls.
"""

import pytest
from pydantic import ValidationError

from models import JobResult, SearchConfig, TieredJob


# =============================================================================
# Helpers
# =============================================================================

def _job(salary_raw=None) -> JobResult:
    return JobResult(
        title="Backend Engineer",
        company="TestCorp",
        salary_raw=salary_raw,
        url="https://example.com/job/1",
        source_domain="example.com",
    )


def _tiered(score: int, tier: str) -> TieredJob:
    return TieredJob(
        job=_job(),
        match_score=score,
        tier=tier,
    )


# =============================================================================
# 1. SearchConfig — strip_and_deduplicate
# =============================================================================

def test_search_config_deduplicates_titles_preserves_order():
    config = SearchConfig(
        target_titles=["Backend Engineer", "backend engineer", "Backend Engineer"],
        target_domains=["himalayas.app"],
    )
    assert config.target_titles == ["Backend Engineer"]


def test_search_config_strips_whitespace_from_titles():
    config = SearchConfig(
        target_titles=["  Backend Engineer  "],
        target_domains=[],
    )
    assert config.target_titles == ["Backend Engineer"]


def test_search_config_removes_blank_entries():
    config = SearchConfig(
        target_titles=["Backend Engineer", "  "],
        target_domains=[],
    )
    assert len(config.target_titles) == 1


# =============================================================================
# 2. SearchConfig — normalize_domains
# =============================================================================

def test_search_config_strips_https_protocol():
    config = SearchConfig(
        target_titles=["Engineer"],
        target_domains=["https://himalayas.app"],
    )
    assert config.target_domains == ["himalayas.app"]


def test_search_config_strips_http_protocol():
    config = SearchConfig(
        target_titles=["Engineer"],
        target_domains=["http://trueup.io/"],
    )
    assert config.target_domains == ["trueup.io"]


def test_search_config_strips_trailing_slash_from_domain():
    config = SearchConfig(
        target_titles=["Engineer"],
        target_domains=["himalayas.app/"],
    )
    assert config.target_domains == ["himalayas.app"]


# =============================================================================
# 3. SearchConfig — validate_no_overlap
# =============================================================================

def test_search_config_raises_on_include_exclude_overlap():
    with pytest.raises(ValidationError) as exc_info:
        SearchConfig(
            target_titles=["Backend Engineer"],
            must_include=["Remote"],
            must_exclude=["remote"],  # same word, different case
            target_domains=[],
        )
    assert "must_include" in str(exc_info.value).lower() or "exclude" in str(exc_info.value).lower()


def test_search_config_no_error_when_no_overlap():
    config = SearchConfig(
        target_titles=["Backend Engineer"],
        must_include=["Remote"],
        must_exclude=["Contract"],
        target_domains=[],
    )
    assert "Remote" in config.must_include
    assert "Contract" in config.must_exclude


# =============================================================================
# 4. JobResult.parse_salary_usd
# =============================================================================

def test_parse_salary_dollar_shorthand():
    assert _job("$120k").parse_salary_usd() == 120_000


def test_parse_salary_dollar_full():
    assert _job("$90,000").parse_salary_usd() == 90_000


def test_parse_salary_range_returns_minimum():
    assert _job("$90k - $150k").parse_salary_usd() == 90_000


def test_parse_salary_usd_suffix():
    assert _job("120k USD").parse_salary_usd() == 120_000


def test_parse_salary_rejects_doe():
    assert _job("DOE").parse_salary_usd() is None


def test_parse_salary_rejects_competitive():
    assert _job("Competitive").parse_salary_usd() is None


def test_parse_salary_rejects_401k():
    """401k must not be mistaken for a $401,000 salary."""
    assert _job("401k match included").parse_salary_usd() is None


def test_parse_salary_none_salary_raw_returns_none():
    assert _job(None).parse_salary_usd() is None


def test_parse_salary_empty_string_returns_none():
    assert _job("").parse_salary_usd() is None


# =============================================================================
# 5. TieredJob.validate_tier
# =============================================================================

def test_tiered_job_rejects_invalid_tier():
    with pytest.raises(ValidationError):
        TieredJob(
            job=_job(),
            match_score=80,
            tier="Tier 5",
        )


def test_tiered_job_accepts_all_valid_tiers():
    for tier, score in [("Tier 1", 90), ("Tier 2", 60), ("Tier 3", 30), ("Tier 4", -1)]:
        t = TieredJob(job=_job(), match_score=score, tier=tier)
        assert t.tier == tier


# =============================================================================
# 6. TieredJob.match_score — regression test for le=100 bug
# =============================================================================

def test_tiered_job_accepts_match_score_115():
    """
    Before the le=100 fix: ValidationError.
    After the fix (le=115): this must pass without error.
    """
    tiered = TieredJob(
        job=_job(),
        match_score=115,
        tier="Tier 1",
    )
    assert tiered.match_score == 115


def test_tiered_job_rejects_score_above_115():
    with pytest.raises(ValidationError):
        TieredJob(job=_job(), match_score=116, tier="Tier 1")


def test_tiered_job_accepts_sentinel_minus_one():
    tiered = TieredJob(job=_job(), match_score=-1, tier="Tier 4")
    assert tiered.match_score == -1


def test_tiered_job_rejects_score_below_minus_one():
    with pytest.raises(ValidationError):
        TieredJob(job=_job(), match_score=-2, tier="Tier 4")
