"""Per-agent dispatch lock backed by DynamoDB.

Table: VipProgressiveAgentLocks
  PK: agentId (S)
  Attributes:
    campaignId (S)  — which campaign this dispatch is for
    lockedAt (N)    — epoch seconds
    ttl (N)         — epoch seconds, 600s from lock acquisition (auto-release)

Acquire uses an atomic conditional PutItem:
  attribute_not_exists(agentId) OR #ttl < :now

This prevents the release-before-acquire race: two concurrent consumer invocations
for the same agent both try to PutItem atomically. Exactly one wins (the other sees
ConditionalCheckFailed because the winner's item now exists and its TTL is in the future).
Stale locks whose TTL has expired but whose item was not yet swept by DynamoDB TTL are
also replaced atomically — no blind delete+insert required.

release() is still called by: (a) the consumer when the campaign queue is empty and
(b) the caller on dial failure, to unblock the agent for the next AVAILABLE event.
"""
from __future__ import annotations

import time

import boto3
from botocore.exceptions import ClientError


_LOCK_TTL_SECONDS = 600  # 10 min — covers longest expected call


class AgentLock:
    def __init__(self, table_name: str, dynamodb_resource=None) -> None:
        self._table = (dynamodb_resource or boto3.resource("dynamodb")).Table(table_name)

    def acquire(self, agent_id: str, *, campaign_id: str) -> bool:
        """Attempt to acquire the lock. Returns True on success, False if already locked.

        Succeeds when: no lock exists OR existing lock's TTL is in the past (stale).
        This single atomic write prevents the double-dispatch race that would occur if
        release() were called first and two concurrent invocations both saw the lock absent.
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
                ConditionExpression="attribute_not_exists(agentId) OR #ttl < :now",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={":now": now},
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def release(self, agent_id: str) -> None:
        """Release the lock unconditionally."""
        self._table.delete_item(Key={"agentId": agent_id})
