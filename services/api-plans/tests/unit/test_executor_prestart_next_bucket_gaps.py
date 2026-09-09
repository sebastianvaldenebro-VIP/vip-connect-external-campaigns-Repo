"""Targeted tests for _prestart_next_bucket's exception-handling branches:
mid-flight save failure, _RedisRebuildingError, _EmptySegmentError (retry vs
exhausted->redis-not-ready-reset vs exhausted->cancelled), and
_CutoffTooCloseError. Also covers the early return when there is no next
bucket.
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


def _setup(bucket_overrides=None):
    campaign = {"id": "c1", "name": "c1"}
    b0 = _bucket_def("b0", [{"id": "c0", "name": "c0"}])
    b1 = _bucket_def("b1", [campaign], **(bucket_overrides or {}))
    plan = _plan([b0, b1])
    run = _run(
        plan,
        [
            _bucket_state("b0", [_campaign_state("c0", status="running")], status="running"),
            _bucket_state("b1", [_campaign_state("c1", status="queued")], status="queued"),
        ],
    )
    return run, plan


class TestNoNextBucket:
    def test_returns_early_when_current_bucket_is_last(self):
        plan = _plan([_bucket_def("b0", [{"id": "c0", "name": "c0"}])])
        run = _run(plan, [_bucket_state("b0", [_campaign_state("c0")], status="running")])
        with patch("executor.save_run") as mock_save:
            executor._prestart_next_bucket(run, plan, 0)
        mock_save.assert_not_called()


class TestMidFlightSaveFailure:
    def test_logs_warning_but_continues_when_mid_flight_save_fails(self):
        run, plan = _setup()
        with (
            patch(
                "executor._create_campaign_only",
                return_value=("connect-1", "seg-1", "arn:seg", True, None, None),
            ),
            patch("executor.save_run", side_effect=[None, RuntimeError("DDB throttled")]),
        ):
            executor._prestart_next_bucket(run, plan, 0)  # must not raise

        cs = run["bucketStates"][1]["campaignStates"][0]
        assert cs["status"] == "warming"
        assert cs["connectCampaignId"] == "connect-1"


class TestRedisRebuildingError:
    def test_leaves_campaign_queued_on_transient_rebuild(self):
        run, plan = _setup()
        with (
            patch("executor._create_campaign_only", side_effect=executor._RedisRebuildingError("rebuilding")),
            patch("executor.save_run"),
        ):
            executor._prestart_next_bucket(run, plan, 0)

        cs = run["bucketStates"][1]["campaignStates"][0]
        assert cs["status"] == "queued"


class TestEmptySegmentError:
    def test_increments_retry_count_below_limit(self):
        run, plan = _setup({"reconcileRetryLimit": 5})
        cs = run["bucketStates"][1]["campaignStates"][0]
        cs["reconcileRetries"] = 2
        with (
            patch("executor._create_campaign_only", side_effect=executor._EmptySegmentError("empty")),
            patch("executor.save_run"),
        ):
            executor._prestart_next_bucket(run, plan, 0)

        assert cs["reconcileRetries"] == 3
        assert cs["status"] == "queued"

    def test_resets_retries_when_redis_not_ready_after_exhaustion(self):
        run, plan = _setup({"reconcileRetryLimit": 2})
        cs = run["bucketStates"][1]["campaignStates"][0]
        cs["reconcileRetries"] = 2  # already at limit
        with (
            patch("executor._create_campaign_only", side_effect=executor._EmptySegmentError("empty")),
            patch("executor.save_run"),
            patch("executor._check_redis_ready", return_value=False),
        ):
            executor._prestart_next_bucket(run, plan, 0)

        assert cs["reconcileRetries"] == 0
        assert cs["status"] == "queued"

    def test_cancels_campaign_when_redis_ready_after_exhaustion(self):
        run, plan = _setup({"reconcileRetryLimit": 2})
        cs = run["bucketStates"][1]["campaignStates"][0]
        cs["reconcileRetries"] = 2  # already at limit
        with (
            patch("executor._create_campaign_only", side_effect=executor._EmptySegmentError("empty")),
            patch("executor.save_run"),
            patch("executor._check_redis_ready", return_value=True),
        ):
            executor._prestart_next_bucket(run, plan, 0)

        assert cs["status"] == "cancelled"
        assert cs["exitReason"] == executor.REASON_SKIPPED_EMPTY


class TestCutoffTooCloseError:
    def test_leaves_campaign_queued_on_cutoff_too_close(self):
        run, plan = _setup()
        with (
            patch("executor._create_campaign_only", side_effect=executor._CutoffTooCloseError("too close")),
            patch("executor.save_run"),
        ):
            executor._prestart_next_bucket(run, plan, 0)

        cs = run["bucketStates"][1]["campaignStates"][0]
        assert cs["status"] == "queued"
