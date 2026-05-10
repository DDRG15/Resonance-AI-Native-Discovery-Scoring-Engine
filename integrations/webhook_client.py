"""
integrations/webhook_client.py — Real-time Webhook Notifications for GEMA.

Implements the "First Hit + Batch Summary" notification strategy:

    1. IMMEDIATE FIRST-HIT PING:
       When the FIRST Tier 1 match is found during a scrape session,
       a single notification fires immediately. This gives the user a
       real-time signal that GEMA found something worth looking at.
       → Your phone pings ONCE, not 20 times.

    2. END-OF-SESSION BATCH SUMMARY:
       When the scrape session ends, if 2+ Tier 1 matches were found,
       a single formatted summary message lists ALL of them.
       → One consolidated ping with the full results.

    DISCORD NOISE PROBLEM (and why we don't use per-job pings):
       If GEMA finds 20 Tier 1 jobs, firing 20 individual webhook calls
       in 5 seconds hits Discord's rate limit (5 requests per 5 seconds
       per webhook) AND floods the user's phone notifications.
       The First Hit + Batch Summary pattern gives real-time awareness
       with zero notification fatigue.

    TRANSPORT:
       Uses Python's built-in urllib.request via run_in_executor —
       no extra dependencies (no aiohttp, no httpx, no requests).
       Runs in a thread pool to avoid blocking the asyncio event loop.

SRE Note:
    Webhook failures are logged and silently swallowed. A failed ping
    must NEVER abort a scrape session — the job data is more valuable
    than the notification.
"""

import asyncio
import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

import requests

import config
from models import TieredJob, ScrapeRunSummary

logger = logging.getLogger(__name__)


# =============================================================================
# Simple Alert Helper — synchronous, fire-and-forget
# =============================================================================

def send_discord_alert(message: str) -> None:
    """Sends a plain-text message to the configured Discord webhook. Never raises."""
    if not config.DISCORD_WEBHOOK_URL:
        return
    try:
        resp = requests.post(
            config.DISCORD_WEBHOOK_URL,
            json={"content": message},
            timeout=5,
        )
        resp.raise_for_status()
    except Exception as exc:
        logger.warning("Discord alert failed: %s", exc)


# =============================================================================
# Payload Builders
# =============================================================================

def _build_discord_first_hit(tiered_job: TieredJob) -> dict:
    """Discord embed payload for the immediate first-hit ping."""
    job = tiered_job.job
    salary_str = job.salary_raw or "Not published"
    return {
        "username": "GEMA Scout",
        "avatar_url": "https://cdn.jsdelivr.net/npm/twemoji@latest/assets/72x72/1f48e.png",
        "embeds": [{
            "title": f"🟢 Tier 1 Match Found — {job.title}",
            "url":   job.url,
            "color": 0x00C851,   # green
            "fields": [
                {"name": "Company",      "value": job.company,              "inline": True},
                {"name": "Salary",       "value": salary_str,               "inline": True},
                {"name": "Match Score",  "value": f"{tiered_job.match_score}%", "inline": True},
                {"name": "Source",       "value": job.source_domain,        "inline": True},
            ],
            "footer": {"text": "GEMA is still running — more results may follow"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
    }


def _build_discord_batch_summary(
    tier1_jobs: list[TieredJob],
    summary: Optional[ScrapeRunSummary],
) -> dict:
    """Discord embed payload for the end-of-session batch summary."""
    duration = ""
    if summary and summary.duration_seconds:
        duration = f" in {summary.duration_seconds:.0f}s"

    lines = []
    for i, tj in enumerate(tier1_jobs, 1):
        score = tj.match_score
        sal   = tj.job.salary_raw or "—"
        lines.append(
            f"**{i}. [{tj.job.title}]({tj.job.url})**\n"
            f"   {tj.job.company} · {sal} · {score}%"
        )

    body = "\n\n".join(lines) if lines else "No jobs to display."

    return {
        "username": "GEMA Scout",
        "avatar_url": "https://cdn.jsdelivr.net/npm/twemoji@latest/assets/72x72/1f48e.png",
        "embeds": [{
            "title": f"💎 GEMA Session Complete — {len(tier1_jobs)} Tier 1 Match(es){duration}",
            "description": body,
            "color": 0x0099FF,   # blue
            "footer": {
                "text": (
                    f"Total seen: {summary.new_processed if summary else '?'} new jobs | "
                    f"Skipped: {(summary.skipped_seen + summary.skipped_ttl) if summary else '?'}"
                )
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
    }


def _build_slack_first_hit(tiered_job: TieredJob) -> dict:
    """Slack Block Kit payload for the immediate first-hit ping."""
    job = tiered_job.job
    return {
        "text": f"🟢 GEMA Tier 1 Match: {job.title} @ {job.company}",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🟢 GEMA — Tier 1 Match Found"},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Role:*\n<{job.url}|{job.title}>"},
                    {"type": "mrkdwn", "text": f"*Company:*\n{job.company}"},
                    {"type": "mrkdwn", "text": f"*Salary:*\n{job.salary_raw or 'Not published'}"},
                    {"type": "mrkdwn", "text": f"*Score:*\n{tiered_job.match_score}%"},
                ],
            },
            {"type": "divider"},
        ],
    }


def _build_slack_batch_summary(
    tier1_jobs: list[TieredJob],
    summary: Optional[ScrapeRunSummary],
) -> dict:
    """Slack Block Kit payload for the end-of-session batch summary."""
    duration = f" ({summary.duration_seconds:.0f}s)" if summary and summary.duration_seconds else ""
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"💎 GEMA Complete — {len(tier1_jobs)} Tier 1{duration}"},
        },
    ]
    for tj in tier1_jobs:
        sal = tj.job.salary_raw or "—"
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*<{tj.job.url}|{tj.job.title}>* — {tj.job.company}\n"
                    f"Salary: {sal} · Score: {tj.match_score}%"
                ),
            },
        })
    return {"text": f"GEMA found {len(tier1_jobs)} Tier 1 matches", "blocks": blocks}


