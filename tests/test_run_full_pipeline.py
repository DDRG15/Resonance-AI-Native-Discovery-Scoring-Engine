"""
tests/test_run_full_pipeline.py — Integration tests for run_full_pipeline().

Verifies that run_full_pipeline() correctly:
  - Wraps run_scrape_session() + bucket_jobs() into one call
  - Returns (tier1, tier2, tier3, tier4, summary)
  - Populates summary.tier1_count / tier2_count / tier3_count
  - Updates DB tiers via bucket_jobs(db=...)

Uses a mock scraper that returns a fixed list of JobResult objects
without touching Playwright or any live board.
"""

import queue
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from models import JobResult, SearchConfig, ScrapeRunSummary


def _make_job(title, location=None, salary=None, n=0):
    return JobResult(
        title=title,
        company="TestCo",
        url=f"https://example.com/job/{n}",
        source_domain="test.board",
        scraped_at=datetime.now(timezone.utc),
        location_raw=location,
        salary_raw=salary,
    )


_CFG = SearchConfig(target_titles=["Python Engineer", "Backend Engineer"])


class TestRunFullPipeline:

    def _run(self, jobs):
        """Helper: patch run_scrape_session to return fixed jobs, then call pipeline."""
        from scraper import run_full_pipeline
        from database import GemaDatabase

        summary = ScrapeRunSummary()
        db = MagicMock(spec=GemaDatabase)

        with patch("scraper.run_scrape_session", return_value=(jobs, summary)):
            t1, t2, t3, t4, s = run_full_pipeline(
                _CFG, db, queue.Queue()
            )
        return t1, t2, t3, t4, s

    def test_returns_five_tuple(self):
        result = self._run([])
        assert len(result) == 5

    def test_empty_jobs_returns_empty_tiers(self):
        t1, t2, t3, t4, s = self._run([])
        assert t1 == [] and t2 == [] and t3 == [] and t4 == []

    def test_summary_tier_counts_populated(self):
        jobs = [
            _make_job("Python Engineer", location="Anywhere", n=1),  # Tier 1
            _make_job("Marketing Manager", n=2),                      # Tier 3
        ]
        t1, t2, t3, t4, s = self._run(jobs)
        assert s.tier1_count == len(t1)
        assert s.tier2_count == len(t2)
        assert s.tier3_count == len(t3)

    def test_tier1_job_classified_correctly(self):
        jobs = [_make_job("Python Engineer", location="Anywhere", n=1)]
        t1, t2, t3, t4, s = self._run(jobs)
        assert len(t1) == 1
        assert t1[0].tier == "Tier 1"

    def test_clearance_job_forced_to_tier3(self):
        jobs = [_make_job("Python Engineer", salary="security clearance required", n=2)]
        t1, t2, t3, t4, s = self._run(jobs)
        assert any(j.tier == "Tier 3" for j in t3)
        assert len(t1) == 0

    def test_db_update_tier_called_for_each_job(self):
        from scraper import run_full_pipeline
        from database import GemaDatabase

        jobs = [_make_job("Python Engineer", n=i) for i in range(3)]
        summary = ScrapeRunSummary()
        db = MagicMock(spec=GemaDatabase)

        with patch("scraper.run_scrape_session", return_value=(jobs, summary)):
            run_full_pipeline(_CFG, db, queue.Queue())

        assert db.update_tier.call_count == 3

    def test_total_jobs_equals_sum_of_tiers(self):
        jobs = [
            _make_job("Python Engineer", location="Anywhere", n=1),
            _make_job("Backend Engineer", n=2),
            _make_job("Marketing Manager", n=3),
            _make_job("Data Scientist", n=4),
        ]
        t1, t2, t3, t4, s = self._run(jobs)
        assert len(t1) + len(t2) + len(t3) + len(t4) == len(jobs)
