"""Per-agent dispatch lock backed by DynamoDB.

Table: VipProgressiveAgentLocks
  PK: agentId (S)
  Attributes:
    campaignId (S)  — which campaign this dispatch is for
    lockedAt (N)    — epoch seconds
    ttl (N)         — epoch seconds, 600s from lock acquisition (auto-release)
    lockToken (S)   — random fencing token identifying THIS lock generation

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

Fencing (VIP-04, 2026-09-11 audit): _LOCK_STALE_SECONDS (60s) is shorter than the SQS
VisibilityTimeout (180s) the caller Lambda runs under. That gap means a SECOND AVAILABLE
event can legitimately acquire a NEW lock generation for the same agent while an OLDER
dispatch's caller-Lambda invocation — still operating on the OLD generation, e.g. stuck
retrying, or just slow — is still in flight. If that older invocation later calls
release() unconditionally, it would delete whatever lock currently exists for that
agentId, which by then may belong to the newer generation — freeing the agent mid-dial
and letting a THIRD, overlapping dispatch begin. acquire() therefore generates and stores
a random `lockToken` and hands it back to the caller; release() requires that exact token
via a ConditionExpression, so a stale caller holding an old generation's token can never
affect a newer generation's lock — its release() call becomes a silent no-op instead.

release() is still called by: (a) the consumer when the campaign queue is empty,
(b) the caller on dial failure (permanent errors), and (c) the caller when get_phone
returns None (contact missing from queue). All three must propagate the exact token
returned by the acquire() call in that same invocation.
"""
from __future__ import annotations

import time
import uuid

import boto3
from botocore.exceptions import ClientError


_LOCK_TTL_SECONDS = 600       # 10 min safety net; stale threshold is the primary re-dispatch gate
_LOCK_STALE_SECONDS = 60      # locks older than this are overrideable on AVAILABLE events


class AgentLock:
    def __init__(self, table_name: str, dynamodb_resource=None) -> None:
        self._table = (dynamodb_resource or boto3.resource("dynamodb")).Table(table_name)

    def acquire(self, agent_id: str, *, campaign_id: str) -> str | None:
        """Attempt to acquire the lock.

        Returns a fencing token (str) identifying this lock generation on
        success, or None if already locked by a live (non-stale) generation.
        Callers MUST hold on to this token and pass it to the matching
        release() call for THIS dispatch — never a token from a prior
        acquire() (VIP-04 fencing; see module docstring).

        Succeeds when: no lock exists, OR existing lock's TTL is past, OR the lock is
        older than _LOCK_STALE_SECONDS (agent came back Available after a completed call).
        The atomic write prevents double-dispatch even from concurrent invocations.
        """
        now = int(time.time())
        token = uuid.uuid4().hex
        try:
            self._table.put_item(
                Item={
                    "agentId": agent_id,
                    "campaignId": campaign_id,
                    "lockedAt": now,
                    "ttl": now + _LOCK_TTL_SECONDS,
                    "lockToken": token,
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
            return token
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise

    def release(self, agent_id: str, token: str) -> None:
        """Release the lock only if `token` matches the currently stored
        lockToken (VIP-04 fencing).

        If the token doesn't match — or the lock has already been deleted, or
        a newer dispatch generation now holds a different token — this is a
        silent no-op, not an error: a stale caller holding an old generation's
        token must never be able to delete a newer generation's lock.
        """
        try:
            self._table.delete_item(
                Key={"agentId": agent_id},
                ConditionExpression="lockToken = :token",
                ExpressionAttributeValues={":token": token},
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return  # stale token, or lock already gone — no-op by design
            raise
