import json
import sys
from unittest.mock import MagicMock, patch

import pytest


def _make_sqs_event(
    correlation_id: str | None = "abc12345",
    *,
    receive_count: str | None = None,
    lock_token: str | None = "tok-default",
) -> dict:
    # destinationPhone is intentionally absent — caller reads it from DynamoDB
    body = {
        "agentArn": "arn:aws:connect:us-east-1:165505826690:instance/abc/agent/agent-001",
        "queueArn": "arn:aws:connect:us-east-1:165505826690:instance/abc/queue/queue-001",
        "campaignId": "campaign-1",
        "contactSk": "2026-06-16T14:00:00.000Z#uuid-1",
        "sourcePhone": "+12125550199",
        "contactFlowId": "3d24320b-c1e3-40f3-90a2-b6867ef70c85",
        "instanceId": "6b3f17ba-68a4-472a-9b20-db1991507009",
    }
    if correlation_id is not None:
        body["correlationId"] = correlation_id
    # VIP-04: the fencing token propagated by handler_consumer.py/handler_kickstart.py.
    # Defaults to a fixed value so tests that don't care about it still exercise the
    # real propagation path (release() called with this exact token).
    if lock_token is not None:
        body["lockToken"] = lock_token
    record = {"body": json.dumps(body), "receiptHandle": "rh-001"}
    if receive_count is not None:
        record["attributes"] = {"ApproximateReceiveCount": receive_count}
    return {"Records": [record]}


def test_calls_start_outbound_voice_contact():
    if "handler_caller" in sys.modules:
        del sys.modules["handler_caller"]

    with patch.dict("os.environ", {
        "CAMPAIGN_QUEUE_TABLE": "VipProgressiveCampaignQueue",
        "AGENT_LOCK_TABLE": "VipProgressiveAgentLocks",
        "FIRSTORION_SECRET_NAME": "vip/firstorion/credentials",
        "OPT_OUT_TABLE": "VipConnectOptOutList",
    }):
        from connect_caller import DialResult
        mock_caller = MagicMock()
        mock_caller.dial.return_value = DialResult(success=True, contact_id="contact-001")
        mock_queue = MagicMock()
        mock_queue.get_phone.return_value = "+15551234567"  # PHI read from DDB, not SQS body
        mock_lock = MagicMock()

        with patch("handler_caller.ConnectCaller", return_value=mock_caller), \
             patch("handler_caller.CampaignQueue", return_value=mock_queue), \
             patch("handler_caller.AgentLock", return_value=mock_lock), \
             patch("handler_caller.build_opt_out_from_env", return_value=MagicMock(is_blocked=lambda *_: False)):
            from handler_caller import lambda_handler
            lambda_handler(_make_sqs_event(), None)

        # Verify phone is read from DDB (not from SQS body)
        mock_queue.get_phone.assert_called_once_with("campaign-1", "2026-06-16T14:00:00.000Z#uuid-1")
        mock_caller.dial.assert_called_once()
        call_kwargs = mock_caller.dial.call_args[1]
        assert call_kwargs["queue_id"] == "queue-001"
        assert call_kwargs["destination_phone"] == "+15551234567"
        # ClientToken must equal contactSk for SQS-redelivery idempotency
        assert call_kwargs["client_token"] == "2026-06-16T14:00:00.000Z#uuid-1"
        mock_queue.mark_dialed.assert_called_once_with("campaign-1", "2026-06-16T14:00:00.000Z#uuid-1", "contact-001")
        # Lock must NOT be released on success — call takes ~14s to bridge after
        # StartOutboundVoiceContact returns; releasing here caused CONTACT_FLOW_DISCONNECT.
        # Re-dispatch is allowed via AgentLock's stale-threshold condition (60s).
        mock_lock.release.assert_not_called()


