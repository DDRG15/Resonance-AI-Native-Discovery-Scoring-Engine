"""
tests/test_config.py — pytest suite for config.validate_config().

Strategy: monkeypatch.setattr() patches the module-level variables that
validate_config() reads directly (GROQ_API_KEY, JITTER_MIN, etc.) without
reloading config. This avoids fighting load_dotenv's override=True behavior.

Coverage:
    1. No LLM key of any kind → warning about missing keys
    2. PRIMARY_LLM=groq but GROQ_API_KEY empty → fallback warning
    3. JITTER_MIN >= JITTER_MAX → configuration warning
    4. JITTER_MIN == JITTER_MAX → configuration warning
    5. All critical values set → no LLM or jitter critical warnings
"""

import pytest
import config


def _patch_defaults(monkeypatch) -> None:
    """Set the module attributes to a clean baseline before each test."""
    monkeypatch.setattr(config, "GROQ_API_KEY", "")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(config, "COHERE_API_KEY", "")
    monkeypatch.setattr(config, "PRIMARY_LLM", "groq")
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr(config, "SLACK_WEBHOOK_URL", "")
    monkeypatch.setattr(config, "NOTION_API_KEY", "")
    monkeypatch.setattr(config, "NOTION_DATABASE_ID", "")
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "")
    monkeypatch.setattr(config, "JITTER_MIN", 5.0)
    monkeypatch.setattr(config, "JITTER_MAX", 15.0)


# =============================================================================
# Test 1 — No LLM keys at all
# =============================================================================

def test_validate_config_warns_when_no_llm_keys(monkeypatch):
    _patch_defaults(monkeypatch)
    # All LLM keys remain empty (set by _patch_defaults)
    warnings = config.validate_config()
    assert any(
        "no llm api key" in w.lower() or "groq_api_key" in w.lower()
        for w in warnings
    ), f"Expected LLM key warning, got: {warnings}"


# =============================================================================
# Test 2 — PRIMARY_LLM=groq but GROQ_API_KEY absent
# =============================================================================

def test_validate_config_warns_groq_primary_without_key(monkeypatch):
    _patch_defaults(monkeypatch)
    # Provide a Gemini key so the "no LLM at all" warning doesn't fire,
    # but leave GROQ_API_KEY empty with PRIMARY_LLM=groq
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-gemini-key")
    monkeypatch.setattr(config, "PRIMARY_LLM", "groq")

    warnings = config.validate_config()
    assert any(
        "groq" in w.lower() and ("fallback" in w.lower() or "empty" in w.lower() or "groq_api_key" in w.lower())
        for w in warnings
    ), f"Expected Groq fallback warning, got: {warnings}"


# =============================================================================
# Test 3 — JITTER_MIN > JITTER_MAX
# =============================================================================

def test_validate_config_warns_on_invalid_jitter(monkeypatch):
    _patch_defaults(monkeypatch)
    monkeypatch.setattr(config, "GROQ_API_KEY", "fake-key")
    monkeypatch.setattr(config, "JITTER_MIN", 15.0)
    monkeypatch.setattr(config, "JITTER_MAX", 5.0)   # min > max

    warnings = config.validate_config()
    assert any("jitter" in w.lower() for w in warnings), \
        f"Expected jitter warning, got: {warnings}"


# =============================================================================
# Test 4 — JITTER_MIN == JITTER_MAX
# =============================================================================

def test_validate_config_warns_on_equal_jitter(monkeypatch):
    _patch_defaults(monkeypatch)
    monkeypatch.setattr(config, "GROQ_API_KEY", "fake-key")
    monkeypatch.setattr(config, "JITTER_MIN", 10.0)
    monkeypatch.setattr(config, "JITTER_MAX", 10.0)   # equal is invalid

    warnings = config.validate_config()
    assert any("jitter" in w.lower() for w in warnings), \
        f"Expected jitter warning, got: {warnings}"


# =============================================================================
# Test 5 — Minimal valid config produces no LLM or jitter critical warnings
# =============================================================================

def test_validate_config_no_critical_warnings_when_configured(monkeypatch):
    _patch_defaults(monkeypatch)
    monkeypatch.setattr(config, "GROQ_API_KEY", "fake-groq-key")
    monkeypatch.setattr(config, "PRIMARY_LLM", "groq")
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/fake")
    monkeypatch.setattr(config, "JITTER_MIN", 5.0)
    monkeypatch.setattr(config, "JITTER_MAX", 15.0)

    warnings = config.validate_config()
    critical_patterns = ["no llm api key", "jitter_min"]
    for w in warnings:
        for pattern in critical_patterns:
            assert pattern not in w.lower(), f"Unexpected critical warning: {w}"
