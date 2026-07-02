"""Per-agent dispatch lock backed by DynamoDB.

Table: VipProgressiveAgentLocks
  PK: agentId (S)
  Attributes:
    campaignId (S)  — which campaign this dispatch is for
    lockedAt (N)    — epoch seconds
    ttl (N)         — epoch seconds, 600s from lock acquisition (auto-release)

Acquire uses an atomic conditional PutItem with three conditions (OR):
  1. attribute_not_exists(agentId)    — no lock; safe to dispatch
  2. #ttl < :now                      — TTL expired but DynamoDB sweep not yet run
  3. lockedAt < :stale_threshold      — lock older than _LOCK_STALE_SECONDS, meaning the
                                        agent returned AVAILABLE after their previous call

Condition 3 is what allows re-dispatch after a call ends without releasing the lock
inside the caller Lambda. The caller does NOT release on success — StartOutboundVoiceContact
is async and the call takes ~14s to bridge after the API returns. Releasing at mark_dialed
(before the flow runs) caused CONTACT_FLOW_DISCONNECT on the first contact when a second
AVAILABLE event arrived in that 14s window. The stale threshold (60s) safely covers the
entire call-setup window (22s SQS delay + ~14s connect) with a comfortable buffer.

Concurrency safety: all three conditions are evaluated atomically by DynamoDB. Two concurrent
AVAILABLE events for the same agent: the first PutItem writes a fresh lock (lockedAt = now).
The second evaluates the condition on the new lock — lockedAt < :stale_threshold is FALSE,
TTL is in the future, item exists — so it gets ConditionalCheckFailed. One dispatch only.

release() is still called by: (a) the consumer when the campaign queue is empty,
(b) the caller on dial failure (permanent errors), and (c) the caller when get_phone
returns None (contact missing from queue).
"""
from __future__ import annotations

import time

import boto3
from botocore.exceptions import ClientError


_LOCK_TTL_SECONDS = 600       # 10 min safety net; stale threshold is the primary re-dispatch gate
_LOCK_STALE_SECONDS = 60      # locks older than this are overrideable on AVAILABLE events


class AgentLock:
    def __init__(self, table_name: str, dynamodb_resource=None) -> None:
        self._table = (dynamodb_resource or boto3.resource("dynamodb")).Table(table_name)

    def acquire(self, agent_id: str, *, campaign_id: str) -> bool:
        """Attempt to acquire the lock. Returns True on success, False if already locked.

        Succeeds when: no lock exists, OR existing lock's TTL is past, OR the lock is
        older than _LOCK_STALE_SECONDS (agent came back Available after a completed call).
        The atomic write prevents double-dispatch even from concurrent invocations.
        """
        now = int(time.time())
        try:
            self._table.put_item(
                Item={
                    "agentId": agent_id,
                    "campaignId": campaign_id,
                    "lockedAt": now,
                    "ttl": now + _LOCK_TTL_SECONDS,
                },
                ConditionExpression=(
                    "attribute_not_exists(agentId) OR "
                    "#ttl < :now OR "
                    "lockedAt < :stale_threshold"
                ),
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":now": now,
                    ":stale_threshold": now - _LOCK_STALE_SECONDS,
                },
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def release(self, agent_id: str) -> None:
        """Release the lock unconditionally."""
        self._table.delete_item(Key={"agentId": agent_id})
