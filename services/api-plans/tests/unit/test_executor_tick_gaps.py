"""Targeted tests for specific executor.tick() branches not otherwise covered:
run/plan not-found early-outs, telephony afterCampaign-prewarm + force-stop
after duration+2, SMS queue polling (drain/error/pending), cross-plan prewarm
exception-swallowing (time-based and status-based last-bucket paths),
_dispatch_cross_bucket_ready save_run, and the external-campaign-deletion
abort path.
"""

from __future__ import annotations

import os
import sys
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

sys.modules.setdefault("vip_shared", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.persistence", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.persistence.audit", MagicMock())

import executor  # noqa: E402, F401


def _campaign_def(cid, **overrides):
    d = {"id": cid, "name": cid, "states": ["NY"], "groups": [], "dependsOn": []}
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


def _bucket_def(bid, campaigns, run_mode="status_based", duration=30, **overrides):
    d = {
        "id": bid,
        "name": bid,
        "run_mode": run_mode,
        "duration_minutes": duration,
        "prestart_next": False,
        "campaigns": campaigns,
    }
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


def _run(plan, bucket_states, bucket_index=0, **overrides):
    r = {
        "planId": "plan-1",
        "runId": "run-1",
        "status": "running",
        "planSnapshot": plan,
        "currentBucketIndex": bucket_index,
        "bucketStates": bucket_states,
        "startedAt": "2026-05-08T10:00:00+00:00",
        "completedAt": None,
        "_version": 0,
        "triggeredBy": "manual",
        "error": None,
    }
    r.update(overrides)
    return r


# Common patches applied to isolate tick()'s own branch logic from the
# (separately-tested) dispatch/advance machinery. Entered via an ExitStack in
# each test so they compose cleanly with test-specific patches.
def _enter_base_patches(stack: ExitStack) -> None:
    stack.enter_context(patch("executor.save_run"))
    stack.enter_context(patch("executor.unlock_plan_run"))
    stack.enter_context(patch("executor._delete_bucket_schedule_safe"))
    stack.enter_context(patch("executor._dispatch_ready_campaigns", return_value=False))
    stack.enter_context(patch("executor._dispatch_cross_bucket_ready", return_value=False))
    stack.enter_context(patch("executor._all_campaigns_terminal", return_value=False))
    stack.enter_context(patch("executor._fire_campaign_chains"))
    # _force_finish_internal (reachable from tick()'s prewarm/force-stop paths in
    # this file) unconditionally clears pending warmup via a real DynamoDB
    # UpdateItem — not mocked here previously, so it silently succeeded against
    # whatever real AWS credentials happened to be on the machine running the
    # suite and only failed in CI (no credentials at all). Mock it like every
    # other DynamoDB write in this base patch set.
    stack.enter_context(patch("executor.update_plan_pending_warmup"))


class TestTickNotFoundBranches:
    def test_returns_run_not_found(self):
        with patch("executor.get_run", return_value=None):
            result = executor.tick("p1", "r1", 0)
        assert result == {"ok": False, "reason": "run_not_found"}

    def test_returns_no_plan_when_snapshot_and_get_plan_both_missing(self):
        bucket_state = _bucket_state("b0", [_campaign_state("c0", status="queued")])
        run = {
            "planId": "p1",
            "runId": "r1",
            "status": "running",
            "planSnapshot": None,
            "bucketStates": [bucket_state],
        }
        with (
            patch("executor.get_run", return_value=run),
            patch("executor.get_plan", return_value=None),
        ):
            result = executor.tick("p1", "r1", 0)
        assert result == {"ok": False, "reason": "no_plan"}


