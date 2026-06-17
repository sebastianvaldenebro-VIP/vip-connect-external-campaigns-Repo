import base64, json
from unittest.mock import MagicMock, patch
import pytest

# Patch all dependencies before importing handler
import sys


def _build_kinesis_event(agent_arn: str, status_type: str, status_name: str, next_status: str | None = None) -> dict:
    agent_event = {
        "EventType": "STATE_CHANGE",
        "AgentARN": agent_arn,
        "AgentSnapshot": {
            "AgentStatus": {"Type": status_type, "Name": status_name},
            "Configuration": {
                "RoutingProfile": {
                    "DefaultOutboundQueue": {"ARN": "arn:aws:connect:us-east-1:123:instance/abc/queue/q1"},
                    "Concurrency": []
                }
            }
        }
    }
    if next_status:
        agent_event["AgentSnapshot"]["NextAgentStatus"] = {"Name": next_status, "EnqueuedTimestamp": "2026"}
    encoded = base64.b64encode(json.dumps(agent_event).encode()).decode()
    return {"Records": [{"kinesis": {"data": encoded}}]}


def test_skips_heartbeat_events():
    heartbeat = {"EventType": "HEART_BEAT"}
    encoded = base64.b64encode(json.dumps(heartbeat).encode()).decode()
    event = {"Records": [{"kinesis": {"data": encoded}}]}

    with patch.dict("os.environ", {
        "CAMPAIGN_QUEUE_TABLE": "VipProgressiveCampaignQueue",
        "AGENT_LOCK_TABLE": "VipProgressiveAgentLocks",
        "SQS_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123/queue",
        "CONNECT_INSTANCE_ID": "instance-1",
        "CONTACT_FLOW_ID": "flow-1",
        "SOURCE_PHONE": "+19174105649",
        "ACTIVE_CAMPAIGN_ID": "campaign-1",
        "FIRSTORION_SECRET_NAME": "vip/firstorion/credentials",
    }):
        with patch("handler_consumer.AgentLock") as mock_lock, \
             patch("handler_consumer.CampaignQueue") as mock_queue, \
             patch("handler_consumer.FirstOrionClient") as mock_fo:
            from handler_consumer import lambda_handler
            result = lambda_handler(event, None)
            mock_lock.return_value.acquire.assert_not_called()


def test_dispatches_contact_when_agent_available():
    with patch.dict("os.environ", {
        "CAMPAIGN_QUEUE_TABLE": "VipProgressiveCampaignQueue",
        "AGENT_LOCK_TABLE": "VipProgressiveAgentLocks",
        "SQS_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123/queue",
        "CONNECT_INSTANCE_ID": "instance-1",
        "CONTACT_FLOW_ID": "flow-1",
        "SOURCE_PHONE": "+19174105649",
        "ACTIVE_CAMPAIGN_ID": "campaign-1",
        "FIRSTORION_SECRET_NAME": "vip/firstorion/credentials",
    }):
        from campaign_queue import Contact
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_queue = MagicMock()
        mock_queue.dequeue.return_value = Contact(
            campaign_id="campaign-1", contact_uuid="uuid-1",
            sk="ts1#uuid-1", phone="+15551234567"
        )
        mock_fo = MagicMock()
        mock_fo.push.return_value = True
        mock_sqs = MagicMock()
        mock_sqs.send_message.return_value = {}

        if "handler_consumer" in sys.modules:
            del sys.modules["handler_consumer"]

        with patch("handler_consumer.AgentLock", return_value=mock_lock), \
             patch("handler_consumer.CampaignQueue", return_value=mock_queue), \
             patch("handler_consumer.FirstOrionClient") as fo_cls, \
             patch("handler_consumer.boto3.client", return_value=mock_sqs):
            fo_cls.build_from_secret.return_value = mock_fo
            from handler_consumer import lambda_handler
            event = _build_kinesis_event("arn:agent/001", "ROUTABLE", "Available")
            result = lambda_handler(event, None)

        mock_fo.push.assert_called_once()
        mock_sqs.send_message.assert_called_once()
        # Verify SQS message has DelaySeconds=22 and no PHI (no destinationPhone)
        sqs_kwargs = mock_sqs.send_message.call_args[1]
        assert sqs_kwargs["DelaySeconds"] == 22
        body = json.loads(sqs_kwargs["MessageBody"])
        assert "destinationPhone" not in body  # PHI must never appear in SQS body
