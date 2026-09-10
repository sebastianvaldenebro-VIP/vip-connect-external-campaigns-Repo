"""Targeted tests for remaining gaps in abort_run, _force_finish_internal,
and force_finish_run (the public retry wrapper, which had zero direct tests).
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


def _campaign_def(cid, **overrides):
    d = {"id": cid, "name": cid, "states": ["NY"], "groups": [], "dependsOn": [], "cleanup": True}
    d.update(overrides)
    return d


def _campaign_state(cid, status="running", **overrides):
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


def _bucket_def(bid, campaigns, **overrides):
    d = {"id": bid, "name": bid, "run_mode": "status_based", "duration_minutes": 30, "campaigns": campaigns, "cleanup": True}
    d.update(overrides)
    return d


def _bucket_state(bid, campaign_states, status="running", **overrides):
    bs = {
        "bucketId": bid,
        "name": bid,
        "status": status,
        "scheduleName": "sched-x",
        "startedAt": "2026-05-08T10:00:00+00:00",
        "completedAt": None,
        "campaignStates": campaign_states,
    }
    bs.update(overrides)
    return bs


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


class TestAbortRunAdditional:
    def test_raises_when_run_not_found(self):
        with patch("executor.get_run", return_value=None):
            with pytest.raises(ValueError, match="not found"):
                executor.abort_run("p1", "r1")

    def test_returns_run_unchanged_when_already_aborted(self):
        plan = _plan([_bucket_def("b0", [_campaign_def("c0")])])
        run = _run(plan, [_bucket_state("b0", [_campaign_state("c0")])], status="aborted")
        with patch("executor.get_run", return_value=run):
            result = executor.abort_run("p1", "r1")
        assert result is run

    def test_raises_when_run_in_other_terminal_status(self):
        plan = _plan([_bucket_def("b0", [_campaign_def("c0")])])
        run = _run(plan, [_bucket_state("b0", [_campaign_state("c0")])], status="completed")
        with patch("executor.get_run", return_value=run):
            with pytest.raises(ValueError, match="not running"):
                executor.abort_run("p1", "r1")

    def test_skips_buckets_not_in_active_status(self):
        plan = _plan([
            _bucket_def("b0", [_campaign_def("c0")]),
            _bucket_def("b1", [_campaign_def("c1")]),
        ])
        run = _run(plan, [
            _bucket_state("b0", [_campaign_state("c0", status="completed")], status="completed"),
            _bucket_state("b1", [_campaign_state("c1", status="running", connectCampaignId="conn-1")]),
        ])
        with (
            patch("executor.get_run", return_value=run),
            patch("executor.save_run"),
            patch("executor.unlock_plan_run"),
            patch("executor.update_plan_pending_warmup"),
            patch("executor._delete_bucket_schedule_safe"),
            patch("executor._safe_stop_campaign") as mock_stop,
        ):
            executor.abort_run("p1", "r1")

        # Only bucket b1's campaign should have been stopped — b0 was already
        # completed and must be left untouched.
        mock_stop.assert_called_once_with("conn-1")
        assert run["bucketStates"][0]["campaignStates"][0]["status"] == "completed"

    def test_stops_sms_campaign_on_abort(self):
        plan = _plan([_bucket_def("b0", [_campaign_def("c0")])])
        cs = _campaign_state("c0", status="running", smsCampaignId="sms-1")
        run = _run(plan, [_bucket_state("b0", [cs])])
        with (
            patch("executor.get_run", return_value=run),
            patch("executor.save_run"),
            patch("executor.unlock_plan_run"),
            patch("executor.update_plan_pending_warmup"),
            patch("executor._delete_bucket_schedule_safe"),
            patch("executor._stop_sms_campaign") as mock_stop_sms,
        ):
            executor.abort_run("p1", "r1")

        mock_stop_sms.assert_called_once_with(cs)
        assert cs["status"] == "cancelled"
        assert cs["exitReason"] == executor.REASON_ABORTED

    def test_unlocks_and_reraises_on_generic_exception_during_save(self):
        plan = _plan([_bucket_def("b0", [_campaign_def("c0")])])
        run = _run(plan, [_bucket_state("b0", [_campaign_state("c0", status="queued")])])
        with (
            patch("executor.get_run", return_value=run),
            patch("executor.save_run", side_effect=RuntimeError("DDB down")),
            patch("executor.unlock_plan_run") as mock_unlock,
            patch("executor._delete_bucket_schedule_safe"),
        ):
            with pytest.raises(RuntimeError, match="DDB down"):
                executor.abort_run("p1", "r1")
        mock_unlock.assert_called_once_with("p1")

    def test_retries_on_concurrent_write_error_then_succeeds(self):
        plan = _plan([_bucket_def("b0", [_campaign_def("c0")])])

        def fresh_run(*_a, **_k):
            return _run(plan, [_bucket_state("b0", [_campaign_state("c0", status="queued")])])

        with (
            patch("executor.get_run", side_effect=fresh_run),
            patch("executor.save_run", side_effect=[ConcurrentWriteError("race"), None]),
            patch("executor.unlock_plan_run") as mock_unlock,
            patch("executor.update_plan_pending_warmup"),
            patch("executor._delete_bucket_schedule_safe"),
        ):
            result = executor.abort_run("p1", "r1")

        assert result["status"] == "aborted"
        mock_unlock.assert_called_once_with("p1")


class TestForceFinishInternalAdditional:
    def test_skips_buckets_not_in_active_status(self):
        plan = _plan([_bucket_def("b0", [_campaign_def("c0")])])
        run = _run(plan, [_bucket_state("b0", [_campaign_state("c0", status="completed")], status="completed")])
        with (
            patch("executor.save_run"),
            patch("executor.unlock_plan_run"),
            patch("executor.update_plan_pending_warmup"),
        ):
            executor._force_finish_internal(run, plan)
        assert run["bucketStates"][0]["status"] == "completed"

    def test_deletes_warming_campaign_and_segment(self):
        plan = _plan([_bucket_def("b0", [_campaign_def("c0")])])
        cs = _campaign_state(
            "c0", status="warming", connectCampaignId="conn-1", segmentName="seg-1"
        )
        run = _run(plan, [_bucket_state("b0", [cs])])
        with (
            patch("executor.save_run"),
            patch("executor.unlock_plan_run"),
            patch("executor.update_plan_pending_warmup"),
            patch("executor._delete_bucket_schedule_safe"),
            patch("executor._safe_stop_campaign") as mock_stop,
            patch("executor._safe_delete_campaign") as mock_del_camp,
            patch("executor._safe_delete_segment") as mock_del_seg,
        ):
            executor._force_finish_internal(run, plan)

        mock_stop.assert_called_once_with("conn-1")
        # Deleted once by the per-campaign "warming" branch, and again by the
        # bucket-level cleanup pass below it (bucket.cleanup=True is the
        # default here) — _safe_delete_campaign/_safe_delete_segment are
        # idempotent no-ops on an already-deleted resource, so this double
        # call is genuine, intentional behavior, not a bug.
        assert mock_del_camp.call_args_list == [(("conn-1",),), (("conn-1",),)]
        assert mock_del_seg.call_args_list == [(("seg-1",),), (("seg-1",),)]

    def test_stops_sms_campaign(self):
        plan = _plan([_bucket_def("b0", [_campaign_def("c0")])])
        cs = _campaign_state("c0", status="running", smsCampaignId="sms-1")
        run = _run(plan, [_bucket_state("b0", [cs])])
        with (
            patch("executor.save_run"),
            patch("executor.unlock_plan_run"),
            patch("executor.update_plan_pending_warmup"),
            patch("executor._delete_bucket_schedule_safe"),
            patch("executor._stop_sms_campaign") as mock_stop_sms,
        ):
            executor._force_finish_internal(run, plan)
        mock_stop_sms.assert_called_once_with(cs)
        assert cs["status"] == "completed"
        assert cs["exitReason"] == "force_finished"

    def test_cleans_up_campaign_and_segment_when_bucket_cleanup_enabled(self):
        plan = _plan([_bucket_def("b0", [_campaign_def("c0", cleanup=True)], cleanup=True)])
        cs = _campaign_state(
            "c0", status="running", connectCampaignId="conn-1", segmentName="seg-1"
        )
        run = _run(plan, [_bucket_state("b0", [cs])])
        with (
            patch("executor.save_run"),
            patch("executor.unlock_plan_run"),
            patch("executor.update_plan_pending_warmup"),
            patch("executor._delete_bucket_schedule_safe"),
            patch("executor._safe_stop_campaign"),
            patch("executor._safe_delete_campaign") as mock_del_camp,
            patch("executor._safe_delete_segment") as mock_del_seg,
        ):
            executor._force_finish_internal(run, plan)

        mock_del_camp.assert_called_with("conn-1")
        mock_del_seg.assert_called_with("seg-1")

    def test_skips_cleanup_when_bucket_cleanup_disabled(self):
        plan = _plan([_bucket_def("b0", [_campaign_def("c0")], cleanup=False)])
        cs = _campaign_state(
            "c0", status="running", connectCampaignId="conn-1", segmentName="seg-1"
        )
        run = _run(plan, [_bucket_state("b0", [cs])])
        with (
            patch("executor.save_run"),
            patch("executor.unlock_plan_run"),
            patch("executor.update_plan_pending_warmup"),
            patch("executor._delete_bucket_schedule_safe"),
            patch("executor._safe_stop_campaign"),
            patch("executor._safe_delete_campaign") as mock_del_camp,
            patch("executor._safe_delete_segment") as mock_del_seg,
        ):
            executor._force_finish_internal(run, plan)

        mock_del_camp.assert_not_called()
        mock_del_seg.assert_not_called()


class TestForceFinishRun:
    def test_raises_when_run_not_found(self):
        with patch("executor.get_run", return_value=None):
            with pytest.raises(ValueError, match="not found"):
                executor.force_finish_run("p1", "r1")

    def test_returns_run_unchanged_when_already_completed(self):
        plan = _plan([_bucket_def("b0", [_campaign_def("c0")])])
        run = _run(plan, [_bucket_state("b0", [_campaign_state("c0")])], status="completed")
        with patch("executor.get_run", return_value=run):
            result = executor.force_finish_run("p1", "r1")
        assert result is run

    def test_returns_run_unchanged_when_already_aborted(self):
        plan = _plan([_bucket_def("b0", [_campaign_def("c0")])])
        run = _run(plan, [_bucket_state("b0", [_campaign_state("c0")])], status="aborted")
        with patch("executor.get_run", return_value=run):
            result = executor.force_finish_run("p1", "r1")
        assert result is run

    def test_raises_when_run_in_unexpected_status(self):
        plan = _plan([_bucket_def("b0", [_campaign_def("c0")])])
        run = _run(plan, [_bucket_state("b0", [_campaign_state("c0")])], status="pending")
        with patch("executor.get_run", return_value=run):
            with pytest.raises(ValueError, match="not running"):
                executor.force_finish_run("p1", "r1")

    def test_force_finishes_successfully(self):
        plan = _plan([_bucket_def("b0", [_campaign_def("c0")])])
        run = _run(plan, [_bucket_state("b0", [_campaign_state("c0", status="queued")])])
        with (
            patch("executor.get_run", return_value=run),
            patch("executor._force_finish_internal") as mock_internal,
        ):
            result = executor.force_finish_run("p1", "r1")
        assert result is run
        mock_internal.assert_called_once_with(run, plan)

    def test_falls_back_to_get_plan_when_snapshot_missing(self):
        run = {
            "planId": "p1",
            "runId": "r1",
            "status": "running",
            "planSnapshot": None,
            "bucketStates": [],
        }
        fallback_plan = _plan([])
        with (
            patch("executor.get_run", return_value=run),
            patch("executor.get_plan", return_value=fallback_plan),
            patch("executor._force_finish_internal") as mock_internal,
        ):
            executor.force_finish_run("p1", "r1")
        mock_internal.assert_called_once_with(run, fallback_plan)

    def test_retries_on_concurrent_write_error_then_succeeds(self):
        plan = _plan([_bucket_def("b0", [_campaign_def("c0")])])
        run = _run(plan, [_bucket_state("b0", [_campaign_state("c0", status="queued")])])
        with (
            patch("executor.get_run", return_value=run),
            patch(
                "executor._force_finish_internal",
                side_effect=[ConcurrentWriteError("race"), None],
            ),
        ):
            result = executor.force_finish_run("p1", "r1")
        assert result is run

    def test_reraises_concurrent_write_error_after_max_retries(self):
        plan = _plan([_bucket_def("b0", [_campaign_def("c0")])])
        run = _run(plan, [_bucket_state("b0", [_campaign_state("c0", status="queued")])])
        with (
            patch("executor.get_run", return_value=run),
            patch(
                "executor._force_finish_internal",
                side_effect=ConcurrentWriteError("race"),
            ),
        ):
            with pytest.raises(ConcurrentWriteError):
                executor.force_finish_run("p1", "r1")
