"""
tests/test_circuit_breaker.py — Unit tests for the CircuitBreaker class in scraper.py.

The CircuitBreaker is the domain-isolation mechanism that kills scraping for a board
when consecutive null extractions or a high null-rate indicate selector drift or bot
blocking. Zero regressions in threshold logic are acceptable — a silent failure here
means an entire board stops producing jobs with no alert.

All tests use the real CircuitBreaker class. No mocks.
"""

import pytest
import config
from scraper import CircuitBreaker, CircuitState


DOMAIN = "test.board"
DOMAIN_B = "other.board"


@pytest.fixture
def cb():
    return CircuitBreaker()


# =============================================================================
# Initial state
# =============================================================================

class TestInitialState:

    def test_initial_state_is_closed(self, cb):
        assert not cb.is_open(DOMAIN)

    def test_is_open_returns_false_on_fresh_domain(self, cb):
        assert cb.is_open(DOMAIN) is False

    def test_get_open_domains_empty_initially(self, cb):
        cb.is_open(DOMAIN)  # ensure domain is registered
        assert DOMAIN not in cb.get_open_domains()

    def test_domain_entry_created_lazily_by_is_open(self, cb):
        assert DOMAIN not in cb._domains
        cb.is_open(DOMAIN)
        assert DOMAIN in cb._domains


# =============================================================================
# record_null → threshold trip
# =============================================================================

class TestRecordNull:

    def test_record_null_increments_null_count(self, cb):
        cb.record_null(DOMAIN, threshold=5)
        assert cb._domains[DOMAIN]["null_count"] == 1

    def test_record_null_increments_total(self, cb):
        cb.record_null(DOMAIN, threshold=5)
        assert cb._domains[DOMAIN]["total"] == 1

    def test_circuit_opens_exactly_at_threshold(self, cb):
        threshold = 3
        for _ in range(threshold):
            cb.record_null(DOMAIN, threshold=threshold)
        assert cb.is_open(DOMAIN)

    def test_circuit_does_not_open_one_below_threshold(self, cb):
        threshold = 5
        for _ in range(threshold - 1):
            cb.record_null(DOMAIN, threshold=threshold)
        assert not cb.is_open(DOMAIN)

    def test_multiple_domains_are_isolated(self, cb):
        threshold = 2
        for _ in range(threshold):
            cb.record_null(DOMAIN, threshold=threshold)
        assert cb.is_open(DOMAIN)
        assert not cb.is_open(DOMAIN_B)

    def test_record_null_logs_actual_null_count_not_threshold(self, cb, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="scraper"):
            for _ in range(3):
                cb.record_null(DOMAIN, threshold=3)
        # Warning must mention the actual null_count (3), not the threshold only
        assert "3" in caplog.text
        assert DOMAIN in caplog.text


# =============================================================================
# record_success — null_count reset
# =============================================================================

class TestRecordSuccess:

    def test_record_success_resets_null_count(self, cb):
        cb.record_null(DOMAIN, threshold=5)
        cb.record_null(DOMAIN, threshold=5)
        cb.record_success(DOMAIN)
        assert cb._domains[DOMAIN]["null_count"] == 0

    def test_record_success_increments_total(self, cb):
        cb.record_success(DOMAIN)
        assert cb._domains[DOMAIN]["total"] == 1

    def test_record_success_after_nulls_does_not_reopen_closed_circuit(self, cb):
        cb.record_null(DOMAIN, threshold=5)
        cb.record_success(DOMAIN)
        assert not cb.is_open(DOMAIN)


# =============================================================================
# open_circuit — forced open
# =============================================================================

class TestOpenCircuit:

    def test_open_circuit_forces_open_immediately(self, cb):
        cb.open_circuit(DOMAIN)
        assert cb.is_open(DOMAIN)

    def test_open_circuit_works_without_prior_nulls(self, cb):
        # Domain has zero history — force-open must still work
        cb.open_circuit(DOMAIN)
        assert cb.is_open(DOMAIN)

    def test_open_circuit_logs_forced_open(self, cb, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="scraper"):
            cb.open_circuit(DOMAIN)
        assert "FORCED OPEN" in caplog.text
        assert DOMAIN in caplog.text


# =============================================================================
# _ensure — threshold priority
# =============================================================================

class TestEnsureThreshold:

    def test_ensure_threshold_updates_upward(self, cb):
        # First call initializes with config default (3)
        cb._ensure(DOMAIN, threshold=config.CIRCUIT_BREAKER_THRESHOLD)
        low = cb._domains[DOMAIN]["threshold"]
        # Second call with higher threshold must win
        cb._ensure(DOMAIN, threshold=low + 10)
        assert cb._domains[DOMAIN]["threshold"] == low + 10

    def test_ensure_threshold_does_not_decrease(self, cb):
        cb._ensure(DOMAIN, threshold=10)
        cb._ensure(DOMAIN, threshold=2)
        assert cb._domains[DOMAIN]["threshold"] == 10


# =============================================================================
# check_null_rate
# =============================================================================

class TestCheckNullRate:

    def test_check_null_rate_requires_minimum_5_samples(self, cb):
        # 4 total → no trip regardless of rate
        for _ in range(4):
            cb.record_null(DOMAIN, threshold=100)
        tripped = cb.check_null_rate(DOMAIN, alert_threshold=0.10)
        assert tripped is False
        assert not cb.is_open(DOMAIN)

    def test_check_null_rate_trips_above_threshold(self, cb):
        # 5 nulls out of 5 total = 100% > 0.40 threshold
        for _ in range(5):
            cb.record_null(DOMAIN, threshold=100)  # high threshold so only rate trips it
        tripped = cb.check_null_rate(DOMAIN, alert_threshold=0.40)
        assert tripped is True
        assert cb.is_open(DOMAIN)

    def test_check_null_rate_does_not_trip_below_threshold(self, cb):
        # 1 null out of 5 total = 20% < 40% threshold
        cb.record_null(DOMAIN, threshold=100)
        for _ in range(4):
            cb.record_success(DOMAIN)
        tripped = cb.check_null_rate(DOMAIN, alert_threshold=0.40)
        assert tripped is False
        assert not cb.is_open(DOMAIN)


# =============================================================================
# get_open_domains
# =============================================================================

class TestGetOpenDomains:

    def test_get_open_domains_returns_only_open(self, cb):
        cb.open_circuit(DOMAIN)
        cb.is_open(DOMAIN_B)  # register but keep closed
        open_list = cb.get_open_domains()
        assert DOMAIN in open_list
        assert DOMAIN_B not in open_list

    def test_get_open_domains_empty_when_all_closed(self, cb):
        cb.is_open(DOMAIN)
        cb.is_open(DOMAIN_B)
        assert cb.get_open_domains() == []
