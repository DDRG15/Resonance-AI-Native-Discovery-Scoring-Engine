"""
tests/test_extraction.py — pytest suite for GEMA core components.

Coverage:
    1. extract_jobs_from_text   — valid mocked LLM response → ExtractionResult
    2. skip_invalid_jobs        — drops jobs with empty title, logs WARNING (F16)
    3. _score_skill_overlap     — bonus points from profile signals (Phase 7)
    4. _score_skill_overlap     — None profile returns 0, no crash
    5. _score_skill_overlap     — bonus capped at 15 pts max

No real HTTP requests or LLM API calls are made. All LLM callers are replaced
with unittest.mock objects that return pre-defined JSON strings.
"""

import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from models import ExtractionResult, JobResult
from matcher import _score_skill_overlap


# =============================================================================
# Shared fixtures
# =============================================================================

VALID_LLM_RESPONSE = json.dumps({
    "jobs": [
        {
            "title": "Backend Engineer",
            "company": "TestCorp",
            "seniority_level": "Senior",
            "employment_type": "full-time",
            "salary_min": 120000,
            "salary_max": 150000,
            "currency": "USD",
            "remote_region": "GLOBAL",
            "is_hybrid": False,
            "location_strictness": "Match",
            "location_notes": "Fully remote — no location restrictions stated.",
            "required_tech": ["Python", "FastAPI"],
            "preferred_tech": ["Docker"],
            "experience_min_years": 4,
            "experience_max_years": None,
            "source_url": "https://example.com/job/1",
            "cv_match_score": 0.80,
            "integrity_score": 1.0,
        }
    ]
})


# =============================================================================
# Test 1 — extract_jobs_from_text: valid mocked LLM response
# =============================================================================

def test_extract_jobs_from_text_parses_valid_response():
    """
    Confirms extract_jobs_from_text returns a populated ExtractionResult when
    the LLM caller returns a valid JSON string. The caller chain is replaced
    by a single MagicMock — no real Groq or Gemini call is made.
    """
    mock_caller = MagicMock(return_value=VALID_LLM_RESPONSE)

    with patch("nlp_engine._build_caller_chain", return_value=[("MockLLM", mock_caller)]):
        from nlp_engine import extract_jobs_from_text
        result = extract_jobs_from_text(
            "Backend Engineer at TestCorp — Python, FastAPI, fully remote.",
            source_url="https://example.com/job/1",
        )

    assert isinstance(result, ExtractionResult)
    assert len(result.jobs) == 1

    job = result.jobs[0]
    assert job.title == "Backend Engineer"
    assert job.company == "TestCorp"
    assert job.cv_match_score == pytest.approx(0.80)
    assert "Python" in job.required_tech
    assert job.remote_region == "GLOBAL"
    mock_caller.assert_called_once()


def test_extract_jobs_from_text_returns_empty_on_bad_json():
    """
    Confirms extract_jobs_from_text returns ExtractionResult(jobs=[]) when
    the mocked LLM returns unparseable output — never raises to the caller.
    """
    mock_caller = MagicMock(return_value="this is not json at all")

    with patch("nlp_engine._build_caller_chain", return_value=[("MockLLM", mock_caller)]):
        from nlp_engine import extract_jobs_from_text
        result = extract_jobs_from_text("some raw text")

    assert isinstance(result, ExtractionResult)
    assert result.jobs == []


# =============================================================================
# Test 2 — skip_invalid_jobs: drop + WARNING log (F16)
# =============================================================================

def test_skip_invalid_jobs_drops_empty_title_and_logs_warning(caplog):
    """
    Confirms ExtractionResult.skip_invalid_jobs drops any job with an empty
    title and emits a WARNING log containing 'dropped' (F16 requirement).
    The valid job in the same payload is preserved.
    """
    data = {
        "jobs": [
            {
                "title": "Backend Engineer",
                "company": "TestCorp",
                "location_strictness": "Match",
                "cv_match_score": 0.70,
                "integrity_score": 0.80,
            },
            {
                "title": "",           # invalid — empty title
                "company": "Ghost Corp",
                "location_strictness": "Match",
                "cv_match_score": 0.50,
                "integrity_score": 0.60,
            },
        ]
    }

    with caplog.at_level(logging.WARNING):
        result = ExtractionResult.model_validate(data)

    assert len(result.jobs) == 1
    assert result.jobs[0].title == "Backend Engineer"
    assert any("dropped" in record.getMessage() for record in caplog.records)


