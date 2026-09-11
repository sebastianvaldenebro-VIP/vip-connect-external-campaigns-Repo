"""Tests for the precall-SMS quiet-hours retry wiring added to tick()'s poll
loop (2026-09 adversarial-review finding: the pre-call SMS quiet-hours check
in sms_sender_handler.py ran exactly once, at bucket activation, while
Connect Campaigns V2's own AREA_CODE quiet-hours re-evaluation is continuous
for as long as the voice campaign stays "running" — a recipient outside
their window at activation could get dialed hours later having never
received the text).

This file covers ONLY the call-site wiring inside tick(): does it invoke
_invoke_sms_retry_quiet_hours for the right campaigns, at the right time, and
does a retry failure ever disrupt the rest of tick()'s work. The retry
Lambda's own logic (retry_quiet_hours_skipped, _process_recipients) is
covered in services/api-sms/tests/unit/test_sms_sender_retry.py.
"""

from __future__ import annotations

import os
import sys
from contextlib import ExitStack
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


def _precall_campaign_def(cid="c0", enabled=True, **overrides):
    campaign = _campaign_def(
        cid,
        campaignConfig={
            "precallSms": {
                "enabled": enabled,
                "messageTemplate": "hi",
                "originationNumberArn": "arn:pn",
                "clinicName": "Clinic",
            }
        },
    )
    campaign.update(overrides)
    return campaign


