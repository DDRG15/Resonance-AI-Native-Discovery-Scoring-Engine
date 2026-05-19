"""
tests/test_config.py — pytest suite for config.validate_config().

Uses importlib.reload() to re-evaluate module-level os.getenv() calls after
monkeypatching environment variables. This is necessary because config.py
binds variables at import time.

Coverage:
    1. No LLM key of any kind → warning about missing keys
    2. PRIMARY_LLM=groq but GROQ_API_KEY absent → fallback warning
    3. JITTER_MIN >= JITTER_MAX → configuration warning
    4. All critical values set → empty warnings list
"""

import importlib
import os

import pytest


def _reload_config(monkeypatch, overrides: dict) -> object:
    """
    Apply env var overrides and reload config so module-level bindings
    (GROQ_API_KEY = os.getenv(...)) pick up the new values.
    """
    # Set every override
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)

    # Unset any key not in overrides that could bleed from the real .env
    keys_to_clear = [
        "GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "COHERE_API_KEY",
        "DISCORD_WEBHOOK_URL", "SLACK_WEBHOOK_URL",
        "NOTION_API_KEY", "NOTION_DATABASE_ID",
        "GOOGLE_SHEET_ID", "GOOGLE_CREDENTIALS_PATH",
        "JITTER_MIN_SECONDS", "JITTER_MAX_SECONDS",
        "PRIMARY_LLM",
    ]
    for key in keys_to_clear:
        if key not in overrides:
            monkeypatch.delenv(key, raising=False)

    import config
    importlib.reload(config)
    return config


# =============================================================================
# Test 1 — No LLM keys at all
# =============================================================================

def test_validate_config_warns_when_no_llm_keys(monkeypatch):
    cfg = _reload_config(monkeypatch, {
        "JITTER_MIN_SECONDS": "5",
        "JITTER_MAX_SECONDS": "15",
    })
    warnings = cfg.validate_config()
    assert any("llm api key" in w.lower() or "groq_api_key" in w.lower() for w in warnings)


# =============================================================================
# Test 2 — PRIMARY_LLM=groq but GROQ_API_KEY empty
# =============================================================================

def test_validate_config_warns_groq_primary_without_key(monkeypatch):
    cfg = _reload_config(monkeypatch, {
        "PRIMARY_LLM": "groq",
        "GEMINI_API_KEY": "fake-gemini-key",
        "JITTER_MIN_SECONDS": "5",
        "JITTER_MAX_SECONDS": "15",
    })
    warnings = cfg.validate_config()
    assert any("groq" in w.lower() and ("fallback" in w.lower() or "empty" in w.lower()) for w in warnings)


# =============================================================================
# Test 3 — JITTER_MIN >= JITTER_MAX
# =============================================================================

def test_validate_config_warns_on_invalid_jitter(monkeypatch):
    cfg = _reload_config(monkeypatch, {
        "GROQ_API_KEY": "fake-key",
        "JITTER_MIN_SECONDS": "15",
        "JITTER_MAX_SECONDS": "5",  # min > max
    })
    warnings = cfg.validate_config()
    assert any("jitter" in w.lower() for w in warnings)


def test_validate_config_warns_on_equal_jitter(monkeypatch):
    cfg = _reload_config(monkeypatch, {
        "GROQ_API_KEY": "fake-key",
        "JITTER_MIN_SECONDS": "10",
        "JITTER_MAX_SECONDS": "10",  # equal is also invalid
    })
    warnings = cfg.validate_config()
    assert any("jitter" in w.lower() for w in warnings)


# =============================================================================
# Test 4 — Minimal valid config produces no critical warnings
# =============================================================================

def test_validate_config_no_critical_warnings_when_configured(monkeypatch):
    cfg = _reload_config(monkeypatch, {
        "GROQ_API_KEY": "fake-groq-key",
        "PRIMARY_LLM": "groq",
        "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/fake",
        "JITTER_MIN_SECONDS": "5",
        "JITTER_MAX_SECONDS": "15",
    })
    warnings = cfg.validate_config()
    # No warning should mention missing LLM keys or jitter config
    critical_patterns = ["no llm api key", "jitter_min"]
    for w in warnings:
        for pattern in critical_patterns:
            assert pattern not in w.lower(), f"Unexpected critical warning: {w}"
