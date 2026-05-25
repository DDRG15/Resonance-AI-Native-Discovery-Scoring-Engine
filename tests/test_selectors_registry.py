"""
tests/test_selectors_registry.py — Structural validation for all registered boards.

These tests assert invariants that the scraper depends on at runtime.
A board entry that violates any of these will cause silent failures or
AttributeErrors deep in the scrape loop — better to catch them here.
"""

import re
from datetime import date

import pytest

from selectors_registry import (
    SELECTORS,
    DomainSelectors,
    get_selectors,
    list_supported_domains,
    is_selector_stale,
)


# =============================================================================
# Registry completeness
# =============================================================================

def test_registry_is_not_empty():
    assert len(SELECTORS) > 0


def test_known_domains_present():
    """Boards that were present before Phase 15 must still be registered."""
    required = {
        "himalayas.app",
        "trueup.io",
        "remote.co",
        "weworkremotely.com",
        "remoteok.com",
        "workingnomads.com",
        "news.ycombinator.com",
        "wellfound.com",
        "arc.dev",
        "builtin.com",
        "welcometothejungle.com",
        "remotivated.com",
        "posthog.com",
        "jobspresso.co",
        "greenhouse.com",
        "python.org",
        "startup.jobs",
    }
    assert required.issubset(set(SELECTORS.keys()))


def test_total_domain_count():
    """17 boards registered after Phase 15. Update this when adding new boards."""
    assert len(SELECTORS) == 17


# =============================================================================
# Per-board structural invariants (parametrized)
# =============================================================================

@pytest.fixture(params=list(SELECTORS.keys()))
def board(request) -> DomainSelectors:
    return SELECTORS[request.param]


def test_domain_field_matches_registry_key(board):
    key = board.domain
    assert key in SELECTORS, f"domain field '{key}' not found as registry key"
    assert SELECTORS[key] is board


def test_search_url_template_contains_title_placeholder(board):
    assert "{title}" in board.search_url_template, (
        f"{board.domain}: search_url_template must contain {{title}} placeholder"
    )


def test_search_url_template_is_https(board):
    assert board.search_url_template.startswith("https://"), (
        f"{board.domain}: search_url_template must use HTTPS"
    )


def test_wait_for_selector_is_non_empty(board):
    assert board.wait_for_selector and board.wait_for_selector.strip(), (
        f"{board.domain}: wait_for_selector is empty — scraper will never proceed"
    )


def test_job_card_has_at_least_one_selector(board):
    assert len(board.job_card) >= 1, (
        f"{board.domain}: job_card fallback chain must have at least one entry"
    )


def test_link_has_at_least_one_selector(board):
    assert len(board.link) >= 1, (
        f"{board.domain}: link fallback chain must have at least one entry"
    )


def test_title_has_at_least_one_selector(board):
    assert len(board.title) >= 1, (
        f"{board.domain}: title fallback chain must have at least one entry"
    )


def test_company_has_at_least_one_selector(board):
    assert len(board.company) >= 1, (
        f"{board.domain}: company fallback chain must have at least one entry"
    )


def test_null_threshold_is_positive(board):
    assert board.null_threshold > 0, (
        f"{board.domain}: null_threshold must be > 0"
    )


def test_null_rate_threshold_is_valid_fraction(board):
    assert 0.0 < board.null_rate_alert_threshold <= 1.0, (
        f"{board.domain}: null_rate_alert_threshold must be in (0.0, 1.0]"
    )


def test_last_verified_is_valid_iso_date(board):
    try:
        date.fromisoformat(board.last_verified)
    except ValueError:
        pytest.fail(
            f"{board.domain}: last_verified '{board.last_verified}' is not a valid ISO date"
        )


def test_selector_lists_have_no_empty_strings(board):
    for field_name, selectors in [
        ("job_card", board.job_card),
        ("link", board.link),
        ("title", board.title),
        ("company", board.company),
        ("salary", board.salary),
    ]:
        for sel in selectors:
            assert sel and sel.strip(), (
                f"{board.domain}.{field_name}: contains an empty or whitespace-only selector"
            )


# =============================================================================
# Lookup API
# =============================================================================

def test_get_selectors_exact_match():
    result = get_selectors("himalayas.app")
    assert result is not None
    assert result.domain == "himalayas.app"


def test_get_selectors_with_path_suffix():
    """Partial-match: 'himalayas.app/jobs?q=python' must still resolve."""
    result = get_selectors("himalayas.app/jobs?q=python")
    assert result is not None
    assert result.domain == "himalayas.app"


def test_get_selectors_with_www_prefix():
    result = get_selectors("www.remote.co")
    assert result is not None
    assert result.domain == "remote.co"


def test_get_selectors_startup_jobs():
    result = get_selectors("startup.jobs")
    assert result is not None
    assert result.domain == "startup.jobs"
    assert "{title}" in result.search_url_template


def test_get_selectors_unknown_domain_returns_none():
    result = get_selectors("unknown-board-xyz.io")
    assert result is None


def test_get_selectors_empty_string_returns_none():
    result = get_selectors("")
    assert result is None


def test_list_supported_domains_returns_all_keys():
    domains = list_supported_domains()
    assert set(domains) == set(SELECTORS.keys())
    assert len(domains) == len(SELECTORS)


# =============================================================================
# Staleness check
# =============================================================================

def test_is_selector_stale_old_date():
    old = DomainSelectors(
        domain="fake.com",
        search_url_template="https://fake.com/?q={title}",
        wait_for_selector=".job",
        job_card=[".job"],
        link=["a"],
        title=["h2"],
        company=[".company"],
        last_verified="2020-01-01",
    )
    assert is_selector_stale(old) is True


def test_is_selector_stale_recent_date():
    recent = DomainSelectors(
        domain="fake.com",
        search_url_template="https://fake.com/?q={title}",
        wait_for_selector=".job",
        job_card=[".job"],
        link=["a"],
        title=["h2"],
        company=[".company"],
        last_verified=date.today().isoformat(),
    )
    assert is_selector_stale(recent) is False


def test_is_selector_stale_invalid_date_returns_true():
    bad = DomainSelectors(
        domain="fake.com",
        search_url_template="https://fake.com/?q={title}",
        wait_for_selector=".job",
        job_card=[".job"],
        link=["a"],
        title=["h2"],
        company=[".company"],
        last_verified="not-a-date",
    )
    assert is_selector_stale(bad) is True


def test_is_selector_stale_custom_threshold():
    # 100-day-old entry is stale at 30 days but fresh at 200 days
    old = DomainSelectors(
        domain="fake.com",
        search_url_template="https://fake.com/?q={title}",
        wait_for_selector=".job",
        job_card=[".job"],
        link=["a"],
        title=["h2"],
        company=[".company"],
        last_verified="2020-01-01",
    )
    assert is_selector_stale(old, warn_after_days=30) is True
    assert is_selector_stale(old, warn_after_days=999999) is False
