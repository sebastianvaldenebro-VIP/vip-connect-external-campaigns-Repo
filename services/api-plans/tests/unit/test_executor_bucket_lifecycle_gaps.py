"""Targeted tests for remaining gaps in _start_bucket and
_activate_warming_bucket: parallel chain-starts, schedule_tick failure
re-raise, and the "cold start" (warmup not yet StartCampaign'd) success path.
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
sys.modules.setdefault("vip_shared.infrastructure.persistence.outbound_campaigns_client", MagicMock())

import executor  # noqa: E402


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


def _bucket_state(bid, campaign_states, status="queued", **overrides):
    bs = {
        "bucketId": bid,
        "name": bid,
        "status": status,
        "scheduleName": None,
        "startedAt": None,
        "completedAt": None,
        "campaignStates": campaign_states,
    }
    bs.update(overrides)
    return bs


def _bucket_def(bid, campaigns, **overrides):
    d = {"id": bid, "name": bid, "run_mode": "status_based", "duration_minutes": 30, "campaigns": campaigns}
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


class TestStartBucketParallelChain:
    def test_chain_starts_next_bucket_when_marked_parallel_and_queued(self):
        b0 = _bucket_def("b0", [{"id": "c0", "name": "c0"}])
        b1 = _bucket_def("b1", [{"id": "c1", "name": "c1"}], parallel=True)
        plan = _plan([b0, b1])
        run = _run(
            plan,
            [
                _bucket_state("b0", [_campaign_state("c0")]),
                _bucket_state("b1", [_campaign_state("c1")], status="queued"),
            ],
        )
        started_indices = []
        original_start_bucket = executor._start_bucket

        def _tracking_start_bucket(_run, index):
            started_indices.append(index)
            if index == 0:
                original_start_bucket(_run, index)

        with (
            patch("executor._schedule_tick", return_value="sched-1"),
            patch("executor.save_run"),
            patch("executor._dispatch_ready_campaigns", return_value=False),
            patch("executor._record_plan_event"),
            patch("executor._start_bucket", side_effect=_tracking_start_bucket) as mock_start,
        ):
            mock_start.side_effect = _tracking_start_bucket
            executor._start_bucket(run, 0)

        assert started_indices == [0, 1]

    def test_does_not_chain_start_when_next_bucket_not_parallel(self):
        b0 = _bucket_def("b0", [{"id": "c0", "name": "c0"}])
        b1 = _bucket_def("b1", [{"id": "c1", "name": "c1"}], parallel=False)
        plan = _plan([b0, b1])
        run = _run(
            plan,
            [
                _bucket_state("b0", [_campaign_state("c0")]),
                _bucket_state("b1", [_campaign_state("c1")], status="queued"),
            ],
        )
        with (
            patch("executor._schedule_tick", return_value="sched-1"),
            patch("executor.save_run"),
            patch("executor._dispatch_ready_campaigns", return_value=False),
            patch("executor._record_plan_event"),
        ):
            executor._start_bucket(run, 0)

        assert run["bucketStates"][1]["status"] == "queued"  # untouched


class TestActivateWarmingBucketAdditional:
    def test_reraises_when_schedule_tick_fails(self):
        plan = _plan([_bucket_def("b0", [{"id": "c0", "name": "c0"}])])
        run = _run(plan, [_bucket_state("b0", [_campaign_state("c0", status="warming")])])
        with (
            patch("executor._schedule_tick", side_effect=RuntimeError("Scheduler down")),
            patch("executor._record_plan_event"),
        ):
            with pytest.raises(RuntimeError, match="Scheduler down"):
                executor._activate_warming_bucket(run, plan, 0)

    def test_cold_starts_warming_campaign_without_warmup_started_flag(self):
        plan = _plan([_bucket_def("b0", [{"id": "c0", "name": "c0", "run_type": "full"}])])
        cs = _campaign_state(
            "c0", status="warming", connectCampaignId="conn-1", segmentName="seg-1"
        )
        run = _run(plan, [_bucket_state("b0", [cs])])
        mock_oc = MagicMock()
        with (
            patch("executor._schedule_tick", return_value="sched-1"),
            patch("executor._record_plan_event"),
            patch("executor._dispatch_ready_campaigns", return_value=False),
            patch("executor.save_run"),
            patch("executor._campaign_end_time", return_value="2026-05-08T12:00:00+00:00"),
            patch(
                "vip_shared.infrastructure.persistence.outbound_campaigns_client.build",
                return_value=mock_oc,
            ),
        ):
            executor._activate_warming_bucket(run, plan, 0)

        mock_oc.update_campaign_schedule.assert_called_once()
        mock_oc.start_campaign.assert_called_once_with("conn-1")
        assert cs["status"] == "running"

    def test_chain_starts_next_bucket_when_marked_parallel_and_queued(self):
        b0 = _bucket_def("b0", [{"id": "c0", "name": "c0"}])
        b1 = _bucket_def("b1", [{"id": "c1", "name": "c1"}], parallel=True)
        plan = _plan([b0, b1])
        run = _run(
            plan,
            [
                _bucket_state("b0", [_campaign_state("c0", status="completed")]),
                _bucket_state("b1", [_campaign_state("c1")], status="queued"),
            ],
        )
        with (
            patch("executor._schedule_tick", return_value="sched-1"),
            patch("executor._record_plan_event"),
            patch("executor._dispatch_ready_campaigns", return_value=False),
            patch("executor.save_run"),
            patch("executor._start_bucket") as mock_start_bucket,
        ):
            executor._activate_warming_bucket(run, plan, 0)

        mock_start_bucket.assert_called_once_with(run, 1)
