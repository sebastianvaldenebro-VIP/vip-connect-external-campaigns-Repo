"""Unit tests for handler_kickstart.py.

Tests are structured around the two entry points:
  - lambda_handler: filters INSERT-only stream records, delegates to _process_insert
  - _process_insert: looks up campaign, finds available agents, dispatches

All AWS clients are mocked — no real AWS calls.
"""
import json
import sys
from unittest.mock import MagicMock, patch

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


def _run(
    records: list[dict],
    *,
    ddb_items=None,
    agents=None,
    lock_acquired=True,
    contact=_UNSET,
    dequeue_side_effect=None,
):
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
    if dequeue_side_effect is not None:
        mock_queue.dequeue.side_effect = dequeue_side_effect
    else:
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


def _sweep_event() -> dict:
    return {
        "version": "0",
        "id": "evt-1",
        "detail-type": "Scheduled Event",
        "source": "aws.events",
        "account": "123",
        "time": "2026-08-27T13:00:00Z",
        "region": "us-east-1",
        "resources": [
            "arn:aws:events:us-east-1:123:rule/vip-progressive-dialer-kickstart-sweep"
        ],
        "detail": {},
    }


def _one_contact_per_campaign_then_empty():
    """Default dequeue() behavior: exactly one contact per distinct campaignId,
    then None — models a small, realistic per-campaign backlog instead of an
    infinite queue (which would spin the sweep's drain loop forever)."""
    seen: dict[str, bool] = {}

    def _dequeue(campaign_id):
        if seen.get(campaign_id):
            return None
        seen[campaign_id] = True
        return _make_contact()

    return _dequeue


def _run_sweep(
    *,
    ddb_items=None,
    agents=None,
    lock_acquired=True,
    dequeue_side_effect=None,
    connect_side_effect=None,
    scan_side_effect=None,
):
    """Run lambda_handler with an EventBridge scheduled-event shape.

    Returns (result, mock_sqs, mock_lock, mock_queue, mock_connect, mock_ddb, mock_cw).
    """
    if ddb_items is None:
        ddb_items = [_CAMPAIGN_ITEM]
    if agents is None:
        agents = [_AGENT_ARN]

    for mod in list(sys.modules.keys()):
        if "handler_kickstart" in mod:
            del sys.modules[mod]

    mock_lock = MagicMock()
    mock_lock.acquire.return_value = lock_acquired
    mock_queue = MagicMock()
    if dequeue_side_effect is not None:
        mock_queue.dequeue.side_effect = dequeue_side_effect
    else:
        mock_queue.dequeue.side_effect = _one_contact_per_campaign_then_empty()
    mock_fo = MagicMock()
    mock_fo.push.return_value = True
    mock_sqs = MagicMock()
    mock_sqs.send_message.return_value = {}
    mock_ddb = MagicMock()
    if scan_side_effect is not None:
        mock_ddb.scan.side_effect = scan_side_effect
    else:
        mock_ddb.scan.return_value = {"Items": ddb_items}
    mock_cw = MagicMock()
    mock_connect = MagicMock()
    if connect_side_effect is not None:
        mock_connect.get_current_user_data.side_effect = connect_side_effect
    else:
        mock_connect.get_current_user_data.return_value = {
            "UserDataList": [
                {"User": {"Arn": a}, "Status": {"StatusName": "Available"}}
                for a in agents
            ]
        }

    with patch.dict("os.environ", _ENV):
        with (
            patch("handler_kickstart.AgentLock", return_value=mock_lock),
            patch("handler_kickstart.CampaignQueue", return_value=mock_queue),
            patch("handler_kickstart.FirstOrionClient") as MockFO,
            patch("handler_kickstart._get_sqs", return_value=mock_sqs),
            patch("handler_kickstart._get_ddb", return_value=mock_ddb),
            patch("handler_kickstart._get_connect", return_value=mock_connect),
            patch("handler_kickstart._get_cw", return_value=mock_cw),
        ):
            MockFO.build_from_secret.return_value = mock_fo
            import handler_kickstart

            result = handler_kickstart.lambda_handler(_sweep_event(), None)

    return result, mock_sqs, mock_lock, mock_queue, mock_connect, mock_ddb, mock_cw


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

    # Bug: an exception raised AFTER the agent lock was acquired (dequeue/push/
    # send_message all lacked their own try/except) leaked the lock — the sweep's
    # new per-campaign fault isolation (this round's fix) now catches and logs
    # such an exception instead of letting it crash the whole invocation, which
    # means the leaked lock is no longer even visible as a loud failure. The
    # agent stays unusable for _LOCK_TTL_SECONDS with no recovery (root-caused
    # 2026-08-27, second adversarial review round).

    def test_releases_lock_when_dequeue_raises_after_acquire(self):
        with pytest.raises(RuntimeError):
            _run(
                [_insert_record()],
                dequeue_side_effect=RuntimeError("DynamoDB throttled"),
            )

    def test_lock_released_before_exception_propagates_on_dequeue_failure(self):
        for mod in list(sys.modules.keys()):
            if "handler_kickstart" in mod:
                del sys.modules[mod]

        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_queue = MagicMock()
        mock_queue.dequeue.side_effect = RuntimeError("DynamoDB throttled")
        mock_ddb = MagicMock()
        mock_ddb.scan.return_value = {"Items": [_CAMPAIGN_ITEM]}
        mock_connect = MagicMock()
        mock_connect.get_current_user_data.return_value = {
            "UserDataList": [
                {"User": {"Arn": _AGENT_ARN}, "Status": {"StatusName": "Available"}}
            ]
        }

        with patch.dict("os.environ", _ENV):
            with (
                patch("handler_kickstart.AgentLock", return_value=mock_lock),
                patch("handler_kickstart.CampaignQueue", return_value=mock_queue),
                patch("handler_kickstart.FirstOrionClient"),
                patch("handler_kickstart._get_ddb", return_value=mock_ddb),
                patch("handler_kickstart._get_connect", return_value=mock_connect),
            ):
                import handler_kickstart

                with pytest.raises(RuntimeError):
                    handler_kickstart.lambda_handler(
                        {"Records": [_insert_record()]}, None
                    )

        mock_lock.release.assert_called_once_with(_AGENT_ARN)


