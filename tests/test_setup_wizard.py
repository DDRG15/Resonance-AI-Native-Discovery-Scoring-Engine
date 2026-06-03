"""
tests/test_setup_wizard.py — Unit tests for setup_wizard.needs_setup().

Tests the detection logic that decides whether to show the first-run wizard.
render_setup_wizard() is not tested here — it requires a live Streamlit session.

Isolation: monkeypatches config attributes directly to avoid touching .env files.
"""

import importlib
import pytest


class TestNeedsSetup:

    def _reload_wizard(self):
        """Import setup_wizard fresh to avoid cached module state."""
        import setup_wizard
        importlib.reload(setup_wizard)
        return setup_wizard

    def test_needs_setup_true_when_all_keys_empty(self, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "GROQ_API_KEY", "")
        monkeypatch.setattr(cfg, "GEMINI_API_KEY", "")
        monkeypatch.setattr(cfg, "OPENROUTER_API_KEY", "")
        monkeypatch.setattr(cfg, "COHERE_API_KEY", "")

        import setup_wizard
        # Patch importlib.reload so needs_setup() reads monkeypatched config
        monkeypatch.setattr(importlib, "reload", lambda m: None)
        assert setup_wizard.needs_setup() is True

    def test_needs_setup_false_when_groq_key_set(self, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "GROQ_API_KEY", "gsk_test_key_123")
        monkeypatch.setattr(cfg, "GEMINI_API_KEY", "")
        monkeypatch.setattr(cfg, "OPENROUTER_API_KEY", "")
        monkeypatch.setattr(cfg, "COHERE_API_KEY", "")

        import setup_wizard
        monkeypatch.setattr(importlib, "reload", lambda m: None)
        assert setup_wizard.needs_setup() is False

    def test_needs_setup_false_when_gemini_key_set(self, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "GROQ_API_KEY", "")
        monkeypatch.setattr(cfg, "GEMINI_API_KEY", "AIzaTestKey")
        monkeypatch.setattr(cfg, "OPENROUTER_API_KEY", "")
        monkeypatch.setattr(cfg, "COHERE_API_KEY", "")

        import setup_wizard
        monkeypatch.setattr(importlib, "reload", lambda m: None)
        assert setup_wizard.needs_setup() is False

    def test_needs_setup_false_when_openrouter_key_set(self, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "GROQ_API_KEY", "")
        monkeypatch.setattr(cfg, "GEMINI_API_KEY", "")
        monkeypatch.setattr(cfg, "OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setattr(cfg, "COHERE_API_KEY", "")

        import setup_wizard
        monkeypatch.setattr(importlib, "reload", lambda m: None)
        assert setup_wizard.needs_setup() is False

    def test_needs_setup_false_when_cohere_key_set(self, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "GROQ_API_KEY", "")
        monkeypatch.setattr(cfg, "GEMINI_API_KEY", "")
        monkeypatch.setattr(cfg, "OPENROUTER_API_KEY", "")
        monkeypatch.setattr(cfg, "COHERE_API_KEY", "cohere-test-key")

        import setup_wizard
        monkeypatch.setattr(importlib, "reload", lambda m: None)
        assert setup_wizard.needs_setup() is False

    def test_needs_setup_false_when_multiple_keys_set(self, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "GROQ_API_KEY", "gsk_test")
        monkeypatch.setattr(cfg, "GEMINI_API_KEY", "AIza_test")
        monkeypatch.setattr(cfg, "OPENROUTER_API_KEY", "")
        monkeypatch.setattr(cfg, "COHERE_API_KEY", "")

        import setup_wizard
        monkeypatch.setattr(importlib, "reload", lambda m: None)
        assert setup_wizard.needs_setup() is False

    def test_needs_setup_true_when_keys_are_whitespace_only(self, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "GROQ_API_KEY", "   ")
        monkeypatch.setattr(cfg, "GEMINI_API_KEY", "")
        monkeypatch.setattr(cfg, "OPENROUTER_API_KEY", "")
        monkeypatch.setattr(cfg, "COHERE_API_KEY", "")

        import setup_wizard
        monkeypatch.setattr(importlib, "reload", lambda m: None)
        # Whitespace-only strings are falsy in Python — treated as not set
        assert setup_wizard.needs_setup() is True
