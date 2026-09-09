"""Targeted tests for remaining gaps in _advance_bucket: the cleanup loop,
the rescue-tick schedule_tick failure, the run-completed error-notification
path, start_run_chained's exception swallow, and the double-failure branch
when both the final save AND its ConcurrentWriteError reschedule fail.
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
from store import ConcurrentWriteError  # noqa: E402


def _campaign_state(cid, status="completed", **overrides):
    cs = {
        "campaignId": cid,
        "name": cid,
        "status": status,
        "connectCampaignId": None,
        "segmentName": None,
        "segmentArn": None,
        "leadCount": None,
        "startedAt": None,
        "completedAt": None,
        "exitReason": None,
        "errorDetail": None,
    }
    cs.update(overrides)
    return cs


def _bucket_state(bid, campaign_states, status="running", **overrides):
    bs = {
        "bucketId": bid,
        "name": bid,
        "status": status,
        "scheduleName": "sched-x",
        "startedAt": "t0",
        "completedAt": None,
        "campaignStates": campaign_states,
    }
    bs.update(overrides)
    return bs


def _bucket_def(bid, campaigns, **overrides):
    d = {"id": bid, "name": bid, "run_mode": "status_based", "duration_minutes": 30, "campaigns": campaigns, "cleanup": True}
    d.update(overrides)
    return d


def _plan(buckets, **overrides):
    p = {"planId": "plan-1", "name": "Test", "trigger": {"type": "manual"}, "isTemplate": False, "buckets": buckets}
    p.update(overrides)
    return p


def _run(plan, bucket_states, **overrides):
    r = {
        "planId": "plan-1",
        "runId": "run-1",
        "status": "running",
        "planSnapshot": plan,
        "currentBucketIndex": 0,
        "bucketStates": bucket_states,
        "startedAt": "t0",
        "completedAt": None,
        "_version": 0,
        "triggeredBy": "manual",
        "error": None,
    }
    r.update(overrides)
    return r


class TestCleanupLoop:
    def test_deletes_connect_campaign_and_segment_when_cleanup_enabled(self):
        plan = _plan([_bucket_def("b0", [{"id": "c0", "name": "c0"}], cleanup=True)])
        cs = _campaign_state("c0", connectCampaignId="conn-1", segmentName="seg-1")
        run = _run(plan, [_bucket_state("b0", [cs])])
        with (
            patch("executor.save_run"),
            patch("executor._delete_bucket_schedule_safe"),
            patch("executor._record_plan_event"),
            patch("executor._fire_bucket_chains"),
            patch("executor._safe_stop_campaign") as mock_stop,
            patch("executor._safe_delete_campaign") as mock_delete_camp,
            patch("executor._safe_delete_segment") as mock_delete_seg,
            patch("executor.unlock_plan_run"),
            patch("executor._maybe_loop"),
            patch("executor.start_run_chained"),
        ):
            executor._advance_bucket(run, plan, 0, reason="all_campaigns_done")

        mock_stop.assert_called_once_with("conn-1")
        mock_delete_camp.assert_called_once_with("conn-1")
        mock_delete_seg.assert_called_once_with("seg-1")


class TestRescueTickFailure:
    def test_logs_but_continues_when_rescue_schedule_tick_fails(self):
        plan = _plan([
            _bucket_def("b0", [{"id": "c0", "name": "c0"}], cleanup=False),
            _bucket_def("b1", [{"id": "c1", "name": "c1"}], cleanup=False, parallel=False),
        ])
        cs0 = _campaign_state("c0")
        cs1 = _campaign_state("c1", status="running")
        run = _run(
            plan,
            [
                _bucket_state("b0", [cs0]),
                _bucket_state("b1", [cs1], status="running", scheduleName=None),
            ],
        )
        with (
            patch("executor.save_run"),
            patch("executor._delete_bucket_schedule_safe"),
            patch("executor._record_plan_event"),
            patch("executor._fire_bucket_chains"),
            patch("executor._schedule_tick", side_effect=RuntimeError("Scheduler down")),
            patch("executor._dispatch_ready_campaigns", return_value=False),
        ):
            executor._advance_bucket(run, plan, 0, reason="all_campaigns_done")  # must not raise

        assert run["bucketStates"][1]["scheduleName"] is None


class TestRunCompletedWithErrors:
    def test_notifies_sns_and_swallows_start_run_chained_error(self):
        plan = _plan([_bucket_def("b0", [{"id": "c0", "name": "c0"}], cleanup=False)])
        cs = _campaign_state("c0", status="error")
        run = _run(plan, [_bucket_state("b0", [cs])])
        with (
            patch("executor.save_run"),
            patch("executor._delete_bucket_schedule_safe"),
            patch("executor._record_plan_event"),
            patch("executor._fire_bucket_chains"),
            patch("executor.unlock_plan_run"),
            patch("executor._maybe_loop"),
            patch("executor._notify_sns") as mock_notify,
            patch("executor.start_run_chained", side_effect=RuntimeError("chain failed")),
        ):
            executor._advance_bucket(run, plan, 0, reason="all_campaigns_done")  # must not raise

        assert run["status"] == "completed"
        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args.kwargs
        assert call_kwargs["attributes"]["alertType"] == "run_completed_with_errors"

    def test_no_notification_when_no_error_campaigns(self):
        plan = _plan([_bucket_def("b0", [{"id": "c0", "name": "c0"}], cleanup=False)])
        cs = _campaign_state("c0", status="completed")
        run = _run(plan, [_bucket_state("b0", [cs])])
        with (
            patch("executor.save_run"),
            patch("executor._delete_bucket_schedule_safe"),
            patch("executor._record_plan_event"),
            patch("executor._fire_bucket_chains"),
            patch("executor.unlock_plan_run"),
            patch("executor._maybe_loop"),
            patch("executor._notify_sns") as mock_notify,
            patch("executor.start_run_chained"),
        ):
            executor._advance_bucket(run, plan, 0, reason="all_campaigns_done")
        mock_notify.assert_not_called()

    def test_connect_deleted_exit_reason_also_triggers_notification(self):
        plan = _plan([_bucket_def("b0", [{"id": "c0", "name": "c0"}], cleanup=False)])
        cs = _campaign_state("c0", status="completed", exitReason="connect_deleted")
        run = _run(plan, [_bucket_state("b0", [cs])])
        with (
            patch("executor.save_run"),
            patch("executor._delete_bucket_schedule_safe"),
            patch("executor._record_plan_event"),
            patch("executor._fire_bucket_chains"),
            patch("executor.unlock_plan_run"),
            patch("executor._maybe_loop"),
            patch("executor._notify_sns") as mock_notify,
            patch("executor.start_run_chained"),
        ):
            executor._advance_bucket(run, plan, 0, reason="all_campaigns_done")
        mock_notify.assert_called_once()


class TestConcurrentWriteDoubleFailure:
    def test_logs_but_does_not_raise_extra_when_reschedule_also_fails(self):
        """When the final save_run raises ConcurrentWriteError AND the
        reschedule attempt inside the except block also fails, the ORIGINAL
        ConcurrentWriteError must still propagate (not the reschedule
        failure) — and the double-failure must be logged, not silently lost.
        """
        plan = _plan([_bucket_def("b0", [{"id": "c0", "name": "c0"}], cleanup=False)])
        cs = _campaign_state("c0")
        run = _run(plan, [_bucket_state("b0", [cs])])
        with (
            patch("executor.save_run", side_effect=ConcurrentWriteError("race")),
            patch("executor._delete_bucket_schedule_safe"),
            patch("executor._schedule_tick", side_effect=RuntimeError("scheduler also down")),
        ):
            with pytest.raises(ConcurrentWriteError, match="race"):
                executor._advance_bucket(run, plan, 0, reason="all_campaigns_done")
