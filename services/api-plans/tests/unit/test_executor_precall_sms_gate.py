"""Tests for the precall-SMS dial-gate state machine introduced by the 2026-09
adversarial-review fix: _attempt_precall_sms_send and
_fire_precall_sms_for_campaign.

Context (see docs/superpowers/plans/2026-09-09-precall-sms-phase1.md and the
adversarial-fix-statemachine-report.md for the full spec): a Connect campaign
with precallSms.enabled is paused immediately after StartCampaign succeeds
(_create_campaign_only / _create_and_start_campaign), so it cannot dial while
paused regardless of its armed startTime. _fire_precall_sms_for_campaign is
what resumes it, once the SMS send attempt has resolved (sent, or
failed-and-logged) — never before, and even if the send raises unexpectedly.

This file unit-tests that per-campaign gate directly. The call-site wiring
(where each function is actually invoked from) is covered in:
  - test_executor_create_campaign_only_gaps.py       (pause after warm-start)
  - test_executor_create_and_start_campaign_gaps.py  (pause after cold-start)
  - test_executor_start_one_campaign_gaps.py         (branded fire-only, and
                                                       both already-has-
                                                       connectCampaignId
                                                       sub-cases)
  - test_executor_v2.py TestFirePrecallSms /
    TestPrecallSmsOrdering                            (bucket-level loop,
                                                       start_run/activate
                                                       ordering, false-promise
                                                       regression)
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

sys.modules.setdefault("vip_shared", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.persistence", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.persistence.audit", MagicMock())

import executor  # noqa: E402


def _stub_oc(mock_oc: MagicMock | None = None) -> tuple[MagicMock, dict]:
    """Stub the vip_shared oc client via sys.modules — every call site resolves
    it through a function-local import, never a module attribute, so
    mocker.patch("executor.oc", ...) would raise AttributeError."""
    if mock_oc is None:
        mock_oc = MagicMock()
    vip_stub = MagicMock()
    vip_stub.build = MagicMock(return_value=mock_oc)
    modules_to_stub = [
        "vip_shared",
        "vip_shared.infrastructure",
        "vip_shared.infrastructure.persistence",
        "vip_shared.infrastructure.persistence.outbound_campaigns_client",
    ]
    originals = {m: sys.modules.get(m) for m in modules_to_stub}
    for m in modules_to_stub:
        sys.modules[m] = vip_stub
    return mock_oc, originals


def _unstub_oc(originals: dict) -> None:
    for m, orig in originals.items():
        if orig is None:
            sys.modules.pop(m, None)
        else:
            sys.modules[m] = orig


def _campaign(precall_enabled: bool = True, **precall_overrides) -> dict:
    precall = {
        "enabled": precall_enabled,
        "messageTemplate": "Hi {{FirstName}}, your visit is coming up.",
        "originationNumberArn": "arn:aws:sns:us-east-1:111122223333:phone-number/PN1",
        "clinicName": "VIP Clinic",
    }
    precall.update(precall_overrides)
    return {"id": "c0", "name": "c0", "campaignConfig": {"precallSms": precall}}


def _cs(**overrides) -> dict:
    """Defaults to a genuinely-paused campaign (precallGatePausedAt set) —
    the baseline "about to be resumed" shape most tests in this file act on.
    Since the 2026-09 adversarial-review resume-retry fix, resume is only
    ever attempted when precallGatePausedAt is set (never merely because
    connectCampaignId is present) — see
    test_resume_not_attempted_when_no_connect_campaign_id below for the
    explicit override that removes it."""
    cs = {
        "campaignId": "c0",
        "status": "warming",
        "connectCampaignId": "conn-1",
        "segmentArn": "arn:cp:seg1",
        "segmentName": "seg1",
        "precallGatePausedAt": "2026-09-09T00:00:00+00:00",
    }
    cs.update(overrides)
    return cs


def _run_plan(campaign: dict, cs: dict) -> tuple[dict, dict]:
    bucket = {"id": "b0", "campaigns": [campaign]}
    plan = {"planId": "p1", "buckets": [bucket]}
    run = {
        "planId": "p1",
        "runId": "r1",
        "bucketStates": [{"status": "running", "campaignStates": [cs]}],
    }
    return run, plan


class TestFirePrecallSmsForCampaignResumeOrdering:
    def test_resume_called_after_sms_send_succeeds(self):
        campaign = _campaign()
        cs = _cs()
        run, plan = _run_plan(campaign, cs)
        order: list = []
        mock_oc, originals = _stub_oc()
        mock_oc.resume_campaign = lambda cid: order.append(("resume", cid))
        try:
            with patch(
                "executor._invoke_sms_sender",
                side_effect=lambda **k: order.append("sms"),
            ):
                executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
        finally:
            _unstub_oc(originals)

        assert order == ["sms", ("resume", "conn-1")]
        assert cs["precallSmsSentAt"]
        assert cs["precallGateResumedAt"]

    def test_resume_still_called_when_sms_send_raises(self):
        """The single most important test in this fix: a text failure must
        never leave the campaign stuck paused — the dial must never be blocked
        by an SMS failure."""
        campaign = _campaign()
        cs = _cs()
        run, plan = _run_plan(campaign, cs)
        mock_oc, originals = _stub_oc()
        try:
            with patch(
                "executor._invoke_sms_sender", side_effect=RuntimeError("SNS down")
            ):
                executor._fire_precall_sms_for_campaign(run, plan, 0, 0)  # must not raise
        finally:
            _unstub_oc(originals)

        mock_oc.resume_campaign.assert_called_once_with("conn-1")
        assert cs.get("precallSmsSentAt") is None  # send failed — never marked sent
        assert cs["precallGateResumedAt"]  # but the dial gate still released

    def test_resume_still_called_when_send_helper_itself_raises_unexpectedly(self):
        """Defense in depth: even if _attempt_precall_sms_send somehow raised
        instead of swallowing its own exception, the resume must still run —
        this is what the try/finally (not just try/except) buys us."""
        campaign = _campaign()
        cs = _cs()
        run, plan = _run_plan(campaign, cs)
        mock_oc, originals = _stub_oc()
        try:
            with patch(
                "executor._attempt_precall_sms_send",
                side_effect=RuntimeError("unexpected"),
            ):
                try:
                    executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
                except RuntimeError:
                    pass
        finally:
            _unstub_oc(originals)

        mock_oc.resume_campaign.assert_called_once_with("conn-1")

    def test_resume_not_attempted_when_no_connect_campaign_id(self):
        """Branded's shape — connectCampaignId is always falsy/absent, and a
        campaign with no connectCampaignId can never have been paused either
        (precallGatePausedAt=None reflects that realistic combination);
        resume must be a structural no-op."""
        campaign = _campaign()
        cs = _cs(connectCampaignId=None, precallGatePausedAt=None)
        run, plan = _run_plan(campaign, cs)
        mock_oc, originals = _stub_oc()
        try:
            with patch("executor._invoke_sms_sender"):
                executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
        finally:
            _unstub_oc(originals)

        mock_oc.resume_campaign.assert_not_called()
        assert "precallGateResumedAt" not in cs

    def test_resume_not_attempted_when_never_actually_paused_by_us(self):
        """Narrowed trigger (2026-09 adversarial-review resume-retry fix):
        having a connectCampaignId is no longer sufficient — covers both the
        cold_started noise case (warmupStarted=False, so
        _create_campaign_only never paused it) and the pause-failed noise
        case. Both leave precallGatePausedAt unset."""
        campaign = _campaign()
        cs = _cs(precallGatePausedAt=None)  # has connectCampaignId, never paused
        run, plan = _run_plan(campaign, cs)
        mock_oc, originals = _stub_oc()
        try:
            with patch("executor._invoke_sms_sender"):
                executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
        finally:
            _unstub_oc(originals)

        mock_oc.resume_campaign.assert_not_called()
        assert "precallGateResumedAt" not in cs

    def test_disabled_precall_sms_never_pauses_or_resumes(self):
        campaign = _campaign(precall_enabled=False)
        cs = _cs()
        run, plan = _run_plan(campaign, cs)
        mock_oc, originals = _stub_oc()
        try:
            with patch("executor._invoke_sms_sender") as invoke:
                executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
        finally:
            _unstub_oc(originals)

        invoke.assert_not_called()
        mock_oc.resume_campaign.assert_not_called()
        assert "precallSmsSentAt" not in cs
        assert "precallGateResumedAt" not in cs

    def test_disabled_precall_sms_short_circuits_before_touching_oc(self):
        """cs may have no connectCampaignId key at all in shapes this helper
        wasn't designed for — enabled=False must return before ever looking."""
        campaign = _campaign(precall_enabled=False)
        cs = {"campaignId": "c0", "status": "running"}  # no connectCampaignId key
        run, plan = _run_plan(campaign, cs)

        executor._fire_precall_sms_for_campaign(run, plan, 0, 0)  # must not raise