def test_raises_on_throttle_for_sqs_retry():
    if "handler_caller" in sys.modules:
        del sys.modules["handler_caller"]

    with patch.dict("os.environ", {
        "CAMPAIGN_QUEUE_TABLE": "VipProgressiveCampaignQueue",
        "AGENT_LOCK_TABLE": "VipProgressiveAgentLocks",
        "FIRSTORION_SECRET_NAME": "vip/firstorion/credentials",
        "OPT_OUT_TABLE": "VipConnectOptOutList",
    }):
        from connect_caller import DialResult
        mock_caller = MagicMock()
        mock_caller.dial.return_value = DialResult(
            success=False, error_code="TooManyRequestsException", throttled=True
        )
        mock_queue = MagicMock()
        mock_queue.get_phone.return_value = "+15551234567"
        mock_lock = MagicMock()

        with patch("handler_caller.ConnectCaller", return_value=mock_caller), \
             patch("handler_caller.CampaignQueue", return_value=mock_queue), \
             patch("handler_caller.AgentLock", return_value=mock_lock), \
             patch("handler_caller.build_opt_out_from_env", return_value=MagicMock(is_blocked=lambda *_: False)):
            from handler_caller import lambda_handler
            # Exception must propagate so SQS redelivers the message
            with pytest.raises(RuntimeError, match="throttled"):
                lambda_handler(_make_sqs_event(), None)

        # mark_dialed must NOT be called on throttle
        mock_queue.mark_dialed.assert_not_called()
        # reset_to_pending must NOT be called — releasing the lock before SQS retry
        # would allow a duplicate First Orion push via the next agent-available event.
        mock_queue.reset_to_pending.assert_not_called()
        # lock must NOT be released — the TTL expiry handles cleanup.
        mock_lock.release.assert_not_called()


# ── VIP-03 (2026-09-11 audit): First Orion re-push moved from throttle-
# detection time to immediately before the retried dial ────────────────────
# Pushing at throttle-detection time (the old behavior) was stale long before
# the actual retried dial ~180s later (SQS visibilityTimeout), since First
# Orion's branding window is only ~10-30s. The push must instead fire right
# before caller.dial() on the REDELIVERED invocation (ApproximateReceiveCount
# > 1), so the push-to-dial gap always stays inside the window regardless of
# how long the message sat in the queue.


def test_no_first_orion_push_at_throttle_detection_time():
    """On the FIRST attempt (receive_count=1) hitting a throttle, no push must
    fire here — a push at throttle-detection time is followed by ~180s of
    queue delay before the retry, long past the branding window. The retry's
    own (redelivered) invocation is responsible for its own fresh push."""
    if "handler_caller" in sys.modules:
        del sys.modules["handler_caller"]

    with patch.dict("os.environ", {
        "CAMPAIGN_QUEUE_TABLE": "VipProgressiveCampaignQueue",
        "AGENT_LOCK_TABLE": "VipProgressiveAgentLocks",
        "FIRSTORION_SECRET_NAME": "vip/firstorion/credentials",
        "OPT_OUT_TABLE": "VipConnectOptOutList",
    }):
        from connect_caller import DialResult

        mock_caller = MagicMock()
        mock_caller.dial.return_value = DialResult(
            success=False, error_code="TooManyRequestsException", throttled=True
        )
        mock_queue = MagicMock()
        mock_queue.get_phone.return_value = "+15551234567"
        mock_lock = MagicMock()

        mock_fo_instance = MagicMock()
        mock_fo_instance.push.return_value = True
        mock_fo_class = MagicMock()
        mock_fo_class.build_from_secret.return_value = mock_fo_instance

        with patch("handler_caller.ConnectCaller", return_value=mock_caller), \
             patch("handler_caller.CampaignQueue", return_value=mock_queue), \
             patch("handler_caller.AgentLock", return_value=mock_lock), \
             patch("handler_caller.FirstOrionClient", mock_fo_class), \
             patch("handler_caller.build_opt_out_from_env", return_value=MagicMock(is_blocked=lambda *_: False)):
            from handler_caller import lambda_handler
            with pytest.raises(RuntimeError, match="throttled"):
                # receive_count=None -> ApproximateReceiveCount absent -> defaults to 1
                lambda_handler(_make_sqs_event(), None)

        mock_fo_instance.push.assert_not_called()


