"""Targeted tests for remaining gaps in executor._create_campaign_only
(journey delivery type, segmentFilters fallback, missing-flow-arn ValueErrors,
start_campaign exception logged-but-swallowed) and executor._poll_campaign_state
(zero prior real-body coverage — always mocked elsewhere).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

sys.modules.setdefault("vip_shared", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.persistence", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.persistence.audit", MagicMock())

import executor  # noqa: E402

_NOW_UTC = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)  # 7am COT — safe daytime


def _stub_vip_shared(mock_oc):
    vip_stub = MagicMock()
    vip_stub.build.return_value = mock_oc
    modules_to_stub = [
        "vip_shared",
        "vip_shared.infrastructure",
        "vip_shared.infrastructure.persistence",
        "vip_shared.infrastructure.persistence.outbound_campaigns_client",
    ]
    originals = {m: sys.modules.get(m) for m in modules_to_stub}
    for m in modules_to_stub:
        sys.modules[m] = vip_stub
    return originals


def _unstub_vip_shared(originals):
    for m, orig in originals.items():
        if orig is None:
            sys.modules.pop(m, None)
        else:
            sys.modules[m] = orig


class TestCreateCampaignOnlyJourneyDeliveryType:
    def test_resolves_journey_flow_arn(self):
        bucket = {"id": "B1", "name": "B1", "campaigns": [], "segmentFilters": {"state": ["TX"]}}
        campaign = {
            "id": "c1", "name": "TX-J", "states": ["TX"], "deliveryType": "journey",
        }
        run = {"planId": "p1", "runId": "r1"}
        mock_oc = MagicMock()
        mock_oc.create_campaign.return_value = {"id": "connect-j1"}
        originals = _stub_vip_shared(mock_oc)
        try:
            with (
                patch("executor._now_utc", return_value=_NOW_UTC),
                patch("executor._create_segment", return_value=("seg1", "arn:cp:seg1", None, None)),
                patch("executor.resolve_journey_flow_arn", return_value="arn:journey-flow") as mock_journey,
                patch("executor.resolve_campaign_flow_arn") as mock_campaign_flow,
                patch(
                    "executor.build_campaign_params",
                    return_value={"connectCampaignFlowArn": "arn:journey-flow"},
                ),
                patch("executor._account_id", return_value="123456789012"),
            ):
                connect_id, *_rest = executor._create_campaign_only(bucket, campaign, run)
        finally:
            _unstub_vip_shared(originals)

        assert connect_id == "connect-j1"
        mock_journey.assert_called_once()
        mock_campaign_flow.assert_not_called()

    def test_raises_value_error_when_journey_flow_arn_missing(self):
        bucket = {"id": "B1", "name": "B1", "campaigns": [], "segmentFilters": {"state": ["TX"]}}
        campaign = {"id": "c1", "name": "TX-J", "states": ["TX"], "deliveryType": "journey"}
        run = {"planId": "p1", "runId": "r1"}
        mock_oc = MagicMock()
        originals = _stub_vip_shared(mock_oc)
        try:
            with (
                patch("executor._now_utc", return_value=_NOW_UTC),
                patch("executor._create_segment", return_value=("seg1", "arn:cp:seg1", None, None)),
                patch("executor.resolve_journey_flow_arn", return_value=None),
                patch("executor.build_campaign_params", return_value={}),
                patch("executor._account_id", return_value="123456789012"),
                pytest.raises(ValueError, match="Journey flow"),
            ):
                executor._create_campaign_only(bucket, campaign, run)
        finally:
            _unstub_vip_shared(originals)


class TestCreateCampaignOnlySegmentFiltersFallback:
    def test_derives_segment_filters_from_campaign_when_bucket_has_none(self):
        bucket = {"id": "B1", "name": "B1", "campaigns": []}  # no segmentFilters key
        campaign = {"id": "c1", "name": "TX-NL", "states": ["TX"], "groups": ["g1"]}
        run = {"planId": "p1", "runId": "r1"}
        mock_oc = MagicMock()
        mock_oc.create_campaign.return_value = {"id": "connect-2"}
        originals = _stub_vip_shared(mock_oc)
        try:
            with (
                patch("executor._now_utc", return_value=_NOW_UTC),
                patch("executor._create_segment", return_value=("seg1", "arn:cp:seg1", None, None)),
                patch("executor.resolve_campaign_flow_arn", return_value="arn:flow"),
                patch("executor.campaign_to_segment_filters", return_value={"state": ["TX"]}) as mock_derive,
                patch(
                    "executor.build_campaign_params",
                    return_value={"connectCampaignFlowArn": "arn:flow"},
                ),
                patch("executor._account_id", return_value="123456789012"),
            ):
                executor._create_campaign_only(bucket, campaign, run)
        finally:
            _unstub_vip_shared(originals)

        mock_derive.assert_called_once_with(campaign)


class TestCreateCampaignOnlyMissingFlowArnNonJourney:
    def test_raises_value_error_when_no_campaign_flow_arn_available(self):
        bucket = {"id": "B1", "name": "B1", "campaigns": [], "segmentFilters": {"state": ["ZZ"]}}
        campaign = {"id": "c1", "name": "ZZ-NL", "states": ["ZZ"]}
        run = {"planId": "p1", "runId": "r1"}
        mock_oc = MagicMock()
        originals = _stub_vip_shared(mock_oc)
        try:
            with (
                patch("executor._now_utc", return_value=_NOW_UTC),
                patch("executor._create_segment", return_value=("seg1", "arn:cp:seg1", None, None)),
                patch("executor.resolve_campaign_flow_arn", return_value=None),
                patch("executor.build_campaign_params", return_value={}),
                patch("executor._account_id", return_value="123456789012"),
                pytest.raises(ValueError, match="No campaign flow ARN"),
            ):
                executor._create_campaign_only(bucket, campaign, run)
        finally:
            _unstub_vip_shared(originals)


class TestCreateCampaignOnlyStartCampaignFailureSwallowed:
    def test_start_campaign_exception_leaves_warmup_started_false(self):
        bucket = {"id": "B1", "name": "B1", "campaigns": [], "segmentFilters": {"state": ["TX"]}}
        campaign = {"id": "c1", "name": "TX-NL", "states": ["TX"]}
        run = {"planId": "p1", "runId": "r1"}
        mock_oc = MagicMock()
        mock_oc.create_campaign.return_value = {"id": "connect-3"}
        mock_oc.start_campaign.side_effect = RuntimeError("Connect busy")
        originals = _stub_vip_shared(mock_oc)
        try:
            with (
                patch("executor._now_utc", return_value=_NOW_UTC),
                patch("executor._create_segment", return_value=("seg1", "arn:cp:seg1", None, None)),
                patch("executor.resolve_campaign_flow_arn", return_value="arn:flow"),
                patch(
                    "executor.build_campaign_params",
                    return_value={"connectCampaignFlowArn": "arn:flow"},
                ),
                patch("executor._account_id", return_value="123456789012"),
            ):
                connect_id, _seg, _arn, warmup_started, _exp, _act, _paused_at = (
                    executor._create_campaign_only(bucket, campaign, run)
                )
        finally:
            _unstub_vip_shared(originals)

        assert connect_id == "connect-3"
        assert warmup_started is False  # start_campaign failed but is not fatal here
        assert _paused_at is None  # never started, so never paused either


class TestCreateCampaignOnlyPrecallSmsGate:
    """Touch point A (2026-09 adversarial-review fix): a precall-SMS-enabled
    campaign must be paused immediately after a successful warm-start, so it
    cannot dial while paused regardless of its 6-min-ahead startTime."""

    def test_precall_enabled_pauses_immediately_after_start_campaign(self):
        bucket = {"id": "B1", "name": "B1", "campaigns": [], "segmentFilters": {"state": ["TX"]}}
        campaign = {
            "id": "c1",
            "name": "TX-NL",
            "states": ["TX"],
            "campaignConfig": {"precallSms": {"enabled": True}},
        }
        run = {"planId": "p1", "runId": "r1"}
        mock_oc = MagicMock()
        mock_oc.create_campaign.return_value = {"id": "connect-4"}
        originals = _stub_vip_shared(mock_oc)
        try:
            with (
                patch("executor._now_utc", return_value=_NOW_UTC),
                patch("executor._create_segment", return_value=("seg1", "arn:cp:seg1", None, None)),
                patch("executor.resolve_campaign_flow_arn", return_value="arn:flow"),
                patch(
                    "executor.build_campaign_params",
                    return_value={"connectCampaignFlowArn": "arn:flow"},
                ),
                patch("executor._account_id", return_value="123456789012"),
            ):
                connect_id, *_rest, paused_at = executor._create_campaign_only(
                    bucket, campaign, run
                )
        finally:
            _unstub_vip_shared(originals)

        assert connect_id == "connect-4"
        mock_oc.pause_campaign.assert_called_once_with("connect-4")
        assert paused_at  # precallGatePausedAt returned — pause actually succeeded

    def test_precall_disabled_never_pauses(self):
        bucket = {"id": "B1", "name": "B1", "campaigns": [], "segmentFilters": {"state": ["TX"]}}
        campaign = {"id": "c1", "name": "TX-NL", "states": ["TX"]}  # no campaignConfig at all
        run = {"planId": "p1", "runId": "r1"}
        mock_oc = MagicMock()
        mock_oc.create_campaign.return_value = {"id": "connect-5"}
        originals = _stub_vip_shared(mock_oc)
        try:
            with (
                patch("executor._now_utc", return_value=_NOW_UTC),
                patch("executor._create_segment", return_value=("seg1", "arn:cp:seg1", None, None)),
                patch("executor.resolve_campaign_flow_arn", return_value="arn:flow"),
                patch(
                    "executor.build_campaign_params",
                    return_value={"connectCampaignFlowArn": "arn:flow"},
                ),
                patch("executor._account_id", return_value="123456789012"),
            ):
                executor._create_campaign_only(bucket, campaign, run)
        finally:
            _unstub_vip_shared(originals)

        mock_oc.pause_campaign.assert_not_called()

    def test_precall_enabled_but_start_campaign_failed_never_pauses(self):
        """warmup_started=False means StartCampaign never actually succeeded —
        pausing a campaign that never started would be a no-op at best and a
        confusing Connect API error at worst. Nothing to gate on either way:
        this degrades to the pre-existing race, same as a pause failure would."""
        bucket = {"id": "B1", "name": "B1", "campaigns": [], "segmentFilters": {"state": ["TX"]}}
        campaign = {
            "id": "c1",
            "name": "TX-NL",
            "states": ["TX"],
            "campaignConfig": {"precallSms": {"enabled": True}},
        }
        run = {"planId": "p1", "runId": "r1"}
        mock_oc = MagicMock()
        mock_oc.create_campaign.return_value = {"id": "connect-6"}
        mock_oc.start_campaign.side_effect = RuntimeError("Connect busy")
        originals = _stub_vip_shared(mock_oc)
        try:
            with (
                patch("executor._now_utc", return_value=_NOW_UTC),
                patch("executor._create_segment", return_value=("seg1", "arn:cp:seg1", None, None)),
                patch("executor.resolve_campaign_flow_arn", return_value="arn:flow"),
                patch(
                    "executor.build_campaign_params",
                    return_value={"connectCampaignFlowArn": "arn:flow"},
                ),
                patch("executor._account_id", return_value="123456789012"),
            ):
                _connect_id, _seg, _arn, warmup_started, _exp, _act, _paused_at = (
                    executor._create_campaign_only(bucket, campaign, run)
                )
        finally:
            _unstub_vip_shared(originals)

        assert warmup_started is False
        mock_oc.pause_campaign.assert_not_called()
        assert _paused_at is None

    def test_pause_campaign_failure_is_logged_and_does_not_raise(self):
        """A pause failure must not fail the whole pre-warm — accepted residual
        risk: the resume-based gate simply has nothing to gate on for this
        campaign, degrading to the pre-existing timer race, not a new failure."""
        bucket = {"id": "B1", "name": "B1", "campaigns": [], "segmentFilters": {"state": ["TX"]}}
        campaign = {
            "id": "c1",
            "name": "TX-NL",
            "states": ["TX"],
            "campaignConfig": {"precallSms": {"enabled": True}},
        }
        run = {"planId": "p1", "runId": "r1"}
        mock_oc = MagicMock()
        mock_oc.create_campaign.return_value = {"id": "connect-7"}
        mock_oc.pause_campaign.side_effect = RuntimeError("pause failed")
        originals = _stub_vip_shared(mock_oc)
        try:
            with (
                patch("executor._now_utc", return_value=_NOW_UTC),
                patch("executor._create_segment", return_value=("seg1", "arn:cp:seg1", None, None)),
                patch("executor.resolve_campaign_flow_arn", return_value="arn:flow"),
                patch(
                    "executor.build_campaign_params",
                    return_value={"connectCampaignFlowArn": "arn:flow"},
                ),
                patch("executor._account_id", return_value="123456789012"),
            ):
                connect_id, *_rest, paused_at = executor._create_campaign_only(
                    bucket, campaign, run
                )  # must not raise despite pause_campaign failing
        finally:
            _unstub_vip_shared(originals)

        assert connect_id == "connect-7"
        assert paused_at is None  # pause failed — never set on failure


class TestPollCampaignState:
    def _cs(self, **overrides):
        cs = {
            "campaignId": "c0",
            "name": "Test Campaign",
            "connectCampaignId": "conn-1",
            "status": "running",
        }
        cs.update(overrides)
        return cs

    def test_completed_state_marks_completed(self):
        cs = self._cs()
        with patch("executor._get_campaign_state", return_value="Completed"):
            executor._poll_campaign_state(cs)
        assert cs["status"] == "completed"
        assert cs["exitReason"] == executor.REASON_COMPLETED
        assert cs["completedAt"] is not None

    def test_failed_state_marks_error_with_detail(self):
        cs = self._cs()
        with patch("executor._get_campaign_state", return_value="Failed"):
            executor._poll_campaign_state(cs)
        assert cs["status"] == "error"
        assert cs["exitReason"] == executor.REASON_ERROR
        assert "Failed" in cs["errorDetail"]

    def test_stopped_state_marks_cancelled(self):
        cs = self._cs()
        with patch("executor._get_campaign_state", return_value="Stopped"):
            executor._poll_campaign_state(cs)
        assert cs["status"] == "cancelled"
        assert cs["exitReason"] == executor.REASON_STOPPED

    def test_deleted_state_marks_cancelled_and_notifies_sns(self):
        cs = self._cs()
        with (
            patch("executor._get_campaign_state", return_value="Deleted"),
            patch("executor._notify_sns") as mock_notify,
        ):
            executor._poll_campaign_state(cs)
        assert cs["status"] == "cancelled"
        assert cs["exitReason"] == "connect_deleted"
        mock_notify.assert_called_once()
        assert mock_notify.call_args.kwargs["attributes"]["alertType"] == "connect_deleted"

    def test_non_terminal_state_leaves_status_untouched(self):
        cs = self._cs(status="running")
        with patch("executor._get_campaign_state", return_value="InProgress"):
            executor._poll_campaign_state(cs)
        assert cs["status"] == "running"
        assert "exitReason" not in cs or cs.get("exitReason") is None


class TestPollCampaignStatePrecallGateRetry:
    """Step 3 of the 2026-09 adversarial-review resume-retry fix: the single
    resume attempt in _fire_precall_sms_for_campaign can itself fail
    (throttling, a transient AWS error), and before this fix nothing ever
    retried it — the campaign would sit paused, never dialing, for the rest
    of its run. tick() calls _poll_campaign_state on every running campaign
    every cycle, so this is where the stranded pause self-heals."""

    def _cs(self, **overrides):
        cs = {
            "campaignId": "c0",
            "name": "Test Campaign",
            "connectCampaignId": "conn-1",
            "status": "running",
            "precallGatePausedAt": "2026-09-09T00:00:00+00:00",
        }
        cs.update(overrides)
        return cs

    def test_retries_resume_when_stranded_paused(self):
        cs = self._cs()
        mock_oc = MagicMock()
        originals = _stub_vip_shared(mock_oc)
        try:
            with patch("executor._get_campaign_state", return_value="Paused"):
                executor._poll_campaign_state(cs, plan_id="p1", run_id="r1")
        finally:
            _unstub_vip_shared(originals)

        mock_oc.resume_campaign.assert_called_once_with("conn-1")
        assert cs["precallGateResumedAt"]
        assert cs["status"] == "running"  # untouched — "Paused" isn't terminal

    def test_does_not_retry_when_already_resumed(self):
        """Idempotent: a resume that already succeeded (precallGateResumedAt
        set) must never be re-attempted, even if Connect still briefly
        reports Paused due to eventual consistency."""
        cs = self._cs(precallGateResumedAt="2026-09-09T00:01:00+00:00")
        mock_oc = MagicMock()
        originals = _stub_vip_shared(mock_oc)
        try:
            with patch("executor._get_campaign_state", return_value="Paused"):
                executor._poll_campaign_state(cs)
        finally:
            _unstub_vip_shared(originals)

        mock_oc.resume_campaign.assert_not_called()

    def test_does_not_retry_when_genuinely_running(self):
        """The very first resume attempt already succeeded and Connect has
        moved on to Running — no redundant/duplicate resume call."""
        cs = self._cs()
        mock_oc = MagicMock()
        originals = _stub_vip_shared(mock_oc)
        try:
            with patch("executor._get_campaign_state", return_value="Running"):
                executor._poll_campaign_state(cs)
        finally:
            _unstub_vip_shared(originals)

        mock_oc.resume_campaign.assert_not_called()

    def test_does_not_retry_when_never_actually_paused_by_us(self):
        """No precallGatePausedAt (the cold-started or pause-failed noise
        cases) — resuming a campaign we never paused would predictably fail,
        so this is a structural no-op here too, same as in
        _fire_precall_sms_for_campaign."""
        cs = self._cs(precallGatePausedAt=None)
        mock_oc = MagicMock()
        originals = _stub_vip_shared(mock_oc)
        try:
            with patch("executor._get_campaign_state", return_value="Paused"):
                executor._poll_campaign_state(cs)
        finally:
            _unstub_vip_shared(originals)

        mock_oc.resume_campaign.assert_not_called()

    def test_retry_failure_is_logged_distinctly_and_does_not_raise(self):
        cs = self._cs()
        mock_oc = MagicMock()
        mock_oc.resume_campaign.side_effect = RuntimeError("Throttled")
        originals = _stub_vip_shared(mock_oc)
        try:
            with (
                patch("executor._get_campaign_state", return_value="Paused"),
                patch("executor._slog") as mock_slog,
            ):
                executor._poll_campaign_state(
                    cs, plan_id="p1", run_id="r1"
                )  # must not raise
        finally:
            _unstub_vip_shared(originals)

        assert cs.get("precallGateResumedAt") is None  # never marked — safe to retry again
        error_events = [c.args[0] for c in mock_slog.error.call_args_list]
        assert "precall_gate_resume_retry_failed" in error_events

    def test_state_fetched_only_once_per_poll(self):
        """The terminal-state check and the paused-retry check must share one
        _get_campaign_state call — this function exists specifically to avoid
        a second, redundant Connect API call per tick per running campaign."""
        cs = self._cs()
        mock_oc = MagicMock()
        originals = _stub_vip_shared(mock_oc)
        try:
            with patch(
                "executor._get_campaign_state", return_value="Paused"
            ) as mock_get_state:
                executor._poll_campaign_state(cs)
        finally:
            _unstub_vip_shared(originals)

        mock_get_state.assert_called_once_with("conn-1")
