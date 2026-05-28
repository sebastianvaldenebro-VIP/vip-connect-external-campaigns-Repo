"""Tests for ScheduleEvaluator using the new ScheduleSpec interface."""

from __future__ import annotations

from datetime import datetime, timezone

from vip_shared.domain.entities.schedule_spec import AllowedHour, ScheduleSpec
from vip_shared.domain.services.schedule_evaluator import ScheduleEvaluator


def test_disabled_spec_not_active():
    spec = ScheduleSpec(enabled=False, timezone="UTC")
    now = datetime(2026, 4, 22, 12, 0, tzinfo=timezone.utc)
    assert ScheduleEvaluator().is_within_window(spec, now) is False


def test_enabled_no_bounds_active():
    spec = ScheduleSpec(enabled=True, timezone="UTC")
    now = datetime(2026, 4, 22, 12, 0, tzinfo=timezone.utc)
    assert ScheduleEvaluator().is_within_window(spec, now) is True


def test_before_start_not_active():
    spec = ScheduleSpec(
        enabled=True,
        timezone="UTC",
        schedule_start_at="2026-04-23T00:00:00Z",
    )
    now = datetime(2026, 4, 22, 23, 0, tzinfo=timezone.utc)
    assert ScheduleEvaluator().is_within_window(spec, now) is False


def test_within_range_active():
    spec = ScheduleSpec(
        enabled=True,
        timezone="UTC",
        schedule_start_at="2026-04-22T00:00:00Z",
        schedule_end_at="2026-04-23T00:00:00Z",
    )
    now = datetime(2026, 4, 22, 12, 0, tzinfo=timezone.utc)
    assert ScheduleEvaluator().is_within_window(spec, now) is True


def test_after_end_not_active():
    spec = ScheduleSpec(
        enabled=True,
        timezone="UTC",
        schedule_end_at="2026-04-22T00:00:00Z",
    )
    now = datetime(2026, 4, 22, 1, 0, tzinfo=timezone.utc)
    assert ScheduleEvaluator().is_within_window(spec, now) is False


def test_allowed_hours_respected():
    # Only active on Wednesdays 10:00-12:00 in America/New_York
    spec = ScheduleSpec(
        enabled=True,
        timezone="America/New_York",
        allowed_hours=(AllowedHour(day_of_week=2, start_hour=10, end_hour=12),),
    )
    # April 22, 2026 is a Wednesday. 14:00 UTC = 10:00 EDT (DST active)
    # But 2026-04-22 is already in DST so 14:00 UTC = 10:00 EDT
    now_in = datetime(2026, 4, 22, 14, 30, tzinfo=timezone.utc)
    now_out = datetime(2026, 4, 22, 17, 0, tzinfo=timezone.utc)  # 13:00 EDT

    assert ScheduleEvaluator().is_within_window(spec, now_in) is True
    assert ScheduleEvaluator().is_within_window(spec, now_out) is False


def test_allowed_hours_different_weekday():
    spec = ScheduleSpec(
        enabled=True,
        timezone="America/New_York",
        allowed_hours=(
            AllowedHour(day_of_week=0, start_hour=9, end_hour=17),
        ),  # Monday
    )
    # Wednesday 14:00 UTC
    wednesday = datetime(2026, 4, 22, 14, 0, tzinfo=timezone.utc)
    assert ScheduleEvaluator().is_within_window(spec, wednesday) is False