def test_skip_invalid_jobs_drops_null_company_and_logs_warning(caplog):
    """
    Confirms skip_invalid_jobs drops a job whose company is None and logs
    a WARNING. Validates the null-company path of the F16 fix.
    """
    data = {
        "jobs": [
            {
                "title": "SRE",
                "company": "ValidCo",
                "cv_match_score": 0.60,
                "integrity_score": 0.60,
            },
            {
                "title": "Staff Engineer",
                "company": None,       # invalid — null company
                "cv_match_score": 0.55,
                "integrity_score": 0.50,
            },
        ]
    }

    with caplog.at_level(logging.WARNING):
        result = ExtractionResult.model_validate(data)

    assert len(result.jobs) == 1
    assert result.jobs[0].company == "ValidCo"
    assert any("dropped" in record.getMessage() for record in caplog.records)


def test_skip_invalid_jobs_no_warning_when_all_valid(caplog):
    """
    Confirms no WARNING is emitted when all jobs pass validation — the
    happy path must remain silent.
    """
    data = {
        "jobs": [
            {
                "title": "Backend Engineer",
                "company": "TestCorp",
                "cv_match_score": 0.80,
                "integrity_score": 1.0,
            }
        ]
    }

    with caplog.at_level(logging.WARNING):
        result = ExtractionResult.model_validate(data)

    assert len(result.jobs) == 1
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert not warning_records


# =============================================================================
# Test 3 — _score_skill_overlap: profile skill bonus (Phase 7)
# =============================================================================

def test_score_skill_overlap_adds_bonus_points():
    """
    Confirms _score_skill_overlap returns a non-zero score when the job's
    title/company text contains strings from the profile's core_skills or
    audit_signals.
    """
    job = JobResult(
        title="Senior SRE Python FastAPI Engineer",
        company="Reliable Inc",
        salary_raw=None,
        url="https://example.com/job/sre",
        source_domain="example.com",
    )
    profile = {
        "core_skills": ["Python", "FastAPI", "Docker"],
        "audit_signals": ["SRE", "idempotency"],
    }

    score, match_reasons, miss_reasons = _score_skill_overlap(job, profile)

    assert score > 0
    assert score <= 15
    matched_signals = {r.split("'")[1] for r in match_reasons if "'" in r}
    assert matched_signals & {"Python", "SRE", "FastAPI"}


def test_score_skill_overlap_returns_zero_for_none_profile():
    """
    Confirms _score_skill_overlap returns 0 and does not raise when profile
    is None — the no-CV-uploaded fast path must be completely safe.
    """
    job = JobResult(
        title="Backend Engineer",
        company="TestCorp",
        url="https://example.com/job/2",
        source_domain="example.com",
    )

    score, match_reasons, miss_reasons = _score_skill_overlap(job, None)

    assert score == 0
    assert any("skipped" in r.lower() for r in match_reasons)
    assert miss_reasons == []


def test_score_skill_overlap_caps_at_15():
    """
    Confirms the skill overlap bonus never exceeds 15 points even when every
    profile signal matches the job text.
    """
    job = JobResult(
        title="Python FastAPI Docker SRE idempotency Engineer",
        company="TestCorp",
        url="https://example.com/job/3",
        source_domain="example.com",
    )
    profile = {
        "core_skills": ["Python", "FastAPI", "Docker"],
        "audit_signals": ["SRE", "idempotency"],
    }

    score, _, _ = _score_skill_overlap(job, profile)

    assert score == 15


def test_score_skill_overlap_returns_zero_for_empty_profile():
    """
    Confirms _score_skill_overlap returns 0 when the profile has no skills
    or signals — an uploaded-but-empty CV must not crash or produce phantom scores.
    """
    job = JobResult(
        title="Backend Engineer Python",
        company="TestCorp",
        url="https://example.com/job/4",
        source_domain="example.com",
    )
    profile = {
        "core_skills": [],
        "audit_signals": [],
    }

    score, match_reasons, miss_reasons = _score_skill_overlap(job, profile)

    assert score == 0
    assert any("skipped" in r.lower() for r in match_reasons)
