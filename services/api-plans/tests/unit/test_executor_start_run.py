"""Tests for executor.start_run and remaining gaps in start_run_chained,
_fire_bucket_chains, and scheduled_run.
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


def _plan(plan_id="p1", buckets=None, **overrides):
    plan = {
        "planId": plan_id,
        "name": "Plan",
        "trigger": {"type": "manual"},
        "isTemplate": False,
        "buckets": buckets if buckets is not None else [{"id": "b0", "campaigns": []}],
    }
    plan.update(overrides)
    return plan


def _run(plan, bucket_count=1, **overrides):
    run = {
        "planId": plan["planId"],
        "runId": "run-1",
        "status": "running",
        "planSnapshot": plan,
        "currentBucketIndex": 0,
        "bucketStates": [
            {
                "bucketId": f"b{i}",
                "status": "queued",
                "campaignStates": [
                    {"campaignId": f"c{i}", "status": "queued", "connectCampaignId": None}
                ],
            }
            for i in range(bucket_count)
        ],
        "startedAt": "t0",
        "completedAt": None,
        "_version": 0,
        "triggeredBy": "manual",
        "error": None,
    }
    run.update(overrides)
    return run


class TestStartRun:
    def test_raises_when_plan_not_found(self):
        with patch("executor.get_plan", return_value=None):
            with pytest.raises(ValueError, match="not found"):
                executor.start_run("p1")

    def test_raises_when_plan_is_template(self):
        with patch("executor.get_plan", return_value=_plan(isTemplate=True)):
            with pytest.raises(ValueError, match="template"):
                executor.start_run("p1")

    def test_raises_when_plan_has_no_buckets(self):
        with patch("executor.get_plan", return_value=_plan(buckets=[])):
            with pytest.raises(ValueError, match="no buckets"):
                executor.start_run("p1")

    def test_raises_when_run_already_active(self):
        plan = _plan()
        with (
            patch("executor.get_plan", return_value=plan),
            patch("executor.get_latest_run", return_value={"runId": "r0", "status": "running"}),
        ):
            with pytest.raises(ValueError, match="already has an active run"):
                executor.start_run("p1")

    def test_locks_creates_run_and_starts_first_bucket(self):
        plan = _plan()
        run = _run(plan)
        with (
            patch("executor.get_plan", return_value=plan),
            patch("executor.get_latest_run", return_value=None),
            patch("executor.lock_plan_run") as mock_lock,
            patch("executor.create_run", return_value=run) as mock_create,
            patch("executor._start_bucket") as mock_start_bucket,
        ):
            result = executor.start_run("p1", triggered_by="manual")

        assert result is run
        mock_lock.assert_called_once()
        mock_create.assert_called_once()
        mock_start_bucket.assert_called_once_with(run, 0)

    def test_marks_skipped_buckets_before_start_index(self):
        plan = _plan(buckets=[{"id": "b0", "campaigns": []}, {"id": "b1", "campaigns": []}])
        run = _run(plan, bucket_count=2)
        with (
            patch("executor.get_plan", return_value=plan),
            patch("executor.get_latest_run", return_value=None),
            patch("executor.lock_plan_run"),
            patch("executor.create_run", return_value=run),
            patch("executor._start_bucket") as mock_start_bucket,
        ):
            executor.start_run("p1", start_bucket_index=1)

        assert run["bucketStates"][0]["status"] == "cancelled"
        assert run["bucketStates"][0]["exitReason"] == "skipped"
        assert run["bucketStates"][0]["campaignStates"][0]["status"] == "cancelled"
        mock_start_bucket.assert_called_once_with(run, 1)

    def test_consumes_fresh_pending_warmup_for_bucket_zero(self):
        plan = _plan()
        run = _run(plan)
        now_iso = executor._now_utc().isoformat()
        plan["pendingWarmup"] = {
            "createdAt": now_iso,
            "campaigns": [
                {
                    "campaignId": "c0",
                    "connectCampaignId": "connect-1",
                    "segmentArn": "arn:seg",
                    "segmentName": "seg-1",
                    "leadCount": 42,
                    "warmupStarted": True,
                }
            ],
        }
        with (
            patch("executor.get_plan", return_value=plan),
            patch("executor.get_latest_run", return_value=None),
            patch("executor.lock_plan_run"),
            patch("executor.create_run", return_value=run),
            patch("executor.save_run") as mock_save,
            patch("executor.update_plan_pending_warmup") as mock_clear,
            patch("executor._activate_warming_bucket") as mock_activate,
            patch("executor._start_bucket") as mock_start_bucket,
        ):
            executor.start_run("p1")

        cs = run["bucketStates"][0]["campaignStates"][0]
        assert cs["connectCampaignId"] == "connect-1"
        assert cs["status"] == "warming"
        mock_save.assert_called_once_with(run)
        mock_clear.assert_called_once_with("p1", None)
        mock_activate.assert_called_once_with(run, plan, 0)
        mock_start_bucket.assert_not_called()

    def test_discards_stale_pending_warmup_older_than_2_hours(self):
        plan = _plan()
        run = _run(plan)
        stale_created_at = (executor._now_utc() - timedelta(hours=3)).isoformat()
        plan["pendingWarmup"] = {"createdAt": stale_created_at, "campaigns": []}
        with (
            patch("executor.get_plan", return_value=plan),
            patch("executor.get_latest_run", return_value=None),
            patch("executor.lock_plan_run"),
            patch("executor.create_run", return_value=run),
            patch("executor.update_plan_pending_warmup") as mock_clear,
            patch("executor._start_bucket") as mock_start_bucket,
        ):
            executor.start_run("p1")

        mock_clear.assert_called_once_with("p1", None)
        mock_start_bucket.assert_called_once_with(run, 0)

    def test_malformed_pending_warmup_created_at_is_ignored(self):
        plan = _plan()
        run = _run(plan)
        plan["pendingWarmup"] = {"createdAt": "not-a-date", "campaigns": []}
        with (
            patch("executor.get_plan", return_value=plan),
            patch("executor.get_latest_run", return_value=None),
            patch("executor.lock_plan_run"),
            patch("executor.create_run", return_value=run),
            patch("executor.save_run"),
            patch("executor.update_plan_pending_warmup"),
            patch("executor._activate_warming_bucket"),
        ):
            executor.start_run("p1")  # must not raise despite malformed createdAt

    def test_pending_warmup_without_matching_campaign_is_skipped(self):
        plan = _plan()
        run = _run(plan)
        now_iso = executor._now_utc().isoformat()
        plan["pendingWarmup"] = {
            "createdAt": now_iso,
            "campaigns": [{"campaignId": "no-match", "connectCampaignId": "connect-x"}],
        }
        with (
            patch("executor.get_plan", return_value=plan),
            patch("executor.get_latest_run", return_value=None),
            patch("executor.lock_plan_run"),
            patch("executor.create_run", return_value=run),
            patch("executor.save_run"),
            patch("executor.update_plan_pending_warmup"),
            patch("executor._activate_warming_bucket"),
        ):
            executor.start_run("p1")

        cs = run["bucketStates"][0]["campaignStates"][0]
        assert cs["status"] == "queued"  # unchanged — no match found

    def test_pending_warmup_ignored_when_start_bucket_index_nonzero(self):
        plan = _plan(buckets=[{"id": "b0", "campaigns": []}, {"id": "b1", "campaigns": []}])
        run = _run(plan, bucket_count=2)
        plan["pendingWarmup"] = {"createdAt": executor._now_utc().isoformat(), "campaigns": []}
        with (
            patch("executor.get_plan", return_value=plan),
            patch("executor.get_latest_run", return_value=None),
            patch("executor.lock_plan_run"),
            patch("executor.create_run", return_value=run),
            patch("executor._start_bucket") as mock_start_bucket,
            patch("executor._activate_warming_bucket") as mock_activate,
        ):
            executor.start_run("p1", start_bucket_index=1)

        mock_activate.assert_not_called()
        mock_start_bucket.assert_called_once_with(run, 1)


class TestFireBucketChainsAdditional:
    def test_skips_when_after_bucket_does_not_match(self):
        downstream = _plan(
            plan_id="p2",
            trigger={"type": "on_plan_complete", "planId": "p1", "afterBucket": 5},
        )
        with (
            patch("executor.find_plans_by_trigger_planid", return_value=[downstream]),
            patch("executor._within_working_hours", return_value=True),
            patch("executor.start_run") as mock_start,
        ):
            executor._fire_bucket_chains("p1", 0)
        mock_start.assert_not_called()

    def test_skips_when_after_bucket_missing(self):
        downstream = _plan(plan_id="p2", trigger={"type": "on_plan_complete", "planId": "p1"})
        with (
            patch("executor.find_plans_by_trigger_planid", return_value=[downstream]),
            patch("executor._within_working_hours", return_value=True),
            patch("executor.start_run") as mock_start,
        ):
            executor._fire_bucket_chains("p1", 0)
        mock_start.assert_not_called()

    def test_skips_when_after_campaign_is_set(self):
        """Campaign-level chaining is handled by _fire_campaign_chains, not here."""
        downstream = _plan(
            plan_id="p2",
            trigger={
                "type": "on_plan_complete",
                "planId": "p1",
                "afterBucket": 0,
                "afterCampaign": "c0",
            },
        )
        with (
            patch("executor.find_plans_by_trigger_planid", return_value=[downstream]),
            patch("executor._within_working_hours", return_value=True),
            patch("executor.start_run") as mock_start,
        ):
            executor._fire_bucket_chains("p1", 0)
        mock_start.assert_not_called()

    def test_fires_when_after_bucket_matches(self):
        downstream = _plan(
            plan_id="p2",
            trigger={"type": "on_plan_complete", "planId": "p1", "afterBucket": 0, "repeat": True},
        )
        with (
            patch("executor.find_plans_by_trigger_planid", return_value=[downstream]),
            patch("executor._within_working_hours", return_value=True),
            patch("executor.start_run") as mock_start,
        ):
            executor._fire_bucket_chains("p1", 0)
        mock_start.assert_called_once_with("p2", triggered_by="chained")

    def test_resets_trigger_when_repeat_false(self):
        downstream = _plan(
            plan_id="p2",
            trigger={"type": "on_plan_complete", "planId": "p1", "afterBucket": 0, "repeat": False},
        )
        with (
            patch("executor.find_plans_by_trigger_planid", return_value=[downstream]),
            patch("executor._within_working_hours", return_value=True),
            patch("executor.start_run"),
            patch("executor.update_plan_trigger") as mock_reset,
        ):
            executor._fire_bucket_chains("p1", 0)
        mock_reset.assert_called_once_with("p2", {"type": "manual"})

    def test_clears_pending_warmup_when_outside_working_hours(self):
        downstream = _plan(
            plan_id="p2",
            trigger={"type": "on_plan_complete", "planId": "p1", "afterBucket": 0},
        )
        downstream["pendingWarmup"] = {"campaigns": []}
        with (
            patch("executor.find_plans_by_trigger_planid", return_value=[downstream]),
            patch("executor._within_working_hours", return_value=False),
            patch("executor.update_plan_pending_warmup") as mock_clear,
            patch("executor.start_run") as mock_start,
        ):
            executor._fire_bucket_chains("p1", 0)
        mock_clear.assert_called_once_with("p2", None)
        mock_start.assert_not_called()

    def test_swallows_error_from_start_run(self):
        downstream = _plan(
            plan_id="p2",
            trigger={"type": "on_plan_complete", "planId": "p1", "afterBucket": 0},
        )
        with (
            patch("executor.find_plans_by_trigger_planid", return_value=[downstream]),
            patch("executor._within_working_hours", return_value=True),
            patch("executor.start_run", side_effect=RuntimeError("boom")),
        ):
            executor._fire_bucket_chains("p1", 0)  # must not raise


class TestStartRunChainedAdditional:
    def test_skips_plan_with_after_bucket_set(self):
        downstream = _plan(
            plan_id="p2",
            trigger={"type": "on_plan_complete", "planId": "p1", "afterBucket": 0},
        )
        with (
            patch("executor.find_plans_by_trigger_planid", return_value=[downstream]),
            patch("executor.start_run") as mock_start,
        ):
            executor.start_run_chained("p1")
        mock_start.assert_not_called()

    def test_skips_plan_with_after_campaign_set(self):
        downstream = _plan(
            plan_id="p2",
            trigger={"type": "on_plan_complete", "planId": "p1", "afterCampaign": "c0"},
        )
        with (
            patch("executor.find_plans_by_trigger_planid", return_value=[downstream]),
            patch("executor.start_run") as mock_start,
        ):
            executor.start_run_chained("p1")
        mock_start.assert_not_called()

    def test_clears_pending_warmup_when_outside_working_hours(self):
        downstream = _plan(
            plan_id="p2", trigger={"type": "on_plan_complete", "planId": "p1"}
        )
        downstream["pendingWarmup"] = {"campaigns": []}
        with (
            patch("executor.find_plans_by_trigger_planid", return_value=[downstream]),
            patch("executor._within_working_hours", return_value=False),
            patch("executor.update_plan_pending_warmup") as mock_clear,
            patch("executor.start_run") as mock_start,
        ):
            executor.start_run_chained("p1")
        mock_clear.assert_called_once_with("p2", None)
        mock_start.assert_not_called()

    def test_skips_without_clearing_when_no_pending_warmup(self):
        downstream = _plan(
            plan_id="p2", trigger={"type": "on_plan_complete", "planId": "p1"}
        )
        with (
            patch("executor.find_plans_by_trigger_planid", return_value=[downstream]),
            patch("executor._within_working_hours", return_value=False),
            patch("executor.update_plan_pending_warmup") as mock_clear,
        ):
            executor.start_run_chained("p1")
        mock_clear.assert_not_called()


class TestScheduledRunAdditional:
    def test_returns_already_running_when_latest_run_active(self):
        with patch("executor.get_latest_run", return_value={"runId": "r0", "status": "running"}):
            result = executor.scheduled_run("p1")
        assert result == {"ok": True, "reason": "already_running"}

    def test_returns_plan_not_found(self):
        with (
            patch("executor.get_latest_run", return_value=None),
            patch("executor.get_plan", return_value=None),
        ):
            result = executor.scheduled_run("p1")
        assert result == {"ok": False, "reason": "plan_not_found"}

    def test_returns_is_template(self):
        with (
            patch("executor.get_latest_run", return_value=None),
            patch("executor.get_plan", return_value=_plan(isTemplate=True)),
        ):
            result = executor.scheduled_run("p1")
        assert result == {"ok": True, "reason": "is_template"}

    def test_returns_outside_working_hours(self):
        with (
            patch("executor.get_latest_run", return_value=None),
            patch("executor.get_plan", return_value=_plan()),
            patch("executor._within_working_hours", return_value=False),
        ):
            result = executor.scheduled_run("p1")
        assert result == {"ok": True, "reason": "outside_working_hours"}

    def test_starts_run_when_all_checks_pass(self):
        with (
            patch("executor.get_latest_run", return_value=None),
            patch("executor.get_plan", return_value=_plan()),
            patch("executor._within_working_hours", return_value=True),
            patch("executor.start_run", return_value={"runId": "r1"}),
        ):
            result = executor.scheduled_run("p1")
        assert result == {"ok": True, "runId": "r1"}
