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

        with patch("executor._invoke_sms_sender", side_effect=RuntimeError("SQS down")), patch("executor.save_run"), patch("executor._stop_sms_campaign"):
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


def _stub_oc(mock_oc=None):
    if mock_oc is None:
        mock_oc = MagicMock()
    oc_stub = MagicMock()
    oc_stub.build = MagicMock(return_value=mock_oc)
    modules_to_stub = [
        "vip_shared",
        "vip_shared.infrastructure",
        "vip_shared.infrastructure.persistence",
        "vip_shared.infrastructure.persistence.outbound_campaigns_client",
    ]
    originals = {m: sys.modules.get(m) for m in modules_to_stub}
    for m in modules_to_stub:
        sys.modules[m] = oc_stub
    return mock_oc, originals


def _unstub_oc(originals):
    for m, orig in originals.items():
        if orig is None:
            sys.modules.pop(m, None)
        else:
            sys.modules[m] = orig


class TestBrandedPrecallSmsFiresBeforeSeeder:
    """Touch point D (2026-09 adversarial-review fix, finding #2): branded
    validated as precall-SMS-eligible but bypasses Connect V2 entirely, so it
    never reached the 'warming' status the Connect-gated helper checks for.
    Fix is pure sequencing — fire the SMS before _invoke_seeder. No pause/resume
    gate object is used or needed: branded's cs has no connectCampaignId."""

    @pytest.fixture(autouse=True)
    def _branded_env(self):
        with (
            patch("executor.save_run"),
            patch("executor._ACTIVE_BRANDED_CAMPAIGNS_TABLE", "VipActiveBrandedCampaigns"),
            patch("executor._CAMPAIGN_QUEUE_TABLE_BRANDED", "VipProgressiveCampaignQueue"),
        ):
            yield

    def _branded_campaign(self, precall_enabled=True):
        return {
            "id": "bc-1",
            "name": "Branded Test",
            "deliveryType": "branded",
            "campaignConfig": {
                "dialerType": "progressive",
                "queueArn": "arn:aws:connect:::queue/q1",
                "contactFlowId": "flow-abc",
                "sourcePhone": "+12125550199",
                "precallSms": {
                    "enabled": precall_enabled,
                    "messageTemplate": "Hi {{FirstName}}",
                    "originationNumberArn": "arn:pn",
                    "clinicName": "VIP Clinic",
                },
            },
        }

    def test_fires_sms_before_invoking_seeder_with_no_pause_or_resume(self):
        campaign = self._branded_campaign()
        cs = _campaign_state("bc-1", status="queued")
        run, plan = _run_plan(campaign, cs)
        order = []

        with (
            patch("executor._get_ddb_client"),
            patch(
                "executor._create_segment",
                return_value=("seg-name", "seg-arn", 1, 1),
            ),
            patch(
                "executor._invoke_sms_sender",
                side_effect=lambda **k: order.append("sms"),
            ),
            patch(
                "executor._invoke_seeder",
                side_effect=lambda **k: (order.append("seed"), 5)[1],
            ),
            patch("executor._write_branded_run_start"),
            patch("executor._emit_branded_metric"),
        ):
            executor._start_one_campaign(run, plan, 0, 0)

        assert order == ["sms", "seed"]
        assert cs["precallSmsSentAt"]
        assert cs["status"] == "running"
        # Branded never touches Connect V2's pause/resume at all.
        assert "precallGateResumedAt" not in cs

    def test_fires_sms_with_pinned_segment_arn(self):
        campaign = self._branded_campaign()
        campaign["pinnedSegmentArn"] = "arn:cp:pinned/seg-x"
        cs = _campaign_state("bc-1", status="queued")
        run, plan = _run_plan(campaign, cs)

        with (
            patch("executor._get_ddb_client"),
            patch("executor._invoke_sms_sender") as invoke_sms,
            patch("executor._invoke_seeder", return_value=5),
            patch("executor._write_branded_run_start"),
            patch("executor._emit_branded_metric"),
        ):
            executor._start_one_campaign(run, plan, 0, 0)

        invoke_sms.assert_called_once()
        assert invoke_sms.call_args.kwargs["segmentArn"] == "arn:cp:pinned/seg-x"
        assert invoke_sms.call_args.kwargs["segmentName"] == "seg-x"

    def test_precall_disabled_never_fires_sms(self):
        campaign = self._branded_campaign(precall_enabled=False)
        cs = _campaign_state("bc-1", status="queued")
        run, plan = _run_plan(campaign, cs)

        with (
            patch("executor._get_ddb_client"),
            patch(
                "executor._create_segment",
                return_value=("seg-name", "seg-arn", 1, 1),
            ),
            patch("executor._invoke_sms_sender") as invoke_sms,
            patch("executor._invoke_seeder", return_value=5),
            patch("executor._write_branded_run_start"),
            patch("executor._emit_branded_metric"),
        ):
            executor._start_one_campaign(run, plan, 0, 0)

        invoke_sms.assert_not_called()
        assert cs["status"] == "running"

    def test_sms_send_failure_does_not_block_seeding(self):
        """A failed pre-call text must degrade to 'no text', never to 'no call' —
        branded's own version of the same invariant _fire_precall_sms_for_campaign
        enforces for the Connect-gated path."""
        campaign = self._branded_campaign()
        cs = _campaign_state("bc-1", status="queued")
        run, plan = _run_plan(campaign, cs)
        order = []

        with (
            patch("executor._get_ddb_client"),
            patch(
                "executor._create_segment",
                return_value=("seg-name", "seg-arn", 1, 1),
            ),
            patch("executor._invoke_sms_sender", side_effect=RuntimeError("SNS down")),
            patch(
                "executor._invoke_seeder",
                side_effect=lambda **k: (order.append("seed"), 5)[1],
            ),
            patch("executor._write_branded_run_start"),
            patch("executor._emit_branded_metric"),
        ):
            executor._start_one_campaign(run, plan, 0, 0)  # must not raise

        assert order == ["seed"]  # seeding still happened despite the send failure
        assert cs.get("precallSmsSentAt") is None
        assert cs["status"] == "running"


