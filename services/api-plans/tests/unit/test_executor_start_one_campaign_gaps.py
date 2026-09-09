"""Targeted tests for remaining gaps in executor._start_one_campaign:

- branded DDB lock put_item raising a non-ClientError exception
- SMS-path sender failure (generic exception)
- pre-warmed campaign with warmupStarted=True (already Running in Connect)
- pre-warmed campaign fresh start succeeding
- pre-warmed campaign's "start time has already passed" recreate path
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


def _run_plan(campaign_def, cs, bucket_overrides=None, **run_overrides):
    bucket = {"id": "b0", "campaigns": [campaign_def]}
    if bucket_overrides:
        bucket.update(bucket_overrides)
    plan = {"planId": "p1", "buckets": [bucket]}
    run = {
        "planId": "p1",
        "runId": "r1",
        "bucketStates": [{"status": "running", "campaignStates": [cs]}],
    }
    run.update(run_overrides)
    return run, plan


class TestBrandedDdbLockGenericException:
    @pytest.fixture(autouse=True)
    def _branded_env(self):
        with (
            patch("executor._ACTIVE_BRANDED_CAMPAIGNS_TABLE", "VipActiveBrandedCampaigns"),
            patch("executor._CAMPAIGN_QUEUE_TABLE_BRANDED", "VipProgressiveCampaignQueue"),
        ):
            yield

    def test_non_client_error_on_put_item_sets_error_status(self):
        campaign = {
            "id": "c0",
            "name": "c0",
            "deliveryType": "branded",
            "campaignConfig": {
                "queueArn": "arn:aws:connect:::queue/q1",
                "contactFlowId": "flow-1",
                "sourcePhone": "+12125550199",
            },
        }
        cs = _campaign_state("c0", status="queued")
        run, plan = _run_plan(campaign, cs)

        with (
            patch("executor._get_ddb_client") as mock_get_ddb,
            patch("executor._emit_branded_metric") as mock_emit,
        ):
            mock_get_ddb.return_value.put_item.side_effect = RuntimeError("network blip")
            executor._start_one_campaign(run, plan, 0, 0)

        assert cs["status"] == "error"
        assert cs["exitReason"] == executor.REASON_ERROR
        assert cs["errorDetail"] == "RuntimeError"
        mock_emit.assert_called_once_with("BrandedStartError")


class TestSmsSenderFailure:
    def test_sms_sender_exception_sets_error_status(self):
        campaign = {
            "id": "c0",
            "name": "c0",
            "deliveryType": "sms",
            "pinnedSegmentArn": "arn:aws:connect:::instance/i1/segment/seg-1",
            "campaignConfig": {
                "smsMessageTemplate": "hi {firstName}",
                "smsOriginationNumberArn": "arn:aws:sms-voice:::phone-number/pn-1",
                "smsOriginationNumber": "+15125550100",
            },
        }
        cs = _campaign_state("c0", status="queued")
        run, plan = _run_plan(campaign, cs)

        with patch("executor._invoke_sms_sender", side_effect=RuntimeError("SQS down")):
            executor._start_one_campaign(run, plan, 0, 0)

        assert cs["status"] == "error"
        assert cs["exitReason"] == executor.REASON_ERROR
        assert cs["errorDetail"] == "RuntimeError"
        assert cs["completedAt"] is not None


class TestPrewarmedCampaignAlreadyStartedInConnect:
    def test_warmup_started_flag_marks_running_without_calling_connect(self):
        campaign = {"id": "c0", "name": "c0"}
        cs = _campaign_state(
            "c0", status="queued", connectCampaignId="conn-1", warmupStarted=True
        )
        run, plan = _run_plan(campaign, cs)

        with patch("executor._safe_stop_campaign") as mock_stop:
            executor._start_one_campaign(run, plan, 0, 0)

        assert cs["status"] == "running"
        assert "warmupStarted" not in cs
        mock_stop.assert_not_called()


class TestPrewarmedCampaignFreshStartSucceeds:
    def test_start_campaign_succeeds_sets_running(self):
        campaign = {"id": "c0", "name": "c0"}
        cs = _campaign_state("c0", status="queued", connectCampaignId="conn-1")
        run, plan = _run_plan(campaign, cs)

        oc_mock = MagicMock()
        oc_stub = MagicMock()
        oc_stub.build = MagicMock(return_value=oc_mock)
        modules_to_stub = [
            "vip_shared",
            "vip_shared.infrastructure",
            "vip_shared.infrastructure.persistence",
            "vip_shared.infrastructure.persistence.outbound_campaigns_client",
        ]
        originals = {m: sys.modules.get(m) for m in modules_to_stub}
        for m in modules_to_stub:
            sys.modules[m] = oc_stub
        try:
            executor._start_one_campaign(run, plan, 0, 0)
        finally:
            for m, orig in originals.items():
                if orig is None:
                    sys.modules.pop(m, None)
                else:
                    sys.modules[m] = orig

        assert cs["status"] == "running"
        oc_mock.update_campaign_schedule.assert_called_once()
        oc_mock.start_campaign.assert_called_once_with("conn-1")


class TestPrewarmedCampaignStartTimePassedRecreates:
    def test_start_time_passed_deletes_stale_campaign_and_falls_through(self):
        campaign = {"id": "c0", "name": "c0", "pinnedSegmentArn": "arn:seg/pinned-1"}
        cs = _campaign_state("c0", status="queued", connectCampaignId="conn-stale")
        run, plan = _run_plan(campaign, cs)

        oc_mock = MagicMock()
        oc_mock.start_campaign.side_effect = RuntimeError(
            "InvalidCampaignStateException: start time has already passed"
        )
        oc_stub = MagicMock()
        oc_stub.build = MagicMock(return_value=oc_mock)
        modules_to_stub = [
            "vip_shared",
            "vip_shared.infrastructure",
            "vip_shared.infrastructure.persistence",
            "vip_shared.infrastructure.persistence.outbound_campaigns_client",
        ]
        originals = {m: sys.modules.get(m) for m in modules_to_stub}
        for m in modules_to_stub:
            sys.modules[m] = oc_stub
        try:
            with (
                patch("executor._safe_stop_campaign") as mock_safe_stop,
                patch("executor._safe_delete_campaign") as mock_safe_delete,
                patch(
                    "executor._create_and_start_campaign",
                    return_value=("conn-new", "seg-new", "arn:seg-new", 1, 1),
                ),
            ):
                executor._start_one_campaign(run, plan, 0, 0)
        finally:
            for m, orig in originals.items():
                if orig is None:
                    sys.modules.pop(m, None)
                else:
                    sys.modules[m] = orig

        mock_safe_stop.assert_called_once_with("conn-stale")
        mock_safe_delete.assert_called_once_with("conn-stale")
        # Fell through to a fresh start — connectCampaignId is no longer the stale one.
        assert cs["connectCampaignId"] != "conn-stale"


class TestEmptySegmentRetriesExhaustedRedisNotReady:
    def test_resets_and_leaves_queued_when_redis_not_ready(self):
        campaign = {"id": "c0", "name": "c0"}
        cs = _campaign_state("c0", status="queued")
        run, plan = _run_plan(campaign, cs, bucket_overrides={"reconcileRetryLimit": 0})

        with (
            patch(
                "executor._create_segment",
                side_effect=executor._EmptySegmentError("No leads"),
            ),
            patch("executor._check_redis_ready", return_value=False),
        ):
            executor._start_one_campaign(run, plan, 0, 0)

        assert cs["status"] == "queued"
        assert cs["reconcileRetries"] == 0


class TestGenericSegmentErrorExhaustedRetries:
    def test_sets_error_after_exhausting_retries(self):
        campaign = {"id": "c0", "name": "c0"}
        cs = _campaign_state("c0", status="queued")
        run, plan = _run_plan(campaign, cs, bucket_overrides={"reconcileRetryLimit": 0})

        with patch(
            "executor._create_segment",
            side_effect=RuntimeError("Redis connection refused"),
        ):
            executor._start_one_campaign(run, plan, 0, 0)

        assert cs["status"] == "error"
        assert cs["exitReason"] == executor.REASON_CREATION_FAILED
        assert "Redis connection refused" in cs["errorDetail"]

    def test_uses_reconcile_failed_reason_when_bucket_configured_to_fail(self):
        campaign = {"id": "c0", "name": "c0"}
        cs = _campaign_state("c0", status="queued")
        run, plan = _run_plan(
            campaign,
            cs,
            bucket_overrides={"reconcileRetryLimit": 0, "onReconcileExhausted": "fail"},
        )

        with patch(
            "executor._create_segment",
            side_effect=RuntimeError("Redis connection refused"),
        ):
            executor._start_one_campaign(run, plan, 0, 0)

        assert cs["status"] == "error"
        assert cs["exitReason"] == executor.REASON_RECONCILE_FAILED


class TestMidFlightSaveBreaksWhenRunDisappears:
    def test_breaks_early_when_get_run_returns_none(self):
        from store import ConcurrentWriteError

        campaign = {"id": "c0", "name": "c0"}
        cs = _campaign_state("c0", status="queued")
        run, plan = _run_plan(campaign, cs)
        run["_version"] = 3

        with (
            patch("executor._create_segment", return_value=("seg", "arn:seg", 1, 1)),
            patch(
                "executor._create_and_start_campaign",
                return_value=("conn-new", "seg"),
            ),
            patch(
                "executor.save_run",
                side_effect=ConcurrentWriteError("version conflict"),
            ) as mock_save,
            patch("executor.get_run", return_value=None),
        ):
            executor._start_one_campaign(run, plan, 0, 0)

        # Only one mid-flight save attempt: get_run returning None broke the retry loop.
        assert mock_save.call_count == 1
        assert cs["status"] == "running"
        assert cs["connectCampaignId"] == "conn-new"


class TestCreateAndStartCampaignRaisesEmptySegment:
    def test_deletes_segment_and_cancels_when_not_pinned(self):
        campaign = {"id": "c0", "name": "c0"}
        cs = _campaign_state("c0", status="queued")
        run, plan = _run_plan(campaign, cs)

        with (
            patch("executor._create_segment", return_value=("seg-1", "arn:seg-1", 1, 1)),
            patch(
                "executor._create_and_start_campaign",
                side_effect=executor._EmptySegmentError("emptied after creation"),
            ),
            patch("executor._safe_delete_segment") as mock_delete_seg,
        ):
            executor._start_one_campaign(run, plan, 0, 0)

        mock_delete_seg.assert_called_once_with("seg-1")
        assert cs["status"] == "cancelled"
        assert cs["exitReason"] == executor.REASON_SKIPPED_EMPTY

    def test_skips_delete_when_segment_is_pinned(self):
        campaign = {"id": "c0", "name": "c0", "pinnedSegmentArn": "arn:seg/pinned-x"}
        cs = _campaign_state("c0", status="queued")
        run, plan = _run_plan(campaign, cs)

        with (
            patch(
                "executor._create_and_start_campaign",
                side_effect=executor._EmptySegmentError("emptied after creation"),
            ),
            patch("executor._safe_delete_segment") as mock_delete_seg,
        ):
            executor._start_one_campaign(run, plan, 0, 0)

        mock_delete_seg.assert_not_called()
        assert cs["status"] == "cancelled"
        assert cs["exitReason"] == executor.REASON_SKIPPED_EMPTY
