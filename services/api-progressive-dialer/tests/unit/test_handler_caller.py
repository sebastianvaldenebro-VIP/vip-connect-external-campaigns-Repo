import json, sys
from unittest.mock import MagicMock, patch
import pytest


def _make_sqs_event() -> dict:
    # destinationPhone is intentionally absent — caller reads it from DynamoDB
    body = {
        "agentArn": "arn:aws:connect:us-east-1:165505826690:instance/abc/agent/agent-001",
        "queueArn": "arn:aws:connect:us-east-1:165505826690:instance/abc/queue/queue-001",
        "campaignId": "campaign-1",
        "contactSk": "2026-06-16T14:00:00.000Z#uuid-1",
        "sourcePhone": "+19174105649",
        "contactFlowId": "3d24320b-c1e3-40f3-90a2-b6867ef70c85",
        "instanceId": "6b3f17ba-68a4-472a-9b20-db1991507009",
    }
    return {"Records": [{"body": json.dumps(body), "receiptHandle": "rh-001"}]}


def test_calls_start_outbound_voice_contact():
    if "handler_caller" in sys.modules:
        del sys.modules["handler_caller"]

    with patch.dict("os.environ", {
        "CAMPAIGN_QUEUE_TABLE": "VipProgressiveCampaignQueue",
        "AGENT_LOCK_TABLE": "VipProgressiveAgentLocks",
    }):
        from connect_caller import DialResult
        mock_caller = MagicMock()
        mock_caller.dial.return_value = DialResult(success=True, contact_id="contact-001")
        mock_queue = MagicMock()
        mock_queue.get_phone.return_value = "+15551234567"  # PHI read from DDB, not SQS body

        with patch("handler_caller.ConnectCaller", return_value=mock_caller), \
             patch("handler_caller.CampaignQueue", return_value=mock_queue):
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


def test_raises_on_throttle_for_sqs_retry():
    if "handler_caller" in sys.modules:
        del sys.modules["handler_caller"]

    with patch.dict("os.environ", {
        "CAMPAIGN_QUEUE_TABLE": "VipProgressiveCampaignQueue",
        "AGENT_LOCK_TABLE": "VipProgressiveAgentLocks",
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
        # contact must be reset to PENDING so the next agent can retry it
        mock_queue.reset_to_pending.assert_called_once_with(
            "campaign-1", "2026-06-16T14:00:00.000Z#uuid-1"
        )
        # lock must be released so the next AVAILABLE event can dispatch
        mock_lock.release.assert_called_once_with(
            "arn:aws:connect:us-east-1:165505826690:instance/abc/agent/agent-001"
        )
