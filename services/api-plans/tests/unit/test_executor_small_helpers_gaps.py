"""Targeted tests for small remaining gaps in executor.py helper functions:

- _maybe_loop's own exception handler (malformed loop time strings)
- _bucket_has_only_legitimate_waits' terminal-status `continue` and
  non-queued `return False` branches (always mocked away elsewhere)
- _bucket_completed's out-of-range `return True` guard
- _next_bucket_warming's out-of-range `return False` guard
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
sys.modules.setdefault(
    "vip_shared.infrastructure.persistence.redis_lead_source", MagicMock()
)

import executor  # noqa: E402, F401


class TestMaybeLoopMalformedTime:
    def test_swallows_malformed_end_time_string(self):
        plan = {"planId": "p1", "loop": {"endTime": "not-a-time"}}
        with (
            patch("executor.get_plan", return_value=plan),
            patch("executor.get_latest_run", return_value=None),
            patch("executor.start_run") as mock_start,
        ):
            executor._maybe_loop("p1")  # must not raise
        mock_start.assert_not_called()

    def test_swallows_malformed_start_time_string(self):
        plan = {"planId": "p1", "loop": {"endTime": "23:59", "startTime": "bad"}}
        with (
            patch("executor.get_plan", return_value=plan),
            patch("executor.get_latest_run", return_value=None),
            patch("executor.start_run") as mock_start,
        ):
            executor._maybe_loop("p1")  # must not raise
        mock_start.assert_not_called()


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


class TestBucketHasOnlyLegitimateWaits:
    def test_terminal_campaign_is_skipped_via_continue(self):
        # A completed campaign (terminal) alongside a queued-with-no-deps
        # campaign: the terminal one must be skipped (continue at line 3474)
        # and the queued-no-deps one still forces an overall False.
        b0 = {"id": "b0", "campaigns": [{"id": "c0", "name": "c0"}, {"id": "c1", "name": "c1"}]}
        plan = _plan([b0])
        bucket_state = _bucket_state(
            "b0",
            [
                _campaign_state("c0", status="completed"),
                _campaign_state("c1", status="queued"),
            ],
        )
        run = _run(plan, [bucket_state])

        result = executor._bucket_has_only_legitimate_waits(run, plan, bucket_state)

        assert result is False  # c1 has no dependsOn, so not a legitimate wait

    def test_non_queued_non_terminal_status_returns_false(self):
        # A campaign in "creating" status is neither terminal nor queued —
        # already active, so this is not a "legitimate wait" bucket.
        b0 = {"id": "b0", "campaigns": [{"id": "c0", "name": "c0"}]}
        plan = _plan([b0])
        bucket_state = _bucket_state(
            "b0", [_campaign_state("c0", status="creating")]
        )
        run = _run(plan, [bucket_state])

        result = executor._bucket_has_only_legitimate_waits(run, plan, bucket_state)

        assert result is False

    def test_all_terminal_campaigns_returns_true(self):
        # Every campaign in the bucket is terminal -> loop body never
        # returns False -> falls through to the final `return True`.
        b0 = {"id": "b0", "campaigns": [{"id": "c0", "name": "c0"}]}
        plan = _plan([b0])
        bucket_state = _bucket_state(
            "b0", [_campaign_state("c0", status="completed")]
        )
        run = _run(plan, [bucket_state])

        result = executor._bucket_has_only_legitimate_waits(run, plan, bucket_state)

        assert result is True


class TestBucketCompletedOutOfRange:
    def test_negative_index_returns_true(self):
        run = {"bucketStates": [{"status": "running"}]}
        assert executor._bucket_completed(run, -1) is True

    def test_index_past_end_returns_true(self):
        run = {"bucketStates": [{"status": "running"}]}
        assert executor._bucket_completed(run, 5) is True


class TestNextBucketWarmingOutOfRange:
    def test_last_bucket_has_no_next_bucket(self):
        run = {"bucketStates": [{"status": "running"}, {"status": "queued"}]}
        assert executor._next_bucket_warming(run, 1) is False


class TestCheckRedisReadyExceptionFallback:
    def test_swallows_import_error_and_assumes_ready(self):
        # vip_shared.infrastructure.persistence.redis_lead_source is stubbed as a
        # bare MagicMock elsewhere in the suite; forcing build_from_env to raise
        # exercises the except-Exception -> return True fallback for real.
        with patch(
            "vip_shared.infrastructure.persistence.redis_lead_source.build_from_env",
            side_effect=RuntimeError("Redis connection refused"),
        ):
            assert executor._check_redis_ready() is True


class TestNormalizePhoneE164AlreadyE164NonNanp:
    def test_returns_non_nanp_plus_prefixed_number_unchanged(self):
        # 12 digits after stripping "+" — doesn't match the 10-digit or
        # 11-digit-starting-with-1 branches, so it falls through to the final
        # "already E.164, pass through unchanged" return.
        assert executor._normalize_phone_e164("+442012345678") == "+442012345678"


class TestIsGeographicNanpPhone:
    def test_geographic_area_code_is_true(self):
        assert executor._is_geographic_nanp_phone("+19174105649") is True

    def test_toll_free_800_is_false(self):
        assert executor._is_geographic_nanp_phone("+18005551234") is False

    def test_each_toll_free_prefix_is_false(self):
        for npa in ("800", "822", "833", "844", "855", "866", "877",
                     "880", "881", "882", "883", "884", "885", "886",
                     "887", "888", "889", "900"):
            assert executor._is_geographic_nanp_phone(f"+1{npa}5551234") is False

    def test_non_nanp_country_code_passes_through(self):
        # Out of scope for the AREA_CODE +1 42-timezone fallback -- not filtered.
        assert executor._is_geographic_nanp_phone("+442012345678") is True

    def test_malformed_plus1_wrong_length_passes_through(self):
        # Defensive: only a well-formed 12-char +1XXXXXXXXXX string is checked
        # against the non-geographic set; anything else is left to the
        # existing _normalize_phone_e164 format validation.
        assert executor._is_geographic_nanp_phone("+1917410564") is True
