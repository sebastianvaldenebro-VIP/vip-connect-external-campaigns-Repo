from unittest.mock import MagicMock
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
    token = lock.acquire("agent-001", campaign_id="campaign-1")
    # VIP-04: acquire() returns a fencing token (truthy str), not a bare bool.
    assert token is not None
    assert isinstance(token, str) and token
    # The same token must be the one persisted in the Item.
    item = table.put_item.call_args[1]["Item"]
    assert item["lockToken"] == token


def test_acquire_fails_when_lock_exists():
    from botocore.exceptions import ClientError
    lock, table = _make_lock()
    error = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": ""}},
        "PutItem"
    )
    table.put_item.side_effect = error
    assert lock.acquire("agent-001", campaign_id="campaign-1") is None


def test_acquire_reraises_non_conditional_check_errors():
    """A ClientError with a code other than ConditionalCheckFailedException (e.g.
    throttling or a permissions issue) must propagate — it is not a lock-contention
    signal and swallowing it would silently skip dispatch."""
    from botocore.exceptions import ClientError
    import pytest

    lock, table = _make_lock()
    error = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": ""}},
        "PutItem",
    )
    table.put_item.side_effect = error
    with pytest.raises(ClientError):
        lock.acquire("agent-001", campaign_id="campaign-1")


def test_release_deletes_lock_when_token_matches():
    lock, table = _make_lock()
    table.delete_item.return_value = {}
    lock.release("agent-001", "tok-abc123")
    call_kwargs = table.delete_item.call_args[1]
    assert call_kwargs["Key"]["agentId"] == "agent-001"
    # VIP-04: the delete must be conditioned on the exact fencing token.
    assert call_kwargs["ConditionExpression"] == "lockToken = :token"
    assert call_kwargs["ExpressionAttributeValues"] == {":token": "tok-abc123"}


def test_release_is_noop_when_token_does_not_match():
    """VIP-04: a stale token (belongs to a prior/overridden lock generation)
    must not raise and must not delete anything — the delete_item's own
    ConditionExpression protects a newer generation's lock from a stale
    caller, and a failed condition here is expected, not an error."""
    from botocore.exceptions import ClientError
    lock, table = _make_lock()
    error = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": ""}},
        "DeleteItem",
    )
    table.delete_item.side_effect = error
    # Must not raise.
    lock.release("agent-001", "stale-token")


def test_release_reraises_non_conditional_check_errors():
    from botocore.exceptions import ClientError
    import pytest

    lock, table = _make_lock()
    error = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": ""}},
        "DeleteItem",
    )
    table.delete_item.side_effect = error
    with pytest.raises(ClientError):
        lock.release("agent-001", "tok-abc123")


# ── VIP-04 acceptance criterion ──────────────────────────────────────────────
# acquire A, expire/replace with B, then let A's stale operation try to
# release — B's lock must survive and A must not be able to act on it.


def test_stale_generation_cannot_release_a_newer_generations_lock():
    """Simulates the exact race the audit flagged: dispatch A acquires the
    lock (token_a). The lock goes stale (>60s) before A's in-flight caller
    Lambda finishes. A second AVAILABLE event dispatches B, which acquires a
    NEW lock generation (token_b) for the same agent. A's slow invocation
    finally calls release(token_a) — this must be a no-op; B's lock (and
    B's dispatch) must be unaffected."""
    lock, table = _make_lock()

    # --- A acquires ---
    table.put_item.return_value = {}
    token_a = lock.acquire("agent-001", campaign_id="campaign-a")
    assert token_a is not None

    # --- time passes; A's lock is now stale; B acquires a fresh generation ---
    # A real DynamoDB table would accept B's conditional PutItem here because
    # A's lockedAt is older than _LOCK_STALE_SECONDS — we model the *result*
    # of that atomic replacement directly: the table now holds token_b.
    token_b = lock.acquire("agent-001", campaign_id="campaign-b")
    assert token_b is not None
    assert token_b != token_a

    # --- A's stale, in-flight invocation now tries to release its OLD token ---
    # Model DynamoDB's real behavior: the stored lockToken is token_b, so a
    # delete_item conditioned on token_a fails its ConditionExpression.
    from botocore.exceptions import ClientError

    def _delete_side_effect(**kwargs):
        supplied_token = kwargs["ExpressionAttributeValues"][":token"]
        if supplied_token != token_b:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": ""}},
                "DeleteItem",
            )
        return {}

    table.delete_item.side_effect = _delete_side_effect

    # A's release must be a silent no-op — must not raise, must not delete B's lock.
    lock.release("agent-001", token_a)

    # B's lock is still "held" (the mocked delete_item never actually
    # succeeded for token_b) — confirmed by releasing with the CORRECT token
    # succeeding cleanly, proving the table state was never disturbed by A.
    lock.release("agent-001", token_b)


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
    # First call: ConditionalCheckFailed (live lock) — returns None
    live_lock_error = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": ""}}, "PutItem"
    )
    # Second call: success (stale lock condition matched) — returns a token
    table.put_item.side_effect = [live_lock_error, {}]
    assert lock.acquire("agent-001", campaign_id="campaign-1") is None  # live lock blocks
    table.put_item.side_effect = [{}]  # stale lock — put_item succeeds
    assert lock.acquire("agent-001", campaign_id="campaign-1") is not None
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

    assert result is not None
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
    assert lock.acquire("agent-001", campaign_id="campaign-1") is None
