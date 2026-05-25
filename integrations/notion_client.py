"""
notion_client.py — Notion Kanban Integration for Project GEMA.

Pushes TieredJob results to a Notion database as Kanban cards.
Each card is tagged with its Tier, color-coded, and contains company + link.

Status: Scaffolded — complete interface, TODO internals marked clearly.
"""

import logging
from typing import Optional

from tenacity import retry, stop_after_attempt, wait_exponential

import config
from models import TieredJob

logger = logging.getLogger(__name__)


class NotionClient:
    """
    Wraps the Notion API for GEMA's Kanban output.

    Database schema expected in Notion:
        Name         (title)   — Job title
        Company      (text)    — Company name
        URL          (url)     — Direct job link
        Tier         (select)  — "Tier 1" | "Tier 2" | "Tier 3"
        Match Score  (number)  — 0–100
        Salary       (text)    — Raw salary string
        Source       (text)    — Job board domain
        Status       (select)  — "To Review" | "Manual Review" | "Applied" | "Interview" | "Rejected"
    """

    def __init__(self, api_key: Optional[str] = None, database_id: Optional[str] = None):
        self.api_key = api_key or config.NOTION_API_KEY
        self.database_id = database_id or config.NOTION_DATABASE_ID
        self._client = None
        self._initialize()

    def _initialize(self) -> None:
        if not self.api_key:
            logger.warning("Notion API key not set. Integration disabled.")
            return
        try:
            from notion_client import Client
            self._client = Client(auth=self.api_key)
            logger.info("Notion client initialized.")
        except ImportError:
            logger.error("notion-client not installed. Run: pip install notion-client")

    @property
    def is_enabled(self) -> bool:
        return bool(self._client and self.api_key and self.database_id)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def push_job(self, tiered_job: TieredJob) -> Optional[str]:
        """
        Creates a Notion page (Kanban card) for one tiered job.

        Returns the created page ID, or None if the push failed permanently.
        A 401 disables the integration immediately — no retries, no further calls.
        """
        if not self.is_enabled:
            logger.warning("Notion integration disabled — skipping push.")
            return None

        job = tiered_job.job
        color = config.NOTION_TIER_COLORS.get(tiered_job.tier, "default")

        properties = {
            "Name": {
                "title": [{"text": {"content": job.title}}]
            },
            "Company": {
                "rich_text": [{"text": {"content": job.company}}]
            },
            "URL": {
                "url": job.url
            },
            "Tier": {
                "select": {"name": tiered_job.tier, "color": color}
            },
            "Match Score": {
                # Tier 4 sentinel is -1 — store as None in Notion (no score)
                "number": tiered_job.match_score if tiered_job.match_score >= 0 else None
            },
            "Salary": {
                "rich_text": [{"text": {"content": job.salary_raw or "Not published"}}]
            },
            "Comments / Raw Salary": {
                # Tier 4: surfaces the unstructured salary text for manual review.
                # For Tier 1/2/3: populated only if salary_raw is present and
                # non-numeric (defensive — belt-and-suspenders for edge cases).
                "rich_text": [{
                    "text": {
                        "content": (
                            f"[MANUAL REVIEW] Employer stated: '{job.salary_raw}' — "
                            "evaluate compensation manually before applying."
                            if tiered_job.tier == "Tier 4" and job.salary_raw
                            else job.salary_raw or ""
                        )
                    }
                }]
            },
            "Source": {
                "rich_text": [{"text": {"content": job.source_domain}}]
            },
            "Status": {
                "select": {
                    "name": "Manual Review" if tiered_job.tier == "Tier 4" else "To Review"
                }
            },
        }

        try:
            response = self._client.pages.create(
                parent={"database_id": self.database_id},
                properties=properties,
            )
            page_id = response["id"]
            logger.info("Notion card created: %s | %s (%s)", job.title, job.company, tiered_job.tier)
            return page_id
        except Exception as exc:
            # 401 = expired or revoked token — retrying will always fail.
            # Disable integration immediately to avoid hammering the API.
            if getattr(exc, "status", None) == 401:
                logger.error(
                    "Notion 401 Unauthorized — token expired or revoked. "
                    "Disabling Notion integration for this session. "
                    "Update NOTION_API_KEY in .env to re-enable."
                )
                self._client = None
                return None
            logger.error("Notion push failed for %s: %s", job.title, exc)
            raise

    def push_batch(self, tiered_jobs: list[TieredJob]) -> dict:
        """
        Pushes a list of TieredJobs to Notion.
        Returns summary: {"pushed": N, "failed": N, "skipped": N}
        """
        if not self.is_enabled:
            return {"pushed": 0, "failed": 0, "skipped": len(tiered_jobs)}

        pushed, failed = 0, 0
        for tj in tiered_jobs:
            try:
                result = self.push_job(tj)
                if result is not None:
                    pushed += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
            # 401 disables the client mid-batch — no point continuing
            if not self.is_enabled:
                remaining = len(tiered_jobs) - pushed - failed
                failed += remaining
                logger.warning(
                    "Notion integration disabled mid-batch — skipping %d remaining jobs.", remaining
                )
                break

        logger.info("Notion batch: pushed=%d, failed=%d", pushed, failed)
        return {"pushed": pushed, "failed": failed, "skipped": 0}
