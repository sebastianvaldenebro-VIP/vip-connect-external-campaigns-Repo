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


class TestForceStartClearsStalePrecallMarkers:
    """Adversarial-review Critical finding: force_start_campaign's Phase 1 reset
    must clear precallSmsSentAt / precallGatePausedAt / precallGateResumedAt.

    Without this, a campaign that already completed one full precall-SMS
    lifecycle (sent its text, been paused+resumed once) before this restart
    inherits those stale markers:
      - stale precallSmsSentAt short-circuits _fire_precall_sms_for_campaign's
        send (_attempt_precall_sms_send returns early) — no text fires this cycle.
      - stale precallGateResumedAt makes the resume-trigger condition
        (precallGatePausedAt and not precallGateResumedAt) false even though
        _create_and_start_campaign pauses the campaign again for THIS cycle —
        so the resume never fires.
      - _poll_campaign_state's stranded-pause self-heal checks the identical
        condition, so it can't catch this either.
    Net effect without the fix: the restarted campaign pauses but never
    resumes — a permanent, silent stall with zero calls dialed.
    """

    @staticmethod
    def _stub_precall_oc(mock_oc=None):
        """Stub the vip_shared oc client via sys.modules — _fire_precall_sms_for_campaign
        resolves it through a function-local import, never a module attribute, so
        patch("executor.oc", ...) would have no effect. Same technique used by
        test_executor_v2.py's _stub_precall_oc."""
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

    @staticmethod
    def _unstub_precall_oc(originals):
        for m, orig in originals.items():
            if orig is None:
                sys.modules.pop(m, None)
            else:
                sys.modules[m] = orig

    @staticmethod
    def _fake_start_one_campaign_with_precall_cycle(new_connect_id="conn-restart-1"):
        """side_effect for a mocked _start_one_campaign that mimics the real
        cold-start sequence for a precall-enabled campaign — _create_and_start_campaign
        pausing it (fresh precallGatePausedAt), then _fire_precall_sms_for_campaign
        resolving the send + resume — without exercising the unrelated segment/Connect
        campaign creation machinery those functions also do. Calls the REAL
        _fire_precall_sms_for_campaign so the actual gate logic under test runs
        unmocked.
        """

        def _side_effect(run, plan, bucket_index, campaign_index):
            cs = run["bucketStates"][bucket_index]["campaignStates"][campaign_index]
            cs["connectCampaignId"] = new_connect_id
            cs["status"] = "running"
            cs["precallGatePausedAt"] = executor._now_iso()
            executor._fire_precall_sms_for_campaign(
                run, plan, bucket_index, campaign_index
            )

        return _side_effect

    def test_restarted_campaign_gets_fresh_precall_sms_and_completes_gate(self):
        """Direct regression test: a campaign with precallSms.enabled and ALL
        THREE stale markers already set (simulating a completed prior cycle) is
        force-started. The SMS-sender must fire again THIS cycle, and the pause/
        resume gate must reflect THIS cycle's own pause+resume — not the stale
        prior-cycle values — proving the campaign doesn't end up stuck Paused.
        """
        campaign_def = {
            "id": "c0",
            "name": "c0",
            "states": ["NY"],
            "groups": [],
            "dependsOn": [],
            "campaignConfig": {
                "precallSms": {
                    "enabled": True,
                    "messageTemplate": "Hi {{FirstName}}",
                    "originationNumberArn": "arn:aws:sns:us-east-1:111122223333:phone-number/PN123",
                    "clinicName": "VIP Clinic",
                },
            },
        }
        plan = _plan([{"id": "b0", "campaigns": [campaign_def]}])
        stale_ts = "2026-05-01T09:00:00+00:00"
        cs = _campaign_state(
            "c0",
            status="cancelled",
            connectCampaignId="conn-old-1",
            precallSmsSentAt=stale_ts,
            precallGatePausedAt=stale_ts,
            precallGateResumedAt=stale_ts,
        )
        bs = _bucket_state("b0", [cs], status="running")
        run = _run(plan, [bs])

        invoke = MagicMock()
        mock_oc, originals = self._stub_precall_oc()
        try:
            with (
                patch("executor.get_run", return_value=run),
                patch("executor._reset_cascade_cancelled_children"),
                patch("executor.save_run"),
                patch("executor._safe_stop_campaign"),
                patch("executor._safe_delete_campaign"),
                patch("executor._invoke_sms_sender", invoke),
                patch(
                    "executor._start_one_campaign",
                    side_effect=self._fake_start_one_campaign_with_precall_cycle(),
                ),
            ):
                executor.force_start_campaign("p1", "r1", 0, 0)
        finally:
            self._unstub_precall_oc(originals)

        # 1. The SMS-sender fires again this cycle — precallSmsSentAt being
        # cleared genuinely lets the send fire fresh.
        invoke.assert_called_once()
        assert cs["precallSmsSentAt"] is not None
        assert cs["precallSmsSentAt"] != stale_ts

        # 2. The pause/resume gate completes for THIS restarted cycle: both
        # markers reflect the new cycle, not the stale prior-cycle timestamp,
        # and the resume call actually fired against the new Connect campaign.
        mock_oc.resume_campaign.assert_called_once_with("conn-restart-1")
        assert cs["precallGatePausedAt"] != stale_ts
        assert cs["precallGateResumedAt"] is not None
        assert cs["precallGateResumedAt"] != stale_ts

    def test_force_start_without_precall_config_still_works_unaffected(self):
        """No regression: a campaign without precallSms.enabled (or with no
        precall config at all) force-starts exactly as before — the new
        precall-marker resets are unconditional but harmless no-ops for it.
        """
        plan = _plan([{"id": "b0", "campaigns": [{"id": "c0", "name": "c0"}]}])
        cs = _campaign_state("c0", status="cancelled", connectCampaignId="conn-old-1")
        bs = _bucket_state("b0", [cs], status="running")
        run = _run(plan, [bs])

        with (
            patch("executor.get_run", return_value=run),
            patch("executor._reset_cascade_cancelled_children"),
            patch("executor.save_run"),
            patch("executor._safe_stop_campaign"),
            patch("executor._safe_delete_campaign"),
            patch("executor._start_one_campaign") as mock_start,
        ):
            executor.force_start_campaign("p1", "r1", 0, 0)

        mock_start.assert_called_once()
        assert cs["precallSmsSentAt"] is None
        assert cs["precallGatePausedAt"] is None
        assert cs["precallGateResumedAt"] is None
        assert "precallSmsGeneration" not in cs
