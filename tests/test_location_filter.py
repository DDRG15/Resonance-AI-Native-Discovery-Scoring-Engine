"""
tests/test_location_filter.py — Unit tests for the location scoring component.

Tests _score_location() in isolation and its integration with score_job().
Verifies the three-tier location logic:
  - Hard block  (-50): security clearance / citizenship required → Tier 3
  - Open        (+10): worldwide / anywhere / all time zones → score bonus
  - Restricted  (-20): explicit region lock (us only, eu only) → score penalty
  - Neutral     (  0): no signal found → no impact
"""

import pytest
from datetime import datetime, timezone

from matcher import _score_location, score_job
from models import JobResult, SearchConfig


# =============================================================================
# Fixtures
# =============================================================================

def _job(title="Python Engineer", company="Acme", location=None, salary=None, url_suffix="1"):
    return JobResult(
        title=title,
        company=company,
        url=f"https://example.com/job/{url_suffix}",
        source_domain="workingnomads.com",
        scraped_at=datetime.now(timezone.utc),
        location_raw=location,
        salary_raw=salary,
    )


_CFG = SearchConfig(target_titles=["Python Engineer", "Backend Engineer"])


# =============================================================================
# _score_location() unit tests
# =============================================================================

class TestScoreLocation:

    def test_neutral_no_signal(self):
        score, match, miss = _score_location(_job(location=None))
        assert score == 0
        assert match == []
        assert miss == []

    def test_neutral_plain_country(self):
        # "USA" alone is NOT a restriction — don't penalise
        score, _, _ = _score_location(_job(location="USA"))
        assert score == 0

    def test_open_anywhere(self):
        score, match, miss = _score_location(_job(location="Anywhere"))
        assert score == 10
        assert any("anywhere" in r.lower() for r in match)
        assert miss == []

    def test_open_worldwide(self):
        score, match, _ = _score_location(_job(location="worldwide"))
        assert score == 10
        assert any("worldwide" in r.lower() for r in match)

    def test_open_all_time_zones(self):
        score, match, _ = _score_location(_job(location="all time zones"))
        assert score == 10

    def test_open_home_based_worldwide(self):
        score, match, _ = _score_location(_job(location="Home based - Worldwide"))
        assert score == 10

    def test_restricted_us_only(self):
        score, _, miss = _score_location(_job(location="us only"))
        assert score == -20
        assert any("restricted" in r.lower() for r in miss)

    def test_restricted_must_be_authorized(self):
        score, _, miss = _score_location(_job(salary="must be authorized to work in the us"))
        assert score == -20

    def test_restricted_eu_only(self):
        score, _, miss = _score_location(_job(location="EU only"))
        assert score == -20

    def test_hard_block_security_clearance(self):
        score, _, miss = _score_location(_job(salary="requires security clearance"))
        assert score == -50
        assert any("block" in r.lower() or "clearance" in r.lower() for r in miss)

    def test_hard_block_top_secret(self):
        score, _, _ = _score_location(_job(salary="TS/SCI clearance required"))
        assert score == -50

    def test_hard_block_us_citizenship(self):
        score, _, _ = _score_location(_job(salary="US citizenship required"))
        assert score == -50

    def test_hard_block_dod_clearance(self):
        score, _, _ = _score_location(_job(salary="DoD clearance required"))
        assert score == -50

    def test_hard_block_in_title(self):
        score, _, _ = _score_location(_job(title="Python Engineer - Top Secret clearance"))
        assert score == -50

    def test_hard_block_in_company_name(self):
        # Unlikely but ensure company field is checked
        score, _, _ = _score_location(_job(company="Federal Contractor Corp"))
        assert score == -50

    def test_case_insensitive(self):
        score, _, _ = _score_location(_job(location="ANYWHERE"))
        assert score == 10

    def test_open_beats_neutral_empty_location(self):
        score_none, _, _ = _score_location(_job(location=None))
        score_open, _, _ = _score_location(_job(location="Anywhere"))
        assert score_open > score_none


# =============================================================================
# Integration with score_job()
# =============================================================================

class TestLocationInScoreJob:

    def test_canonical_anywhere_gets_bonus(self):
        j = _job(company="Canonical", location="Anywhere", url_suffix="canonical")
        tiered = score_job(j, _CFG)
        assert tiered.tier == "Tier 1"
        assert tiered.match_score >= 90
        assert any("open" in r.lower() or "anywhere" in r.lower()
                   for r in tiered.match_reasons)

    def test_clearance_forces_tier3_bypassing_tier4(self):
        # Even if salary_raw is non-numeric text (which would normally trigger Tier 4),
        # a hard location block must route to Tier 3, not Tier 4.
        j = _job(salary="security clearance required — non-numeric text", url_suffix="gov")
        tiered = score_job(j, _CFG)
        assert tiered.tier == "Tier 3"
        assert tiered.match_score == 0

    def test_us_only_explicit_downgrades_score(self):
        # location_raw="us only" triggers -20 penalty in the mathematical path.
        # Base (no salary/filter): 50+25+15+10 = 100. With -20: 80 → still Tier 1 threshold,
        # but score is lower than without restriction.
        j_open    = _job(location="Anywhere", url_suffix="open")
        j_usonly  = _job(location="us only", url_suffix="usonly")
        t_open   = score_job(j_open, _CFG)
        t_usonly = score_job(j_usonly, _CFG)
        # Restricted job scores lower than open-anywhere job
        assert t_usonly.match_score < t_open.match_score
        assert any("restricted" in r.lower() for r in t_usonly.miss_reasons)

    def test_neutral_usa_does_not_penalise(self):
        j_no_loc = _job(location=None, url_suffix="noloc")
        j_usa    = _job(location="USA", url_suffix="usa")
        t_no_loc = score_job(j_no_loc, _CFG)
        t_usa    = score_job(j_usa, _CFG)
        assert t_no_loc.match_score == t_usa.match_score

    def test_match_reasons_contain_location_info(self):
        j = _job(location="Anywhere", url_suffix="anywherejob")
        tiered = score_job(j, _CFG)
        assert any("location" in r.lower() for r in tiered.match_reasons)

    def test_miss_reasons_contain_location_info_on_restriction(self):
        # Use location_raw (not salary_raw) so the -20 fires in mathematical path
        j = _job(location="us only", url_suffix="usonly2")
        tiered = score_job(j, _CFG)
        assert any("location" in r.lower() or "restricted" in r.lower()
                   for r in tiered.miss_reasons)
