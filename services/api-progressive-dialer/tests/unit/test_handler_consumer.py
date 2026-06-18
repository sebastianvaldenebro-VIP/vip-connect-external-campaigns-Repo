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
    if "handler_consumer" in sys.modules:
        del sys.modules["handler_consumer"]

    heartbeat = {"EventType": "HEART_BEAT"}
    encoded = base64.b64encode(json.dumps(heartbeat).encode()).decode()
    event = {"Records": [{"kinesis": {"data": encoded}}]}

    with patch.dict("os.environ", {
        "CAMPAIGN_QUEUE_TABLE": "VipProgressiveCampaignQueue",
        "AGENT_LOCK_TABLE": "VipProgressiveAgentLocks",
        "SQS_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123/queue",
        "CONNECT_INSTANCE_ID": "instance-1",
        "ACTIVE_CAMPAIGNS_TABLE": "VipActiveBrandedCampaigns",
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
        "ACTIVE_CAMPAIGNS_TABLE": "VipActiveBrandedCampaigns",
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
        mock_ddb = MagicMock()
        mock_ddb.query.return_value = {"Items": [{
            "campaignId": {"S": "campaign-1"},
            "contactFlowId": {"S": "flow-1"},
            "sourcePhone": {"S": "+12125550199"},
            "priority": {"N": "0"},
            "createdAt": {"S": "2026-06-18T10:00:00"},
        }]}

        if "handler_consumer" in sys.modules:
            del sys.modules["handler_consumer"]

        with patch("handler_consumer.AgentLock", return_value=mock_lock), \
             patch("handler_consumer.CampaignQueue", return_value=mock_queue), \
             patch("handler_consumer.FirstOrionClient") as fo_cls, \
             patch("handler_consumer._get_ddb", return_value=mock_ddb), \
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
        # correlationId must be present in the SQS body for end-to-end tracing
        assert "correlationId" in body
        assert len(body["correlationId"]) == 8  # short UUID prefix


def test_lambda_handler_propagates_processing_error():
    """Kinesis ESM must receive a non-200 so it retries the batch on DDB/SQS failures."""
    if "handler_consumer" in sys.modules:
        del sys.modules["handler_consumer"]

    with patch.dict("os.environ", {
        "CAMPAIGN_QUEUE_TABLE": "VipProgressiveCampaignQueue",
        "AGENT_LOCK_TABLE": "VipProgressiveAgentLocks",
        "SQS_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123/queue",
        "CONNECT_INSTANCE_ID": "instance-1",
        "ACTIVE_CAMPAIGNS_TABLE": "VipActiveBrandedCampaigns",
        "FIRSTORION_SECRET_NAME": "vip/firstorion/credentials",
    }):
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_queue = MagicMock()
        mock_queue.dequeue.side_effect = RuntimeError("DynamoDB unavailable")
        mock_ddb = MagicMock()
        mock_ddb.query.return_value = {"Items": [{
            "campaignId": {"S": "campaign-1"},
            "contactFlowId": {"S": "flow-1"},
            "sourcePhone": {"S": "+12125550199"},
            "priority": {"N": "0"},
            "createdAt": {"S": "2026-06-18T10:00:00"},
        }]}

        if "handler_consumer" in sys.modules:
            del sys.modules["handler_consumer"]

        with patch("handler_consumer.AgentLock", return_value=mock_lock), \
             patch("handler_consumer.CampaignQueue", return_value=mock_queue), \
             patch("handler_consumer.FirstOrionClient") as fo_cls, \
             patch("handler_consumer._get_ddb", return_value=mock_ddb), \
             patch("handler_consumer.boto3.client", return_value=MagicMock()):
            fo_cls.build_from_secret.return_value = MagicMock()
            from handler_consumer import lambda_handler
            event = _build_kinesis_event("arn:agent/001", "ROUTABLE", "Available")
            # Exception must propagate — Kinesis ESM retries the batch
            with pytest.raises(RuntimeError, match="DynamoDB unavailable"):
                lambda_handler(event, None)


class TestGsiCampaignLookup:
    """Consumer queries VipActiveBrandedCampaigns by queueArn instead of static env."""

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        """Ensure handler_consumer is importable for every test in this class."""
        monkeypatch.setenv("CAMPAIGN_QUEUE_TABLE", "VipProgressiveCampaignQueue")
        monkeypatch.setenv("AGENT_LOCK_TABLE", "VipProgressiveAgentLocks")
        monkeypatch.setenv("SQS_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/queue")
        monkeypatch.setenv("CONNECT_INSTANCE_ID", "instance-1")
        monkeypatch.setenv("ACTIVE_CAMPAIGNS_TABLE", "VipActiveBrandedCampaigns")
        monkeypatch.setenv("FIRSTORION_SECRET_NAME", "vip/firstorion/credentials")
        # Ensure fresh import so env vars are picked up
        sys.modules.pop("handler_consumer", None)
        import handler_consumer  # noqa: F401 — side-effect: populates sys.modules
        yield
        sys.modules.pop("handler_consumer", None)

    def _make_campaign_item(self, campaign_id, priority=0, created_at="2026-06-18T10:00:00"):
        return {
            "campaignId": {"S": campaign_id},
            "contactFlowId": {"S": "flow-abc"},
            "sourcePhone": {"S": "+12125550199"},
            "priority": {"N": str(priority)},
            "createdAt": {"S": created_at},
        }

    def test_queries_active_campaigns_by_queue_arn(self, mocker):
        ddb = mocker.patch("handler_consumer._get_ddb")
        ddb.return_value.query.return_value = {"Items": []}
        mocker.patch("handler_consumer.is_agent_available", return_value=True)
        mocker.patch("handler_consumer.extract_agent_info", return_value={
            "agent_arn": "arn:aws:connect:::agent/a1",
            "queue_arn": "arn:aws:connect:::queue/q1",
        })
        mocker.patch("handler_consumer.is_queue_allowed", return_value=True)

        import handler_consumer, base64, json
        record = {"kinesis": {"data": base64.b64encode(json.dumps({}).encode()).decode()}}
        handler_consumer._process_record(record)

        ddb.return_value.query.assert_called_once_with(
            TableName="VipActiveBrandedCampaigns",
            IndexName="queueArn-index",
            KeyConditionExpression="queueArn = :q",
            ExpressionAttributeValues={":q": {"S": "arn:aws:connect:::queue/q1"}},
        )

    def test_picks_lowest_priority_campaign_first(self, mocker):
        items = [
            self._make_campaign_item("camp-low",  priority=1, created_at="2026-06-18T10:00:00"),
            self._make_campaign_item("camp-high", priority=0, created_at="2026-06-18T10:05:00"),
        ]
        mocker.patch("handler_consumer._get_ddb").return_value.query.return_value = {"Items": items}
        mocker.patch("handler_consumer.is_agent_available", return_value=True)
        mocker.patch("handler_consumer.extract_agent_info", return_value={
            "agent_arn": "arn::agent/a1", "queue_arn": "arn::queue/q1",
        })
        mocker.patch("handler_consumer.is_queue_allowed", return_value=True)
        lock = mocker.patch("handler_consumer._get_lock").return_value
        lock.acquire.return_value = True
        queue = mocker.patch("handler_consumer._get_queue").return_value
        queue.dequeue.return_value = None  # both empty — just verify order

        import handler_consumer, base64, json
        record = {"kinesis": {"data": base64.b64encode(json.dumps({}).encode()).decode()}}
        handler_consumer._process_record(record)

        calls = [c.args[0] for c in queue.dequeue.call_args_list]
        assert calls == ["camp-high", "camp-low"]  # priority 0 first

    def test_no_active_campaigns_skips_dispatch(self, mocker):
        mocker.patch("handler_consumer._get_ddb").return_value.query.return_value = {"Items": []}
        mocker.patch("handler_consumer.is_agent_available", return_value=True)
        mocker.patch("handler_consumer.extract_agent_info", return_value={
            "agent_arn": "arn::agent/a1", "queue_arn": "arn::queue/q1",
        })
        mocker.patch("handler_consumer.is_queue_allowed", return_value=True)
        lock = mocker.patch("handler_consumer._get_lock").return_value

        import handler_consumer, base64, json
        record = {"kinesis": {"data": base64.b64encode(json.dumps({}).encode()).decode()}}
        handler_consumer._process_record(record)

        lock.acquire.assert_not_called()