def test_first_orion_pushed_before_dial_on_redelivered_message():
    """A redelivered message (ApproximateReceiveCount > 1) must get a fresh
    First Orion push immediately before caller.dial() is called — regardless
    of whether this attempt then succeeds or throttles again."""
    if "handler_caller" in sys.modules:
        del sys.modules["handler_caller"]

    with patch.dict("os.environ", {
        "CAMPAIGN_QUEUE_TABLE": "VipProgressiveCampaignQueue",
        "AGENT_LOCK_TABLE": "VipProgressiveAgentLocks",
        "FIRSTORION_SECRET_NAME": "vip/firstorion/credentials",
        "OPT_OUT_TABLE": "VipConnectOptOutList",
    }):
        from connect_caller import DialResult

        call_order: list[str] = []

        mock_caller = MagicMock()

        def _dial(**kwargs):
            call_order.append("dial")
            return DialResult(success=True, contact_id="contact-retry-1")

        mock_caller.dial.side_effect = _dial
        mock_queue = MagicMock()
        mock_queue.get_phone.return_value = "+15551234567"
        mock_lock = MagicMock()

        mock_fo_instance = MagicMock()

        def _push(**kwargs):
            call_order.append("push")
            return True

        mock_fo_instance.push.side_effect = _push
        mock_fo_class = MagicMock()
        mock_fo_class.build_from_secret.return_value = mock_fo_instance

        with patch("handler_caller.ConnectCaller", return_value=mock_caller), \
             patch("handler_caller.CampaignQueue", return_value=mock_queue), \
             patch("handler_caller.AgentLock", return_value=mock_lock), \
             patch("handler_caller.FirstOrionClient", mock_fo_class), \
             patch("handler_caller.build_opt_out_from_env", return_value=MagicMock(is_blocked=lambda *_: False)):
            from handler_caller import lambda_handler
            lambda_handler(_make_sqs_event(receive_count="2"), None)

        mock_fo_instance.push.assert_called_once_with(
            a_number="+12125550199",
            b_number="+15551234567",
        )
        # Push must happen BEFORE the dial call, not after/independently of it.
        assert call_order == ["push", "dial"]


def test_first_orion_push_immediately_precedes_retried_dial_regardless_of_queue_delay():
    """Fake-clock proof (VIP-03 acceptance criterion): no matter how long a
    redelivered message sat in the SQS queue before this invocation ran, the
    First Orion push for it must land at essentially zero elapsed time before
    the dial call — never at some earlier, now-irrelevant clock value."""
    if "handler_caller" in sys.modules:
        del sys.modules["handler_caller"]

    with patch.dict("os.environ", {
        "CAMPAIGN_QUEUE_TABLE": "VipProgressiveCampaignQueue",
        "AGENT_LOCK_TABLE": "VipProgressiveAgentLocks",
        "FIRSTORION_SECRET_NAME": "vip/firstorion/credentials",
        "OPT_OUT_TABLE": "VipConnectOptOutList",
    }):
        from connect_caller import DialResult

        # A fake clock this test fully controls — advanced by an arbitrary
        # "time spent sitting in the SQS queue" BEFORE each invocation runs,
        # to model the redelivery delay (which can be anywhere from the 180s
        # visibilityTimeout up to however long the DLQ/redrive policy allows).
        fake_clock = {"t": 0.0}

        mock_caller = MagicMock()
        mock_queue = MagicMock()
        mock_queue.get_phone.return_value = "+15551234567"
        mock_lock = MagicMock()
        mock_fo_instance = MagicMock()
        mock_fo_class = MagicMock()
        mock_fo_class.build_from_secret.return_value = mock_fo_instance

        with patch("handler_caller.ConnectCaller", return_value=mock_caller), \
             patch("handler_caller.CampaignQueue", return_value=mock_queue), \
             patch("handler_caller.AgentLock", return_value=mock_lock), \
             patch("handler_caller.FirstOrionClient", mock_fo_class), \
             patch("handler_caller.build_opt_out_from_env", return_value=MagicMock(is_blocked=lambda *_: False)):
            from handler_caller import lambda_handler

            # First Orion's real branding window is ~10-30s — assert well past
            # it, at several different simulated queue delays.
            for queue_delay_seconds in (30, 180, 3_600, 86_400):
                timeline: list[tuple[str, float]] = []

                def _push(**kwargs):
                    timeline.append(("push", fake_clock["t"]))
                    return True

                def _dial(**kwargs):
                    timeline.append(("dial", fake_clock["t"]))
                    return DialResult(success=True, contact_id="contact-x")

                mock_fo_instance.push.side_effect = _push
                mock_caller.dial.side_effect = _dial

                fake_clock["t"] += queue_delay_seconds
                lambda_handler(_make_sqs_event(receive_count="2"), None)

                assert [name for name, _ in timeline] == ["push", "dial"]
                push_time = timeline[0][1]
                dial_time = timeline[1][1]
                # No wall-clock time is spent between push and dial — the
                # push always lands at "now" (this invocation's clock),
                # independent of how large queue_delay_seconds was.
                assert dial_time - push_time < 1.0, (
                    f"push-to-dial gap was {dial_time - push_time}s at "
                    f"simulated queue delay {queue_delay_seconds}s"
                )


