"""Targeted tests for remaining gaps in _dispatch_cross_bucket_ready
(non-queued campaign state skip) and _dispatch_ready_campaigns (Phase 1
recovery when checking Connect state itself raises).
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


class TestDispatchCrossBucketReadySkipsNonQueuedCampaign:
    def test_skips_campaign_not_in_queued_status(self):
        b0 = {"id": "b0", "campaigns": [{"id": "c0", "name": "c0"}]}
        b1 = {
            "id": "b1",
            "campaigns": [{"id": "c1", "name": "c1", "dependsOn": ["c0"]}],
        }
        plan = _plan([b0, b1])
        run = _run(
            plan,
            [
                _bucket_state("b0", [_campaign_state("c0", status="completed")], status="running"),
                _bucket_state(
                    "b1",
                    [_campaign_state("c1", status="running", connectCampaignId="conn-1")],
                    status="queued",
                ),
            ],
        )
        with patch("executor.save_run") as mock_save:
            result = executor._dispatch_cross_bucket_ready(run, plan, 0)

        assert result is False
        mock_save.assert_not_called()
        # Bucket must remain untouched since the only campaign wasn't queued.
        assert run["bucketStates"][1]["status"] == "queued"


class TestDispatchReadyCampaignsRecoveryConnectStateCheckFails:
    def test_resets_to_queued_when_connect_state_check_raises(self):
        # A dependsOn on a non-existent parent keeps Phase 2 from ever
        # considering this campaign "ready" again this call, so we can
        # isolate Phase 1's own recovery branch in the exception path.
        plan = _plan(
            [{"id": "b0", "campaigns": [{"id": "c0", "name": "c0", "dependsOn": ["never-satisfied"]}]}]
        )
        cs = _campaign_state(
            "c0",
            status="creating",
            connectCampaignId="conn-1",
            segmentArn="arn:seg",
            segmentName="seg-1",
            reconcileRetries=2,
        )
        run = _run(plan, [_bucket_state("b0", [cs], status="running")])
        with (
            patch("executor._get_campaign_state", side_effect=RuntimeError("Connect unavailable")),
            patch("executor.save_run"),
        ):
            executor._dispatch_ready_campaigns(run, plan, 0)

        assert cs["status"] == "queued"
        assert cs["connectCampaignId"] is None
        assert cs["segmentArn"] is None
        assert cs["segmentName"] is None
        assert cs["reconcileRetries"] == 0
