"""Tests for executor._fire_campaign_chains (zero prior coverage) and the
remaining gaps in _maybe_loop (plan-not-found, already-running guards).
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


def _plan(plan_id="p2", **overrides):
    p = {"planId": plan_id, "name": "Downstream", "isTemplate": False}
    p.update(overrides)
    return p


class TestFireCampaignChains:
    def test_skips_template_plans(self):
        downstream = _plan(isTemplate=True, trigger={"type": "on_plan_complete", "afterCampaign": "c0"})
        with (
            patch("executor.find_plans_by_trigger_planid", return_value=[downstream]),
            patch("executor.start_run") as mock_start,
        ):
            executor._fire_campaign_chains("p1", 0, {"c0"})
        mock_start.assert_not_called()

    def test_clears_pending_warmup_when_outside_working_hours(self):
        downstream = _plan(
            trigger={"type": "on_plan_complete", "afterCampaign": "c0"},
            pendingWarmup={"campaigns": []},
        )
        with (
            patch("executor.find_plans_by_trigger_planid", return_value=[downstream]),
            patch("executor._within_working_hours", return_value=False),
            patch("executor.update_plan_pending_warmup") as mock_clear,
            patch("executor.start_run") as mock_start,
        ):
            executor._fire_campaign_chains("p1", 0, {"c0"})
        mock_clear.assert_called_once_with("p2", None)
        mock_start.assert_not_called()

    def test_skips_non_on_plan_complete_trigger(self):
        downstream = _plan(trigger={"type": "manual"})
        with (
            patch("executor.find_plans_by_trigger_planid", return_value=[downstream]),
            patch("executor._within_working_hours", return_value=True),
            patch("executor.start_run") as mock_start,
        ):
            executor._fire_campaign_chains("p1", 0, {"c0"})
        mock_start.assert_not_called()

    def test_skips_when_no_after_campaign_configured(self):
        downstream = _plan(trigger={"type": "on_plan_complete"})
        with (
            patch("executor.find_plans_by_trigger_planid", return_value=[downstream]),
            patch("executor._within_working_hours", return_value=True),
            patch("executor.start_run") as mock_start,
        ):
            executor._fire_campaign_chains("p1", 0, {"c0"})
        mock_start.assert_not_called()

    def test_skips_when_after_campaign_not_in_completed_set(self):
        downstream = _plan(trigger={"type": "on_plan_complete", "afterCampaign": "c-other"})
        with (
            patch("executor.find_plans_by_trigger_planid", return_value=[downstream]),
            patch("executor._within_working_hours", return_value=True),
            patch("executor.start_run") as mock_start,
        ):
            executor._fire_campaign_chains("p1", 0, {"c0"})
        mock_start.assert_not_called()

    def test_skips_when_after_bucket_guard_mismatches(self):
        downstream = _plan(
            trigger={"type": "on_plan_complete", "afterCampaign": "c0", "afterBucket": 1}
        )
        with (
            patch("executor.find_plans_by_trigger_planid", return_value=[downstream]),
            patch("executor._within_working_hours", return_value=True),
            patch("executor.start_run") as mock_start,
        ):
            executor._fire_campaign_chains("p1", 0, {"c0"})
        mock_start.assert_not_called()

    def test_fires_when_after_campaign_matches(self):
        downstream = _plan(
            trigger={"type": "on_plan_complete", "afterCampaign": "c0", "repeat": True}
        )
        with (
            patch("executor.find_plans_by_trigger_planid", return_value=[downstream]),
            patch("executor._within_working_hours", return_value=True),
            patch("executor.start_run") as mock_start,
        ):
            executor._fire_campaign_chains("p1", 0, {"c0"})
        mock_start.assert_called_once_with("p2", triggered_by="chained")

    def test_resets_trigger_when_repeat_false(self):
        downstream = _plan(
            trigger={"type": "on_plan_complete", "afterCampaign": "c0", "repeat": False}
        )
        with (
            patch("executor.find_plans_by_trigger_planid", return_value=[downstream]),
            patch("executor._within_working_hours", return_value=True),
            patch("executor.start_run"),
            patch("executor.update_plan_trigger") as mock_reset,
        ):
            executor._fire_campaign_chains("p1", 0, {"c0"})
        mock_reset.assert_called_once_with("p2", {"type": "manual"})

    def test_swallows_start_run_error(self):
        downstream = _plan(trigger={"type": "on_plan_complete", "afterCampaign": "c0"})
        with (
            patch("executor.find_plans_by_trigger_planid", return_value=[downstream]),
            patch("executor._within_working_hours", return_value=True),
            patch("executor.start_run", side_effect=RuntimeError("boom")),
        ):
            executor._fire_campaign_chains("p1", 0, {"c0"})  # must not raise


class TestMaybeLoopAdditional:
    def test_returns_when_plan_not_found(self):
        with (
            patch("executor.get_plan", return_value=None),
            patch("executor.start_run") as mock_start,
        ):
            executor._maybe_loop("p1")
        mock_start.assert_not_called()

    def test_returns_when_already_running(self):
        plan = {"planId": "p1", "loop": {"endTime": "23:59"}}
        with (
            patch("executor.get_plan", return_value=plan),
            patch("executor.get_latest_run", return_value={"status": "running"}),
            patch("executor.start_run") as mock_start,
        ):
            executor._maybe_loop("p1")
        mock_start.assert_not_called()
