"""Final remaining gaps in executor.py's small utility functions —
_check_native_queue_collision's empty-queueId guard, _emit_queue_collision_metric
(success + exception-swallow), _notify_sns (success + exception-swallow, always
mocked away elsewhere), _future_iso and _campaign_end_time's custom/legacy
run_type branches (never called directly), and _within_working_hours' non-
working-day branch.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

sys.modules.setdefault("vip_shared", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.persistence", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.persistence.audit", MagicMock())

import executor  # noqa: E402


class TestCheckNativeQueueCollisionEmptyQueueId:
    def test_returns_early_when_queue_id_missing(self):
        bucket = {"campaignConfig": {}}
        campaign = {"campaignConfig": {}}  # no queueId anywhere
        with (
            patch("executor._ACTIVE_BRANDED_CAMPAIGNS_TABLE", "VipActiveBrandedCampaigns"),
            patch("executor.CONNECT_INSTANCE_ID", "instance-1"),
            patch("executor._get_ddb_client") as mock_get_ddb,
        ):
            executor._check_native_queue_collision(bucket, campaign, "p1", "r1")
        mock_get_ddb.assert_not_called()


class TestEmitQueueCollisionMetric:
    def test_puts_metric_data_successfully(self):
        mock_cw = MagicMock()
        with patch("executor.boto3.client", return_value=mock_cw):
            executor._emit_queue_collision_metric("q-1")
        mock_cw.put_metric_data.assert_called_once()
        assert mock_cw.put_metric_data.call_args.kwargs["Namespace"] == "VIPPlans"

    def test_swallows_exception(self):
        with patch("executor.boto3.client", side_effect=RuntimeError("CloudWatch down")):
            executor._emit_queue_collision_metric("q-1")  # must not raise


class TestNotifySnsRealBody:
    def test_publishes_when_topic_configured(self):
        mock_sns = MagicMock()
        with (
            patch("executor.SNS_ALERTS_TOPIC_ARN", "arn:aws:sns:us-east-1:123:vip-plans-alerts"),
            patch("executor.boto3.client", return_value=mock_sns),
        ):
            executor._notify_sns(
                subject="Test subject",
                detail="Test detail",
                attributes={"PlanId": "p1", "RunId": "r1"},
            )
        mock_sns.publish.assert_called_once()
        call_kwargs = mock_sns.publish.call_args.kwargs
        assert call_kwargs["Subject"] == "Test subject"
        assert call_kwargs["MessageAttributes"]["PlanId"] == {
            "DataType": "String",
            "StringValue": "p1",
        }

    def test_publishes_with_no_attributes(self):
        mock_sns = MagicMock()
        with (
            patch("executor.SNS_ALERTS_TOPIC_ARN", "arn:aws:sns:us-east-1:123:vip-plans-alerts"),
            patch("executor.boto3.client", return_value=mock_sns),
        ):
            executor._notify_sns(subject="Test subject", detail="Test detail")
        mock_sns.publish.assert_called_once()
        assert mock_sns.publish.call_args.kwargs["MessageAttributes"] == {}

    def test_swallows_publish_exception(self):
        with (
            patch("executor.SNS_ALERTS_TOPIC_ARN", "arn:aws:sns:us-east-1:123:vip-plans-alerts"),
            patch("executor.boto3.client", side_effect=RuntimeError("SNS down")),
        ):
            executor._notify_sns(subject="s", detail="d")  # must not raise


class TestFutureIsoDirect:
    def test_adds_minutes_to_base(self):
        base = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)
        result = executor._future_iso(base, 30)
        assert result == "2026-05-19T12:30:00+00:00"


class TestCampaignEndTimeCustomAndLegacyBranches:
    def test_custom_run_type_uses_run_duration_minutes(self):
        now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)
        result = executor._campaign_end_time(
            now, {"run_duration_minutes": 45}, "custom"
        )
        assert result == "2026-05-19T12:45:00+00:00"

    def test_custom_run_type_falls_back_to_daily_cutoff_when_no_minutes(self):
        now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)
        with patch("executor._daily_cutoff_iso", return_value="fallback-cutoff") as mock_cutoff:
            result = executor._campaign_end_time(now, {}, "custom")
        assert result == "fallback-cutoff"
        mock_cutoff.assert_called_once_with(now)

    def test_legacy_run_type_uses_fixed_minutes_table(self):
        now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)
        result = executor._campaign_end_time(now, {}, "time_30")
        assert result == "2026-05-19T12:30:00+00:00"


class TestWithinWorkingHoursNonWorkingDay:
    def test_returns_false_on_disallowed_day(self):
        plan = {"workingHours": {"startTime": "08:00", "endTime": "20:00", "days": ["MON"]}}
        with patch("executor._is_working_day", return_value=False):
            assert executor._within_working_hours(plan) is False
