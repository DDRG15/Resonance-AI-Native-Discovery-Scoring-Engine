"""
sheets_client.py — Google Sheets Historical Data Warehouse for Project GEMA.

Appends one row per TieredJob to a Google Sheet for longitudinal analysis.
Provides market trend data: salary ranges by month, remote volume over time.

Status: Scaffolded — complete interface, TODO internals marked clearly.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from tenacity import retry, stop_after_attempt, wait_exponential

import config
from models import TieredJob, ScrapeRunSummary

logger = logging.getLogger(__name__)


class SheetsClient:
    """
    Wraps gspread for GEMA's historical data warehouse output.

    Sheet schema (columns in order from config.SHEETS_COLUMN_ORDER):
        scraped_at | title | company | salary | url | tier | match_score | search_run_id
    """

    def __init__(
        self,
        credentials_path: Optional[str] = None,
        sheet_id: Optional[str] = None,
    ):
        self.credentials_path = credentials_path or config.GOOGLE_CREDENTIALS_PATH
        self.sheet_id = sheet_id or config.GOOGLE_SHEET_ID
        self._sheet = None
        self._initialize()

    def _initialize(self) -> None:
        if not self.sheet_id:
            logger.warning("Google Sheet ID not set. Integration disabled.")
            return
        try:
            import gspread
            from google.oauth2.service_account import Credentials

            scopes = [
                "https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive",
            ]
            creds = Credentials.from_service_account_file(
                self.credentials_path, scopes=scopes
            )
            client = gspread.authorize(creds)
            self._sheet = client.open_by_key(self.sheet_id).sheet1
            logger.info("Google Sheets client initialized.")

            # Ensure header row exists
            self._ensure_headers()

        except FileNotFoundError:
            logger.error("Google credentials file not found: %s", self.credentials_path)
        except ImportError:
            logger.error("gspread not installed. Run: pip install gspread google-auth")
        except Exception as exc:
            logger.error("Sheets initialization failed: %s", exc)

    def _ensure_headers(self) -> None:
        """Writes the header row if the sheet is empty."""
        if not self._sheet:
            return
        first_row = self._sheet.row_values(1)
        if not first_row:
            self._sheet.append_row(config.SHEETS_COLUMN_ORDER, value_input_option="RAW")
            logger.info("Header row written to Google Sheet.")

    @property
    def is_enabled(self) -> bool:
        return bool(self._sheet and self.sheet_id)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def append_job(self, tiered_job: TieredJob, run_id: str = "") -> bool:
        """Appends one row to the historical data warehouse sheet."""
        if not self.is_enabled:
            logger.warning("Sheets integration disabled — skipping.")
            return False

        job = tiered_job.job
        row = [
            job.scraped_at.isoformat(),
            job.title,
            job.company,
            job.salary_raw or "",
            job.url,
            tiered_job.tier,
            tiered_job.match_score,
            run_id,
        ]

        try:
            self._sheet.append_row(row, value_input_option="RAW")
            logger.debug("Sheet row appended: %s @ %s", job.title, job.company)
            return True
        except Exception as exc:
            logger.error("Sheet append failed: %s", exc)
            raise

    def append_batch(
        self,
        tiered_jobs: list[TieredJob],
        summary: Optional[ScrapeRunSummary] = None,
    ) -> dict:
        """
        Appends all jobs in a single batch for efficiency.
        Returns summary: {"appended": N, "failed": N}
        """
        if not self.is_enabled:
            return {"appended": 0, "failed": len(tiered_jobs)}

        run_id = summary.run_id if summary else ""
        appended, failed = 0, 0

        for tj in tiered_jobs:
            try:
                self.append_job(tj, run_id)
                appended += 1
            except Exception:
                failed += 1

        logger.info("Sheets batch: appended=%d, failed=%d", appended, failed)
        return {"appended": appended, "failed": failed}
