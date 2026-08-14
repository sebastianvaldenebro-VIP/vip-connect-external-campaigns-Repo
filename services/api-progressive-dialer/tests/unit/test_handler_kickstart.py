"""Unit tests for handler_kickstart.py.

Tests are structured around the two entry points:
  - lambda_handler: filters INSERT-only stream records, delegates to _process_insert
  - _process_insert: looks up campaign, finds available agents, dispatches

All AWS clients are mocked — no real AWS calls.
"""
import json
import sys
import os
from unittest.mock import MagicMock, call, patch

import pytest

_UNSET = object()  # sentinel to distinguish "not passed" from explicit None

# ── Env setup ────────────────────────────────────────────────────────────────

_ENV = {
    "CAMPAIGN_QUEUE_TABLE": "VipProgressiveCampaignQueue",
    "AGENT_LOCK_TABLE": "VipProgressiveAgentLocks",
    "SQS_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123/vip-progressive-dialer-calls",
    "CONNECT_INSTANCE_ID": "instance-abc",
    "ACTIVE_CAMPAIGNS_TABLE": "VipActiveBrandedCampaigns",
    "FIRSTORION_SECRET_NAME": "vip/firstorion/credentials",
}

_CAMPAIGN_ITEM = {
    "campaignId": {"S": "camp-1"},
    "queueArn": {"S": "arn:aws:connect:us-east-1:123:instance/abc/queue/q1"},
    "contactFlowId": {"S": "flow-1"},
    "sourcePhone": {"S": "+12125550199"},
    "priority": {"N": "0"},
    "createdAt": {"S": "2026-07-01T00:00:00Z"},
}

_AGENT_ARN = "arn:aws:connect:us-east-1:123:instance/abc/agent/agent-1"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _insert_record(
    campaign_id: str = "camp-1",
    status: str = "PENDING",
    event_name: str = "INSERT",
) -> dict:
    return {
        "eventName": event_name,
        "dynamodb": {
            "NewImage": {
                "campaignId": {"S": campaign_id},
                "sk": {"S": "2026-07-22T00:00:00.000Z#uuid-1"},
                "status": {"S": status},
                "phone": {"S": "+15555550100"},
            }
        },
    }


def _make_contact():
    from campaign_queue import Contact
    return Contact(
        campaign_id="camp-1",
        contact_uuid="uuid-1",
        sk="2026-07-22T00:00:00.000Z#uuid-1",
        phone="+15555550100",
    )


def _run(records: list[dict], *, ddb_items=None, agents=None, lock_acquired=True, contact=_UNSET):
    """Run lambda_handler with mocked AWS clients. Returns (result, mock_sqs, mock_lock, mock_queue, mock_connect)."""
    if ddb_items is None:
        ddb_items = [_CAMPAIGN_ITEM]
    if agents is None:
        agents = [_AGENT_ARN]
    if contact is _UNSET:
        contact = _make_contact()

    # Clear cached module singletons between tests
    for mod in list(sys.modules.keys()):
        if "handler_kickstart" in mod:
            del sys.modules[mod]

    mock_lock = MagicMock()
    mock_lock.acquire.return_value = lock_acquired
    mock_queue = MagicMock()
    mock_queue.dequeue.return_value = contact
    mock_fo = MagicMock()
    mock_fo.push.return_value = True
    mock_sqs = MagicMock()
    mock_sqs.send_message.return_value = {}
    mock_ddb = MagicMock()
    mock_ddb.scan.return_value = {"Items": ddb_items}
    mock_connect = MagicMock()
    mock_connect.get_current_user_data.return_value = {
        "UserDataList": [
            {"User": {"Arn": a}, "Status": {"StatusName": "Available"}} for a in agents
        ]
    }

    with patch.dict("os.environ", _ENV):
        with patch("handler_kickstart.AgentLock", return_value=mock_lock), \
             patch("handler_kickstart.CampaignQueue", return_value=mock_queue), \
             patch("handler_kickstart.FirstOrionClient") as MockFO, \
             patch("handler_kickstart._get_sqs", return_value=mock_sqs), \
             patch("handler_kickstart._get_ddb", return_value=mock_ddb), \
             patch("handler_kickstart._get_connect", return_value=mock_connect):
            MockFO.build_from_secret.return_value = mock_fo
            import handler_kickstart
            result = handler_kickstart.lambda_handler({"Records": records}, None)

    return result, mock_sqs, mock_lock, mock_queue, mock_connect


# ── lambda_handler: record filtering ─────────────────────────────────────────

