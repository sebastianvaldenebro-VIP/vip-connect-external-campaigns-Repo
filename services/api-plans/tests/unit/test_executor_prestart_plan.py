"""Tests for executor._prestart_plan (zero prior direct coverage) and the
exception-swallow branch of _emit_prewarm_failure.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

sys.modules.setdefault("vip_shared", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.persistence", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.persistence.audit", MagicMock())

import executor  # noqa: E402


def _plan(plan_id="p2", buckets=None, **overrides):
    p = {
        "planId": plan_id,
        "name": "Downstream",
        "isTemplate": False,
        "buckets": buckets if buckets is not None else [],
    }
    p.update(overrides)
    return p


class TestEmitPrewarmFailure:
    def test_swallows_cloudwatch_errors(self):
        with patch("executor.boto3.client", side_effect=RuntimeError("CloudWatch down")):
            executor._emit_prewarm_failure("p1")  # must not raise


class TestPrestartPlan:
    def test_returns_when_plan_not_found(self):
        with (
            patch("executor.get_plan", return_value=None),
            patch("executor.update_plan_pending_warmup") as mock_update,
        ):
            executor._prestart_plan("p2")
        mock_update.assert_not_called()

    def test_returns_when_plan_is_template(self):
        with (
            patch("executor.get_plan", return_value=_plan(isTemplate=True)),
            patch("executor.update_plan_pending_warmup") as mock_update,
        ):
            executor._prestart_plan("p2")
        mock_update.assert_not_called()

    def test_returns_when_already_running(self):
        with (
            patch("executor.get_plan", return_value=_plan()),
            patch("executor.get_latest_run", return_value={"runId": "r0", "status": "running"}),
            patch("executor.update_plan_pending_warmup") as mock_update,
        ):
            executor._prestart_plan("p2")
        mock_update.assert_not_called()

    def test_returns_when_no_buckets(self):
        with (
            patch("executor.get_plan", return_value=_plan(buckets=[])),
            patch("executor.get_latest_run", return_value=None),
            patch("executor.update_plan_pending_warmup") as mock_update,
        ):
            executor._prestart_plan("p2")
        mock_update.assert_not_called()

    def test_returns_when_all_stage1_campaigns_already_warmed(self):
        plan = _plan(
            buckets=[{"id": "b0", "campaigns": [{"id": "c0", "name": "c0"}]}],
            pendingWarmup={"campaigns": [{"campaignId": "c0", "connectCampaignId": "conn-1"}]},
        )
        with (
            patch("executor.get_plan", return_value=plan),
            patch("executor.get_latest_run", return_value=None),
            patch("executor._create_campaign_only") as mock_create,
            patch("executor.update_plan_pending_warmup") as mock_update,
        ):
            executor._prestart_plan("p2")
        mock_create.assert_not_called()
        mock_update.assert_not_called()

    def test_skips_branded_and_sms_campaigns(self):
        plan = _plan(
            buckets=[
                {
                    "id": "b0",
                    "campaigns": [
                        {"id": "c0", "name": "c0", "deliveryType": "branded"},
                        {"id": "c1", "name": "c1", "deliveryType": "sms"},
                    ],
                }
            ]
        )
        with (
            patch("executor.get_plan", return_value=plan),
            patch("executor.get_latest_run", return_value=None),
            patch("executor._create_campaign_only") as mock_create,
            patch("executor.update_plan_pending_warmup") as mock_update,
        ):
            executor._prestart_plan("p2")
        mock_create.assert_not_called()
        mock_update.assert_not_called()

    def test_warms_new_stage1_campaign_and_persists_pending_warmup(self):
        plan = _plan(buckets=[{"id": "b0", "campaigns": [{"id": "c0", "name": "c0"}]}])
        with (
            patch("executor.get_plan", return_value=plan),
            patch("executor.get_latest_run", return_value=None),
            patch(
                "executor._create_campaign_only",
                return_value=("conn-1", "seg-1", "arn:seg", True, None, None),
            ),
            patch("executor.update_plan_pending_warmup") as mock_update,
        ):
            executor._prestart_plan("p2")

        mock_update.assert_called_once()
        call_args = mock_update.call_args.args
        assert call_args[0] == "p2"
        warmed = call_args[1]["campaigns"]
        assert warmed[0]["campaignId"] == "c0"
        assert warmed[0]["connectCampaignId"] == "conn-1"

    def test_merges_with_existing_partial_warmup_and_retries_missing_only(self):
        plan = _plan(
            buckets=[
                {
                    "id": "b0",
                    "campaigns": [
                        {"id": "c0", "name": "c0"},
                        {"id": "c1", "name": "c1"},
                    ],
                }
            ],
            pendingWarmup={
                "campaigns": [
                    {"campaignId": "c0", "connectCampaignId": "conn-existing"}
                ]
            },
        )
        with (
            patch("executor.get_plan", return_value=plan),
            patch("executor.get_latest_run", return_value=None),
            patch(
                "executor._create_campaign_only",
                return_value=("conn-1", "seg-1", "arn:seg", True, None, None),
            ) as mock_create,
            patch("executor.update_plan_pending_warmup") as mock_update,
        ):
            executor._prestart_plan("p2")

        # Only c1 (not already warmed) triggers a new _create_campaign_only call.
        mock_create.assert_called_once()
        warmed_ids = {c["campaignId"] for c in mock_update.call_args.args[1]["campaigns"]}
        assert warmed_ids == {"c0", "c1"}

    def test_emits_prewarm_failure_and_still_persists_partial_warmup_on_error(self):
        plan = _plan(
            buckets=[
                {
                    "id": "b0",
                    "campaigns": [
                        {"id": "c0", "name": "c0"},
                        {"id": "c1", "name": "c1"},
                    ],
                }
            ]
        )
        with (
            patch("executor.get_plan", return_value=plan),
            patch("executor.get_latest_run", return_value=None),
            patch(
                "executor._create_campaign_only",
                side_effect=[
                    ("conn-1", "seg-1", "arn:seg", True, None, None),
                    RuntimeError("Redis unavailable"),
                ],
            ),
            patch("executor._emit_prewarm_failure") as mock_emit,
            patch("executor.update_plan_pending_warmup") as mock_update,
        ):
            executor._prestart_plan("p2")

        mock_emit.assert_called_once_with("p2", 1)
        warmed_ids = {c["campaignId"] for c in mock_update.call_args.args[1]["campaigns"]}
        assert warmed_ids == {"c0"}

    def test_does_not_persist_warmup_when_nothing_warmed(self):
        plan = _plan(buckets=[{"id": "b0", "campaigns": [{"id": "c0", "name": "c0"}]}])
        with (
            patch("executor.get_plan", return_value=plan),
            patch("executor.get_latest_run", return_value=None),
            patch("executor._create_campaign_only", side_effect=RuntimeError("boom")),
            patch("executor._emit_prewarm_failure"),
            patch("executor.update_plan_pending_warmup") as mock_update,
        ):
            executor._prestart_plan("p2")
        mock_update.assert_not_called()