class TestPrecallGateResumeFailureHandling:
    def test_resume_failure_is_logged_distinctly_and_does_not_raise(self):
        campaign = _campaign()
        cs = _cs()
        run, plan = _run_plan(campaign, cs)
        mock_oc, originals = _stub_oc()
        mock_oc.resume_campaign.side_effect = RuntimeError("InvalidCampaignState")
        try:
            with (
                patch("executor._invoke_sms_sender"),
                patch("executor._slog") as mock_slog,
            ):
                executor._fire_precall_sms_for_campaign(run, plan, 0, 0)  # must not raise
        finally:
            _unstub_oc(originals)

        assert cs.get("precallGateResumedAt") is None  # never marked — safe to retry
        error_events = [c.args[0] for c in mock_slog.error.call_args_list]
        assert "precall_gate_resume_failed" in error_events

    def test_resume_is_retried_on_a_second_call_if_it_failed_last_time(self):
        campaign = _campaign()
        cs = _cs()
        run, plan = _run_plan(campaign, cs)
        mock_oc, originals = _stub_oc()
        mock_oc.resume_campaign.side_effect = [RuntimeError("boom"), None]
        try:
            with patch("executor._invoke_sms_sender") as invoke:
                executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
                assert cs.get("precallGateResumedAt") is None
                executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
        finally:
            _unstub_oc(originals)

        assert mock_oc.resume_campaign.call_count == 2
        assert cs["precallGateResumedAt"]
        # SMS itself was NOT re-sent on the retry — precallSmsSentAt guard held,
        # independently of the resume retry.
        assert invoke.call_count == 1


