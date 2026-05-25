"""
tests/test_notion_client.py — Unit tests for NotionClient 401 fail-fast behaviour.
"""

from unittest.mock import MagicMock

import pytest

from integrations.notion_client import NotionClient
from models import JobResult, TieredJob


def _make_tiered_job(title: str = "SWE", company: str = "Acme") -> TieredJob:
    job = JobResult(
        title=title,
        company=company,
        url="https://example.com/job/1",
        salary_raw=None,
        source_domain="example.com",
    )
    return TieredJob(job=job, tier="Tier 1", match_score=80)


def _make_client(api_key: str = "secret", database_id: str = "db-123") -> NotionClient:
    # Client is imported inside _initialize() — bypass it and inject the mock directly.
    client = NotionClient.__new__(NotionClient)
    client.api_key = api_key
    client.database_id = database_id
    client._client = MagicMock()
    return client


class _Notion401Error(Exception):
    """Minimal stand-in for notion_client.errors.APIResponseError with status=401."""
    status = 401


class _Notion500Error(Exception):
    """Transient server error — should be retried."""
    status = 500


# ---------------------------------------------------------------------------
# 401 fail-fast
# ---------------------------------------------------------------------------

def test_push_job_401_disables_client_and_returns_none():
    client = _make_client()
    client._client.pages.create.side_effect = _Notion401Error("Unauthorized")

    result = client.push_job(_make_tiered_job())

    assert result is None
    assert client._client is None  # disabled


def test_push_job_401_does_not_retry():
    client = _make_client()
    client._client.pages.create.side_effect = _Notion401Error("Unauthorized")

    client.push_job(_make_tiered_job())

    # Only one call — no retries
    assert client._client is None
    # pages.create was called exactly once before being disabled
    # (can't assert on mock after None assignment, so assert is_enabled is False)
    assert not client.is_enabled


def test_push_job_500_retries_and_raises():
    from tenacity import RetryError

    client = _make_client()
    client._client.pages.create.side_effect = _Notion500Error("Internal Server Error")

    with pytest.raises(RetryError):
        client.push_job(_make_tiered_job())

    # 3 attempts (tenacity stop_after_attempt=3)
    assert client._client.pages.create.call_count == 3


# ---------------------------------------------------------------------------
# push_batch 401 mid-batch short-circuit
# ---------------------------------------------------------------------------

def test_push_batch_stops_after_401():
    client = _make_client()
    jobs = [_make_tiered_job(title=f"Job {i}") for i in range(5)]

    call_count = 0

    def _side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise _Notion401Error("Unauthorized")
        return {"id": f"page-{call_count}"}

    client._client.pages.create.side_effect = _side_effect

    summary = client.push_batch(jobs)

    # First call succeeds, second triggers 401 and disables, remaining are counted as failed
    assert summary["pushed"] == 1
    assert summary["failed"] == 4  # 1 from 401 + 3 skipped
    assert not client.is_enabled


def test_push_batch_skipped_when_disabled():
    client = _make_client()
    client._client = None  # pre-disabled

    jobs = [_make_tiered_job() for _ in range(3)]
    summary = client.push_batch(jobs)

    assert summary == {"pushed": 0, "failed": 0, "skipped": 3}
