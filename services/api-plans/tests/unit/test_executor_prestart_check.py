"""Tests for executor.prestart_check — zero prior direct coverage. Covers
all four phases: time-trigger pre-warm, scheduled_run fallback (delta=-1),
stuck-run detection, and no-active-campaign detection.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

sys.modules.setdefault("vip_shared", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.persistence", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.persistence.audit", MagicMock())

import executor  # noqa: E402

_COT = timezone(timedelta(hours=-5))


def _plan(plan_id="p1", **overrides):
    p = {"planId": plan_id, "name": "Plan", "isTemplate": False}
    p.update(overrides)
    return p


def _now_cot_time_str(delta_minutes: int) -> str:
    """Return an HH:MM string `delta_minutes` in the future relative to now (COT)."""
    now = datetime.now(_COT)
    target = now + timedelta(minutes=delta_minutes)
    return f"{target.hour:02d}:{target.minute:02d}"


@pytest.fixture(autouse=True)
def _default_mocks():
    with (
        patch("executor.boto3.client", return_value=MagicMock()),
        patch("executor._bucket_has_only_legitimate_waits", return_value=False),
    ):
        yield


class TestTimeTriggerFiltering:
    def test_skips_templates(self):
        with patch("executor.list_plans", return_value=[_plan(isTemplate=True, trigger={"type": "time", "time": "08:00"})]):
            result = executor.prestart_check()
        assert result["warmed"] == []

    def test_skips_non_time_triggers(self):
        with patch("executor.list_plans", return_value=[_plan(trigger={"type": "manual"})]):
            result = executor.prestart_check()
        assert result["warmed"] == []

    def test_skips_time_trigger_with_empty_time_string(self):
        with patch("executor.list_plans", return_value=[_plan(trigger={"type": "time", "time": ""})]):
            result = executor.prestart_check()
        assert result["warmed"] == []

    def test_swallows_error_for_one_plan_and_continues(self):
        bad_plan = _plan(plan_id="bad", trigger={"type": "time", "time": "not-a-time"})
        good_plan = _plan(plan_id="good", trigger={"type": "manual"})
        with patch("executor.list_plans", return_value=[bad_plan, good_plan]):
            result = executor.prestart_check()  # must not raise
        assert result["warmed"] == []


class TestPrewarmWindow:
    def test_warms_plan_within_prewarm_window(self):
        time_str = _now_cot_time_str(5)
        plan = _plan(trigger={"type": "time", "time": time_str})
        with (
            patch("executor.list_plans", return_value=[plan]),
            patch("executor._is_working_day", return_value=True),
            patch("executor._ensure_scheduled_run_permission") as mock_ensure,
            patch("executor._prestart_plan") as mock_prestart,
        ):
            result = executor.prestart_check()
        mock_ensure.assert_called_once_with("p1")
        mock_prestart.assert_called_once_with("p1")
        assert result["warmed"] == ["p1"]

    def test_skips_prewarm_on_non_working_day(self):
        time_str = _now_cot_time_str(5)
        plan = _plan(trigger={"type": "time", "time": time_str})
        with (
            patch("executor.list_plans", return_value=[plan]),
            patch("executor._is_working_day", return_value=False),
            patch("executor._prestart_plan") as mock_prestart,
        ):
            result = executor.prestart_check()
        mock_prestart.assert_not_called()
        assert result["warmed"] == []

    def test_does_not_warm_outside_window(self):
        time_str = _now_cot_time_str(30)  # too far away
        plan = _plan(trigger={"type": "time", "time": time_str})
        with (
            patch("executor.list_plans", return_value=[plan]),
            patch("executor._is_working_day", return_value=True),
            patch("executor._prestart_plan") as mock_prestart,
        ):
            result = executor.prestart_check()
        mock_prestart.assert_not_called()
        assert result["warmed"] == []


class TestFallbackTrigger:
    def test_skips_fallback_on_non_working_day(self):
        time_str = _now_cot_time_str(-1)
        plan = _plan(trigger={"type": "time", "time": time_str})
        with (
            patch("executor.list_plans", return_value=[plan]),
            patch("executor._is_working_day", return_value=False),
            patch("executor.scheduled_run") as mock_scheduled,
        ):
            result = executor.prestart_check()
        mock_scheduled.assert_not_called()
        assert result["fallback_triggered"] == []

    def test_skips_fallback_when_run_already_running(self):
        time_str = _now_cot_time_str(-1)
        plan = _plan(trigger={"type": "time", "time": time_str})
        with (
            patch("executor.list_plans", return_value=[plan]),
            patch("executor._is_working_day", return_value=True),
            patch("executor.get_latest_run", return_value={"status": "running"}),
            patch("executor.scheduled_run") as mock_scheduled,
        ):
            result = executor.prestart_check()
        mock_scheduled.assert_not_called()
        assert result["fallback_triggered"] == []

    def test_skips_fallback_when_run_started_recently(self):
        time_str = _now_cot_time_str(-1)
        plan = _plan(trigger={"type": "time", "time": time_str})
        recent_start = executor._now_utc().isoformat()
        with (
            patch("executor.list_plans", return_value=[plan]),
            patch("executor._is_working_day", return_value=True),
            patch(
                "executor.get_latest_run",
                return_value={"status": "completed", "startedAt": recent_start},
            ),
            patch("executor.scheduled_run") as mock_scheduled,
        ):
            result = executor.prestart_check()
        mock_scheduled.assert_not_called()
        assert result["fallback_triggered"] == []

    def test_ignores_malformed_started_at_and_still_triggers_fallback(self):
        time_str = _now_cot_time_str(-1)
        plan = _plan(trigger={"type": "time", "time": time_str})
        with (
            patch("executor.list_plans", return_value=[plan]),
            patch("executor._is_working_day", return_value=True),
            patch(
                "executor.get_latest_run",
                return_value={"status": "completed", "startedAt": "not-a-date"},
            ),
            patch("executor.scheduled_run") as mock_scheduled,
        ):
            result = executor.prestart_check()
        mock_scheduled.assert_called_once_with("p1")
        assert result["fallback_triggered"] == ["p1"]

    def test_triggers_fallback_when_no_recent_run(self):
        time_str = _now_cot_time_str(-1)
        plan = _plan(trigger={"type": "time", "time": time_str})
        with (
            patch("executor.list_plans", return_value=[plan]),
            patch("executor._is_working_day", return_value=True),
            patch("executor.get_latest_run", return_value=None),
            patch("executor.scheduled_run") as mock_scheduled,
        ):
            result = executor.prestart_check()
        mock_scheduled.assert_called_once_with("p1")
        assert result["fallback_triggered"] == ["p1"]

    def test_swallows_cloudwatch_metric_failure_during_fallback(self):
        time_str = _now_cot_time_str(-1)
        plan = _plan(trigger={"type": "time", "time": time_str})
        mock_cw = MagicMock()
        mock_cw.put_metric_data.side_effect = RuntimeError("CloudWatch down")
        with (
            patch("executor.boto3.client", return_value=mock_cw),
            patch("executor._bucket_has_only_legitimate_waits", return_value=False),
            patch("executor.list_plans", return_value=[plan]),
            patch("executor._is_working_day", return_value=True),
            patch("executor.get_latest_run", return_value=None),
            patch("executor.scheduled_run") as mock_scheduled,
        ):
            result = executor.prestart_check()  # must not raise
        mock_scheduled.assert_called_once_with("p1")


class TestStuckRunDetection:
    def test_skips_plan_without_plan_id(self):
        with patch("executor.list_plans", return_value=[{"name": "no id"}]):
            result = executor.prestart_check()
        assert result["stuck"] == []

    def test_skips_when_get_latest_run_raises(self):
        with (
            patch("executor.list_plans", return_value=[_plan()]),
            patch("executor.get_latest_run", side_effect=RuntimeError("DDB down")),
        ):
            result = executor.prestart_check()  # must not raise
        assert result["stuck"] == []

    def test_skips_run_without_started_at(self):
        with (
            patch("executor.list_plans", return_value=[_plan()]),
            patch("executor.get_latest_run", return_value={"status": "running", "runId": "r1"}),
        ):
            result = executor.prestart_check()
        assert result["stuck"] == []

    def test_detects_stuck_run_and_emits_metric(self):
        stale_started = (executor._now_utc() - timedelta(hours=5)).isoformat()
        mock_cw = MagicMock()
        with (
            patch("executor.boto3.client", return_value=mock_cw),
            patch("executor._bucket_has_only_legitimate_waits", return_value=False),
            patch("executor.list_plans", return_value=[_plan()]),
            patch(
                "executor.get_latest_run",
                return_value={"status": "running", "runId": "r1", "startedAt": stale_started},
            ),
        ):
            result = executor.prestart_check()
        assert result["stuck"] == ["p1"]
        metric_names = {
            m["MetricName"]
            for call in mock_cw.put_metric_data.call_args_list
            for m in call.kwargs["MetricData"]
        }
        assert "StuckRun" in metric_names

    def test_swallows_cloudwatch_error_for_stuck_run_metric(self):
        stale_started = (executor._now_utc() - timedelta(hours=5)).isoformat()
        mock_cw = MagicMock()
        mock_cw.put_metric_data.side_effect = RuntimeError("CloudWatch down")
        with (
            patch("executor.boto3.client", return_value=mock_cw),
            patch("executor._bucket_has_only_legitimate_waits", return_value=False),
            patch("executor.list_plans", return_value=[_plan()]),
            patch(
                "executor.get_latest_run",
                return_value={"status": "running", "runId": "r1", "startedAt": stale_started},
            ),
        ):
            result = executor.prestart_check()  # must not raise
        assert result["stuck"] == ["p1"]

    def test_not_stuck_when_under_threshold(self):
        recent_started = (executor._now_utc() - timedelta(hours=1)).isoformat()
        with (
            patch("executor.list_plans", return_value=[_plan()]),
            patch(
                "executor.get_latest_run",
                return_value={"status": "running", "runId": "r1", "startedAt": recent_started},
            ),
        ):
            result = executor.prestart_check()
        assert result["stuck"] == []


class TestNoActiveCampaignDetection:
    def test_skips_plan_without_plan_id(self):
        with patch("executor.list_plans", return_value=[{"name": "no id"}]):
            result = executor.prestart_check()
        assert result["no_active_campaign"] == []

    def test_skips_when_get_latest_run_raises(self):
        with (
            patch("executor.list_plans", return_value=[_plan()]),
            patch("executor.get_latest_run", side_effect=RuntimeError("DDB down")),
        ):
            result = executor.prestart_check()  # must not raise
        assert result["no_active_campaign"] == []

    def test_skips_bucket_without_started_at(self):
        run = {
            "status": "running",
            "runId": "r1",
            "bucketStates": [{"status": "running", "campaignStates": []}],
        }
        with (
            patch("executor.list_plans", return_value=[_plan()]),
            patch("executor.get_latest_run", return_value=run),
        ):
            result = executor.prestart_check()
        assert result["no_active_campaign"] == []

    def test_detects_no_active_campaign_and_emits_metric(self):
        stale_started = (executor._now_utc() - timedelta(minutes=10)).isoformat()
        run = {
            "status": "running",
            "runId": "r1",
            "bucketStates": [
                {
                    "status": "running",
                    "bucketId": "b0",
                    "startedAt": stale_started,
                    "campaignStates": [{"status": "completed"}],
                }
            ],
        }
        mock_cw = MagicMock()
        with (
            patch("executor.boto3.client", return_value=mock_cw),
            patch("executor._bucket_has_only_legitimate_waits", return_value=False),
            patch("executor.list_plans", return_value=[_plan()]),
            patch("executor.get_latest_run", return_value=run),
        ):
            result = executor.prestart_check()
        assert result["no_active_campaign"] == ["p1"]
        metric_names = {
            m["MetricName"]
            for call in mock_cw.put_metric_data.call_args_list
            for m in call.kwargs["MetricData"]
        }
        assert "NoActiveCampaign" in metric_names

    def test_swallows_cloudwatch_error_for_no_active_campaign_metric(self):
        stale_started = (executor._now_utc() - timedelta(minutes=10)).isoformat()
        run = {
            "status": "running",
            "runId": "r1",
            "bucketStates": [
                {
                    "status": "running",
                    "bucketId": "b0",
                    "startedAt": stale_started,
                    "campaignStates": [],
                }
            ],
        }
        mock_cw = MagicMock()
        mock_cw.put_metric_data.side_effect = RuntimeError("CloudWatch down")
        with (
            patch("executor.boto3.client", return_value=mock_cw),
            patch("executor._bucket_has_only_legitimate_waits", return_value=False),
            patch("executor.list_plans", return_value=[_plan()]),
            patch("executor.get_latest_run", return_value=run),
        ):
            result = executor.prestart_check()  # must not raise
        assert result["no_active_campaign"] == ["p1"]

    def test_skips_when_active_campaign_status_present(self):
        recent_started = (executor._now_utc() - timedelta(minutes=10)).isoformat()
        run = {
            "status": "running",
            "runId": "r1",
            "bucketStates": [
                {
                    "status": "running",
                    "bucketId": "b0",
                    "startedAt": recent_started,
                    "campaignStates": [{"status": "running"}],
                }
            ],
        }
        with (
            patch("executor.list_plans", return_value=[_plan()]),
            patch("executor.get_latest_run", return_value=run),
        ):
            result = executor.prestart_check()
        assert result["no_active_campaign"] == []

    def test_skips_when_only_legitimate_waits(self):
        stale_started = (executor._now_utc() - timedelta(minutes=10)).isoformat()
        run = {
            "status": "running",
            "runId": "r1",
            "bucketStates": [
                {
                    "status": "running",
                    "bucketId": "b0",
                    "startedAt": stale_started,
                    "campaignStates": [{"status": "queued"}],
                }
            ],
        }
        with (
            patch("executor._bucket_has_only_legitimate_waits", return_value=True),
            patch("executor.list_plans", return_value=[_plan()]),
            patch("executor.get_latest_run", return_value=run),
        ):
            result = executor.prestart_check()
        assert result["no_active_campaign"] == []

    def test_skips_bucket_under_grace_period(self):
        fresh_started = executor._now_utc().isoformat()
        run = {
            "status": "running",
            "runId": "r1",
            "bucketStates": [
                {
                    "status": "running",
                    "bucketId": "b0",
                    "startedAt": fresh_started,
                    "campaignStates": [],
                }
            ],
        }
        with (
            patch("executor.list_plans", return_value=[_plan()]),
            patch("executor.get_latest_run", return_value=run),
        ):
            result = executor.prestart_check()
        assert result["no_active_campaign"] == []
