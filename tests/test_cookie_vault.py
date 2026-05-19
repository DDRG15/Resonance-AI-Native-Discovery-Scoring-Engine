"""
tests/test_cookie_vault.py — Unit tests for integrations/cookie_vault.py

Tests cover:
    - has_cookies()  : True/False detection, missing file, empty array
    - load_cookies() : normalization of Chrome → Playwright format
    - list_domains() : scan cookies/ directory
    - inject_into_context(): injection count, failure resilience
    - _normalize()   : sameSite mapping, expirationDate→expires, missing fields
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from integrations.cookie_vault import CookieVault


# =============================================================================
# Fixtures
# =============================================================================

MINIMAL_COOKIE = {
    "name": "session",
    "value": "abc123",
    "domain": ".example.com",
    "path": "/",
}

FULL_CHROME_COOKIE = {
    "name": "_site_session",
    "value": "xyz789",
    "domain": ".example.com",
    "path": "/",
    "expirationDate": 1810735770.0,
    "httpOnly": True,
    "secure": True,
    "sameSite": "Lax",
    "session": False,
    "storeId": "0",
    "id": 1,
}

DATADOME_COOKIE = {
    "name": "datadome",
    "value": "eeDNf8OW...",
    "domain": ".example.com",
    "path": "/",
    "expirationDate": 1810735770.717084,
    "httpOnly": False,
    "secure": True,
    "sameSite": "lax",
}


@pytest.fixture
def vault_dir(tmp_path) -> Path:
    """Returns a temporary cookies directory."""
    d = tmp_path / "cookies"
    d.mkdir()
    return d


@pytest.fixture
def vault(vault_dir) -> CookieVault:
    return CookieVault(cookies_dir=str(vault_dir))


def _write_cookies(vault_dir: Path, domain: str, cookies: list) -> Path:
    path = vault_dir / f"{domain}.json"
    path.write_text(json.dumps(cookies), encoding="utf-8")
    return path


# =============================================================================
# has_cookies()
# =============================================================================

def test_has_cookies_returns_false_when_no_file(vault):
    assert vault.has_cookies("example.com") is False


def test_has_cookies_returns_false_for_empty_array(vault, vault_dir):
    _write_cookies(vault_dir, "example.com", [])
    assert vault.has_cookies("example.com") is False


def test_has_cookies_returns_true_when_file_exists(vault, vault_dir):
    _write_cookies(vault_dir, "example.com", [MINIMAL_COOKIE])
    assert vault.has_cookies("example.com") is True


def test_has_cookies_strips_leading_dot_from_domain(vault, vault_dir):
    _write_cookies(vault_dir, "example.com", [MINIMAL_COOKIE])
    # Domain with leading dot should still find the file
    assert vault.has_cookies(".example.com") is True


# =============================================================================
# load_cookies()
# =============================================================================

def test_load_cookies_returns_empty_list_when_no_file(vault):
    result = vault.load_cookies("nonexistent.com")
    assert result == []


def test_load_cookies_returns_normalized_cookies(vault, vault_dir):
    _write_cookies(vault_dir, "example.com", [FULL_CHROME_COOKIE])
    result = vault.load_cookies("example.com")
    assert len(result) == 1
    cookie = result[0]
    assert cookie["name"] == "_site_session"
    assert cookie["value"] == "xyz789"
    assert cookie["domain"] == ".example.com"
    assert cookie["path"] == "/"


def test_load_cookies_maps_expiration_date_to_expires(vault, vault_dir):
    _write_cookies(vault_dir, "example.com", [FULL_CHROME_COOKIE])
    result = vault.load_cookies("example.com")
    assert "expires" in result[0]
    assert result[0]["expires"] == pytest.approx(1810735770.0)


def test_load_cookies_maps_http_only_and_secure(vault, vault_dir):
    _write_cookies(vault_dir, "example.com", [FULL_CHROME_COOKIE])
    result = vault.load_cookies("example.com")
    assert result[0]["httpOnly"] is True
    assert result[0]["secure"] is True


def test_load_cookies_preserves_valid_same_site(vault, vault_dir):
    _write_cookies(vault_dir, "example.com", [FULL_CHROME_COOKIE])
    result = vault.load_cookies("example.com")
    assert result[0]["sameSite"] == "Lax"


def test_load_cookies_drops_invalid_same_site(vault, vault_dir):
    cookie = {**MINIMAL_COOKIE, "sameSite": "unspecified"}
    _write_cookies(vault_dir, "example.com", [cookie])
    result = vault.load_cookies("example.com")
    assert "sameSite" not in result[0]


def test_load_cookies_drops_no_restriction_same_site(vault, vault_dir):
    cookie = {**MINIMAL_COOKIE, "sameSite": "no_restriction"}
    _write_cookies(vault_dir, "example.com", [cookie])
    result = vault.load_cookies("example.com")
    assert "sameSite" not in result[0]


def test_load_cookies_handles_multiple_cookies(vault, vault_dir):
    cookies = [MINIMAL_COOKIE, FULL_CHROME_COOKIE, DATADOME_COOKIE]
    _write_cookies(vault_dir, "example.com", cookies)
    result = vault.load_cookies("example.com")
    assert len(result) == 3


def test_load_cookies_does_not_raise_on_malformed_json(vault, vault_dir):
    path = vault_dir / "example.com.json"
    path.write_text("not valid json {{{", encoding="utf-8")
    result = vault.load_cookies("example.com")
    assert result == []


def test_load_cookies_does_not_raise_when_root_is_not_list(vault, vault_dir):
    path = vault_dir / "example.com.json"
    path.write_text('{"name": "not_a_list"}', encoding="utf-8")
    result = vault.load_cookies("example.com")
    assert result == []


# =============================================================================
# list_domains()
# =============================================================================

def test_list_domains_returns_empty_when_dir_missing(tmp_path):
    vault = CookieVault(cookies_dir=str(tmp_path / "nonexistent"))
    assert vault.list_domains() == []


def test_list_domains_returns_all_json_files(vault, vault_dir):
    _write_cookies(vault_dir, "wellfound.com", [MINIMAL_COOKIE])
    _write_cookies(vault_dir, "arc.dev", [MINIMAL_COOKIE])
    domains = vault.list_domains()
    assert "wellfound.com" in domains
    assert "arc.dev" in domains
    assert len(domains) == 2


def test_list_domains_ignores_non_json_files(vault, vault_dir):
    _write_cookies(vault_dir, "wellfound.com", [MINIMAL_COOKIE])
    (vault_dir / "README.md").write_text("docs", encoding="utf-8")
    domains = vault.list_domains()
    assert domains == ["wellfound.com"]


# =============================================================================
# inject_into_context()
# =============================================================================

@pytest.mark.asyncio
async def test_inject_returns_zero_when_no_cookies(vault):
    mock_ctx = AsyncMock()
    count = await vault.inject_into_context("nonexistent.com", mock_ctx)
    assert count == 0
    mock_ctx.add_cookies.assert_not_called()


@pytest.mark.asyncio
async def test_inject_calls_add_cookies_and_returns_count(vault, vault_dir):
    _write_cookies(vault_dir, "example.com", [MINIMAL_COOKIE, FULL_CHROME_COOKIE])
    mock_ctx = AsyncMock()
    count = await vault.inject_into_context("example.com", mock_ctx)
    assert count == 2
    mock_ctx.add_cookies.assert_called_once()


@pytest.mark.asyncio
async def test_inject_returns_zero_on_playwright_error(vault, vault_dir):
    _write_cookies(vault_dir, "example.com", [MINIMAL_COOKIE])
    mock_ctx = AsyncMock()
    mock_ctx.add_cookies.side_effect = Exception("Playwright context closed")
    count = await vault.inject_into_context("example.com", mock_ctx)
    assert count == 0  # Never raises — returns 0 on failure
