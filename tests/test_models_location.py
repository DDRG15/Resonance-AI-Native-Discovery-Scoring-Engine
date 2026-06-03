"""
tests/test_models_location.py — Validation tests for JobResult.location_raw field.

location_raw is Optional[str] added in the location-filter feature session.
It feeds directly into _score_location() in matcher.py. Edge cases here
(None, whitespace, empty string) determine whether the location penalty fires.
"""

import pytest
from datetime import datetime, timezone

from database import GemaDatabase
from models import JobResult


def _now():
    return datetime.now(timezone.utc)


def _job(**kwargs):
    defaults = dict(
        title="Python Engineer",
        company="TestCo",
        url="https://example.com/job/1",
        source_domain="workingnomads.com",
        scraped_at=_now(),
    )
    defaults.update(kwargs)
    return JobResult(**defaults)


# =============================================================================
# Field existence and defaults
# =============================================================================

class TestLocationRawField:

    def test_location_raw_defaults_to_none(self):
        j = _job()
        assert j.location_raw is None

    def test_location_raw_accepts_string(self):
        j = _job(location_raw="Anywhere")
        assert j.location_raw == "Anywhere"

    def test_location_raw_accepts_none_explicitly(self):
        j = _job(location_raw=None)
        assert j.location_raw is None

    def test_location_raw_accepts_empty_string(self):
        j = _job(location_raw="")
        assert j.location_raw == ""

    def test_location_raw_accepts_whitespace_string(self):
        # Whitespace is stored as-is — scorer handles stripping
        j = _job(location_raw="   ")
        assert j.location_raw == "   "

    def test_location_raw_accepts_multiword(self):
        j = _job(location_raw="Poland, Serbia, Cyprus")
        assert j.location_raw == "Poland, Serbia, Cyprus"

    def test_location_raw_in_model_dump(self):
        j = _job(location_raw="Brazil")
        d = j.model_dump()
        assert "location_raw" in d
        assert d["location_raw"] == "Brazil"

    def test_location_raw_none_in_model_dump(self):
        j = _job()
        d = j.model_dump()
        assert "location_raw" in d
        assert d["location_raw"] is None


# =============================================================================
# URL normalization does not interfere with location_raw
# =============================================================================

class TestLocationRawWithUrlNormalization:

    def test_url_normalization_preserves_location_raw(self):
        j = _job(url="https://example.com/job/1/", location_raw="Anywhere")
        assert j.url == "https://example.com/job/1"   # trailing slash stripped
        assert j.location_raw == "Anywhere"            # untouched


# =============================================================================
# Hash independence
# =============================================================================

class TestHashIndependence:

    def test_same_url_same_hash_regardless_of_location(self):
        url = "https://example.com/job/99"
        j1 = _job(url=url, location_raw="USA")
        j2 = _job(url=url, location_raw="Anywhere")
        assert GemaDatabase.compute_hash(j1.url) == GemaDatabase.compute_hash(j2.url)

    def test_different_url_different_hash(self):
        j1 = _job(url="https://example.com/job/1", location_raw="USA")
        j2 = _job(url="https://example.com/job/2", location_raw="USA")
        assert GemaDatabase.compute_hash(j1.url) != GemaDatabase.compute_hash(j2.url)
