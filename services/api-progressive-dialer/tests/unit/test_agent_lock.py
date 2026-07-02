from unittest.mock import MagicMock
import pytest
from agent_lock import AgentLock

TABLE_NAME = "VipProgressiveAgentLocks"


def _make_lock():
    mock_table = MagicMock()
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table
    lock = AgentLock(TABLE_NAME, dynamodb_resource=mock_resource)
    lock._table = mock_table
    return lock, mock_table


def test_acquire_succeeds_when_no_existing_lock():
    lock, table = _make_lock()
    table.put_item.return_value = {}
    assert lock.acquire("agent-001", campaign_id="campaign-1") is True


def test_acquire_fails_when_lock_exists():
    from botocore.exceptions import ClientError
    lock, table = _make_lock()
    error = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": ""}},
        "PutItem"
    )
    table.put_item.side_effect = error
    assert lock.acquire("agent-001", campaign_id="campaign-1") is False


def test_release_deletes_lock():
    lock, table = _make_lock()
    lock.release("agent-001")
    call_kwargs = table.delete_item.call_args[1]
    assert call_kwargs["Key"]["agentId"] == "agent-001"


def test_acquire_writes_correct_ttl():
    import time
    lock, table = _make_lock()
    table.put_item.return_value = {}
    lock.acquire("agent-001", campaign_id="campaign-1")
    item = table.put_item.call_args[1]["Item"]
    assert item["agentId"] == "agent-001"
    assert item["campaignId"] == "campaign-1"
    # TTL should be ~600s from now
    assert abs(item["ttl"] - (int(time.time()) + 600)) < 5


def test_acquire_succeeds_when_lock_is_stale():
    """Stale lock (TTL expired but DynamoDB TTL sweep not yet run) must be atomically replaced."""
    from botocore.exceptions import ClientError
    lock, table = _make_lock()
    # First call: ConditionalCheckFailed (live lock) — returns False
    live_lock_error = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": ""}}, "PutItem"
    )
    # Second call: success (stale lock condition matched) — returns True
    table.put_item.side_effect = [live_lock_error, {}]
    assert lock.acquire("agent-001", campaign_id="campaign-1") is False  # live lock blocks
    table.put_item.side_effect = [{}]  # stale lock — put_item succeeds
    assert lock.acquire("agent-001", campaign_id="campaign-1") is True
    # Verify the three-clause condition expression is actually sent to DynamoDB
    call_kwargs = table.put_item.call_args[1]
    assert call_kwargs["ConditionExpression"] == (
        "attribute_not_exists(agentId) OR #ttl < :now OR lockedAt < :stale_threshold"
    )
    assert call_kwargs["ExpressionAttributeNames"] == {"#ttl": "ttl"}
    assert ":now" in call_kwargs["ExpressionAttributeValues"]
    assert ":stale_threshold" in call_kwargs["ExpressionAttributeValues"]


def test_acquire_stale_threshold_value_is_60s_in_past():
    """stale_threshold must equal now - 60 so locks older than 60s can be overwritten."""
    import time
    lock, table = _make_lock()
    table.put_item.return_value = {}
    lock.acquire("agent-001", campaign_id="campaign-1")
    values = table.put_item.call_args[1]["ExpressionAttributeValues"]
    now = int(time.time())
    assert abs(values[":stale_threshold"] - (now - 60)) < 5


def test_acquire_overrides_lock_older_than_60s():
    """An AVAILABLE event after a completed call (lock > 60s old) must be dispatchable.

    DynamoDB evaluates the stale-threshold condition atomically, so the second-to-arrive
    concurrent request still gets ConditionalCheckFailed (it sees the freshly-written lock).
    """
    import time
    from unittest.mock import patch
    lock, table = _make_lock()
    table.put_item.return_value = {}

    # Simulate time 61s after lock was originally set
    with patch("agent_lock.time") as mock_time:
        mock_time.time.return_value = int(time.time()) + 61
        result = lock.acquire("agent-001", campaign_id="campaign-2")

    assert result is True
    values = table.put_item.call_args[1]["ExpressionAttributeValues"]
    # stale_threshold should be 61 - 60 = 1s in the past relative to the mocked now
    expected_threshold = int(time.time()) + 61 - 60
    assert abs(values[":stale_threshold"] - expected_threshold) < 5


def test_acquire_blocked_within_stale_window():
    """A lock acquired only 30s ago (call setup still in progress) must NOT be overridden."""
    from botocore.exceptions import ClientError
    lock, table = _make_lock()
    # DynamoDB returns ConditionalCheckFailed — stale_threshold hasn't been reached yet
    error = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": ""}}, "PutItem"
    )
    table.put_item.side_effect = error
    assert lock.acquire("agent-001", campaign_id="campaign-1") is False