class TestAttemptPrecallSmsSendDirectly:
    """Direct coverage of the shared send-only helper both the Connect-gated
    per-campaign gate and the branded fire-only call site build on."""

    def test_sends_once_and_sets_marker(self):
        cs = {}
        with patch("executor._invoke_sms_sender") as invoke:
            executor._attempt_precall_sms_send(
                run={"planId": "p1", "runId": "r1"},
                cs=cs,
                precall={
                    "messageTemplate": "hi",
                    "originationNumberArn": "arn:pn",
                    "clinicName": "Clinic",
                },
                segment_arn="arn:seg",
                segment_name="seg-1",
                bucket_index=0,
                campaign_index=0,
                sms_campaign_id="sms-1",
            )

        invoke.assert_called_once()
        assert invoke.call_args.kwargs["segmentArn"] == "arn:seg"
        assert cs["precallSmsSentAt"]

    def test_second_call_is_a_no_op_once_sent(self):
        cs = {"precallSmsSentAt": "2026-09-09T00:00:00+00:00"}
        with patch("executor._invoke_sms_sender") as invoke:
            executor._attempt_precall_sms_send(
                run={"planId": "p1", "runId": "r1"},
                cs=cs,
                precall={},
                segment_arn="arn:seg",
                segment_name="seg-1",
                bucket_index=0,
                campaign_index=0,
                sms_campaign_id="sms-1",
            )

        invoke.assert_not_called()

    def test_send_failure_never_raises(self):
        cs = {}
        with patch(
            "executor._invoke_sms_sender", side_effect=RuntimeError("boom")
        ):
            executor._attempt_precall_sms_send(
                run={"planId": "p1", "runId": "r1"},
                cs=cs,
                precall={},
                segment_arn="arn:seg",
                segment_name="seg-1",
                bucket_index=0,
                campaign_index=0,
                sms_campaign_id="sms-1",
            )  # must not raise

        assert "precallSmsSentAt" not in cs