class TestPrewarmedWarmupStartedCallsFirePrecallGate:
    """Touch point E, sub-case 1 (2026-09 adversarial-review fix): this
    early-return branch used to leave without ever calling anything precall-SMS-
    related — a campaign reached this way (not through
    _activate_warming_bucket) would never resume if it had been paused."""

    def test_calls_fire_precall_sms_for_campaign_before_returning(self):
        campaign = {
            "id": "c0",
            "name": "c0",
            "campaignConfig": {"precallSms": {"enabled": True}},
        }
        cs = _campaign_state(
            "c0", status="queued", connectCampaignId="conn-1", warmupStarted=True
        )
        run, plan = _run_plan(campaign, cs)

        with (
            patch("executor._safe_stop_campaign") as mock_stop,
            patch("executor._fire_precall_sms_for_campaign") as mock_fire,
        ):
            executor._start_one_campaign(run, plan, 0, 0)

        assert cs["status"] == "running"
        assert "warmupStarted" not in cs
        mock_stop.assert_not_called()
        mock_fire.assert_called_once_with(run, plan, 0, 0)


class TestPrewarmedFreshStartPausesWhenPrecallEnabled:
    """Touch point E, sub-case 2 (2026-09 adversarial-review fix): this sub-case
    calls start_campaign directly (not through _create_campaign_only or
    _create_and_start_campaign), so it needs its own pause-after-start too."""

    def test_pauses_after_start_then_calls_fire_helper(self):
        campaign = {
            "id": "c0",
            "name": "c0",
            "campaignConfig": {"precallSms": {"enabled": True}},
        }
        cs = _campaign_state("c0", status="queued", connectCampaignId="conn-1")
        run, plan = _run_plan(campaign, cs)
        order = []

        mock_oc = MagicMock()
        mock_oc.start_campaign = lambda cid: order.append(("start", cid))
        mock_oc.pause_campaign = lambda cid: order.append(("pause", cid))
        mock_oc, originals = _stub_oc(mock_oc)
        try:
            with patch(
                "executor._fire_precall_sms_for_campaign",
                side_effect=lambda *a, **k: order.append("fire"),
            ) as mock_fire:
                executor._start_one_campaign(run, plan, 0, 0)
        finally:
            _unstub_oc(originals)

        assert order == [("start", "conn-1"), ("pause", "conn-1"), "fire"]
        assert cs["status"] == "running"
        mock_fire.assert_called_once_with(run, plan, 0, 0)
        assert cs["precallGatePausedAt"]  # marker set immediately after pause succeeds

    def test_precall_disabled_never_pauses_but_still_calls_real_fire_helper(self):
        """_fire_precall_sms_for_campaign is always called unconditionally here —
        it is a no-op for a disabled campaign, exercised here with the REAL
        (unmocked) function to prove that end-to-end."""
        campaign = {"id": "c0", "name": "c0"}  # no campaignConfig at all
        cs = _campaign_state("c0", status="queued", connectCampaignId="conn-1")
        run, plan = _run_plan(campaign, cs)

        mock_oc, originals = _stub_oc()
        try:
            executor._start_one_campaign(run, plan, 0, 0)
        finally:
            _unstub_oc(originals)

        mock_oc.pause_campaign.assert_not_called()
        mock_oc.resume_campaign.assert_not_called()
        assert cs["status"] == "running"
        assert "precallGatePausedAt" not in cs

    def test_pause_campaign_failure_is_logged_and_does_not_raise(self):
        campaign = {
            "id": "c0",
            "name": "c0",
            "campaignConfig": {"precallSms": {"enabled": True}},
        }
        cs = _campaign_state("c0", status="queued", connectCampaignId="conn-1")
        run, plan = _run_plan(campaign, cs)

        mock_oc, originals = _stub_oc()
        mock_oc.pause_campaign.side_effect = RuntimeError("pause failed")
        try:
            with patch("executor._fire_precall_sms_for_campaign") as mock_fire:
                executor._start_one_campaign(run, plan, 0, 0)  # must not raise
        finally:
            _unstub_oc(originals)

        assert cs["status"] == "running"
        mock_fire.assert_called_once_with(run, plan, 0, 0)
        assert "precallGatePausedAt" not in cs  # pause failed — never set on failure


