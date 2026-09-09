"""Tests for executor.force_start_bucket and executor.force_stop_bucket
(the real state-machine functions — not the HTTP handler wrappers in
handlers/runs.py, which mock these away entirely)."""

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


class TestForceStartBucket:
    def test_raises_when_run_not_found(self):
        with patch("executor.get_run", return_value=None):
            with pytest.raises(ValueError, match="not found"):
                executor.force_start_bucket("p1", "r1", 0)

    def test_raises_when_run_not_running(self):
        run = _run(_plan([]), [], status="completed")
        with patch("executor.get_run", return_value=run):
            with pytest.raises(ValueError, match="is not running"):
                executor.force_start_bucket("p1", "r1", 0)

    def test_raises_when_bucket_index_out_of_range(self):
        run = _run(_plan([]), [_bucket_state("b0", [])])
        with patch("executor.get_run", return_value=run):
            with pytest.raises(ValueError, match="out of range"):
                executor.force_start_bucket("p1", "r1", 5)

    def test_raises_when_bucket_not_queued_or_warming(self):
        run = _run(_plan([]), [_bucket_state("b0", [], status="running")])
        with patch("executor.get_run", return_value=run):
            with pytest.raises(ValueError, match="cannot force-start"):
                executor.force_start_bucket("p1", "r1", 0)

    def test_starts_queued_bucket_directly(self):
        run = _run(_plan([]), [_bucket_state("b0", [_campaign_state("c0")], status="queued")])
        with (
            patch("executor.get_run", return_value=run),
            patch("executor._start_bucket") as mock_start,
        ):
            result = executor.force_start_bucket("p1", "r1", 0)
        assert result is run
        mock_start.assert_called_once_with(run, 0)

    def test_resets_warming_campaigns_to_queued_and_cleans_up_connect(self):
        cs = _campaign_state(
            "c0", status="warming", connectCampaignId="conn-1", segmentName="seg-1", segmentArn="arn:seg"
        )
        bs = _bucket_state("b0", [cs], status="warming")
        run = _run(_plan([]), [bs])
        with (
            patch("executor.get_run", return_value=run),
            patch("executor._safe_stop_campaign") as mock_stop,
            patch("executor._safe_delete_campaign") as mock_delete,
            patch("executor._start_bucket") as mock_start,
        ):
            executor.force_start_bucket("p1", "r1", 0)

        mock_stop.assert_called_once_with("conn-1")
        mock_delete.assert_called_once_with("conn-1")
        assert cs["status"] == "queued"
        assert cs["connectCampaignId"] is None
        assert cs["segmentName"] is None
        assert cs["segmentArn"] is None
        mock_start.assert_called_once_with(run, 0)

    def test_resets_already_queued_campaigns_in_warming_bucket_without_connect_cleanup(self):
        """A campaign already 'queued' within a warming bucket (never actually
        started warming itself) must still be reset, but with no Connect
        cleanup since it has no connectCampaignId."""
        cs = _campaign_state("c0", status="queued")
        bs = _bucket_state("b0", [cs], status="warming")
        run = _run(_plan([]), [bs])
        with (
            patch("executor.get_run", return_value=run),
            patch("executor._safe_stop_campaign") as mock_stop,
            patch("executor._safe_delete_campaign") as mock_delete,
            patch("executor._start_bucket"),
        ):
            executor.force_start_bucket("p1", "r1", 0)

        mock_stop.assert_not_called()
        mock_delete.assert_not_called()
        assert cs["status"] == "queued"


class TestForceStopBucket:
    def test_raises_when_run_not_found(self):
        with patch("executor.get_run", return_value=None):
            with pytest.raises(ValueError, match="not found"):
                executor.force_stop_bucket("p1", "r1", 0)

    def test_raises_when_run_not_running(self):
        run = _run(_plan([]), [], status="aborted")
        with patch("executor.get_run", return_value=run):
            with pytest.raises(ValueError, match="is not running"):
                executor.force_stop_bucket("p1", "r1", 0)

    def test_raises_when_bucket_index_out_of_range(self):
        run = _run(_plan([]), [_bucket_state("b0", [])])
        with patch("executor.get_run", return_value=run):
            with pytest.raises(ValueError, match="out of range"):
                executor.force_stop_bucket("p1", "r1", 5)

    def test_raises_when_bucket_not_active(self):
        run = _run(_plan([]), [_bucket_state("b0", [], status="queued")])
        with patch("executor.get_run", return_value=run):
            with pytest.raises(ValueError, match="not active"):
                executor.force_stop_bucket("p1", "r1", 0)

    def test_expires_active_bucket_with_force_stopped_reason(self):
        plan = _plan([{"id": "b0", "campaigns": []}])
        run = _run(plan, [_bucket_state("b0", [], status="running")])
        with (
            patch("executor.get_run", return_value=run),
            patch("executor._expire_bucket") as mock_expire,
        ):
            result = executor.force_stop_bucket("p1", "r1", 0)
        assert result is run
        mock_expire.assert_called_once_with(run, plan, 0, reason="force_stopped")

    def test_falls_back_to_get_plan_when_snapshot_missing(self):
        run = {
            "planId": "p1",
            "runId": "r1",
            "status": "running",
            "planSnapshot": None,
            "bucketStates": [_bucket_state("b0", [], status="warming")],
        }
        fallback_plan = _plan([{"id": "b0", "campaigns": []}])
        with (
            patch("executor.get_run", return_value=run),
            patch("executor.get_plan", return_value=fallback_plan),
            patch("executor._expire_bucket") as mock_expire,
        ):
            executor.force_stop_bucket("p1", "r1", 0)
        mock_expire.assert_called_once_with(run, fallback_plan, 0, reason="force_stopped")
