"""
tests/test_scoring_pipeline.py — Integration tests for the full score_job() execution order.

score_job() has three mutually exclusive paths:
  1. Hard location block (clearance/citizenship) → Tier 3 immediately, score=0
  2. Tier 4 bypass (non-empty, non-numeric salary) → Tier 4, score=-1
  3. Mathematical path (Tier 1/2/3) → title+salary+include+excl+skill+loc_delta

The priority order is: hard block → Tier 4 bypass → math path.
A regression that swaps this order (e.g. Tier 4 bypass fires before the hard block)
silently buries clearance-required jobs in Tier 4 instead of Tier 3, where they
accumulate unreviewed in the Notion database without any location flag.

After BUG-2 fix, _score_location() is called exactly once per score_job() invocation.
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import patch, call

from matcher import score_job, _score_location
from models import JobResult, SearchConfig
import config


def _now():
    return datetime.now(timezone.utc)


def _job(title="Python Engineer", company="TestCo", n=1,
         location=None, salary=None):
    return JobResult(
        title=title, company=company,
        url=f"https://example.com/job/{n}",
        source_domain="test.board",
        scraped_at=_now(),
        location_raw=location,
        salary_raw=salary,
    )


_CFG = SearchConfig(target_titles=["Python Engineer", "Backend Engineer"])
_CFG_MIN = SearchConfig(target_titles=["Python Engineer"], min_salary=100_000)


# =============================================================================
# Execution path priority
# =============================================================================

class TestExecutionPathPriority:

    def test_hard_block_fires_before_tier4_bypass(self):
        # clearance required (hard block) + text salary (tier4 trigger)
        # Hard block must win → Tier 3, not Tier 4
        j = _job(salary="security clearance required — non-numeric", location=None, n=1)
        tiered = score_job(j, _CFG)
        assert tiered.tier == "Tier 3"
        assert tiered.match_score == 0

    def test_tier4_bypass_fires_before_math_path(self):
        # Text salary but no clearance → Tier 4
        j = _job(salary="Competitive + equity", location="Anywhere", n=2)
        tiered = score_job(j, _CFG)
        assert tiered.tier == "Tier 4"
        assert tiered.match_score == -1

    def test_math_path_fires_when_no_bypass_conditions(self):
        # No clearance, numeric salary → mathematical path → Tier 1/2/3
        j = _job(salary="$90,000 per year", n=3)
        tiered = score_job(j, _CFG)
        assert tiered.tier in ("Tier 1", "Tier 2", "Tier 3")
        assert tiered.match_score >= 0

    def test_no_salary_goes_through_math_path(self):
        # None salary → math path (not Tier 4 bypass)
        j = _job(salary=None, n=4)
        tiered = score_job(j, _CFG)
        assert tiered.tier in ("Tier 1", "Tier 2", "Tier 3")


# =============================================================================
# _score_location called exactly once (BUG-2 regression guard)
# =============================================================================

class TestScoreLocationCalledOnce:

    def test_score_location_called_once_in_math_path(self):
        j = _job(location="Anywhere", n=10)
        with patch("matcher._score_location", wraps=_score_location) as mock_loc:
            score_job(j, _CFG)
        assert mock_loc.call_count == 1

    def test_score_location_called_once_on_hard_block(self):
        j = _job(salary="security clearance required", n=11)
        with patch("matcher._score_location", wraps=_score_location) as mock_loc:
            score_job(j, _CFG)
        assert mock_loc.call_count == 1

    def test_score_location_called_once_on_tier4(self):
        j = _job(salary="Competitive", n=12)
        with patch("matcher._score_location", wraps=_score_location) as mock_loc:
            score_job(j, _CFG)
        assert mock_loc.call_count == 1


# =============================================================================
# Location delta applied in mathematical path
# =============================================================================

class TestLocationDeltaInMathPath:

    def test_location_delta_positive_raises_score(self):
        j_neutral  = _job(location=None, n=20)
        j_open     = _job(location="Anywhere", n=21)
        t_neutral  = score_job(j_neutral, _CFG)
        t_open     = score_job(j_open, _CFG)
        assert t_open.match_score > t_neutral.match_score

    def test_location_delta_negative_reduces_score(self):
        j_neutral     = _job(location=None, n=22)
        j_restricted  = _job(location="us only", n=23)
        t_neutral     = score_job(j_neutral, _CFG)
        t_restricted  = score_job(j_restricted, _CFG)
        assert t_restricted.match_score < t_neutral.match_score

    def test_location_neutral_no_score_change(self):
        j_no_loc = _job(location=None, n=24)
        j_usa    = _job(location="USA", n=25)
        t_no_loc = score_job(j_no_loc, _CFG)
        t_usa    = score_job(j_usa, _CFG)
        # Plain "USA" without explicit restriction = neutral (0 delta)
        assert t_no_loc.match_score == t_usa.match_score


# =============================================================================
# Score bounds
# =============================================================================

class TestScoreBounds:

    def test_score_capped_at_115(self):
        # Perfect title match (50) + no salary filter (25) + must_include (15)
        # + excl bonus (10) + skill bonus (15) + location bonus (10) = 125 → cap 115
        profile = {"core_skills": ["python", "fastapi", "django", "postgres", "redis",
                                   "docker", "kubernetes", "aws", "api", "backend",
                                   "engineer", "sql", "nosql", "linux", "git"],
                   "audit_signals": []}
        j = _job(title="Python Engineer", location="Anywhere", n=30)
        tiered = score_job(j, _CFG, profile=profile)
        assert tiered.match_score <= 115

    def test_score_floor_at_zero(self):
        # Extreme penalty scenario: no title match + no salary + restricted location
        j = _job(title="Unrelated Job Title XYZ", location="us only", n=31)
        tiered = score_job(j, _CFG)
        assert tiered.match_score >= 0


# =============================================================================
# Tier assignment boundary conditions
# =============================================================================

class TestTierBoundaries:

    def test_tier1_threshold_is_config_tier1_min_score(self):
        assert config.TIER1_MIN_SCORE == 80

    def test_tier2_threshold_is_config_tier2_min_score(self):
        assert config.TIER2_MIN_SCORE == 50

    def test_score_at_tier1_min_is_tier1(self):
        # Verify _assign_tier boundary directly — score=80 → Tier 1 (80 >= TIER1_MIN_SCORE)
        from matcher import _assign_tier
        assert _assign_tier(80) == "Tier 1"
        assert _assign_tier(79) == "Tier 2"

    def test_score_below_tier2_min_is_tier3(self):
        # Verify _assign_tier boundary — score=50 → Tier 2, score=49 → Tier 3
        from matcher import _assign_tier
        assert _assign_tier(50) == "Tier 2"
        assert _assign_tier(49) == "Tier 3"

    def test_perfect_title_match_no_location_scores_90(self):
        # Exact title match=50, no salary filter=25, must_include empty=15,
        # excl empty=0, no skill=0, location neutral=0 → raw=90 → Tier 1
        j = _job(title="Python Engineer", location=None, n=42)
        tiered = score_job(j, _CFG)
        assert tiered.match_score == 90
        assert tiered.tier == "Tier 1"


# =============================================================================
# Reason population
# =============================================================================

class TestReasonPopulation:

    def test_match_reasons_include_location_info_for_open(self):
        j = _job(location="Anywhere", n=50)
        tiered = score_job(j, _CFG)
        assert any("location" in r.lower() or "open" in r.lower() or "anywhere" in r.lower()
                   for r in tiered.match_reasons)

    def test_miss_reasons_include_location_info_for_restricted(self):
        j = _job(location="us only", n=51)
        tiered = score_job(j, _CFG)
        assert any("location" in r.lower() or "restricted" in r.lower()
                   for r in tiered.miss_reasons)

    def test_hard_block_miss_reasons_mention_block(self):
        j = _job(salary="Top Secret clearance required", n=52)
        tiered = score_job(j, _CFG)
        assert tiered.tier == "Tier 3"
        assert any("block" in r.lower() or "clearance" in r.lower() or "hard" in r.lower()
                   for r in tiered.miss_reasons)