def test_lock_held_after_mark_dialed_for_call_connect_window():
    """Lock must NOT be released on dial success.

    StartOutboundVoiceContact is async — the call takes ~14s to bridge to the agent
    after the API returns. Releasing at mark_dialed allowed a concurrent AVAILABLE event
    to dispatch a second call, causing CONTACT_FLOW_DISCONNECT on the first contact.
    Re-dispatch is gated by AgentLock.acquire()'s stale-threshold condition after 60s.
    """
    if "handler_caller" in sys.modules:
        del sys.modules["handler_caller"]

    with patch.dict("os.environ", {
        "CAMPAIGN_QUEUE_TABLE": "VipProgressiveCampaignQueue",
        "AGENT_LOCK_TABLE": "VipProgressiveAgentLocks",
        "FIRSTORION_SECRET_NAME": "vip/firstorion/credentials",
        "OPT_OUT_TABLE": "VipConnectOptOutList",
    }):
        from connect_caller import DialResult
        mock_caller = MagicMock()
        mock_caller.dial.return_value = DialResult(success=True, contact_id="contact-002")
        mock_queue = MagicMock()
        mock_queue.get_phone.return_value = "+15559999999"
        mock_lock = MagicMock()

        with patch("handler_caller.ConnectCaller", return_value=mock_caller), \
             patch("handler_caller.CampaignQueue", return_value=mock_queue), \
             patch("handler_caller.AgentLock", return_value=mock_lock), \
             patch("handler_caller.build_opt_out_from_env", return_value=MagicMock(is_blocked=lambda *_: False)):
            from handler_caller import lambda_handler
            lambda_handler(_make_sqs_event(correlation_id="corr0001"), None)

        mock_queue.mark_dialed.assert_called_once()
        # Lock must stay held — releasing here caused CONTACT_FLOW_DISCONNECT
        mock_lock.release.assert_not_called()


def test_reset_and_lock_released_when_phone_not_found():
    """Fix #5: when get_phone returns None, reset contact to PENDING and release lock."""
    if "handler_caller" in sys.modules:
        del sys.modules["handler_caller"]

    with patch.dict("os.environ", {
        "CAMPAIGN_QUEUE_TABLE": "VipProgressiveCampaignQueue",
        "AGENT_LOCK_TABLE": "VipProgressiveAgentLocks",
        "FIRSTORION_SECRET_NAME": "vip/firstorion/credentials",
        "OPT_OUT_TABLE": "VipConnectOptOutList",
    }):
        mock_caller = MagicMock()
        mock_queue = MagicMock()
        mock_queue.get_phone.return_value = None  # contact missing or phone field absent
        mock_lock = MagicMock()

        with patch("handler_caller.ConnectCaller", return_value=mock_caller), \
             patch("handler_caller.CampaignQueue", return_value=mock_queue), \
             patch("handler_caller.AgentLock", return_value=mock_lock), \
             patch("handler_caller.build_opt_out_from_env", return_value=MagicMock(is_blocked=lambda *_: False)):
            from handler_caller import lambda_handler
            result = lambda_handler(_make_sqs_event(correlation_id="corr0002"), None)

        assert result == {"status": "ok"}
        # dial must NOT be attempted — no phone available
        mock_caller.dial.assert_not_called()
        # contact must be reset so the next agent can retry it
        mock_queue.reset_to_pending.assert_called_once_with(
            "campaign-1", "2026-06-16T14:00:00.000Z#uuid-1"
        )
        # agent lock must be released so the agent can take the next dispatch,
        # using the exact fencing token this message carried (VIP-04).
        mock_lock.release.assert_called_once_with(
            "arn:aws:connect:us-east-1:165505826690:instance/abc/agent/agent-001",
            "tok-default",
        )


