"""Targeted tests for validation/SMS-cleanup branches in skip_campaign and
force_stop_campaign not already covered by test_executor_v2.py.
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


def _campaign_state(cid, status="running", **overrides):
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


class TestSkipCampaignValidation:
    def test_raises_when_run_not_found(self):
        with patch("executor.get_run", return_value=None):
            with pytest.raises(ValueError, match="not found"):
                executor.skip_campaign("p1", "r1", 0, 0)

    def test_raises_when_run_not_running(self):
        run = _run(_plan([]), [], status="completed")
        with patch("executor.get_run", return_value=run):
            with pytest.raises(ValueError, match="is not running"):
                executor.skip_campaign("p1", "r1", 0, 0)

    def test_raises_when_bucket_index_out_of_range(self):
        run = _run(_plan([]), [_bucket_state("b0", [])])
        with patch("executor.get_run", return_value=run):
            with pytest.raises(ValueError, match="Bucket index"):
                executor.skip_campaign("p1", "r1", 5, 0)

    def test_raises_when_campaign_index_out_of_range(self):
        run = _run(_plan([]), [_bucket_state("b0", [_campaign_state("c0")])])
        with patch("executor.get_run", return_value=run):
            with pytest.raises(ValueError, match="Campaign index"):
                executor.skip_campaign("p1", "r1", 0, 5)

    def test_reraises_concurrent_write_error_after_max_retries(self):
        plan = _plan([{"id": "b0", "campaigns": [{"id": "c0", "name": "c0"}]}])

        def _fresh_run(*_a, **_k):
            return _run(plan, [_bucket_state("b0", [_campaign_state("c0", status="running")])])

        with (
            patch("executor.get_run", side_effect=_fresh_run),
            patch("executor._dispatch_ready_campaigns", return_value=False),
            patch("executor._all_campaigns_terminal", return_value=False),
            patch("executor.save_run", side_effect=executor.ConcurrentWriteError("race")),
        ):
            with pytest.raises(executor.ConcurrentWriteError, match="race"):
                executor.skip_campaign("p1", "r1", 0, 0)

    def test_stops_sms_campaign_when_running(self):
        plan = _plan([{"id": "b0", "campaigns": [{"id": "c0", "name": "c0"}]}])
        cs = _campaign_state("c0", status="running", smsCampaignId="sms-1")
        bs = _bucket_state("b0", [cs])
        run = _run(plan, [bs])
        with (
            patch("executor.get_run", return_value=run),
            patch("executor._stop_sms_campaign") as mock_stop_sms,
            patch("executor._dispatch_ready_campaigns", return_value=False),
            patch("executor._all_campaigns_terminal", return_value=True),
            patch("executor._advance_bucket"),
        ):
            executor.skip_campaign("p1", "r1", 0, 0)
        mock_stop_sms.assert_called_once_with(cs)
        assert cs["status"] == "cancelled"
        assert cs["exitReason"] == "skipped"


class TestForceStopCampaignValidation:
    def test_raises_when_run_not_found(self):
        with patch("executor.get_run", return_value=None):
            with pytest.raises(ValueError, match="not found"):
                executor.force_stop_campaign("p1", "r1", 0, 0)

    def test_raises_when_run_not_running(self):
        run = _run(_plan([]), [], status="completed")
        with patch("executor.get_run", return_value=run):
            with pytest.raises(ValueError, match="is not running"):
                executor.force_stop_campaign("p1", "r1", 0, 0)

    def test_raises_when_bucket_index_out_of_range(self):
        run = _run(_plan([]), [_bucket_state("b0", [])])
        with patch("executor.get_run", return_value=run):
            with pytest.raises(ValueError, match="Bucket index"):
                executor.force_stop_campaign("p1", "r1", 5, 0)

    def test_raises_when_campaign_index_out_of_range(self):
        run = _run(_plan([]), [_bucket_state("b0", [_campaign_state("c0")])])
        with patch("executor.get_run", return_value=run):
            with pytest.raises(ValueError, match="Campaign index"):
                executor.force_stop_campaign("p1", "r1", 0, 5)

    def test_raises_when_campaign_not_running_and_first_attempt(self):
        run = _run(_plan([]), [_bucket_state("b0", [_campaign_state("c0", status="queued")])])
        with patch("executor.get_run", return_value=run):
            with pytest.raises(ValueError, match="can only stop running campaigns"):
                executor.force_stop_campaign("p1", "r1", 0, 0)

    def test_stops_connect_campaign_when_present(self):
        plan = _plan([{"id": "b0", "campaigns": [{"id": "c0", "name": "c0"}]}])
        cs = _campaign_state("c0", status="running", connectCampaignId="conn-1")
        bs = _bucket_state("b0", [cs])
        run = _run(plan, [bs])
        with (
            patch("executor.get_run", return_value=run),
            patch("executor._safe_stop_campaign") as mock_stop,
            patch("executor._all_campaigns_terminal", return_value=True),
            patch("executor._advance_bucket"),
        ):
            executor.force_stop_campaign("p1", "r1", 0, 0)
        mock_stop.assert_called_once_with("conn-1")
        assert cs["status"] == "expired"
        assert cs["exitReason"] == "manually_stopped"

    def test_stops_branded_campaign_and_writes_summary(self):
        plan = _plan([{"id": "b0", "campaigns": [{"id": "c0", "name": "c0"}]}])
        cs = _campaign_state("c0", status="running", brandedCampaignId="bc-1", queueArn="arn:q1")
        bs = _bucket_state("b0", [cs])
        run = _run(plan, [bs])
        with (
            patch("executor.get_run", return_value=run),
            patch("executor._write_branded_run_summary") as mock_write,
            patch("executor._stop_branded_campaign") as mock_stop_branded,
            patch("executor._all_campaigns_terminal", return_value=True),
            patch("executor._advance_bucket"),
        ):
            executor.force_stop_campaign("p1", "r1", 0, 0)
        mock_write.assert_called_once_with("p1", "r1", cs)
        mock_stop_branded.assert_called_once_with(cs)

    def test_stops_sms_campaign(self):
        plan = _plan([{"id": "b0", "campaigns": [{"id": "c0", "name": "c0"}]}])
        cs = _campaign_state("c0", status="running", smsCampaignId="sms-1")
        bs = _bucket_state("b0", [cs])
        run = _run(plan, [bs])
        with (
            patch("executor.get_run", return_value=run),
            patch("executor._stop_sms_campaign") as mock_stop_sms,
            patch("executor._all_campaigns_terminal", return_value=True),
            patch("executor._advance_bucket"),
        ):
            executor.force_stop_campaign("p1", "r1", 0, 0)
        mock_stop_sms.assert_called_once_with(cs)

    def test_returns_run_when_campaign_already_terminal_on_retry(self):
        """A concurrent tick that already advanced the campaign to a terminal
        state between retry attempts must be treated as success, not raise."""
        plan = _plan([{"id": "b0", "campaigns": [{"id": "c0", "name": "c0"}]}])
        first_run = _run(plan, [_bucket_state("b0", [_campaign_state("c0", status="running", connectCampaignId="conn-1")])])
        second_run = _run(plan, [_bucket_state("b0", [_campaign_state("c0", status="completed")])])

        with (
            patch("executor.get_run", side_effect=[first_run, second_run]),
            patch("executor._safe_stop_campaign"),
            patch("executor._all_campaigns_terminal", return_value=False),
            patch("executor.save_run", side_effect=executor.ConcurrentWriteError("race")),
        ):
            result = executor.force_stop_campaign("p1", "r1", 0, 0)

        assert result is second_run

    def test_reraises_concurrent_write_error_after_max_retries(self):
        plan = _plan([{"id": "b0", "campaigns": [{"id": "c0", "name": "c0"}]}])

        def _fresh_run(*_a, **_k):
            return _run(
                plan,
                [_bucket_state("b0", [_campaign_state("c0", status="running", connectCampaignId="conn-1")])],
            )

        with (
            patch("executor.get_run", side_effect=_fresh_run),
            patch("executor._safe_stop_campaign"),
            patch("executor._all_campaigns_terminal", return_value=False),
            patch("executor.save_run", side_effect=executor.ConcurrentWriteError("race")),
        ):
            with pytest.raises(executor.ConcurrentWriteError, match="race"):
                executor.force_stop_campaign("p1", "r1", 0, 0)

    def test_saves_run_directly_when_not_all_terminal(self):
        plan = _plan([{"id": "b0", "campaigns": [{"id": "c0", "name": "c0"}]}])
        cs = _campaign_state("c0", status="running", connectCampaignId="conn-1")
        bs = _bucket_state("b0", [cs])
        run = _run(plan, [bs])
        with (
            patch("executor.get_run", return_value=run),
            patch("executor._safe_stop_campaign"),
            patch("executor._all_campaigns_terminal", return_value=False),
            patch("executor.save_run") as mock_save,
            patch("executor._advance_bucket") as mock_advance,
        ):
            executor.force_stop_campaign("p1", "r1", 0, 0)
        mock_save.assert_called_once_with(run)
        mock_advance.assert_not_called()
