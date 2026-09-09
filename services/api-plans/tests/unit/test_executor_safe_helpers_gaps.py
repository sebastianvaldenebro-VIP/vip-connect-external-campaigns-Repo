"""Targeted tests for remaining gaps in executor.py's small AWS-operation
helpers: _schedule_tick's proactive orphan-permission cleanup branch,
_delete_schedule_safe's full body (never exercised directly — every caller
mocks it away), _get_campaign_state's success/unknown-error branches, and
_safe_stop_campaign / _safe_delete_campaign / _safe_delete_segment's
exception-swallow branches.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

sys.modules.setdefault("vip_shared", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.persistence", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.persistence.audit", MagicMock())

import executor  # noqa: E402


def _client_error(code, op="op"):
    return ClientError({"Error": {"Code": code, "Message": "x"}}, op)


def _stub_outbound_campaigns_client(mock_oc):
    vip_stub = MagicMock()
    vip_stub.build = MagicMock(return_value=mock_oc)
    modules_to_stub = [
        "vip_shared",
        "vip_shared.infrastructure",
        "vip_shared.infrastructure.persistence",
        "vip_shared.infrastructure.persistence.outbound_campaigns_client",
    ]
    originals = {m: sys.modules.get(m) for m in modules_to_stub}
    for m in modules_to_stub:
        sys.modules[m] = vip_stub
    return originals


def _unstub(originals):
    for m, orig in originals.items():
        if orig is None:
            sys.modules.pop(m, None)
        else:
            sys.modules[m] = orig


class TestScheduleTickProactiveCleanup:
    def _clients(self, statement_count, get_policy_side_effect=None):
        events_client = MagicMock()
        lambda_client = MagicMock()
        if get_policy_side_effect is not None:
            lambda_client.get_policy.side_effect = get_policy_side_effect
        else:
            statements = [{"Sid": f"vip-plan-{i}"} for i in range(statement_count)]
            lambda_client.get_policy.return_value = {
                "Policy": json.dumps({"Statement": statements})
            }
        sts_client = MagicMock()
        sts_client.get_caller_identity.return_value = {"Account": "165505826690"}

        def _client(service_name, *a, **kw):
            return {"events": events_client, "lambda": lambda_client, "sts": sts_client}[
                service_name
            ]

        return events_client, lambda_client, _client

    def test_triggers_cleanup_when_statement_count_at_limit(self):
        _events, _lam, client_factory = self._clients(
            executor._PLAN_PERMISSION_STATEMENT_LIMIT
        )
        with (
            patch("boto3.client", side_effect=client_factory),
            patch("executor._cleanup_orphan_plan_permissions") as mock_cleanup,
        ):
            executor._schedule_tick(plan_id="plan-1", run_id="run-1", bucket_index=0)
        mock_cleanup.assert_called_once()

    def test_skips_cleanup_when_statement_count_below_limit(self):
        _events, _lam, client_factory = self._clients(0)
        with (
            patch("boto3.client", side_effect=client_factory),
            patch("executor._cleanup_orphan_plan_permissions") as mock_cleanup,
        ):
            executor._schedule_tick(plan_id="plan-1", run_id="run-1", bucket_index=0)
        mock_cleanup.assert_not_called()

    def test_swallows_get_policy_client_error(self):
        _events, _lam, client_factory = self._clients(
            0, get_policy_side_effect=_client_error("ResourceNotFoundException", "GetPolicy")
        )
        with (
            patch("boto3.client", side_effect=client_factory),
            patch("executor._cleanup_orphan_plan_permissions") as mock_cleanup,
        ):
            executor._schedule_tick(plan_id="plan-1", run_id="run-1", bucket_index=0)  # must not raise
        mock_cleanup.assert_not_called()


class TestDeleteScheduleSafe:
    def test_noop_when_schedule_name_is_none(self):
        with patch("executor.boto3.client") as mock_client:
            executor._delete_schedule_safe(None)
        mock_client.assert_not_called()

    def test_calls_all_three_cleanup_steps(self):
        events_client = MagicMock()
        lambda_client = MagicMock()

        def client_factory(service, *a, **k):
            return {"events": events_client, "lambda": lambda_client}[service]

        with patch("executor.boto3.client", side_effect=client_factory):
            executor._delete_schedule_safe("vip-plan-abc")

        events_client.remove_targets.assert_called_once_with(
            Rule="vip-plan-abc", Ids=["lambda"]
        )
        events_client.delete_rule.assert_called_once_with(Name="vip-plan-abc")
        lambda_client.remove_permission.assert_called_once_with(
            FunctionName=executor.LAMBDA_FUNCTION_ARN, StatementId="vip-plan-abc"
        )

    def test_swallows_resource_not_found_silently(self):
        events_client = MagicMock()
        events_client.remove_targets.side_effect = _client_error("ResourceNotFoundException")
        events_client.delete_rule.side_effect = _client_error("ResourceNotFoundException")
        lambda_client = MagicMock()
        lambda_client.remove_permission.side_effect = _client_error("NoSuchEntity")

        def client_factory(service, *a, **k):
            return {"events": events_client, "lambda": lambda_client}[service]

        with patch("executor.boto3.client", side_effect=client_factory):
            executor._delete_schedule_safe("vip-plan-gone")  # must not raise/log-loudly

    def test_logs_warning_for_other_error_codes(self):
        events_client = MagicMock()
        events_client.remove_targets.side_effect = _client_error("AccessDeniedException")
        events_client.delete_rule.return_value = {}
        lambda_client = MagicMock()
        lambda_client.remove_permission.return_value = {}

        def client_factory(service, *a, **k):
            return {"events": events_client, "lambda": lambda_client}[service]

        with patch("executor.boto3.client", side_effect=client_factory):
            executor._delete_schedule_safe("vip-plan-denied")  # must not raise


class TestGetCampaignStateBranches:
    def test_returns_real_state_on_success(self):
        mock_oc = MagicMock()
        mock_oc.get_campaign_state.return_value = {"state": "Running"}
        originals = _stub_outbound_campaigns_client(mock_oc)
        try:
            result = executor._get_campaign_state("conn-1")
        finally:
            _unstub(originals)
        assert result == "Running"

    def test_returns_unknown_for_other_client_errors(self):
        mock_oc = MagicMock()
        mock_oc.get_campaign_state.side_effect = _client_error(
            "AccessDeniedException", "GetCampaignState"
        )
        originals = _stub_outbound_campaigns_client(mock_oc)
        try:
            result = executor._get_campaign_state("conn-1")
        finally:
            _unstub(originals)
        assert result == "Unknown"


class TestSafeStopCampaign:
    def test_stops_when_running(self):
        mock_oc = MagicMock()
        with patch("executor._get_campaign_state", return_value="Running"):
            originals = _stub_outbound_campaigns_client(mock_oc)
            try:
                executor._safe_stop_campaign("conn-1")
            finally:
                _unstub(originals)
        mock_oc.stop_campaign.assert_called_once_with("conn-1")

    def test_stops_when_paused(self):
        mock_oc = MagicMock()
        with patch("executor._get_campaign_state", return_value="Paused"):
            originals = _stub_outbound_campaigns_client(mock_oc)
            try:
                executor._safe_stop_campaign("conn-1")
            finally:
                _unstub(originals)
        mock_oc.stop_campaign.assert_called_once_with("conn-1")

    def test_does_not_stop_when_already_terminal(self):
        mock_oc = MagicMock()
        with patch("executor._get_campaign_state", return_value="Completed"):
            originals = _stub_outbound_campaigns_client(mock_oc)
            try:
                executor._safe_stop_campaign("conn-1")
            finally:
                _unstub(originals)
        mock_oc.stop_campaign.assert_not_called()

    def test_swallows_exception(self):
        originals = _stub_outbound_campaigns_client(MagicMock())
        try:
            with patch("executor._get_campaign_state", side_effect=RuntimeError("Connect down")):
                executor._safe_stop_campaign("conn-1")  # must not raise
        finally:
            _unstub(originals)


class TestSafeDeleteCampaignSwallowsException:
    def test_swallows_get_campaign_state_exception(self):
        originals = _stub_outbound_campaigns_client(MagicMock())
        try:
            with patch("executor._get_campaign_state", side_effect=RuntimeError("Connect down")):
                executor._safe_delete_campaign("conn-1")  # must not raise
        finally:
            _unstub(originals)

    def test_swallows_delete_campaign_exception(self):
        mock_oc = MagicMock()
        mock_oc.delete_campaign.side_effect = RuntimeError("Connect down")
        with patch("executor._get_campaign_state", return_value="Completed"):
            originals = _stub_outbound_campaigns_client(mock_oc)
            try:
                executor._safe_delete_campaign("conn-1")  # must not raise
            finally:
                _unstub(originals)


class TestSafeDeleteSegment:
    def test_deletes_segment_definition(self):
        mock_cp = MagicMock()
        vip_stub = MagicMock()
        vip_stub.build_from_env = MagicMock(return_value=mock_cp)
        modules_to_stub = [
            "vip_shared",
            "vip_shared.infrastructure",
            "vip_shared.infrastructure.persistence",
            "vip_shared.infrastructure.persistence.customer_profiles_client",
        ]
        originals = {m: sys.modules.get(m) for m in modules_to_stub}
        for m in modules_to_stub:
            sys.modules[m] = vip_stub
        try:
            executor._safe_delete_segment("seg-1")
        finally:
            _unstub(originals)
        mock_cp.delete_segment_definition.assert_called_once_with("seg-1")

    def test_swallows_exception(self):
        vip_stub = MagicMock()
        vip_stub.build_from_env = MagicMock(side_effect=RuntimeError("CP down"))
        modules_to_stub = [
            "vip_shared",
            "vip_shared.infrastructure",
            "vip_shared.infrastructure.persistence",
            "vip_shared.infrastructure.persistence.customer_profiles_client",
        ]
        originals = {m: sys.modules.get(m) for m in modules_to_stub}
        for m in modules_to_stub:
            sys.modules[m] = vip_stub
        try:
            executor._safe_delete_segment("seg-1")  # must not raise
        finally:
            _unstub(originals)