def test_blocked_number_skips_dial_and_releases_lock():
    if "handler_caller" in sys.modules:
        del sys.modules["handler_caller"]

    with patch.dict("os.environ", {
        "CAMPAIGN_QUEUE_TABLE": "VipProgressiveCampaignQueue",
        "AGENT_LOCK_TABLE": "VipProgressiveAgentLocks",
        "FIRSTORION_SECRET_NAME": "vip/firstorion/credentials",
        "OPT_OUT_TABLE": "VipConnectOptOutList",
    }):
        mock_caller = MagicMock()
        mock_queue = MagicMock()
        mock_queue.get_phone.return_value = "+15551234567"
        mock_lock = MagicMock()
        mock_opt_out = MagicMock()
        mock_opt_out.is_blocked.return_value = True

        with patch("handler_caller.ConnectCaller", return_value=mock_caller), \
             patch("handler_caller.CampaignQueue", return_value=mock_queue), \
             patch("handler_caller.AgentLock", return_value=mock_lock), \
             patch("handler_caller.build_opt_out_from_env", return_value=mock_opt_out):
            from handler_caller import lambda_handler
            lambda_handler(_make_sqs_event(), None)

        mock_opt_out.is_blocked.assert_called_once_with("+15551234567")
        mock_caller.dial.assert_not_called()
        mock_queue.mark_blocked.assert_called_once_with(
            "campaign-1", "2026-06-16T14:00:00.000Z#uuid-1"
        )
        mock_lock.release.assert_called_once_with(
            "arn:aws:connect:us-east-1:165505826690:instance/abc/agent/agent-001",
            "tok-default",
        )


def test_permanent_dial_failure_resets_contact_and_releases_lock():
    """A non-throttle dial failure (e.g. InvalidParameterException) must reset
    the contact to PENDING and release the agent lock, without raising."""
    if "handler_caller" in sys.modules:
        del sys.modules["handler_caller"]

    with patch.dict("os.environ", {
        "CAMPAIGN_QUEUE_TABLE": "VipProgressiveCampaignQueue",
        "AGENT_LOCK_TABLE": "VipProgressiveAgentLocks",
        "FIRSTORION_SECRET_NAME": "vip/firstorion/credentials",
        "OPT_OUT_TABLE": "VipConnectOptOutList",
    }):
        from connect_caller import DialResult
        mock_caller = MagicMock()
        mock_caller.dial.return_value = DialResult(
            success=False, error_code="InvalidParameterException"
        )
        mock_queue = MagicMock()
        mock_queue.get_phone.return_value = "+15551234567"
        mock_lock = MagicMock()

        with patch("handler_caller.ConnectCaller", return_value=mock_caller), \
             patch("handler_caller.CampaignQueue", return_value=mock_queue), \
             patch("handler_caller.AgentLock", return_value=mock_lock), \
             patch("handler_caller.build_opt_out_from_env", return_value=MagicMock(is_blocked=lambda *_: False)):
            from handler_caller import lambda_handler
            result = lambda_handler(_make_sqs_event(), None)

        assert result == {"status": "ok"}
        mock_queue.mark_dialed.assert_not_called()
        mock_queue.reset_to_pending.assert_called_once_with(
            "campaign-1", "2026-06-16T14:00:00.000Z#uuid-1"
        )
        mock_lock.release.assert_called_once_with(
            "arn:aws:connect:us-east-1:165505826690:instance/abc/agent/agent-001",
            "tok-default",
        )


def test_permanent_dial_failure_logs_but_does_not_raise_when_reset_fails():
    """reset_to_pending raising must be caught and logged, not propagate — a
    permanent dial failure must always ack the SQS message."""
    if "handler_caller" in sys.modules:
        del sys.modules["handler_caller"]

    with patch.dict("os.environ", {
        "CAMPAIGN_QUEUE_TABLE": "VipProgressiveCampaignQueue",
        "AGENT_LOCK_TABLE": "VipProgressiveAgentLocks",
        "FIRSTORION_SECRET_NAME": "vip/firstorion/credentials",
        "OPT_OUT_TABLE": "VipConnectOptOutList",
    }):
        from connect_caller import DialResult
        mock_caller = MagicMock()
        mock_caller.dial.return_value = DialResult(
            success=False, error_code="InvalidParameterException"
        )
        mock_queue = MagicMock()
        mock_queue.get_phone.return_value = "+15551234567"
        mock_queue.reset_to_pending.side_effect = RuntimeError("DynamoDB throttled")
        mock_lock = MagicMock()
        mock_lock.release.side_effect = RuntimeError("DynamoDB throttled")

        with patch("handler_caller.ConnectCaller", return_value=mock_caller), \
             patch("handler_caller.CampaignQueue", return_value=mock_queue), \
             patch("handler_caller.AgentLock", return_value=mock_lock), \
             patch("handler_caller.build_opt_out_from_env", return_value=MagicMock(is_blocked=lambda *_: False)):
            from handler_caller import lambda_handler
            result = lambda_handler(_make_sqs_event(), None)

        assert result == {"status": "ok"}