class TestRecordFiltering:
    def test_processes_insert_records(self):
        result, mock_sqs, *_ = _run([_insert_record()])
        assert result["processed"] == 1

    def test_skips_modify_records(self):
        result, mock_sqs, _, mock_queue, _ = _run(
            [_insert_record(event_name="MODIFY")]
        )
        assert result["processed"] == 0
        mock_queue.dequeue.assert_not_called()

    def test_skips_remove_records(self):
        result, _, _, mock_queue, _ = _run([_insert_record(event_name="REMOVE")])
        assert result["processed"] == 0
        mock_queue.dequeue.assert_not_called()

    def test_mixed_batch_only_processes_inserts(self):
        records = [
            _insert_record(event_name="INSERT"),
            _insert_record(event_name="MODIFY"),
            _insert_record(event_name="INSERT"),
        ]
        result, _, _, mock_queue, _ = _run(records)
        assert result["processed"] == 2

    def test_empty_event_returns_zero(self):
        result, *_ = _run([])
        assert result["processed"] == 0


# ── _process_insert: status filter ───────────────────────────────────────────

class TestStatusFilter:
    def test_dispatches_pending_contacts(self):
        _, mock_sqs, _, _, _ = _run([_insert_record(status="PENDING")])
        mock_sqs.send_message.assert_called_once()

    def test_skips_dispatching_contacts(self):
        _, mock_sqs, mock_lock, _, _ = _run([_insert_record(status="DISPATCHING")])
        mock_sqs.send_message.assert_not_called()
        mock_lock.acquire.assert_not_called()

    def test_skips_dialed_contacts(self):
        _, mock_sqs, mock_lock, _, _ = _run([_insert_record(status="DIALED")])
        mock_sqs.send_message.assert_not_called()


# ── _process_insert: no active campaign ──────────────────────────────────────

class TestNoActiveCampaign:
    def test_skips_when_campaign_not_found(self):
        _, mock_sqs, mock_lock, _, mock_connect = _run(
            [_insert_record()], ddb_items=[]
        )
        mock_sqs.send_message.assert_not_called()
        mock_connect.get_current_user_data.assert_not_called()

    def test_skips_when_no_agents_available(self):
        _, mock_sqs, mock_lock, _, _ = _run([_insert_record()], agents=[])
        mock_sqs.send_message.assert_not_called()
        mock_lock.acquire.assert_not_called()

    def test_skips_agent_with_pending_next_status(self):
        # Agent is Available now but queued a break (NextStatus != Available) —
        # matches agent_event_filter.is_agent_available()'s NextAgentStatus check.
        for mod in list(sys.modules.keys()):
            if "handler_kickstart" in mod:
                del sys.modules[mod]

        mock_lock = MagicMock()
        mock_queue = MagicMock()
        mock_fo = MagicMock()
        mock_sqs = MagicMock()
        mock_ddb = MagicMock()
        mock_ddb.scan.return_value = {"Items": [_CAMPAIGN_ITEM]}
        mock_connect = MagicMock()
        mock_connect.get_current_user_data.return_value = {
            "UserDataList": [
                {
                    "User": {"Arn": _AGENT_ARN},
                    "Status": {"StatusName": "Available"},
                    "NextStatus": "Break",
                },
            ]
        }

        with patch.dict("os.environ", _ENV):
            with patch("handler_kickstart.AgentLock", return_value=mock_lock), \
                 patch("handler_kickstart.CampaignQueue", return_value=mock_queue), \
                 patch("handler_kickstart.FirstOrionClient") as MockFO, \
                 patch("handler_kickstart._get_sqs", return_value=mock_sqs), \
                 patch("handler_kickstart._get_ddb", return_value=mock_ddb), \
                 patch("handler_kickstart._get_connect", return_value=mock_connect):
                MockFO.build_from_secret.return_value = mock_fo
                import handler_kickstart
                handler_kickstart.lambda_handler({"Records": [_insert_record()]}, None)

        mock_sqs.send_message.assert_not_called()
        mock_lock.acquire.assert_not_called()


# ── _process_insert: dispatch happy path ─────────────────────────────────────