# ── lambda_handler: EventBridge sweep (timer backstop) ────────────────────────


class TestSweep:
    def test_eventbridge_event_routes_to_sweep_not_records_path(self):
        result, *_ = _run_sweep()
        assert "sweepDispatched" in result
        assert "processed" not in result

    def test_dispatches_to_active_campaign_with_available_agent(self):
        result, mock_sqs, *_ = _run_sweep(ddb_items=[_CAMPAIGN_ITEM])
        mock_sqs.send_message.assert_called_once()
        assert result["sweepDispatched"] == 1

    def test_skips_campaign_with_no_available_agents(self):
        result, mock_sqs, *_ = _run_sweep(ddb_items=[_CAMPAIGN_ITEM], agents=[])
        mock_sqs.send_message.assert_not_called()
        assert result["sweepDispatched"] == 0

    def test_sweeps_multiple_active_campaigns_independently(self):
        camp2 = dict(_CAMPAIGN_ITEM, campaignId={"S": "camp-2"})
        result, mock_sqs, *_ = _run_sweep(ddb_items=[_CAMPAIGN_ITEM, camp2])
        assert mock_sqs.send_message.call_count == 2
        assert result["sweepDispatched"] == 2

    def test_drains_multiple_pending_contacts_for_same_campaign_in_one_sweep(self):
        # Two contacts available for the one active campaign — unlike the
        # single stream-INSERT kickstart (one dispatch per INSERT), the sweep
        # keeps dispatching until no agent/contact pair remains.
        contact_a = _make_contact()
        result, mock_sqs, *_ = _run_sweep(
            ddb_items=[_CAMPAIGN_ITEM],
            dequeue_side_effect=[contact_a, contact_a, None, None],
        )
        assert mock_sqs.send_message.call_count == 2
        assert result["sweepDispatched"] == 2

    def test_interleaves_dispatches_across_campaigns_instead_of_draining_one_first(
        self,
    ):
        """Two campaigns sharing a queue, each with multiple pending contacts — the
        sweep must alternate between them per round instead of fully draining
        campaign 1 (up to its cap) before ever attempting campaign 2. Same
        starvation shape as the FIFO-by-createdAt bug in handler_consumer.py,
        but inside a single sweep tick's drain loop (root-caused 2026-08-27)."""
        camp1 = dict(_CAMPAIGN_ITEM, campaignId={"S": "camp-1"})
        camp2 = dict(_CAMPAIGN_ITEM, campaignId={"S": "camp-2"})
        counts = {"camp-1": 0, "camp-2": 0}

        def _dequeue(campaign_id):
            if counts[campaign_id] >= 3:
                return None
            counts[campaign_id] += 1
            return _make_contact()

        result, mock_sqs, *_ = _run_sweep(
            ddb_items=[camp1, camp2], dequeue_side_effect=_dequeue
        )

        dispatched_order = [
            json.loads(c.kwargs["MessageBody"])["campaignId"]
            for c in mock_sqs.send_message.call_args_list
        ]
        assert dispatched_order == ["camp-1", "camp-2"] * 3
        assert result["sweepDispatched"] == 6

    def test_scans_active_campaigns_table_without_filter(self):
        # Distinguishes the sweep's full-table scan from _get_campaign_config's
        # single-campaign FilterExpression scan used on the stream-INSERT path.
        _, _, _, _, _, mock_ddb, _ = _run_sweep(ddb_items=[_CAMPAIGN_ITEM])
        mock_ddb.scan.assert_called_once()
        assert "FilterExpression" not in mock_ddb.scan.call_args.kwargs
        assert (
            mock_ddb.scan.call_args.kwargs["TableName"] == "VipActiveBrandedCampaigns"
        )

    # Bug: one campaign's dispatch failure aborted the sweep for every other
    # active campaign in the same tick (root-caused 2026-08-27, adversarial
    # code review) — a single ClientError/KeyError anywhere in the round-robin
    # loop propagated out of _process_sweep, so a throttled or malformed
    # campaign silently starved every OTHER campaign sharing that sweep tick.

    def test_isolates_campaign_dispatch_failure_and_continues_with_others(self):
        """One campaign's dispatch raising must not abort the sweep for other
        active campaigns in the same tick, and must emit a metric instead of
        failing silently."""
        camp_failing = dict(
            _CAMPAIGN_ITEM,
            campaignId={"S": "camp-fail"},
            queueArn={"S": "arn:aws:connect:us-east-1:123:instance/abc/queue/q-fail"},
        )
        camp_ok = dict(
            _CAMPAIGN_ITEM,
            campaignId={"S": "camp-ok"},
            queueArn={"S": "arn:aws:connect:us-east-1:123:instance/abc/queue/q-ok"},
        )

        def _connect_side_effect(**kwargs):
            queue_id = kwargs["Filters"]["Queues"][0]
            if queue_id == "q-fail":
                raise RuntimeError("Connect throttled")
            return {
                "UserDataList": [
                    {"User": {"Arn": _AGENT_ARN}, "Status": {"StatusName": "Available"}}
                ]
            }

        result, mock_sqs, _, _, _, _, mock_cw = _run_sweep(
            ddb_items=[camp_failing, camp_ok],
            connect_side_effect=_connect_side_effect,
        )

        dispatched_campaigns = [
            json.loads(c.kwargs["MessageBody"])["campaignId"]
            for c in mock_sqs.send_message.call_args_list
        ]
        assert dispatched_campaigns == ["camp-ok"]
        assert result["sweepDispatched"] == 1
        mock_cw.put_metric_data.assert_called_once()
        metric_names = {
            m["MetricName"]
            for m in mock_cw.put_metric_data.call_args.kwargs["MetricData"]
        }
        assert "SweepCampaignDispatchFailed" in metric_names

    def test_sweep_scan_failure_does_not_crash_lambda_handler(self):
        """A failure scanning VipActiveBrandedCampaigns itself (before any
        per-campaign loop) must not raise out of lambda_handler — the next
        scheduled tick will simply retry."""
        result, _, _, _, _, _, mock_cw = _run_sweep(
            scan_side_effect=RuntimeError("DynamoDB unavailable")
        )
        assert result["sweepDispatched"] == 0
        assert result.get("sweepError") is True
        metric_names = {
            m["MetricName"]
            for m in mock_cw.put_metric_data.call_args.kwargs["MetricData"]
        }
        assert "SweepTickFailed" in metric_names