def test_missing_phone_logs_but_does_not_raise_when_reset_and_release_fail():
    """When get_phone returns None AND both reset_to_pending and lock.release
    raise, _process_message must still swallow both and return cleanly."""
    if "handler_caller" in sys.modules:
        del sys.modules["handler_caller"]

    with patch.dict("os.environ", {
        "CAMPAIGN_QUEUE_TABLE": "VipProgressiveCampaignQueue",
        "AGENT_LOCK_TABLE": "VipProgressiveAgentLocks",
        "FIRSTORION_SECRET_NAME": "vip/firstorion/credentials",
    }):
        mock_caller = MagicMock()
        mock_queue = MagicMock()
        mock_queue.get_phone.return_value = None
        mock_queue.reset_to_pending.side_effect = RuntimeError("DynamoDB throttled")
        mock_lock = MagicMock()
        mock_lock.release.side_effect = RuntimeError("DynamoDB throttled")

        with patch("handler_caller.ConnectCaller", return_value=mock_caller), \
             patch("handler_caller.CampaignQueue", return_value=mock_queue), \
             patch("handler_caller.AgentLock", return_value=mock_lock):
            from handler_caller import lambda_handler
            result = lambda_handler(_make_sqs_event(), None)

        assert result == {"status": "ok"}
        mock_caller.dial.assert_not_called()


def test_emit_metric_swallows_cloudwatch_errors():
    if "handler_caller" in sys.modules:
        del sys.modules["handler_caller"]

    with patch.dict("os.environ", {
        "CAMPAIGN_QUEUE_TABLE": "VipProgressiveCampaignQueue",
        "AGENT_LOCK_TABLE": "VipProgressiveAgentLocks",
        "FIRSTORION_SECRET_NAME": "vip/firstorion/credentials",
    }):
        import handler_caller

        mock_cw = MagicMock()
        mock_cw.put_metric_data.side_effect = RuntimeError("CloudWatch unavailable")
        with patch("handler_caller._get_cw", return_value=mock_cw):
            handler_caller._emit_metric("SomeMetric")  # must not raise

        mock_cw.put_metric_data.assert_called_once()


def test_correlation_id_fallback_when_absent_from_message():
    """Fix #3 backward compat: messages without correlationId fall back to contactSk[:8]."""
    if "handler_caller" in sys.modules:
        del sys.modules["handler_caller"]

    with patch.dict("os.environ", {
        "CAMPAIGN_QUEUE_TABLE": "VipProgressiveCampaignQueue",
        "AGENT_LOCK_TABLE": "VipProgressiveAgentLocks",
        "FIRSTORION_SECRET_NAME": "vip/firstorion/credentials",
        "OPT_OUT_TABLE": "VipConnectOptOutList",
    }):
        from connect_caller import DialResult
        mock_caller = MagicMock()
        mock_caller.dial.return_value = DialResult(success=True, contact_id="contact-003")
        mock_queue = MagicMock()
        mock_queue.get_phone.return_value = "+15558888888"
        mock_lock = MagicMock()

        with patch("handler_caller.ConnectCaller", return_value=mock_caller), \
             patch("handler_caller.CampaignQueue", return_value=mock_queue), \
             patch("handler_caller.AgentLock", return_value=mock_lock), \
             patch("handler_caller.build_opt_out_from_env", return_value=MagicMock(is_blocked=lambda *_: False)):
            from handler_caller import lambda_handler
            # correlationId=None means key is absent from the SQS body
            result = lambda_handler(_make_sqs_event(correlation_id=None), None)

        assert result == {"status": "ok"}
        # Dial and mark_dialed should still complete normally
        mock_caller.dial.assert_called_once()
        mock_queue.mark_dialed.assert_called_once()
