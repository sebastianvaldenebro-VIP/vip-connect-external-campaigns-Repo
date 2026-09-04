import base64
import json
from datetime import datetime, timezone
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
             patch("handler_consumer.CampaignQueue"), \
             patch("handler_consumer.FirstOrionClient"):
            from handler_consumer import lambda_handler
            lambda_handler(event, None)
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
            lambda_handler(event, None)

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

        import handler_consumer
        import base64
        import json
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

        import handler_consumer
        import base64
        import json
        record = {"kinesis": {"data": base64.b64encode(json.dumps({}).encode()).decode()}}
        handler_consumer._process_record(record)

        calls = [c.args[0] for c in queue.dequeue.call_args_list]
        assert calls == ["camp-high", "camp-low"]  # priority 0 first

    # Bug: same-priority campaigns on a shared queue were pure FIFO-by-createdAt
    # (root-caused 2026-08-27) — in production every campaign has priority=0, so
    # the oldest campaign always won every single dispatch event for as long as
    # it had any pending contact, fully starving newer campaigns on that queue.

    def test_prefers_least_recently_dispatched_campaign_at_same_priority(self, mocker):
        """Among same-priority campaigns, the one that waited longest since its last
        successful dispatch goes first — not simply the oldest by createdAt."""
        recently_served = self._make_campaign_item(
            "camp-recent", priority=0, created_at="2026-06-18T09:00:00"
        )
        recently_served["lastDispatchedAt"] = {"S": "2026-08-27T15:59:00+00:00"}
        never_served = self._make_campaign_item(
            "camp-waiting", priority=0, created_at="2026-06-18T10:00:00"
        )
        # camp-waiting is newer by createdAt but has never been dispatched to —
        # old FIFO-by-createdAt logic would try camp-recent first; fairness must not.
        mocker.patch("handler_consumer._get_ddb").return_value.query.return_value = {
            "Items": [recently_served, never_served]
        }
        mocker.patch("handler_consumer.is_agent_available", return_value=True)
        mocker.patch(
            "handler_consumer.extract_agent_info",
            return_value={
                "agent_arn": "arn::agent/a1",
                "queue_arn": "arn::queue/q1",
            },
        )
        mocker.patch("handler_consumer.is_queue_allowed", return_value=True)
        lock = mocker.patch("handler_consumer._get_lock").return_value
        lock.acquire.return_value = True
        queue = mocker.patch("handler_consumer._get_queue").return_value
        queue.dequeue.return_value = None  # both empty — just verify order

        import handler_consumer
        import base64
        import json

        record = {
            "kinesis": {"data": base64.b64encode(json.dumps({}).encode()).decode()}
        }
        handler_consumer._process_record(record)

        calls = [c.args[0] for c in queue.dequeue.call_args_list]
        assert calls == ["camp-waiting", "camp-recent"]

    def test_records_last_dispatched_at_on_the_winning_campaign(self, mocker):
        """After a successful dispatch, the winning campaign's active-campaign
        record must be updated with lastDispatchedAt so the next event on this
        queue rotates to a different campaign instead of re-picking this one."""
        from campaign_queue import Contact

        campaign_item = self._make_campaign_item("camp-1")
        ddb = mocker.patch("handler_consumer._get_ddb").return_value
        ddb.query.return_value = {"Items": [campaign_item]}
        mocker.patch("handler_consumer.is_agent_available", return_value=True)
        mocker.patch(
            "handler_consumer.extract_agent_info",
            return_value={
                "agent_arn": "arn::agent/a1",
                "queue_arn": "arn::queue/q1",
            },
        )
        mocker.patch("handler_consumer.is_queue_allowed", return_value=True)
        lock = mocker.patch("handler_consumer._get_lock").return_value
        lock.acquire.return_value = True
        queue = mocker.patch("handler_consumer._get_queue").return_value
        queue.dequeue.return_value = Contact(
            campaign_id="camp-1",
            contact_uuid="uuid-1",
            sk="ts1#uuid-1",
            phone="+15551234567",
        )
        mock_fo = mocker.MagicMock()
        mock_fo.push.return_value = True
        mocker.patch(
            "handler_consumer.FirstOrionClient"
        ).build_from_secret.return_value = mock_fo
        mocker.patch("handler_consumer.boto3.client", return_value=mocker.MagicMock())

        import handler_consumer
        import base64
        import json

        record = {
            "kinesis": {"data": base64.b64encode(json.dumps({}).encode()).decode()}
        }
        handler_consumer._process_record(record)

        ddb.update_item.assert_called_once()
        kwargs = ddb.update_item.call_args.kwargs
        assert kwargs["Key"] == {
            "pk": {"S": "QUEUE#arn::queue/q1"},
            "sk": {"S": "CAMPAIGN#camp-1"},
        }
        assert "lastDispatchedAt" in kwargs["UpdateExpression"]

    # Bug: _get_active_campaigns queries a GSI (always eventually-consistent in
    # DynamoDB) immediately after _record_dispatch wrote lastDispatchedAt to the
    # base table — within the SAME Lambda invocation processing several Kinesis
    # records sequentially, a later record's query can still see the pre-write
    # (stale) value and re-pick the campaign that just won, reproducing the
    # exact FIFO starvation BD-018 fixed (root-caused 2026-08-27, adversarial
    # code review).

    def test_get_active_campaigns_uses_locally_recorded_dispatch_despite_gsi_lag(
        self, mocker
    ):
        """After _record_dispatch() writes locally, a later _get_active_campaigns()
        call in the SAME warm invocation must treat that campaign as just-served
        even if the GSI read hasn't caught up yet."""
        stale_camp1 = self._make_campaign_item(
            "camp-1", created_at="2026-06-18T09:00:00"
        )
        camp2 = self._make_campaign_item("camp-2", created_at="2026-06-18T09:30:00")
        camp2["lastDispatchedAt"] = {"S": "2026-08-27T15:00:00+00:00"}
        mocker.patch("handler_consumer._get_ddb").return_value.query.return_value = {
            "Items": [stale_camp1, camp2]
        }

        import handler_consumer

        # Fixed, controlled clock — round-2 fix: the test previously relied on
        # the real wall clock being later than camp2's hardcoded lastDispatchedAt,
        # a time-bomb that only passed because this sandbox's date happens to be
        # 2026-08-27 (root-caused 2026-08-27, second adversarial review round).
        fixed_now = datetime(2026, 8, 27, 15, 5, tzinfo=timezone.utc)
        mocker.patch("handler_consumer.datetime").now.return_value = fixed_now

        handler_consumer._record_dispatch("arn::queue/q1", "camp-1")

        result = handler_consumer._get_active_campaigns("arn::queue/q1")
        assert [c["campaignId"]["S"] for c in result] == ["camp-2", "camp-1"]

    # Bug: _recent_dispatches is keyed only by campaign_id, with no TTL/eviction
    # and no awareness of the record's own generation. brandedCampaignId is
    # deterministic per (planId, runId, bucket_index, campaign_index) and is
    # REUSED verbatim across a stop/force-restart within the same run — a warm
    # container's stale pre-stop timestamp could otherwise be resurrected via
    # max(gsi_value, local_value) against the restarted campaign's fresh
    # DynamoDB item, inverting the fairness ordering BD-018 built (root-caused
    # 2026-08-27, second adversarial review round).

    def test_local_cache_ignored_when_older_than_campaigns_own_created_at(self, mocker):
        """A local dispatch timestamp from BEFORE a stop/restart must not be
        trusted for the restarted (same campaignId, fresh createdAt) record —
        it belongs to a prior generation of that campaign. A stale value that
        old would otherwise make the restarted campaign look like it has been
        waiting even longer than it really has, skewing the ordering against
        a genuinely-longer-waiting sibling."""
        restarted_camp = self._make_campaign_item(
            "camp-1", created_at="2026-08-27T16:00:00+00:00"
        )
        never_served_camp = self._make_campaign_item(
            "camp-2", created_at="2026-08-27T15:00:00+00:00"
        )
        mocker.patch("handler_consumer._get_ddb").return_value.query.return_value = {
            "Items": [restarted_camp, never_served_camp]
        }

        import handler_consumer

        # Stale — from the prior generation, well before camp-1's restart.
        handler_consumer._recent_dispatches["camp-1"] = "2026-08-27T14:00:00+00:00"

        result = handler_consumer._get_active_campaigns("arn::queue/q1")

        # camp-2 has been waiting since 15:00 (never served) — genuinely
        # longer than camp-1's true wait since its 16:00 restart — so camp-2
        # must go first. The stale 14:00 cache entry must not let camp-1
        # jump ahead of it.
        assert [c["campaignId"]["S"] for c in result] == ["camp-2", "camp-1"]

    def test_record_dispatch_emits_metric_when_update_fails(self, mocker):
        """A failed lastDispatchedAt write must be visible via a metric, not
        just a warning log — otherwise a lost IAM permission silently degrades
        the fairness fix back to pure FIFO with no operational signal."""
        mock_ddb = mocker.patch("handler_consumer._get_ddb").return_value
        mock_ddb.update_item.side_effect = RuntimeError("AccessDeniedException")
        mock_cw = mocker.patch("handler_consumer._get_cw").return_value

        import handler_consumer

        handler_consumer._record_dispatch("arn::queue/q1", "camp-1")

        mock_cw.put_metric_data.assert_called_once()
        metric_names = {
            m["MetricName"]
            for m in mock_cw.put_metric_data.call_args.kwargs["MetricData"]
        }
        assert "LastDispatchedAtWriteFailed" in metric_names

    def test_no_active_campaigns_skips_dispatch(self, mocker):
        mocker.patch("handler_consumer._get_ddb").return_value.query.return_value = {"Items": []}
        mocker.patch("handler_consumer.is_agent_available", return_value=True)
        mocker.patch("handler_consumer.extract_agent_info", return_value={
            "agent_arn": "arn::agent/a1", "queue_arn": "arn::queue/q1",
        })
        mocker.patch("handler_consumer.is_queue_allowed", return_value=True)
        lock = mocker.patch("handler_consumer._get_lock").return_value

        import handler_consumer
        import base64
        import json
        record = {"kinesis": {"data": base64.b64encode(json.dumps({}).encode()).decode()}}
        handler_consumer._process_record(record)

        lock.acquire.assert_not_called()

    def test_finds_campaign_via_inbound_queue_when_outbound_has_none(self, mocker):
        """DefaultOutboundQueue has no campaign; InboundQueue does — must match inbound."""
        campaign_item = self._make_campaign_item("camp-inbound")

        def _query(**kwargs):
            q = kwargs["ExpressionAttributeValues"][":q"]["S"]
            return {"Items": [campaign_item] if q == "arn::queue/inbound" else []}

        mocker.patch("handler_consumer._get_ddb").return_value.query.side_effect = _query
        mocker.patch("handler_consumer.is_agent_available", return_value=True)
        mocker.patch("handler_consumer.extract_agent_info", return_value={
            "agent_arn": "arn::agent/a1",
            "queue_arn": "arn::queue/outbound",
            "inbound_queue_arns": ["arn::queue/inbound"],
        })
        mocker.patch("handler_consumer.is_queue_allowed", return_value=True)
        lock = mocker.patch("handler_consumer._get_lock").return_value
        lock.acquire.return_value = True
        queue = mocker.patch("handler_consumer._get_queue").return_value
        queue.dequeue.return_value = None

        import handler_consumer
        import base64
        import json
        record = {"kinesis": {"data": base64.b64encode(json.dumps({}).encode()).decode()}}
        handler_consumer._process_record(record)

        # Lock must have been acquired — campaign was found via inbound queue
        lock.acquire.assert_called_once()

    def test_no_campaigns_on_any_queue_skips_dispatch(self, mocker):
        """Neither DefaultOutbound nor any InboundQueue has active campaigns → skip."""
        mocker.patch("handler_consumer._get_ddb").return_value.query.return_value = {"Items": []}
        mocker.patch("handler_consumer.is_agent_available", return_value=True)
        mocker.patch("handler_consumer.extract_agent_info", return_value={
            "agent_arn": "arn::agent/a1",
            "queue_arn": "arn::queue/outbound",
            "inbound_queue_arns": ["arn::queue/inbound-1", "arn::queue/inbound-2"],
        })
        mocker.patch("handler_consumer.is_queue_allowed", return_value=True)
        lock = mocker.patch("handler_consumer._get_lock").return_value

        import handler_consumer
        import base64
        import json
        record = {"kinesis": {"data": base64.b64encode(json.dumps({}).encode()).decode()}}
        handler_consumer._process_record(record)

        lock.acquire.assert_not_called()

    def test_uses_inbound_queue_arn_in_dispatch_when_matched(self, mocker):
        """When campaign is found via an InboundQueue, that ARN must appear in the SQS message."""
        from campaign_queue import Contact
        campaign_item = self._make_campaign_item("camp-inbound")

        def _query(**kwargs):
            q = kwargs["ExpressionAttributeValues"][":q"]["S"]
            return {"Items": [campaign_item] if q == "arn::queue/inbound" else []}

        mocker.patch("handler_consumer._get_ddb").return_value.query.side_effect = _query
        mocker.patch("handler_consumer.is_agent_available", return_value=True)
        mocker.patch("handler_consumer.extract_agent_info", return_value={
            "agent_arn": "arn::agent/a1",
            "queue_arn": "arn::queue/outbound",
            "inbound_queue_arns": ["arn::queue/inbound"],
        })
        mocker.patch("handler_consumer.is_queue_allowed", return_value=True)

        lock = mocker.patch("handler_consumer._get_lock").return_value
        lock.acquire.return_value = True
        queue = mocker.patch("handler_consumer._get_queue").return_value
        queue.dequeue.return_value = Contact(
            campaign_id="camp-inbound", contact_uuid="uuid-x",
            sk="ts1#uuid-x", phone="+15551234567"
        )
        mock_fo = mocker.MagicMock()
        mock_fo.push.return_value = True
        mocker.patch("handler_consumer.FirstOrionClient").build_from_secret.return_value = mock_fo
        mock_sqs = mocker.MagicMock()
        mock_sqs.send_message.return_value = {}
        mocker.patch("handler_consumer.boto3.client", return_value=mock_sqs)

        import handler_consumer
        import base64
        import json
        record = {"kinesis": {"data": base64.b64encode(json.dumps({}).encode()).decode()}}
        handler_consumer._process_record(record)

        sqs_call = mock_sqs.send_message.call_args
        if sqs_call:
            body = json.loads(sqs_call[1]["MessageBody"])
            assert body["queueArn"] == "arn::queue/inbound"