class TestFreshStartCallsFirePrecallGate:
    """Touch point F — the actual P0#1 fix for the most common cold-start case:
    manual "Run Now", force_start_bucket, parallel-bucket chain-starts, and
    _advance_bucket's queued-bucket fallback all route through
    _dispatch_ready_campaigns → _start_one_campaign's fresh-start path, which
    never called _fire_precall_sms (or anything precall-SMS-related) before
    this fix."""

    def test_calls_fire_precall_sms_for_campaign_after_phase_2_succeeds(self):
        campaign = {
            "id": "c0",
            "name": "c0",
            "campaignConfig": {"precallSms": {"enabled": True}},
        }
        cs = _campaign_state("c0", status="queued")
        run, plan = _run_plan(campaign, cs)

        with (
            patch(
                "executor._create_segment",
                return_value=("seg-1", "arn:seg-1", None, None),
            ),
            patch(
                "executor._create_and_start_campaign",
                return_value=("conn-new", "seg-1"),
            ),
            patch("executor.save_run"),
            patch("executor._fire_precall_sms_for_campaign") as mock_fire,
        ):
            executor._start_one_campaign(run, plan, 0, 0)

        assert cs["status"] == "running"
        assert cs["connectCampaignId"] == "conn-new"
        mock_fire.assert_called_once_with(run, plan, 0, 0)

    def test_precall_disabled_still_calls_real_fire_helper_as_a_no_op(self):
        campaign = {"id": "c0", "name": "c0"}  # no campaignConfig at all
        cs = _campaign_state("c0", status="queued")
        run, plan = _run_plan(campaign, cs)

        with (
            patch(
                "executor._create_segment",
                return_value=("seg-1", "arn:seg-1", None, None),
            ),
            patch(
                "executor._create_and_start_campaign",
                return_value=("conn-new", "seg-1"),
            ),
            patch("executor.save_run"),
            patch("executor._invoke_sms_sender") as invoke_sms,
        ):
            executor._start_one_campaign(run, plan, 0, 0)

        assert cs["status"] == "running"
        invoke_sms.assert_not_called()

    def test_sends_the_real_sms_end_to_end(self):
        """Direct regression test for finding #1 (P0): a campaign reaching this
        fresh-start path with NO pendingWarmup at all — the manual "Run Now",
        force_start_bucket, and _advance_bucket queued-bucket fallback path —
        used to never call _fire_precall_sms anywhere. Verifies the real send
        (not just that the wiring called the helper) and the follow-on resume."""
        campaign = {
            "id": "c0",
            "name": "c0",
            "campaignConfig": {
                "precallSms": {
                    "enabled": True,
                    "messageTemplate": "Hi {{FirstName}}",
                    "originationNumberArn": "arn:pn",
                    "clinicName": "VIP Clinic",
                }
            },
        }
        cs = _campaign_state("c0", status="queued")
        run, plan = _run_plan(campaign, cs)

        def _fake_create_and_start(*_args, cs=None, **_kwargs):
            # Simulates the real _create_and_start_campaign's contract: it
            # mutates cs["precallGatePausedAt"] directly on a successful
            # pause rather than returning it (see its docstring) — this test
            # mocks the function wholesale, so that side effect must be
            # reproduced here for the downstream resume gate to trigger.
            if cs is not None:
                cs["precallGatePausedAt"] = "2026-01-01T00:00:00+00:00"
            return "conn-new", "seg-1"

        mock_oc, originals = _stub_oc()
        try:
            with (
                patch(
                    "executor._create_segment",
                    return_value=("seg-1", "arn:seg-1", None, None),
                ),
                patch(
                    "executor._create_and_start_campaign",
                    side_effect=_fake_create_and_start,
                ),
                patch("executor.save_run"),
                patch("executor._invoke_sms_sender") as invoke_sms,
            ):
                executor._start_one_campaign(run, plan, 0, 0)
        finally:
            _unstub_oc(originals)

        invoke_sms.assert_called_once()
        assert cs["precallSmsSentAt"]
        mock_oc.resume_campaign.assert_called_once_with("conn-new")
        assert cs["precallGateResumedAt"]
