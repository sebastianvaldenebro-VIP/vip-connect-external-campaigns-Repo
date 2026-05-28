"""Tests for CommLimitsEvaluator using decoupled CommLimits entity."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from vip_shared.domain.entities.comm_limits import CommLimits
from vip_shared.domain.services.comm_limits_evaluator import CommLimitsEvaluator


def _now():
    return datetime(2026, 4, 22, 12, 0, tzinfo=timezone.utc)


def _iso(days_ago: int) -> str:
    ts = _now() - timedelta(days=days_ago)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_no_limits_never_exceeded():
    limits = CommLimits()  # all zero
    assert CommLimitsEvaluator.exceeded(limits, [_iso(0)] * 100, _now()) is False


def test_per_day_limit_respected():
    limits = CommLimits(per_day=2)
    # 2 pushes in last 24h → exceeded (>= 2)
    pushes = [_iso(0), _iso(0)]
    assert CommLimitsEvaluator.exceeded(limits, pushes, _now()) is True


def test_per_day_limit_not_exceeded_when_under():
    limits = CommLimits(per_day=3)
    pushes = [_iso(0), _iso(0)]
    assert CommLimitsEvaluator.exceeded(limits, pushes, _now()) is False


def test_per_week_limit():
    limits = CommLimits(per_week=5)
    # 5 pushes in last week
    pushes = [_iso(0), _iso(1), _iso(2), _iso(3), _iso(4)]
    assert CommLimitsEvaluator.exceeded(limits, pushes, _now()) is True


def test_per_month_limit():
    limits = CommLimits(per_month=10)
    pushes = [_iso(i) for i in range(15)]  # 15 pushes in last 15 days
    assert CommLimitsEvaluator.exceeded(limits, pushes, _now()) is True


def test_old_pushes_outside_window_ignored():
    limits = CommLimits(per_day=2)
    # pushes from 2 and 3 days ago — outside the 1-day window
    pushes = [_iso(2), _iso(3), _iso(5)]
    assert CommLimitsEvaluator.exceeded(limits, pushes, _now()) is False


def test_invalid_timestamp_skipped():
    limits = CommLimits(per_day=1)
    pushes = ["not-an-iso-date", _iso(0)]
    assert CommLimitsEvaluator.exceeded(limits, pushes, _now()) is True
