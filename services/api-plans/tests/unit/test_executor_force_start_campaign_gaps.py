"""Targeted tests for executor.force_start_campaign's validation branches and
final-save retry-exhaustion paths not already covered by test_executor_v2.py.
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


def _campaign_state(cid, status="queued", **overrides):
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
        "scheduleName": None,
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


class TestValidationErrors:
    def test_raises_when_run_not_found(self):
        with patch("executor.get_run", return_value=None):
            with pytest.raises(ValueError, match="not found"):
                executor.force_start_campaign("p1", "r1", 0, 0)

    def test_raises_when_run_not_running(self):
        run = _run(_plan([]), [], status="completed")
        with patch("executor.get_run", return_value=run):
            with pytest.raises(ValueError, match="is not running"):
                executor.force_start_campaign("p1", "r1", 0, 0)

    def test_raises_when_bucket_index_out_of_range(self):
        run = _run(_plan([]), [_bucket_state("b0", [])])
        with patch("executor.get_run", return_value=run):
            with pytest.raises(ValueError, match="Bucket index"):
                executor.force_start_campaign("p1", "r1", 5, 0)

    def test_raises_when_campaign_index_out_of_range(self):
        run = _run(_plan([]), [_bucket_state("b0", [_campaign_state("c0")])])
        with patch("executor.get_run", return_value=run):
            with pytest.raises(ValueError, match="Campaign index"):
                executor.force_start_campaign("p1", "r1", 0, 5)

    def test_raises_when_campaign_status_not_startable(self):
        run = _run(_plan([]), [_bucket_state("b0", [_campaign_state("c0", status="running")])])
        with patch("executor.get_run", return_value=run):
            with pytest.raises(ValueError, match="can only force-start"):
                executor.force_start_campaign("p1", "r1", 0, 0)

    def test_raises_when_bucket_status_invalid_for_force_start(self):
        run = _run(
            _plan([]),
            [_bucket_state("b0", [_campaign_state("c0", status="cancelled")], status="expired")],
        )
        with patch("executor.get_run", return_value=run):
            with pytest.raises(ValueError, match="requires an active or completed bucket"):
                executor.force_start_campaign("p1", "r1", 0, 0)


class TestScheduleTickExceptionSwallowing:
    def test_logs_but_continues_when_schedule_tick_fails_for_completed_bucket(self):
        plan = _plan([{"id": "b0", "campaigns": [{"id": "c0", "name": "c0"}]}])
        cs = _campaign_state("c0", status="cancelled")
        bs = _bucket_state("b0", [cs], status="completed")
        run = _run(plan, [bs])
        with (
            patch("executor.get_run", return_value=run),
            patch("executor._schedule_tick", side_effect=RuntimeError("Scheduler down")),
            patch("executor._reset_cascade_cancelled_children"),
            patch("executor.save_run"),
            patch("executor._start_one_campaign"),
        ):
            executor.force_start_campaign("p1", "r1", 0, 0)  # must not raise
        assert bs["status"] == "running"

    def test_logs_but_continues_when_schedule_tick_fails_for_queued_bucket(self):
        plan = _plan([{"id": "b0", "campaigns": [{"id": "c0", "name": "c0"}]}])
        cs = _campaign_state("c0", status="queued")
        bs = _bucket_state("b0", [cs], status="queued")
        run = _run(plan, [bs])
        with (
            patch("executor.get_run", return_value=run),
            patch("executor._schedule_tick", side_effect=RuntimeError("Scheduler down")),
            patch("executor._reset_cascade_cancelled_children"),
            patch("executor.save_run"),
            patch("executor._start_one_campaign"),
        ):
            executor.force_start_campaign("p1", "r1", 0, 0)  # must not raise
        assert bs["status"] == "running"


class TestWarmingSiblingCleanup:
    def test_stops_and_deletes_connect_campaign_for_warming_sibling(self):
        plan = _plan([{"id": "b0", "campaigns": [{"id": "c0", "name": "c0"}, {"id": "c1", "name": "c1"}]}])
        target = _campaign_state("c0", status="queued")
        sibling = _campaign_state("c1", status="warming", connectCampaignId="conn-sib")
        bs = _bucket_state("b0", [target, sibling], status="warming")
        run = _run(plan, [bs])
        with (
            patch("executor.get_run", return_value=run),
            patch("executor._schedule_tick", return_value="sched-1"),
            patch("executor._reset_cascade_cancelled_children"),
            patch("executor.save_run"),
            patch("executor._start_one_campaign"),
            patch("executor._safe_stop_campaign") as mock_stop,
            patch("executor._safe_delete_campaign") as mock_delete,
        ):
            executor.force_start_campaign("p1", "r1", 0, 0)

        mock_stop.assert_called_once_with("conn-sib")
        mock_delete.assert_called_once_with("conn-sib")
        assert sibling["status"] == "queued"
        assert sibling["connectCampaignId"] is None


class TestFinalSaveRetryExhaustion:
    """Phase 1 (the claim save) must succeed in these tests so the
    ConcurrentWriteError under test comes from the FINAL save loop, not
    Phase 1's own (unrelated, non-retrying) except block."""

    def test_raises_after_exhausting_final_save_retries(self):
        plan = _plan([{"id": "b0", "campaigns": [{"id": "c0", "name": "c0"}]}])
        cs = _campaign_state("c0", status="queued")
        bs = _bucket_state("b0", [cs], status="running")
        run = _run(plan, [bs])

        def _fresh_run(*_a, **_k):
            return _run(
                plan,
                [_bucket_state("b0", [_campaign_state("c0", status="creating")], status="running")],
            )

        # Phase 1 claim save succeeds (None); all 3 final-save attempts fail.
        with (
            patch("executor.get_run", side_effect=[run, _fresh_run(), _fresh_run()]),
            patch("executor._reset_cascade_cancelled_children"),
            patch(
                "executor.save_run",
                side_effect=[None, ConcurrentWriteError("race"), ConcurrentWriteError("race"), ConcurrentWriteError("race")],
            ),
            patch("executor._start_one_campaign"),
        ):
            with pytest.raises(ConcurrentWriteError, match="race"):
                executor.force_start_campaign("p1", "r1", 0, 0)

    def test_raises_when_run_disappears_during_final_save_retry(self):
        plan = _plan([{"id": "b0", "campaigns": [{"id": "c0", "name": "c0"}]}])
        cs = _campaign_state("c0", status="queued")
        bs = _bucket_state("b0", [cs], status="running")
        run = _run(plan, [bs])

        # Phase 1 claim save succeeds; first final-save attempt fails; the
        # retry's get_run() then returns None (run vanished).
        with (
            patch("executor.get_run", side_effect=[run, None]),
            patch("executor._reset_cascade_cancelled_children"),
            patch("executor.save_run", side_effect=[None, ConcurrentWriteError("race")]),
            patch("executor._start_one_campaign"),
        ):
            with pytest.raises(ValueError, match="not found after force_start_campaign retry"):
                executor.force_start_campaign("p1", "r1", 0, 0)

    def test_returns_run_when_concurrent_tick_already_adopted_campaign(self):
        """If a concurrent tick already adopted the same Connect campaign onto
        this campaign state (status=running, matching connectCampaignId), the
        retry loop must accept that as success rather than re-raising."""
        plan = _plan([{"id": "b0", "campaigns": [{"id": "c0", "name": "c0"}]}])
        cs = _campaign_state("c0", status="queued")
        bs = _bucket_state("b0", [cs], status="running")
        run = _run(plan, [bs])

        adopted_run = _run(
            plan,
            [
                _bucket_state(
                    "b0",
                    [_campaign_state("c0", status="running", connectCampaignId="conn-adopted")],
                    status="running",
                )
            ],
        )

        def _fake_start_one_campaign(_run, _plan, _bi, _ci):
            # Simulate _start_one_campaign having set connectCampaignId on cs.
            cs["connectCampaignId"] = "conn-adopted"
            cs["status"] = "running"

        # Phase 1 claim save succeeds; the FIRST final-save attempt fails,
        # triggering the retry's get_run() -> adopted_run.
        with (
            patch("executor.get_run", side_effect=[run, adopted_run]),
            patch("executor._reset_cascade_cancelled_children"),
            patch("executor.save_run", side_effect=[None, ConcurrentWriteError("race")]),
            patch("executor._start_one_campaign", side_effect=_fake_start_one_campaign),
        ):
            result = executor.force_start_campaign("p1", "r1", 0, 0)

        assert result is adopted_run
