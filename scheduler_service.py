"""
scheduler_service.py — Background auto-scrape scheduler for Project GEMA.

Wraps APScheduler's BackgroundScheduler as a singleton so the scrape job
survives Streamlit re-renders. main.py acquires the singleton via
@st.cache_resource and wires the sidebar UI to enable/disable/configure it.

Architecture:
    APScheduler BackgroundScheduler runs in a daemon thread, independent of
    any Streamlit session. On each fire, it spawns a regular threading.Thread
    (identical to the manual scrape path in main.py) and calls run_scrape_session
    inside asyncio.run(). Results are stored on the instance for the sidebar
    status display; Discord notifications fire via the existing send_discord_alert.

Thread safety:
    self.is_running guards against concurrent fires (APScheduler may fire a
    second time before the first run completes on slow systems). The flag is
    set before the thread starts and cleared inside the thread's finally block.
"""

import logging
import queue
import random
import threading
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

_JOB_ID = "gema_auto_scrape"


class SchedulerService:
    def __init__(self) -> None:
        self._scheduler = BackgroundScheduler(
            job_defaults={"misfire_grace_time": 300},  # tolerate 5-min late fires
        )
        self._scheduler.start()

        self.enabled: bool = False
        self.interval_hours: int = 4
        self.config = None          # SearchConfig | None
        self.profile = None         # dict | None — ephemeral CV profile
        self.is_running: bool = False
        self.last_run_at: Optional[datetime] = None
        self.last_run_new_jobs: int = 0
        self.last_run_tier1: int = 0

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def enable(self, interval_hours: int, config, profile, db) -> None:
        """Start (or restart) the interval job with the given config."""
        self.interval_hours = interval_hours
        self.config = config
        self.profile = profile

        # Remove existing job before re-adding — handles interval changes
        if self._scheduler.get_job(_JOB_ID):
            self._scheduler.remove_job(_JOB_ID)

        self._scheduler.add_job(
            func=self._fire,
            trigger=IntervalTrigger(hours=interval_hours),
            id=_JOB_ID,
            args=[db],
            replace_existing=True,
        )
        self.enabled = True
        logger.info(
            "[SCHEDULER] Auto-scrape enabled: every %dh. Next: %s",
            interval_hours,
            self.get_next_run_time(),
        )

    def disable(self) -> None:
        """Stop the interval job."""
        if self._scheduler.get_job(_JOB_ID):
            self._scheduler.remove_job(_JOB_ID)
        self.enabled = False
        logger.info("[SCHEDULER] Auto-scrape disabled.")

    def update_config(self, config, profile) -> None:
        """Update the stored SearchConfig without changing the schedule."""
        self.config = config
        self.profile = profile
        logger.info("[SCHEDULER] Config updated.")

    def get_next_run_time(self) -> str:
        """Return a human-readable string for the next scheduled fire time."""
        job = self._scheduler.get_job(_JOB_ID)
        if not job or not job.next_run_time:
            return "—"
        nrt = job.next_run_time
        now = datetime.now(tz=nrt.tzinfo)
        delta = nrt - now
        total_seconds = int(delta.total_seconds())
        if total_seconds < 0:
            return nrt.strftime("%H:%M")
        hours, remainder = divmod(total_seconds, 3600)
        minutes = remainder // 60
        time_str = nrt.strftime("%H:%M")
        if hours > 0:
            return f"{time_str} (in {hours}h {minutes}m)"
        return f"{time_str} (in {minutes}m)"

    def get_last_run_summary(self) -> str:
        """Return a one-line summary of the last completed run."""
        if self.last_run_at is None:
            return "No runs yet"
        ts = self.last_run_at.strftime("%H:%M")
        return f"{ts} | {self.last_run_new_jobs} new, {self.last_run_tier1} Tier 1"

    # -------------------------------------------------------------------------
    # Internal — job function (runs in APScheduler thread)
    # -------------------------------------------------------------------------

    def _fire(self, db) -> None:
        """Called by APScheduler on each interval tick."""
        if self.is_running:
            logger.warning("[SCHEDULER] Previous run still active — skipping this tick.")
            return
        if self.config is None:
            logger.warning("[SCHEDULER] No SearchConfig set — skipping fire.")
            return

        self.is_running = True
        logger.info("[SCHEDULER] Auto-scrape firing...")

        config_snapshot = self.config
        profile_snapshot = self.profile
        log_q: queue.Queue = queue.Queue()
        result: dict = {}

        def _run():
            try:
                from scraper import run_scrape_session
                from matcher import bucket_jobs
                from integrations.webhook_client import send_discord_alert

                start_phrases = [
                    "'GEMA auto-scrape starting.'",
                    "'Scheduled run initiated.'",
                    "'GEMA is on autopilot. Sit back.'",
                ]
                threading.Thread(
                    target=send_discord_alert,
                    args=(random.choice(start_phrases),),
                    daemon=True,
                ).start()

                jobs, summary = run_scrape_session(
                    config_snapshot,
                    db,
                    log_q,
                    ttl_hours=24,   # default TTL for scheduled runs
                    profile=profile_snapshot,
                )
                result["jobs"] = jobs
                result["summary"] = summary

                t1, t2, t3, t4 = bucket_jobs(jobs, config_snapshot, db, profile=profile_snapshot)
                self.last_run_new_jobs = len(jobs)
                self.last_run_tier1 = len(t1)
                self.last_run_at = datetime.now(timezone.utc)

                report = (
                    f"📅 Scheduled run complete:\n"
                    f"- New jobs: {len(jobs)}\n"
                    f"- Tier 1: {len(t1)} | Tier 2: {len(t2)} | Tier 3: {len(t3)}"
                )
                threading.Thread(
                    target=send_discord_alert, args=(report,), daemon=True
                ).start()

                logger.info(
                    "[SCHEDULER] Run complete. New=%d T1=%d T2=%d",
                    len(jobs), len(t1), len(t2),
                )
            except Exception as exc:
                logger.error("[SCHEDULER] Run failed: %s", exc)
            finally:
                self.is_running = False

        threading.Thread(target=_run, daemon=True).start()
