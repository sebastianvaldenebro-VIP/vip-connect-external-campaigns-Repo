import json
import sys
from unittest.mock import MagicMock, patch

import pytest


def _make_sqs_event(correlation_id: str | None = "abc12345") -> dict:
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
    return {"Records": [{"body": json.dumps(body), "receiptHandle": "rh-001"}]}


def test_calls_start_outbound_voice_contact():
    if "handler_caller" in sys.modules:
        del sys.modules["handler_caller"]

    with patch.dict("os.environ", {
        "CAMPAIGN_QUEUE_TABLE": "VipProgressiveCampaignQueue",
        "AGENT_LOCK_TABLE": "VipProgressiveAgentLocks",
        "FIRSTORION_SECRET_NAME": "vip/firstorion/credentials",
    }):
        from connect_caller import DialResult
        mock_caller = MagicMock()
        mock_caller.dial.return_value = DialResult(success=True, contact_id="contact-001")
        mock_queue = MagicMock()
        mock_queue.get_phone.return_value = "+15551234567"  # PHI read from DDB, not SQS body
        mock_lock = MagicMock()

        with patch("handler_caller.ConnectCaller", return_value=mock_caller), \
             patch("handler_caller.CampaignQueue", return_value=mock_queue), \
             patch("handler_caller.AgentLock", return_value=mock_lock):
            from handler_caller import lambda_handler
            result = lambda_handler(_make_sqs_event(), None)

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
             patch("handler_caller.AgentLock", return_value=mock_lock):
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


def test_first_orion_repushed_before_raise_on_throttle():
    """First Orion push must fire before re-raising on throttle so the SQS-retried
    dial lands inside a fresh branding window (original window expired during
    the 180s visibilityTimeout)."""
    if "handler_caller" in sys.modules:
        del sys.modules["handler_caller"]

    with patch.dict("os.environ", {
        "CAMPAIGN_QUEUE_TABLE": "VipProgressiveCampaignQueue",
        "AGENT_LOCK_TABLE": "VipProgressiveAgentLocks",
        "FIRSTORION_SECRET_NAME": "vip/firstorion/credentials",
    }):
        from connect_caller import DialResult

        mock_caller = MagicMock()
        mock_caller.dial.return_value = DialResult(
            success=False, error_code="TooManyRequestsException", throttled=True
        )
        mock_queue = MagicMock()
        mock_queue.get_phone.return_value = "+15551234567"
        mock_lock = MagicMock()

        # _get_fo() calls FirstOrionClient.build_from_secret() which returns the instance.
        # We patch the class so build_from_secret() returns a controllable mock instance.
        mock_fo_instance = MagicMock()
        mock_fo_instance.push.return_value = True
        mock_fo_class = MagicMock()
        mock_fo_class.build_from_secret.return_value = mock_fo_instance

        with patch("handler_caller.ConnectCaller", return_value=mock_caller), \
             patch("handler_caller.CampaignQueue", return_value=mock_queue), \
             patch("handler_caller.AgentLock", return_value=mock_lock), \
             patch("handler_caller.FirstOrionClient", mock_fo_class):
            from handler_caller import lambda_handler
            with pytest.raises(RuntimeError, match="throttled"):
                lambda_handler(_make_sqs_event(), None)

        # First Orion push must have been called with a_number=sourcePhone, b_number=destinationPhone
        mock_fo_instance.push.assert_called_once_with(
            a_number="+12125550199",
            b_number="+15551234567",
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
    }):
        from connect_caller import DialResult
        mock_caller = MagicMock()
        mock_caller.dial.return_value = DialResult(success=True, contact_id="contact-002")
        mock_queue = MagicMock()
        mock_queue.get_phone.return_value = "+15559999999"
        mock_lock = MagicMock()

        with patch("handler_caller.ConnectCaller", return_value=mock_caller), \
             patch("handler_caller.CampaignQueue", return_value=mock_queue), \
             patch("handler_caller.AgentLock", return_value=mock_lock):
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
    }):
        mock_caller = MagicMock()
        mock_queue = MagicMock()
        mock_queue.get_phone.return_value = None  # contact missing or phone field absent
        mock_lock = MagicMock()

        with patch("handler_caller.ConnectCaller", return_value=mock_caller), \
             patch("handler_caller.CampaignQueue", return_value=mock_queue), \
             patch("handler_caller.AgentLock", return_value=mock_lock):
            from handler_caller import lambda_handler
            result = lambda_handler(_make_sqs_event(correlation_id="corr0002"), None)

        assert result == {"status": "ok"}
        # dial must NOT be attempted — no phone available
        mock_caller.dial.assert_not_called()
        # contact must be reset so the next agent can retry it
        mock_queue.reset_to_pending.assert_called_once_with(
            "campaign-1", "2026-06-16T14:00:00.000Z#uuid-1"
        )
        # agent lock must be released so the agent can take the next dispatch
        mock_lock.release.assert_called_once_with(
            "arn:aws:connect:us-east-1:165505826690:instance/abc/agent/agent-001"
        )


def test_correlation_id_fallback_when_absent_from_message():
    """Fix #3 backward compat: messages without correlationId fall back to contactSk[:8]."""
    if "handler_caller" in sys.modules:
        del sys.modules["handler_caller"]

    with patch.dict("os.environ", {
        "CAMPAIGN_QUEUE_TABLE": "VipProgressiveCampaignQueue",
        "AGENT_LOCK_TABLE": "VipProgressiveAgentLocks",
        "FIRSTORION_SECRET_NAME": "vip/firstorion/credentials",
    }):
        from connect_caller import DialResult
        mock_caller = MagicMock()
        mock_caller.dial.return_value = DialResult(success=True, contact_id="contact-003")
        mock_queue = MagicMock()
        mock_queue.get_phone.return_value = "+15558888888"
        mock_lock = MagicMock()

        with patch("handler_caller.ConnectCaller", return_value=mock_caller), \
             patch("handler_caller.CampaignQueue", return_value=mock_queue), \
             patch("handler_caller.AgentLock", return_value=mock_lock):
            from handler_caller import lambda_handler
            # correlationId=None means key is absent from the SQS body
            result = lambda_handler(_make_sqs_event(correlation_id=None), None)

        assert result == {"status": "ok"}
        # Dial and mark_dialed should still complete normally
        mock_caller.dial.assert_called_once()
        mock_queue.mark_dialed.assert_called_once()