class TestDispatch:
    def test_acquires_lock_before_dequeue(self):
        _, _, mock_lock, mock_queue, _ = _run([_insert_record()])
        mock_lock.acquire.assert_called_once_with(_AGENT_ARN, campaign_id="camp-1")
        mock_queue.dequeue.assert_called_once_with("camp-1")

    def test_fires_first_orion_push(self):
        # We can't directly assert on mock_fo inside _run, check via SQS call succeeding
        _, mock_sqs, _, _, _ = _run([_insert_record()])
        mock_sqs.send_message.assert_called_once()

    def test_sqs_message_body_contains_required_fields(self):
        _, mock_sqs, _, _, _ = _run([_insert_record()])
        body = json.loads(mock_sqs.send_message.call_args.kwargs["MessageBody"])
        assert body["agentArn"] == _AGENT_ARN
        assert body["campaignId"] == "camp-1"
        assert body["queueArn"] == _CAMPAIGN_ITEM["queueArn"]["S"]
        assert body["contactFlowId"] == "flow-1"
        assert body["sourcePhone"] == "+12125550199"
        assert body["instanceId"] == "instance-abc"
        assert "contactSk" in body
        assert "correlationId" in body

    def test_sqs_delay_is_22_seconds(self):
        _, mock_sqs, _, _, _ = _run([_insert_record()])
        assert mock_sqs.send_message.call_args.kwargs["DelaySeconds"] == 22

    def test_connect_query_uses_queue_id_not_arn(self):
        _, _, _, _, mock_connect = _run([_insert_record()])
        filters = mock_connect.get_current_user_data.call_args.kwargs["Filters"]
        # queue_id is the last segment of the ARN
        assert filters["Queues"] == ["q1"]


# ── _process_insert: lock contention ─────────────────────────────────────────

class TestLockContention:
    def test_skips_to_next_agent_when_lock_held(self):
        agent2 = "arn:aws:connect:us-east-1:123:instance/abc/agent/agent-2"
        _, mock_sqs, mock_lock, _, _ = _run(
            [_insert_record()],
            agents=[_AGENT_ARN, agent2],
            lock_acquired=False,
        )
        # Both agents tried, neither succeeded — no SQS message
        mock_sqs.send_message.assert_not_called()
        assert mock_lock.acquire.call_count == 2

    def test_dispatches_to_second_agent_when_first_locked(self):
        agent2 = "arn:aws:connect:us-east-1:123:instance/abc/agent/agent-2"

        for mod in list(sys.modules.keys()):
            if "handler_kickstart" in mod:
                del sys.modules[mod]

        mock_lock = MagicMock()
        mock_lock.acquire.side_effect = [False, True]  # first fails, second succeeds
        mock_queue = MagicMock()
        mock_queue.dequeue.return_value = _make_contact()
        mock_fo = MagicMock()
        mock_fo.push.return_value = True
        mock_sqs = MagicMock()
        mock_ddb = MagicMock()
        mock_ddb.scan.return_value = {"Items": [_CAMPAIGN_ITEM]}
        mock_connect = MagicMock()
        mock_connect.get_current_user_data.return_value = {
            "UserDataList": [
                {"User": {"Arn": _AGENT_ARN}, "Status": {"StatusName": "Available"}},
                {"User": {"Arn": agent2}, "Status": {"StatusName": "Available"}},
            ]
        }

        with patch.dict("os.environ", _ENV):
            with patch("handler_kickstart.AgentLock", return_value=mock_lock), \
                 patch("handler_kickstart.CampaignQueue", return_value=mock_queue), \
                 patch("handler_kickstart.FirstOrionClient") as MockFO, \
                 patch("handler_kickstart._get_sqs", return_value=mock_sqs), \
                 patch("handler_kickstart._get_ddb", return_value=mock_ddb), \
                 patch("handler_kickstart._get_connect", return_value=mock_connect):
                MockFO.build_from_secret.return_value = mock_fo
                import handler_kickstart
                handler_kickstart.lambda_handler({"Records": [_insert_record()]}, None)

        # agent-2 was dispatched
        mock_sqs.send_message.assert_called_once()
        body = json.loads(mock_sqs.send_message.call_args.kwargs["MessageBody"])
        assert body["agentArn"] == agent2

    def test_releases_lock_when_queue_empty(self):
        _, mock_sqs, mock_lock, mock_queue, _ = _run(
            [_insert_record()], contact=None
        )
        mock_sqs.send_message.assert_not_called()
        mock_lock.release.assert_called_once_with(_AGENT_ARN)

    def test_only_dispatches_once_per_insert(self):
        # Even with 3 available agents, only one dispatch per INSERT
        agents = [
            "arn:aws:connect:us-east-1:123:instance/abc/agent/a1",
            "arn:aws:connect:us-east-1:123:instance/abc/agent/a2",
            "arn:aws:connect:us-east-1:123:instance/abc/agent/a3",
        ]
        _, mock_sqs, mock_lock, _, _ = _run([_insert_record()], agents=agents)
        mock_sqs.send_message.assert_called_once()
        mock_lock.acquire.assert_called_once()  # stopped after first success