# ---------------------------------------------------------------------------
# H-8: lock released on exception between acquire and SQS send
# ---------------------------------------------------------------------------

class TestH8LockReleaseOnException:
    """H-8: if an exception occurs after lock.acquire() succeeds, lock.release() is called."""

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("CAMPAIGN_QUEUE_TABLE", "VipProgressiveCampaignQueue")
        monkeypatch.setenv("AGENT_LOCK_TABLE", "VipProgressiveAgentLocks")
        monkeypatch.setenv("SQS_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/queue")
        monkeypatch.setenv("CONNECT_INSTANCE_ID", "instance-1")
        monkeypatch.setenv("ACTIVE_CAMPAIGNS_TABLE", "VipActiveBrandedCampaigns")
        monkeypatch.setenv("FIRSTORION_SECRET_NAME", "vip/firstorion/credentials")
        sys.modules.pop("handler_consumer", None)
        import handler_consumer  # noqa: F401
        yield
        sys.modules.pop("handler_consumer", None)

    def test_lock_released_on_dequeue_exception(self, mocker):
        """If _get_queue().dequeue raises after lock is acquired, lock.release must be called
        and the exception must propagate so Kinesis ESM retries the batch.
        """
        agent_arn = "arn:aws:connect:us-east-1:123:instance/abc/agent/agent-h8"

        mock_ddb = mocker.patch("handler_consumer._get_ddb")
        mock_ddb.return_value.query.return_value = {"Items": [{
            "campaignId": {"S": "campaign-h8"},
            "contactFlowId": {"S": "flow-abc"},
            "sourcePhone": {"S": "+12125550199"},
            "priority": {"N": "0"},
            "createdAt": {"S": "2026-06-18T10:00:00"},
        }]}

        lock = mocker.patch("handler_consumer._get_lock").return_value
        lock.acquire.return_value = True

        queue = mocker.patch("handler_consumer._get_queue").return_value
        queue.dequeue.side_effect = RuntimeError("DynamoDB throttled")

        mocker.patch("handler_consumer.is_agent_available", return_value=True)
        mocker.patch("handler_consumer.extract_agent_info", return_value={
            "agent_arn": agent_arn,
            "queue_arn": "arn:aws:connect:us-east-1:123:instance/abc/queue/q1",
        })
        mocker.patch("handler_consumer.is_queue_allowed", return_value=True)

        import handler_consumer
        import base64
        import json
        record = {"kinesis": {"data": base64.b64encode(json.dumps({}).encode()).decode()}}

        with pytest.raises(RuntimeError, match="DynamoDB throttled"):
            handler_consumer._process_record(record)

        lock.release.assert_called_once_with(agent_arn)
