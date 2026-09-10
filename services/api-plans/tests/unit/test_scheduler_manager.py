"""Tests for scheduler_manager.py — EventBridge Rules management for plan
daily schedules (upsert_schedule/delete_schedule + their helpers)."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

import scheduler_manager as sm  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_account_id_cache():
    sm._cached_account_id = None
    yield
    sm._cached_account_id = None


class TestAccountId:
    def test_fetches_and_caches_account_id(self):
        mock_sts = MagicMock()
        mock_sts.get_caller_identity.return_value = {"Account": "165505826690"}
        with patch("boto3.client", return_value=mock_sts) as mock_boto:
            first = sm._account_id()
            second = sm._account_id()
        mock_boto.assert_called_once_with("sts")
        assert first == "165505826690"
        assert second == "165505826690"
        mock_sts.get_caller_identity.assert_called_once()


class TestRuleName:
    def test_strips_dashes_and_truncates_to_20_chars(self):
        plan_id = "abcdefgh-1234-5678-9012-abcdefabcdef"
        rule_name = sm._rule_name(plan_id)
        assert rule_name.startswith("vip-sched-")
        assert len(rule_name) == len("vip-sched-") + 20
        assert "-" not in rule_name[len("vip-sched-"):]


class TestBuildCron:
    def test_uses_provided_timezone(self):
        cron = sm._build_cron(hour=8, minute=30, timezone="America/Bogota", days=["MON", "TUE"])
        assert cron.startswith("cron(")
        assert "MON,TUE" in cron

    def test_falls_back_to_default_timezone_on_invalid_timezone(self):
        # Must not raise even with a bogus IANA timezone name.
        cron = sm._build_cron(hour=9, minute=0, timezone="Not/ARealZone", days=[])
        assert cron.startswith("cron(")

    def test_defaults_days_to_mon_sun_when_empty(self):
        cron = sm._build_cron(hour=8, minute=0, timezone="America/Bogota", days=[])
        assert "MON-SUN" in cron


class TestParseTrigger:
    def test_parses_new_time_format(self):
        hour, minute, tz, days = sm._parse_trigger({"type": "time", "time": "14:30"})
        assert (hour, minute) == (14, 30)
        assert tz == sm.DEFAULT_TIMEZONE
        assert days == ["MON-SUN"]

    def test_defaults_time_when_missing(self):
        hour, minute, tz, days = sm._parse_trigger({"type": "time"})
        assert (hour, minute) == (8, 0)

    def test_parses_legacy_format(self):
        hour, minute, tz, days = sm._parse_trigger(
            {"hour": 7, "minute": 15, "timezone": "America/New_York", "days": ["SAT"]}
        )
        assert (hour, minute) == (7, 15)
        assert tz == "America/New_York"
        assert days == ["SAT"]

    def test_legacy_format_defaults_timezone_and_days(self):
        hour, minute, tz, days = sm._parse_trigger({"hour": 7, "minute": 15})
        assert tz == sm.DEFAULT_TIMEZONE
        assert days == ["MON", "TUE", "WED", "THU", "FRI"]


class TestUpsertSchedule:
    def test_creates_rule_and_target_and_permission(self, monkeypatch):
        monkeypatch.setattr(sm, "LAMBDA_FUNCTION_ARN", "arn:aws:lambda:us-east-1:123:function:api-plans")
        mock_events = MagicMock()
        mock_lambda = MagicMock()
        mock_sts = MagicMock()
        mock_sts.get_caller_identity.return_value = {"Account": "123"}

        def client_factory(service, *a, **k):
            return {"events": mock_events, "lambda": mock_lambda, "sts": mock_sts}[service]

        with patch("boto3.client", side_effect=client_factory):
            sm.upsert_schedule("plan-1", {"type": "time", "time": "08:00"})

        mock_events.put_rule.assert_called_once()
        put_rule_kwargs = mock_events.put_rule.call_args.kwargs
        assert put_rule_kwargs["Name"] == sm._rule_name("plan-1")
        assert put_rule_kwargs["State"] == "ENABLED"

        mock_events.put_targets.assert_called_once()
        target = mock_events.put_targets.call_args.kwargs["Targets"][0]
        assert target["Arn"] == "arn:aws:lambda:us-east-1:123:function:api-plans"
        assert target["RetryPolicy"] == {
            "MaximumRetryAttempts": 2,
            "MaximumEventAgeInSeconds": 300,
        }

        mock_lambda.add_permission.assert_called_once()
        add_perm_kwargs = mock_lambda.add_permission.call_args.kwargs
        assert add_perm_kwargs["SourceArn"] == f"arn:aws:events:us-east-1:123:rule/{sm._rule_name('plan-1')}"

    def test_swallows_resource_conflict_on_add_permission(self, monkeypatch):
        """A permission that already exists (ResourceConflictException) must
        not fail the upsert — the rule/target update already succeeded."""
        monkeypatch.setattr(sm, "LAMBDA_FUNCTION_ARN", "arn:aws:lambda:us-east-1:123:function:api-plans")
        mock_events = MagicMock()
        mock_lambda = MagicMock()
        mock_lambda.add_permission.side_effect = ClientError(
            {"Error": {"Code": "ResourceConflictException", "Message": "exists"}},
            "AddPermission",
        )
        mock_sts = MagicMock()
        mock_sts.get_caller_identity.return_value = {"Account": "123"}

        def client_factory(service, *a, **k):
            return {"events": mock_events, "lambda": mock_lambda, "sts": mock_sts}[service]

        with patch("boto3.client", side_effect=client_factory):
            sm.upsert_schedule("plan-1", {"type": "time", "time": "08:00"})  # must not raise

    def test_reraises_non_conflict_error_on_add_permission(self, monkeypatch):
        monkeypatch.setattr(sm, "LAMBDA_FUNCTION_ARN", "arn:aws:lambda:us-east-1:123:function:api-plans")
        mock_events = MagicMock()
        mock_lambda = MagicMock()
        mock_lambda.add_permission.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
            "AddPermission",
        )
        mock_sts = MagicMock()
        mock_sts.get_caller_identity.return_value = {"Account": "123"}

        def client_factory(service, *a, **k):
            return {"events": mock_events, "lambda": mock_lambda, "sts": mock_sts}[service]

        with patch("boto3.client", side_effect=client_factory):
            with pytest.raises(ClientError, match="AccessDeniedException"):
                sm.upsert_schedule("plan-1", {"type": "time", "time": "08:00"})


class TestDeleteSchedule:
    def test_removes_targets_rule_and_permission(self, monkeypatch):
        monkeypatch.setattr(sm, "LAMBDA_FUNCTION_ARN", "arn:aws:lambda:us-east-1:123:function:api-plans")
        mock_events = MagicMock()
        mock_lambda = MagicMock()

        def client_factory(service, *a, **k):
            return {"events": mock_events, "lambda": mock_lambda}[service]

        with patch("boto3.client", side_effect=client_factory):
            sm.delete_schedule("plan-1")

        mock_events.remove_targets.assert_called_once_with(
            Rule=sm._rule_name("plan-1"), Ids=["lambda"]
        )
        mock_events.delete_rule.assert_called_once_with(Name=sm._rule_name("plan-1"))
        mock_lambda.remove_permission.assert_called_once_with(
            FunctionName="arn:aws:lambda:us-east-1:123:function:api-plans",
            StatementId=sm._rule_name("plan-1"),
        )

    def test_is_idempotent_when_resources_already_gone(self):
        """ResourceNotFoundException/NoSuchEntity on any of the three calls
        must be swallowed — delete_schedule is expected to be safely
        re-callable on an already-deleted schedule."""
        mock_events = MagicMock()
        mock_events.remove_targets.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}},
            "RemoveTargets",
        )
        mock_events.delete_rule.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}},
            "DeleteRule",
        )
        mock_lambda = MagicMock()
        mock_lambda.remove_permission.side_effect = ClientError(
            {"Error": {"Code": "NoSuchEntity", "Message": "gone"}},
            "RemovePermission",
        )

        def client_factory(service, *a, **k):
            return {"events": mock_events, "lambda": mock_lambda}[service]

        with patch("boto3.client", side_effect=client_factory):
            sm.delete_schedule("plan-1")  # must not raise

    def test_logs_but_does_not_raise_on_unexpected_error_code(self):
        """A non-idempotent-safe error code (e.g. AccessDenied) is logged as
        a warning but delete_schedule still doesn't raise — it always
        attempts all three cleanup calls best-effort."""
        mock_events = MagicMock()
        mock_events.remove_targets.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
            "RemoveTargets",
        )
        mock_lambda = MagicMock()

        def client_factory(service, *a, **k):
            return {"events": mock_events, "lambda": mock_lambda}[service]

        with patch("boto3.client", side_effect=client_factory):
            sm.delete_schedule("plan-1")  # must not raise

        mock_events.delete_rule.assert_called_once()
