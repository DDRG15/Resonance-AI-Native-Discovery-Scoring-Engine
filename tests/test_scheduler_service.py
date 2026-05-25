"""
tests/test_scheduler_service.py — Unit tests for SchedulerService.

BackgroundScheduler is mocked so no real threads are spawned during tests.
All assertions target the pure-logic surface: state transitions, guard clauses,
and output formatting — not the APScheduler internals themselves.
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from scheduler_service import SchedulerService


# =============================================================================
# Fixture — service with mocked APScheduler
# =============================================================================

@pytest.fixture
def service():
    """SchedulerService with BackgroundScheduler replaced by a MagicMock."""
    with patch("scheduler_service.BackgroundScheduler") as mock_cls:
        mock_sched = MagicMock()
        mock_cls.return_value = mock_sched
        svc = SchedulerService()
        # Expose the mock directly for per-test setup
        svc._scheduler = mock_sched
        return svc


# =============================================================================
# Initial state
# =============================================================================

def test_initial_state(service):
    assert service.enabled is False
    assert service.is_running is False
    assert service.last_run_at is None
    assert service.last_run_new_jobs == 0
    assert service.last_run_tier1 == 0
    assert service.config is None
    assert service.profile is None


# =============================================================================
# get_last_run_summary
# =============================================================================

def test_get_last_run_summary_no_runs_yet(service):
    assert service.get_last_run_summary() == "No runs yet"


def test_get_last_run_summary_with_data(service):
    service.last_run_at = datetime(2026, 5, 25, 14, 30, tzinfo=timezone.utc)
    service.last_run_new_jobs = 42
    service.last_run_tier1 = 7

    summary = service.get_last_run_summary()

    assert "42 new" in summary
    assert "7 Tier 1" in summary
    assert "14:30" in summary


def test_get_last_run_summary_zero_results(service):
    service.last_run_at = datetime(2026, 5, 25, 9, 0, tzinfo=timezone.utc)
    service.last_run_new_jobs = 0
    service.last_run_tier1 = 0

    summary = service.get_last_run_summary()

    assert "0 new" in summary
    assert "0 Tier 1" in summary


# =============================================================================
# get_next_run_time
# =============================================================================

def test_get_next_run_time_no_job_returns_dash(service):
    service._scheduler.get_job.return_value = None
    assert service.get_next_run_time() == "—"


def test_get_next_run_time_no_next_run_time_returns_dash(service):
    mock_job = MagicMock()
    mock_job.next_run_time = None
    service._scheduler.get_job.return_value = mock_job

    assert service.get_next_run_time() == "—"


def test_get_next_run_time_future_shows_hours_and_minutes(service):
    # Use a large delta so exact-minute drift from datetime.now() re-evaluation
    # inside get_next_run_time() does not cross a boundary.
    now = datetime.now(tz=timezone.utc)
    future = now + timedelta(hours=2, minutes=30)

    mock_job = MagicMock()
    mock_job.next_run_time = future
    service._scheduler.get_job.return_value = mock_job

    result = service.get_next_run_time()

    # "2h" must appear; exact minute value is not asserted because
    # get_next_run_time() re-calls datetime.now() internally, which may
    # advance a few milliseconds past our test baseline.
    assert "2h" in result
    assert "m)" in result   # some minute value is present


def test_get_next_run_time_under_one_hour_shows_minutes_only(service):
    now = datetime.now(tz=timezone.utc)
    future = now + timedelta(minutes=45)

    mock_job = MagicMock()
    mock_job.next_run_time = future
    service._scheduler.get_job.return_value = mock_job

    result = service.get_next_run_time()

    # Exact minute not asserted — datetime.now() re-evaluation inside the method
    # can drift by 1m. Assert structural form: minutes-only path (no "Xh" present).
    assert "m)" in result
    assert "h " not in result  # "2h 30m" shape must not appear


def test_get_next_run_time_past_shows_hhmm_only(service):
    past = datetime(2026, 5, 25, 8, 0, tzinfo=timezone.utc)

    mock_job = MagicMock()
    mock_job.next_run_time = past
    service._scheduler.get_job.return_value = mock_job

    result = service.get_next_run_time()

    # Past time — just the HH:MM string, no "(in ...)" suffix
    assert "(" not in result
    assert "08:00" in result


# =============================================================================
# disable
# =============================================================================

def test_disable_sets_enabled_false(service):
    service.enabled = True
    service._scheduler.get_job.return_value = MagicMock()

    service.disable()

    assert service.enabled is False


def test_disable_removes_job_when_present(service):
    service._scheduler.get_job.return_value = MagicMock()

    service.disable()

    service._scheduler.remove_job.assert_called_once()


def test_disable_does_not_error_when_no_job(service):
    service._scheduler.get_job.return_value = None

    service.disable()  # must not raise

    service._scheduler.remove_job.assert_not_called()


# =============================================================================
# update_config
# =============================================================================

def test_update_config_stores_new_config(service):
    new_config = object()
    new_profile = {"name": "test"}

    service.update_config(new_config, new_profile)

    assert service.config is new_config
    assert service.profile is new_profile


def test_update_config_overwrites_previous(service):
    old_config = object()
    service.config = old_config

    new_config = object()
    service.update_config(new_config, None)

    assert service.config is new_config
    assert service.config is not old_config


# =============================================================================
# _fire guard clauses
# =============================================================================

def test_fire_skips_when_already_running(service):
    service.is_running = True
    db = MagicMock()

    service._fire(db)

    # is_running stays True — was not cleared (skip path never sets it False)
    assert service.is_running is True


def test_fire_skips_when_config_is_none(service):
    service.is_running = False
    service.config = None
    db = MagicMock()

    service._fire(db)

    # is_running was set True then cleared in the thread's finally block.
    # But since config is None, the thread is never spawned — is_running
    # is set to True in _fire before the guard returns early. Wait, let me
    # re-read the code...
    # Actually: guard checks is_running first, then config. Config=None
    # triggers an early return BEFORE is_running = True. So is_running stays False.
    assert service.is_running is False
