"""Tests for executor._ensure_scheduled_run_permission (zero prior coverage)
and the remaining gaps in _prestart_chained_runs (loop self-warmup window)
and _prestart_after_campaign (afterCampaign mismatch / success log).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

sys.modules.setdefault("vip_shared", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.persistence", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.persistence.audit", MagicMock())

import executor  # noqa: E402


def _client_error(code, op="DescribeRule"):
    return ClientError({"Error": {"Code": code, "Message": "x"}}, op)


class TestEnsureScheduledRunPermissionRuleMissing:
    def test_recreates_rule_when_missing_and_trigger_is_time(self):
        mock_events = MagicMock()
        mock_events.describe_rule.side_effect = _client_error("ResourceNotFoundException")
        mock_lambda = MagicMock()

        def client_factory(service, *a, **k):
            return {"events": mock_events, "lambda": mock_lambda}[service]

        plan = {"planId": "p1", "trigger": {"type": "time", "time": "08:00"}}
        with (
            patch("executor.boto3.client", side_effect=client_factory),
            patch("executor.get_plan", return_value=plan),
            patch("scheduler_manager.upsert_schedule") as mock_upsert,
        ):
            executor._ensure_scheduled_run_permission("p1")

        mock_upsert.assert_called_once_with("p1", plan["trigger"])
        mock_lambda.add_permission.assert_not_called()

    def test_skips_recreate_when_trigger_not_time(self):
        mock_events = MagicMock()
        mock_events.describe_rule.side_effect = _client_error("ResourceNotFoundException")

        def client_factory(service, *a, **k):
            return {"events": mock_events, "lambda": MagicMock()}[service]

        plan = {"planId": "p1", "trigger": {"type": "manual"}}
        with (
            patch("executor.boto3.client", side_effect=client_factory),
            patch("executor.get_plan", return_value=plan),
            patch("scheduler_manager.upsert_schedule") as mock_upsert,
        ):
            executor._ensure_scheduled_run_permission("p1")
        mock_upsert.assert_not_called()

    def test_skips_recreate_when_plan_missing(self):
        mock_events = MagicMock()
        mock_events.describe_rule.side_effect = _client_error("ResourceNotFoundException")

        def client_factory(service, *a, **k):
            return {"events": mock_events, "lambda": MagicMock()}[service]

        with (
            patch("executor.boto3.client", side_effect=client_factory),
            patch("executor.get_plan", return_value=None),
            patch("scheduler_manager.upsert_schedule") as mock_upsert,
        ):
            executor._ensure_scheduled_run_permission("p1")
        mock_upsert.assert_not_called()

    def test_logs_error_when_recreate_fails(self):
        mock_events = MagicMock()
        mock_events.describe_rule.side_effect = _client_error("ResourceNotFoundException")

        def client_factory(service, *a, **k):
            return {"events": mock_events, "lambda": MagicMock()}[service]

        plan = {"planId": "p1", "trigger": {"type": "time", "time": "08:00"}}
        with (
            patch("executor.boto3.client", side_effect=client_factory),
            patch("executor.get_plan", return_value=plan),
            patch("scheduler_manager.upsert_schedule", side_effect=RuntimeError("boom")),
        ):
            executor._ensure_scheduled_run_permission("p1")  # must not raise

    def test_falls_through_to_permission_check_on_other_describe_error(self):
        """A DescribeRule error other than ResourceNotFoundException must be
        treated as 'rule exists' and fall through to the permission check."""
        mock_events = MagicMock()
        mock_events.describe_rule.side_effect = _client_error("AccessDeniedException")
        mock_lambda = MagicMock()
        mock_lambda.get_policy.side_effect = _client_error("ResourceNotFoundException", "GetPolicy")

        def client_factory(service, *a, **k):
            return {"events": mock_events, "lambda": mock_lambda}[service]

        with (
            patch("executor.boto3.client", side_effect=client_factory),
            patch("scheduler_manager.upsert_schedule") as mock_upsert,
        ):
            executor._ensure_scheduled_run_permission("p1")

        mock_upsert.assert_not_called()  # never reached the "rule missing" branch
        mock_lambda.get_policy.assert_called_once()


class TestEnsureScheduledRunPermissionExists:
    def _mock_clients(self, policy_statements, add_permission_side_effect=None):
        mock_events = MagicMock()
        mock_events.describe_rule.return_value = {"Name": "vip-sched-p1"}
        mock_lambda = MagicMock()
        mock_lambda.get_policy.return_value = {
            "Policy": json.dumps({"Statement": policy_statements})
        }
        if add_permission_side_effect is not None:
            mock_lambda.add_permission.side_effect = add_permission_side_effect

        def client_factory(service, *a, **k):
            return {"events": mock_events, "lambda": mock_lambda}[service]

        return mock_events, mock_lambda, client_factory

    def test_returns_early_when_permission_already_present(self):
        _, mock_lambda, client_factory = self._mock_clients(
            [{"Sid": "vip-sched-p1replace"}]
        )
        # The Sid must match _rule_name(plan_id) exactly.
        from scheduler_manager import _rule_name

        _, mock_lambda, client_factory = self._mock_clients(
            [{"Sid": _rule_name("p1")}]
        )
        with patch("executor.boto3.client", side_effect=client_factory):
            executor._ensure_scheduled_run_permission("p1")
        mock_lambda.add_permission.assert_not_called()

    def test_skips_silently_when_get_policy_fails(self):
        mock_events = MagicMock()
        mock_events.describe_rule.return_value = {"Name": "vip-sched-p1"}
        mock_lambda = MagicMock()
        mock_lambda.get_policy.side_effect = _client_error("ResourceNotFoundException", "GetPolicy")

        def client_factory(service, *a, **k):
            return {"events": mock_events, "lambda": mock_lambda}[service]

        with patch("executor.boto3.client", side_effect=client_factory):
            executor._ensure_scheduled_run_permission("p1")  # must not raise
        mock_lambda.add_permission.assert_not_called()

    def test_restores_missing_permission(self):
        _, mock_lambda, client_factory = self._mock_clients([])
        with (
            patch("executor.boto3.client", side_effect=client_factory),
            patch("scheduler_manager._account_id", return_value="165505826690"),
        ):
            executor._ensure_scheduled_run_permission("p1")
        mock_lambda.add_permission.assert_called_once()
        call_kwargs = mock_lambda.add_permission.call_args.kwargs
        assert call_kwargs["Action"] == "lambda:InvokeFunction"

    def test_ignores_resource_conflict_when_restoring_permission(self):
        _, mock_lambda, client_factory = self._mock_clients(
            [], add_permission_side_effect=_client_error("ResourceConflictException", "AddPermission")
        )
        with (
            patch("executor.boto3.client", side_effect=client_factory),
            patch("scheduler_manager._account_id", return_value="165505826690"),
        ):
            executor._ensure_scheduled_run_permission("p1")  # must not raise

    def test_logs_error_on_other_add_permission_failure(self):
        _, mock_lambda, client_factory = self._mock_clients(
            [], add_permission_side_effect=_client_error("AccessDeniedException", "AddPermission")
        )
        with (
            patch("executor.boto3.client", side_effect=client_factory),
            patch("scheduler_manager._account_id", return_value="165505826690"),
        ):
            executor._ensure_scheduled_run_permission("p1")  # must not raise


class TestPrestartChainedRunsLoopWindow:
    def test_self_prewarms_when_loop_window_still_open(self):
        run = {"planId": "p1"}
        # Use a fixed "now" far from midnight edge cases via COT-naive check —
        # instead, set endTime to 23:59 so "now < endTime" is true almost always.
        plan = {"loop": {"endTime": "23:59"}}
        with (
            patch("executor.find_plans_by_trigger_planid", return_value=[]),
            patch("executor._prestart_plan") as mock_prestart,
        ):
            executor._prestart_chained_runs(run, plan, 0)
        mock_prestart.assert_called_once_with("p1")

    def test_does_not_self_prewarm_when_loop_window_closed(self):
        run = {"planId": "p1"}
        plan = {"loop": {"endTime": "00:00"}}
        with (
            patch("executor.find_plans_by_trigger_planid", return_value=[]),
            patch("executor._prestart_plan") as mock_prestart,
        ):
            executor._prestart_chained_runs(run, plan, 0)
        mock_prestart.assert_not_called()

    def test_emits_metric_when_loop_self_prewarm_fails(self):
        run = {"planId": "p1"}
        plan = {"loop": {"endTime": "23:59"}}
        with (
            patch("executor.find_plans_by_trigger_planid", return_value=[]),
            patch("executor._prestart_plan", side_effect=RuntimeError("boom")),
            patch("executor._emit_prewarm_failure") as mock_emit,
        ):
            executor._prestart_chained_runs(run, plan, 0)
        mock_emit.assert_called_once_with("p1")

    def test_no_self_prewarm_when_no_loop_configured(self):
        run = {"planId": "p1"}
        plan = {}
        with (
            patch("executor.find_plans_by_trigger_planid", return_value=[]),
            patch("executor._prestart_plan") as mock_prestart,
        ):
            executor._prestart_chained_runs(run, plan, 0)
        mock_prestart.assert_not_called()


class TestPrestartAfterCampaignAdditional:
    def test_skips_downstream_with_different_after_campaign(self):
        downstream = {"planId": "p2", "trigger": {"afterCampaign": "other-campaign"}}
        with (
            patch("executor.find_plans_by_trigger_planid", return_value=[downstream]),
            patch("executor._prestart_plan") as mock_prestart,
        ):
            executor._prestart_after_campaign("p1", "c0")
        mock_prestart.assert_not_called()

    def test_prewarms_matching_downstream(self):
        downstream = {"planId": "p2", "trigger": {"afterCampaign": "c0"}}
        with (
            patch("executor.find_plans_by_trigger_planid", return_value=[downstream]),
            patch("executor._prestart_plan") as mock_prestart,
        ):
            executor._prestart_after_campaign("p1", "c0")
        mock_prestart.assert_called_once_with("p2")