class TestTelephonyAfterCampaignPrewarmAndForceStop:
    def _setup(self, elapsed_minutes: float):
        started = (datetime.now(timezone.utc) - timedelta(minutes=elapsed_minutes)).isoformat()
        campaign = _campaign_def("c0", duration_minutes=30)
        cs = _campaign_state(
            "c0", status="running", connectCampaignId="connect-1", startedAt=started
        )
        bucket = _bucket_def("b0", [campaign])
        bucket_state = _bucket_state("b0", [cs], startedAt=started)
        plan = _plan([bucket])
        run = _run(plan, [bucket_state])
        return run, plan, cs

    def test_prewarms_after_campaign_when_5_min_from_ending(self):
        run, plan, cs = self._setup(elapsed_minutes=26)  # 26 >= 30-5
        with ExitStack() as stack:
            _enter_base_patches(stack)
            stack.enter_context(patch("executor.get_run", return_value=run))
            stack.enter_context(patch("executor._poll_campaign_state"))  # leaves status "running"
            mock_prewarm = stack.enter_context(patch("executor._prestart_after_campaign"))
            executor.tick("p1", "r1", 0)
        mock_prewarm.assert_called_once_with("p1", "c0")
        assert cs["afterCampaignPrewarmed"] is True

    def test_prewarm_failure_emits_metric_and_does_not_raise(self):
        run, plan, cs = self._setup(elapsed_minutes=26)
        with ExitStack() as stack:
            _enter_base_patches(stack)
            stack.enter_context(patch("executor.get_run", return_value=run))
            stack.enter_context(patch("executor._poll_campaign_state"))
            stack.enter_context(
                patch("executor._prestart_after_campaign", side_effect=RuntimeError("boom"))
            )
            mock_emit = stack.enter_context(patch("executor._emit_prewarm_failure"))
            executor.tick("p1", "r1", 0)  # must not raise
        mock_emit.assert_called_once_with("p1")

    def test_does_not_reprewarm_when_already_prewarmed(self):
        run, plan, cs = self._setup(elapsed_minutes=26)
        cs["afterCampaignPrewarmed"] = True
        with ExitStack() as stack:
            _enter_base_patches(stack)
            stack.enter_context(patch("executor.get_run", return_value=run))
            stack.enter_context(patch("executor._poll_campaign_state"))
            mock_prewarm = stack.enter_context(patch("executor._prestart_after_campaign"))
            executor.tick("p1", "r1", 0)
        mock_prewarm.assert_not_called()

    def test_force_stops_telephony_campaign_running_past_duration_plus_2(self):
        run, plan, cs = self._setup(elapsed_minutes=33)  # > 30 + 2
        cs["afterCampaignPrewarmed"] = True  # skip prewarm branch for isolation
        with ExitStack() as stack:
            _enter_base_patches(stack)
            stack.enter_context(patch("executor.get_run", return_value=run))
            stack.enter_context(patch("executor._poll_campaign_state"))
            mock_stop = stack.enter_context(patch("executor._safe_stop_campaign"))
            executor.tick("p1", "r1", 0)
        mock_stop.assert_called_once_with("connect-1")


class TestSmsQueuePolling:
    def _setup(self):
        campaign = _campaign_def("c0")
        cs = _campaign_state("c0", status="running", smsCampaignId="sms-1")
        bucket = _bucket_def("b0", [campaign])
        bucket_state = _bucket_state("b0", [cs])
        plan = _plan([bucket])
        run = _run(plan, [bucket_state])
        return run, cs

    def test_completes_when_sms_queue_drained(self):
        run, cs = self._setup()
        with ExitStack() as stack:
            _enter_base_patches(stack)
            stack.enter_context(patch("executor.get_run", return_value=run))
            stack.enter_context(patch("executor._count_sms_queue", return_value=0))
            mock_complete = stack.enter_context(patch("executor._complete_sms_campaign"))
            executor.tick("p1", "r1", 0)
        assert cs["status"] == "completed"
        assert cs["exitReason"] == "queue_drained"
        mock_complete.assert_called_once_with(cs)

    def test_leaves_running_when_sms_queue_still_pending(self):
        run, cs = self._setup()
        with ExitStack() as stack:
            _enter_base_patches(stack)
            stack.enter_context(patch("executor.get_run", return_value=run))
            stack.enter_context(patch("executor._count_sms_queue", return_value=5))
            executor.tick("p1", "r1", 0)
        assert cs["status"] == "running"

    def test_swallows_sms_poll_error_and_leaves_running(self):
        run, cs = self._setup()
        with ExitStack() as stack:
            _enter_base_patches(stack)
            stack.enter_context(patch("executor.get_run", return_value=run))
            stack.enter_context(
                patch("executor._count_sms_queue", side_effect=RuntimeError("DDB error"))
            )
            executor.tick("p1", "r1", 0)  # must not raise
        assert cs["status"] == "running"


class TestCrossPlanPrewarmExceptionSwallowing:
    def test_time_based_last_bucket_prewarm_failure_is_logged_not_raised(self):
        campaign = _campaign_def("c0")
        cs = _campaign_state("c0", status="completed")
        started = (datetime.now(timezone.utc) - timedelta(minutes=26)).isoformat()
        bucket = _bucket_def("b0", [campaign], run_mode="time_based", duration=30, prestart_next=False)
        bucket_state = _bucket_state("b0", [cs], startedAt=started)
        plan = _plan([bucket])
        run = _run(plan, [bucket_state])

        with ExitStack() as stack:
            _enter_base_patches(stack)
            stack.enter_context(patch("executor.get_run", return_value=run))
            stack.enter_context(
                patch("executor._prestart_chained_runs", side_effect=RuntimeError("boom"))
            )
            stack.enter_context(patch("executor._expire_bucket"))
            executor.tick("p1", "r1", 0)  # must not raise

    def test_status_based_last_bucket_prewarm_failure_is_logged_not_raised(self):
        campaign = _campaign_def("c0", duration_minutes=30)
        cs = _campaign_state("c0", status="completed")
        started = (datetime.now(timezone.utc) - timedelta(minutes=26)).isoformat()
        bucket = _bucket_def("b0", [campaign], run_mode="status_based", prestart_next=False)
        bucket_state = _bucket_state("b0", [cs], startedAt=started)
        plan = _plan([bucket])
        run = _run(plan, [bucket_state])

        with ExitStack() as stack:
            _enter_base_patches(stack)
            stack.enter_context(patch("executor.get_run", return_value=run))
            stack.enter_context(
                patch("executor._prestart_chained_runs", side_effect=RuntimeError("boom"))
            )
            executor.tick("p1", "r1", 0)  # must not raise