# =============================================================================
# Transport
# =============================================================================

def _http_post_sync(url: str, payload: dict) -> None:
    """
    Synchronous HTTP POST via urllib.request.
    Designed to run in a thread-pool executor — never blocks the event loop.
    Raises urllib.error.URLError on network failure (caller handles it).
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req  = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    # 10s connect+read timeout — webhook latency is predictable
    with urllib.request.urlopen(req, timeout=10) as resp:
        status = resp.status
    logger.debug("Webhook POST → HTTP %d", status)


async def _post_webhook_async(url: str, payload: dict) -> None:
    """
    Async wrapper: runs _http_post_sync in the default thread pool executor.

    asyncio.get_running_loop() is used instead of get_event_loop().
    get_event_loop() is deprecated in Python 3.10 and raises DeprecationWarning
    in 3.12 when called with no running loop. get_running_loop() is safe:
    it always returns the currently running loop (we are always inside one
    here — called from within an async context in GemaScraper.run()).
    """
    if not url:
        return
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _http_post_sync, url, payload)
    except urllib.error.URLError as exc:
        logger.warning("Webhook delivery failed (URLError): %s", exc)
    except Exception as exc:
        logger.warning("Webhook delivery failed (%s): %s", type(exc).__name__, exc)


# =============================================================================
# WebhookClient
# =============================================================================

class WebhookClient:
    """
    Stateful webhook manager for one scrape session.

    One instance per GemaScraper.run() call. Tracks session state:
        _first_hit_sent  — ensures only one immediate ping per session
        _tier1_buffer    — accumulates all Tier 1 hits for batch summary

    Usage in scraper:
        webhook = WebhookClient()
        # During scraping (per Tier 1 hit):
        await webhook.notify_tier1(tiered_job)
        # At session end:
        await webhook.flush_summary(run_summary)
    """

    def __init__(
        self,
        discord_url: str = "",
        slack_url:   str = "",
    ) -> None:
        self._discord_url    = discord_url or config.DISCORD_WEBHOOK_URL
        self._slack_url      = slack_url   or config.SLACK_WEBHOOK_URL
        self._first_hit_sent = False
        self._tier1_buffer:  list[TieredJob] = []

    @property
    def is_enabled(self) -> bool:
        return bool(self._discord_url or self._slack_url)

    async def notify_tier1(self, tiered_job: TieredJob) -> None:
        """
        Called as soon as a Tier 1 match is identified.

        First call: fires an immediate ping to both configured webhooks.
        Subsequent calls: buffers the job silently (no immediate ping).
        All calls: appends to _tier1_buffer for the batch summary.

        SRE: webhook failures are swallowed — scraping continues regardless.
        """
        if not self.is_enabled:
            return

        self._tier1_buffer.append(tiered_job)

        if not self._first_hit_sent:
            self._first_hit_sent = True
            logger.info(
                "[WEBHOOK] First Tier 1 hit — firing immediate ping: %s @ %s",
                tiered_job.job.title, tiered_job.job.company,
            )
            await asyncio.gather(
                _post_webhook_async(
                    self._discord_url,
                    _build_discord_first_hit(tiered_job),
                ),
                _post_webhook_async(
                    self._slack_url,
                    _build_slack_first_hit(tiered_job),
                ),
                return_exceptions=True,   # one failure doesn't kill the other
            )
        else:
            logger.info(
                "[WEBHOOK] Tier 1 buffered (no ping): %s @ %s",
                tiered_job.job.title, tiered_job.job.company,
            )

    async def flush_summary(
        self,
        summary: Optional[ScrapeRunSummary] = None,
    ) -> None:
        """
        Called at the end of a scrape session.

        If 2+ Tier 1 hits were found, sends one consolidated summary message.
        If exactly 1 hit was found, the immediate ping was sufficient — skips.
        If 0 hits, sends nothing.
        """
        if not self.is_enabled or len(self._tier1_buffer) < 2:
            return

        logger.info(
            "[WEBHOOK] Flushing batch summary: %d Tier 1 hits",
            len(self._tier1_buffer),
        )
        await asyncio.gather(
            _post_webhook_async(
                self._discord_url,
                _build_discord_batch_summary(self._tier1_buffer, summary),
            ),
            _post_webhook_async(
                self._slack_url,
                _build_slack_batch_summary(self._tier1_buffer, summary),
            ),
            return_exceptions=True,
        )
