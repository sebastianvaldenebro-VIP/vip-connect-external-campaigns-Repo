"""Targeted tests for executor._cleanup_orphan_plan_permissions, _sweep_orphan_rules,
and janitor_cleanup_orphan_schedules — zero prior direct coverage (always
mocked away or simply never exercised).
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

sys.modules.setdefault("vip_shared", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.persistence", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.persistence.audit", MagicMock())

import executor  # noqa: E402, F401


def _client_error(code, op="op"):
    return ClientError({"Error": {"Code": code, "Message": "x"}}, op)


class TestCleanupOrphanPlanPermissions:
    def test_returns_early_when_get_policy_fails(self):
        mock_lambda = MagicMock()
        mock_lambda.get_policy.side_effect = _client_error("ResourceNotFoundException", "GetPolicy")
        mock_events = MagicMock()

        def client_factory(service, *a, **k):
            return {"lambda": mock_lambda, "events": mock_events}[service]

        with patch("executor.boto3.client", side_effect=client_factory):
            executor._cleanup_orphan_plan_permissions()

        mock_events.describe_rule.assert_not_called()

    def test_skips_statements_not_matching_vip_plan_prefix(self):
        mock_lambda = MagicMock()
        mock_lambda.get_policy.return_value = {
            "Policy": json.dumps({"Statement": [{"Sid": "some-other-sid"}]})
        }
        mock_events = MagicMock()

        def client_factory(service, *a, **k):
            return {"lambda": mock_lambda, "events": mock_events}[service]

        with patch("executor.boto3.client", side_effect=client_factory):
            executor._cleanup_orphan_plan_permissions()

        mock_events.describe_rule.assert_not_called()
        mock_lambda.remove_permission.assert_not_called()

    def test_removes_permission_when_rule_no_longer_exists(self):
        mock_lambda = MagicMock()
        mock_lambda.get_policy.return_value = {
            "Policy": json.dumps({"Statement": [{"Sid": "vip-plan-abc123"}]})
        }
        mock_events = MagicMock()
        mock_events.describe_rule.side_effect = _client_error("ResourceNotFoundException", "DescribeRule")

        def client_factory(service, *a, **k):
            return {"lambda": mock_lambda, "events": mock_events}[service]

        with patch("executor.boto3.client", side_effect=client_factory):
            executor._cleanup_orphan_plan_permissions()

        mock_lambda.remove_permission.assert_called_once_with(
            FunctionName=executor.LAMBDA_FUNCTION_ARN, StatementId="vip-plan-abc123"
        )

    def test_leaves_permission_when_rule_still_exists(self):
        mock_lambda = MagicMock()
        mock_lambda.get_policy.return_value = {
            "Policy": json.dumps({"Statement": [{"Sid": "vip-plan-stillalive"}]})
        }
        mock_events = MagicMock()
        mock_events.describe_rule.return_value = {"Name": "vip-plan-stillalive"}

        def client_factory(service, *a, **k):
            return {"lambda": mock_lambda, "events": mock_events}[service]

        with patch("executor.boto3.client", side_effect=client_factory):
            executor._cleanup_orphan_plan_permissions()

        mock_lambda.remove_permission.assert_not_called()

    def test_other_describe_rule_error_does_not_flag_orphan(self):
        mock_lambda = MagicMock()
        mock_lambda.get_policy.return_value = {
            "Policy": json.dumps({"Statement": [{"Sid": "vip-plan-transient"}]})
        }
        mock_events = MagicMock()
        mock_events.describe_rule.side_effect = _client_error("AccessDeniedException", "DescribeRule")

        def client_factory(service, *a, **k):
            return {"lambda": mock_lambda, "events": mock_events}[service]

        with patch("executor.boto3.client", side_effect=client_factory):
            executor._cleanup_orphan_plan_permissions()

        mock_lambda.remove_permission.assert_not_called()

    def test_logs_warning_when_remove_permission_fails(self):
        mock_lambda = MagicMock()
        mock_lambda.get_policy.return_value = {
            "Policy": json.dumps({"Statement": [{"Sid": "vip-plan-fail1"}]})
        }
        mock_lambda.remove_permission.side_effect = _client_error("AccessDeniedException", "RemovePermission")
        mock_events = MagicMock()
        mock_events.describe_rule.side_effect = _client_error("ResourceNotFoundException", "DescribeRule")

        def client_factory(service, *a, **k):
            return {"lambda": mock_lambda, "events": mock_events}[service]

        with patch("executor.boto3.client", side_effect=client_factory):
            executor._cleanup_orphan_plan_permissions()  # must not raise


class TestSweepOrphanRules:
    def test_deletes_unprotected_rules_and_skips_protected(self):
        events = MagicMock()
        events.list_rules.return_value = {
            "Rules": [{"Name": "vip-plan-a"}, {"Name": "vip-plan-b"}]
        }
        with patch("executor._delete_schedule_safe") as mock_delete:
            deleted, failed = executor._sweep_orphan_rules(events, "vip-plan-", {"vip-plan-b"})

        assert deleted == ["vip-plan-a"]
        assert failed == []
        mock_delete.assert_called_once_with("vip-plan-a")

    def test_paginates_via_next_token(self):
        events = MagicMock()
        events.list_rules.side_effect = [
            {"Rules": [{"Name": "vip-plan-page1"}], "NextToken": "tok2"},
            {"Rules": [{"Name": "vip-plan-page2"}]},
        ]
        with patch("executor._delete_schedule_safe"):
            deleted, failed = executor._sweep_orphan_rules(events, "vip-plan-", set())

        assert deleted == ["vip-plan-page1", "vip-plan-page2"]
        assert events.list_rules.call_count == 2
        assert events.list_rules.call_args_list[1].kwargs["NextToken"] == "tok2"

    def test_records_failure_when_delete_raises(self):
        events = MagicMock()
        events.list_rules.return_value = {"Rules": [{"Name": "vip-plan-bad"}]}
        with patch(
            "executor._delete_schedule_safe", side_effect=RuntimeError("EventBridge down")
        ):
            deleted, failed = executor._sweep_orphan_rules(events, "vip-plan-", set())

        assert deleted == []
        assert failed == ["vip-plan-bad"]


class TestJanitorCleanupOrphanSchedules:
    def test_skips_plan_without_plan_id(self):
        with (
            patch("executor.list_plans", return_value=[{"name": "no id"}]),
            patch("executor.boto3.client", return_value=MagicMock()),
            patch("executor._sweep_orphan_rules", return_value=([], [])),
        ):
            result = executor.janitor_cleanup_orphan_schedules()
        assert result == {"deleted": [], "failed": []}

    def test_swallows_get_latest_run_exception(self):
        plan = {"planId": "p1", "isTemplate": False, "trigger": {"type": "manual"}}
        with (
            patch("executor.list_plans", return_value=[plan]),
            patch("executor.get_latest_run", side_effect=RuntimeError("DDB down")),
            patch("executor.boto3.client", return_value=MagicMock()),
            patch("executor._sweep_orphan_rules", return_value=([], [])),
        ):
            result = executor.janitor_cleanup_orphan_schedules()  # must not raise
        assert result == {"deleted": [], "failed": []}

    def test_protects_time_trigger_schedule_and_active_bucket_schedule(self):
        plan = {"planId": "p1", "isTemplate": False, "trigger": {"type": "time", "time": "08:00"}}
        run = {
            "status": "running",
            "bucketStates": [{"scheduleName": "vip-plan-p1-run-r1-b0"}],
        }
        captured = {}

        def fake_sweep(events, prefix, protected):
            captured[prefix] = protected
            return [], []

        with (
            patch("executor.list_plans", return_value=[plan]),
            patch("executor.get_latest_run", return_value=run),
            patch("executor.boto3.client", return_value=MagicMock()),
            patch("executor._sweep_orphan_rules", side_effect=fake_sweep),
            patch("scheduler_manager._rule_name", return_value="vip-sched-p1"),
        ):
            executor.janitor_cleanup_orphan_schedules()

        assert "vip-plan-p1-run-r1-b0" in captured["vip-plan-"]
        assert "vip-sched-p1" in captured["vip-sched-"]

    def test_notifies_sns_when_rules_deleted(self):
        plan = {"planId": "p1", "isTemplate": True}
        with (
            patch("executor.list_plans", return_value=[plan]),
            patch("executor.boto3.client", return_value=MagicMock()),
            patch(
                "executor._sweep_orphan_rules",
                side_effect=[(["vip-plan-a"], []), ([], [])],
            ),
            patch("executor._notify_sns") as mock_notify,
        ):
            result = executor.janitor_cleanup_orphan_schedules()

        assert result["deleted"] == ["vip-plan-a"]
        mock_notify.assert_called_once()
        assert mock_notify.call_args.kwargs["attributes"]["alertType"] == "janitor_cleanup"

    def test_logs_error_when_deletes_fail(self):
        plan = {"planId": "p1", "isTemplate": True}
        with (
            patch("executor.list_plans", return_value=[plan]),
            patch("executor.boto3.client", return_value=MagicMock()),
            patch(
                "executor._sweep_orphan_rules",
                side_effect=[([], ["vip-plan-bad"]), ([], [])],
            ),
        ):
            result = executor.janitor_cleanup_orphan_schedules()  # must not raise

        assert result["failed"] == ["vip-plan-bad"]