class TestDispatchCrossBucketReadySavesRun:
    def test_saves_run_when_cross_bucket_dispatch_changed_state(self):
        campaign = _campaign_def("c0")
        cs = _campaign_state("c0", status="queued")
        bucket = _bucket_def("b0", [campaign], prestart_next=False)
        bucket_state = _bucket_state("b0", [cs])
        plan = _plan([bucket])
        run = _run(plan, [bucket_state])

        with (
            patch("executor.get_run", return_value=run),
            patch("executor.unlock_plan_run"),
            patch("executor._delete_bucket_schedule_safe"),
            patch("executor._dispatch_ready_campaigns", return_value=False),
            patch("executor._dispatch_cross_bucket_ready", return_value=True),
            patch("executor._all_campaigns_terminal", return_value=False),
            patch("executor._fire_campaign_chains"),
            patch("executor.save_run") as mock_save,
        ):
            executor.tick("p1", "r1", 0)

        # save_run is called both from the _dispatch_cross_bucket_ready branch
        # (line under test) AND again from tick()'s own final fallthrough save
        # — assert the branch-specific call happened, not an exact call count.
        assert any(call.args == (run,) for call in mock_save.call_args_list)
        assert mock_save.call_count == 2


class TestExternalDeletionAbort:
    def test_aborts_run_when_all_deleted_and_none_completed(self):
        campaign = _campaign_def("c0")
        cs = _campaign_state(
            "c0", status="error", exitReason="connect_deleted"
        )
        bucket = _bucket_def("b0", [campaign], prestart_next=False)
        bucket_state = _bucket_state("b0", [cs])
        plan = _plan([bucket])
        run = _run(plan, [bucket_state])

        with (
            patch("executor.get_run", return_value=run),
            patch("executor.unlock_plan_run") as mock_unlock,
            patch("executor._delete_bucket_schedule_safe"),
            patch("executor._dispatch_ready_campaigns", return_value=False),
            patch("executor._dispatch_cross_bucket_ready", return_value=False),
            patch("executor._all_campaigns_terminal", return_value=True),
            patch("executor._fire_campaign_chains"),
            patch("executor.save_run") ,
            patch("executor._notify_sns") as mock_notify,
            patch("executor._advance_bucket") as mock_advance,
        ):
            result = executor.tick("p1", "r1", 0)

        assert result == {"ok": False, "reason": "aborted_external_deletion"}
        assert run["status"] == "aborted"
        assert run["abortReason"] == "external_campaign_deletion"
        mock_unlock.assert_called_once_with("p1")
        mock_notify.assert_called_once()
        mock_advance.assert_not_called()

    def test_advances_normally_when_some_completed_despite_deletions(self):
        campaign1 = _campaign_def("c0")
        campaign2 = _campaign_def("c1")
        cs1 = _campaign_state("c0", status="completed")
        cs2 = _campaign_state("c1", status="error", exitReason="connect_deleted")
        bucket = _bucket_def("b0", [campaign1, campaign2], prestart_next=False)
        bucket_state = _bucket_state("b0", [cs1, cs2])
        plan = _plan([bucket])
        run = _run(plan, [bucket_state])

        with (
            patch("executor.get_run", return_value=run),
            patch("executor.unlock_plan_run") as mock_unlock,
            patch("executor._delete_bucket_schedule_safe"),
            patch("executor._dispatch_ready_campaigns", return_value=False),
            patch("executor._dispatch_cross_bucket_ready", return_value=False),
            patch("executor._all_campaigns_terminal", return_value=True),
            patch("executor._fire_campaign_chains"),
            patch("executor.save_run"),
            patch("executor._advance_bucket") as mock_advance,
        ):
            result = executor.tick("p1", "r1", 0)

        assert result == {"ok": True, "reason": "advanced"}
        mock_advance.assert_called_once()
        mock_unlock.assert_not_called()