def _campaign_state(cid, status="running", **overrides):
    cs = {
        "campaignId": cid,
        "name": cid,
        "status": status,
        "connectCampaignId": "connect-1",
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


def _bucket_def(bid, campaigns, run_mode="status_based", duration=0, **overrides):
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
    p = {
        "planId": "plan-1",
        "name": "Test",
        "trigger": {"type": "manual"},
        "isTemplate": False,
        "buckets": buckets,
    }
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


def _enter_base_patches(stack: ExitStack) -> None:
    stack.enter_context(patch("executor.save_run"))
    stack.enter_context(patch("executor.unlock_plan_run"))
    stack.enter_context(patch("executor._delete_bucket_schedule_safe"))
    stack.enter_context(patch("executor._dispatch_ready_campaigns", return_value=False))
    stack.enter_context(
        patch("executor._dispatch_cross_bucket_ready", return_value=False)
    )
    stack.enter_context(patch("executor._all_campaigns_terminal", return_value=False))
    stack.enter_context(patch("executor._fire_campaign_chains"))


class TestPrecallSmsQuietHoursRetryTriggered:
    def test_triggers_for_running_precall_enabled_campaign_with_sms_already_sent(self):
        campaign = _precall_campaign_def()
        cs = _campaign_state("c0", precallSmsSentAt="2026-09-09T00:00:00+00:00")
        bucket = _bucket_def("b0", [campaign])
        bucket_state = _bucket_state("b0", [cs])
        plan = _plan([bucket])
        run = _run(plan, [bucket_state])

        with ExitStack() as stack:
            _enter_base_patches(stack)
            stack.enter_context(patch("executor.get_run", return_value=run))
            stack.enter_context(patch("executor._poll_campaign_state"))
            mock_retry = stack.enter_context(
                patch("executor._invoke_sms_retry_quiet_hours")
            )
            executor.tick("plan-1", "run-1", 0)

        mock_retry.assert_called_once()
        call_kwargs = mock_retry.call_args.kwargs
        assert call_kwargs["plan_id"] == "plan-1"
        assert call_kwargs["run_id"] == "run-1"
        assert call_kwargs["campaign_id"] == executor._precall_sms_campaign_id(
            run, 0, 0
        )

    def test_does_not_trigger_when_precall_sms_not_enabled(self):
        campaign = _precall_campaign_def(enabled=False)
        cs = _campaign_state("c0", precallSmsSentAt="2026-09-09T00:00:00+00:00")
        bucket = _bucket_def("b0", [campaign])
        bucket_state = _bucket_state("b0", [cs])
        plan = _plan([bucket])
        run = _run(plan, [bucket_state])

        with ExitStack() as stack:
            _enter_base_patches(stack)
            stack.enter_context(patch("executor.get_run", return_value=run))
            stack.enter_context(patch("executor._poll_campaign_state"))
            mock_retry = stack.enter_context(
                patch("executor._invoke_sms_retry_quiet_hours")
            )
            executor.tick("plan-1", "run-1", 0)

        mock_retry.assert_not_called()

    def test_does_not_trigger_when_campaign_has_no_precall_sms_config(self):
        campaign = _campaign_def("c0")  # no campaignConfig key at all
        cs = _campaign_state("c0", precallSmsSentAt="2026-09-09T00:00:00+00:00")
        bucket = _bucket_def("b0", [campaign])
        bucket_state = _bucket_state("b0", [cs])
        plan = _plan([bucket])
        run = _run(plan, [bucket_state])

        with ExitStack() as stack:
            _enter_base_patches(stack)
            stack.enter_context(patch("executor.get_run", return_value=run))
            stack.enter_context(patch("executor._poll_campaign_state"))
            mock_retry = stack.enter_context(
                patch("executor._invoke_sms_retry_quiet_hours")
            )
            executor.tick("plan-1", "run-1", 0)

        mock_retry.assert_not_called()

    def test_does_not_trigger_when_precall_sms_has_not_fired_yet(self):
        campaign = _precall_campaign_def()
        cs = _campaign_state("c0")  # no precallSmsSentAt
        bucket = _bucket_def("b0", [campaign])
        bucket_state = _bucket_state("b0", [cs])
        plan = _plan([bucket])
        run = _run(plan, [bucket_state])

        with ExitStack() as stack:
            _enter_base_patches(stack)
            stack.enter_context(patch("executor.get_run", return_value=run))
            stack.enter_context(patch("executor._poll_campaign_state"))
            mock_retry = stack.enter_context(
                patch("executor._invoke_sms_retry_quiet_hours")
            )
            executor.tick("plan-1", "run-1", 0)

        mock_retry.assert_not_called()

    def test_does_not_trigger_when_campaign_leaves_running_this_tick(self):
        """The retry's bound: the instant _poll_campaign_state transitions the
        campaign out of "running" (completed/cancelled/error), no more
        retries — mirrors the voice side's own active window."""
        campaign = _precall_campaign_def()
        cs = _campaign_state("c0", precallSmsSentAt="2026-09-09T00:00:00+00:00")
        bucket = _bucket_def("b0", [campaign])
        bucket_state = _bucket_state("b0", [cs])
        plan = _plan([bucket])
        run = _run(plan, [bucket_state])

        def _poll_to_completed(cs_arg, **_kw):
            cs_arg["status"] = "completed"

        with ExitStack() as stack:
            _enter_base_patches(stack)
            stack.enter_context(patch("executor.get_run", return_value=run))
            stack.enter_context(
                patch("executor._poll_campaign_state", side_effect=_poll_to_completed)
            )
            mock_retry = stack.enter_context(
                patch("executor._invoke_sms_retry_quiet_hours")
            )
            executor.tick("plan-1", "run-1", 0)

        mock_retry.assert_not_called()


class TestPrecallSmsQuietHoursRetryFailureIsolation:
    def test_retry_failure_does_not_disrupt_rest_of_tick(self):
        """A retry-invocation failure must never disrupt anything else in
        tick() — matches this feature's "an SMS problem degrades to no-text,
        never to a bigger failure" principle throughout. Proven here by a
        second, unrelated campaign in the same bucket that still completes
        normally despite the first campaign's retry raising."""
        campaign0 = _precall_campaign_def("c0")
        cs0 = _campaign_state("c0", precallSmsSentAt="2026-09-09T00:00:00+00:00")
        campaign1 = _campaign_def("c1")
        cs1 = {"campaignId": "c1", "status": "running", "smsCampaignId": "sms-c1"}
        bucket = _bucket_def("b0", [campaign0, campaign1])
        bucket_state = _bucket_state("b0", [cs0, cs1])
        plan = _plan([bucket])
        run = _run(plan, [bucket_state])

        with ExitStack() as stack:
            _enter_base_patches(stack)
            stack.enter_context(patch("executor.get_run", return_value=run))
            stack.enter_context(patch("executor._poll_campaign_state"))
            stack.enter_context(
                patch(
                    "executor._invoke_sms_retry_quiet_hours",
                    side_effect=RuntimeError("Lambda invoke failed"),
                )
            )
            stack.enter_context(patch("executor._count_sms_queue", return_value=0))
            mock_complete = stack.enter_context(
                patch("executor._complete_sms_campaign")
            )
            result = executor.tick("plan-1", "run-1", 0)  # must not raise

        assert result["ok"] is True
        assert cs1["status"] == "completed"
        mock_complete.assert_called_once_with(cs1)
