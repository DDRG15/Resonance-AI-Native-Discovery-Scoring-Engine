"""
tests/test_webhook.py — pytest suite for integrations/webhook_client.py.

Coverage:
    1. _build_discord_first_hit  — embed structure (title, url, color, fields)
    2. _build_discord_batch_summary — embeds list, description contains job titles
    3. _build_slack_first_hit    — blocks array has 'section' type block
    4. send_discord_alert        — no-op when URL not set (urlopen never called)
    5. send_discord_alert        — calls urlopen exactly once when URL is set

No real network calls are made — urllib.request.urlopen is mocked.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from integrations.webhook_client import (
    _build_discord_batch_summary,
    _build_discord_first_hit,
    _build_slack_first_hit,
    send_discord_alert,
)
from models import JobResult, ScrapeRunSummary, TieredJob


# =============================================================================
# Fixtures
# =============================================================================

def _make_tier1_job(title: str = "Backend Engineer", url_id: int = 1) -> TieredJob:
    return TieredJob(
        job=JobResult(
            title=title,
            company="TestCorp",
            salary_raw="$120k",
            url=f"https://example.com/job/{url_id}",
            source_domain="example.com",
        ),
        match_score=90,
        tier="Tier 1",
    )


# =============================================================================
# 1. _build_discord_first_hit
# =============================================================================

def test_discord_first_hit_has_embeds_list():
    payload = _build_discord_first_hit(_make_tier1_job())
    assert "embeds" in payload
    assert isinstance(payload["embeds"], list)
    assert len(payload["embeds"]) == 1


def test_discord_first_hit_embed_has_title_and_url():
    job = _make_tier1_job("Senior SRE")
    payload = _build_discord_first_hit(job)
    embed = payload["embeds"][0]
    assert "Senior SRE" in embed["title"]
    assert embed["url"] == job.job.url


def test_discord_first_hit_embed_color_is_green():
    payload = _build_discord_first_hit(_make_tier1_job())
    assert payload["embeds"][0]["color"] == 0x00C851


def test_discord_first_hit_embed_has_required_fields():
    payload = _build_discord_first_hit(_make_tier1_job())
    field_names = {f["name"] for f in payload["embeds"][0]["fields"]}
    assert {"Company", "Salary", "Match Score", "Source"} <= field_names


# =============================================================================
# 2. _build_discord_batch_summary
# =============================================================================

def test_discord_batch_summary_embeds_contain_all_job_titles():
    jobs = [_make_tier1_job("SRE Lead", 1), _make_tier1_job("Backend Engineer", 2)]
    payload = _build_discord_batch_summary(jobs, summary=None)
    description = payload["embeds"][0]["description"]
    assert "SRE Lead" in description
    assert "Backend Engineer" in description


def test_discord_batch_summary_color_is_blue():
    payload = _build_discord_batch_summary([_make_tier1_job()], summary=None)
    assert payload["embeds"][0]["color"] == 0x0099FF


def test_discord_batch_summary_empty_list_does_not_crash():
    payload = _build_discord_batch_summary([], summary=None)
    assert "embeds" in payload


# =============================================================================
# 3. _build_slack_first_hit
# =============================================================================

def test_slack_first_hit_has_blocks_array():
    payload = _build_slack_first_hit(_make_tier1_job())
    assert "blocks" in payload
    assert isinstance(payload["blocks"], list)
    assert len(payload["blocks"]) >= 1


def test_slack_first_hit_has_section_block():
    payload = _build_slack_first_hit(_make_tier1_job())
    block_types = {b["type"] for b in payload["blocks"]}
    assert "section" in block_types


def test_slack_first_hit_top_level_text_present():
    payload = _build_slack_first_hit(_make_tier1_job("Backend Engineer"))
    assert "Backend Engineer" in payload.get("text", "")


# =============================================================================
# 4 & 5. send_discord_alert — transport behavior
# =============================================================================

def test_send_discord_alert_noop_when_no_url_configured(monkeypatch):
    """When DISCORD_WEBHOOK_URL is empty, urlopen must never be called."""
    monkeypatch.setattr("config.DISCORD_WEBHOOK_URL", "")

    with patch("urllib.request.urlopen") as mock_urlopen:
        send_discord_alert("Hello GEMA")
        mock_urlopen.assert_not_called()


def test_send_discord_alert_calls_urlopen_once_when_url_set(monkeypatch):
    """When DISCORD_WEBHOOK_URL is set, urlopen is called exactly once."""
    monkeypatch.setattr(
        "config.DISCORD_WEBHOOK_URL",
        "https://discord.com/api/webhooks/fake/token",
    )

    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
    mock_ctx.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_ctx) as mock_urlopen:
        send_discord_alert("Test alert")
        mock_urlopen.assert_called_once()


def test_send_discord_alert_never_raises_on_network_error(monkeypatch):
    """Network errors must be swallowed — never abort the scrape session."""
    monkeypatch.setattr(
        "config.DISCORD_WEBHOOK_URL",
        "https://discord.com/api/webhooks/fake/token",
    )
    with patch("urllib.request.urlopen", side_effect=Exception("connection refused")):
        send_discord_alert("Test alert")  # must not raise
