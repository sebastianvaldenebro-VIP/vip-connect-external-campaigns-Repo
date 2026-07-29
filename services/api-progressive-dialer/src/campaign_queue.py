"""DynamoDB-backed FIFO contact queue for progressive branded campaigns.

Table: VipProgressiveCampaignQueue
  PK: campaignId (S)
  SK: createdAt#contactUUID (S)  — ISO-8601 timestamp + UUID ensures strict FIFO
  Attributes:
    phone (S)          — customer phone number (PHI — encrypted at rest via KMS CMK)
    status (S)         — PENDING | DISPATCHING | DIALED | DONE
    contactUUID (S)    — for reference in the SK
    agentId (S)        — set when dispatched
    contactId (S)      — set after StartOutboundVoiceContact succeeds
    ttl (N)            — epoch seconds, 24h from enqueue
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr, Key


@dataclass
class Contact:
    campaign_id: str
    contact_uuid: str
    sk: str
    phone: str


class CampaignQueue:
    def __init__(self, table_name: str, dynamodb_resource=None) -> None:
        self._table = (dynamodb_resource or boto3.resource("dynamodb")).Table(table_name)

    def enqueue(self, campaign_id: str, phone: str) -> str:
        """Add a contact to the queue. Returns the SK."""
        contact_uuid = str(uuid.uuid4())
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        sk = f"{ts}#{contact_uuid}"
        ttl = int(time.time()) + 86400  # 24h

        self._table.put_item(Item={
            "campaignId": campaign_id,
            "sk": sk,
            "contactUUID": contact_uuid,
            "phone": phone,
            "status": "PENDING",
            "ttl": ttl,
        })
        return sk

    def dequeue(self, campaign_id: str) -> Contact | None:
        """Atomically dequeue the oldest PENDING contact. Returns None if empty.

        Paginates through the partition without Limit so that contacts in
        DISPATCHING/DIALED state at the front never block PENDING items further
        back. DynamoDB applies FilterExpression after Limit, so Limit=10 would
        silently return 0 results if the first 10 items are all non-PENDING.
        """
        query_kwargs: dict = {
            "KeyConditionExpression": Key("campaignId").eq(campaign_id),
            "FilterExpression": Attr("status").eq("PENDING"),
            "ScanIndexForward": True,  # oldest first
        }
        while True:
            response = self._table.query(**query_kwargs)
            for item in response.get("Items", []):
                if item.get("status") != "PENDING":
                    continue  # client-side guard: mock returns all items regardless of filter
                try:
                    self._table.update_item(
                        Key={"campaignId": item["campaignId"], "sk": item["sk"]},
                        UpdateExpression="SET #s = :dispatching",
                        ConditionExpression=Attr("status").eq("PENDING"),
                        ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={":dispatching": "DISPATCHING"},
                    )
                    return Contact(
                        campaign_id=item["campaignId"],
                        contact_uuid=item["contactUUID"],
                        sk=item["sk"],
                        phone=item["phone"],
                    )
                except self._table.meta.client.exceptions.ConditionalCheckFailedException:
                    continue  # another Lambda won the race — try next item
            if "LastEvaluatedKey" not in response:
                return None
            query_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def mark_dialed(self, campaign_id: str, sk: str, contact_id: str) -> None:
        """Record the Connect contactId after StartOutboundVoiceContact succeeds.

        Guards on status=DISPATCHING so SQS redelivery after a successful dial
        does not overwrite a contact another flow has already advanced to DONE.
        ConditionalCheckFailedException is treated as idempotent success.
        """
        try:
            self._table.update_item(
                Key={"campaignId": campaign_id, "sk": sk},
                UpdateExpression="SET #s = :dialed, contactId = :cid",
                ConditionExpression=Attr("status").eq("DISPATCHING"),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":dialed": "DIALED", ":cid": contact_id},
            )
        except self._table.meta.client.exceptions.ConditionalCheckFailedException:
            pass  # already advanced by another flow — idempotent success

    def reset_to_pending(self, campaign_id: str, sk: str) -> None:
        """Reset a DISPATCHING contact back to PENDING so the next available agent retries it.

        Called by handler_caller on dial failure (throttle, limit exceeded) to prevent
        contacts from being permanently stranded in DISPATCHING status.
        """
        try:
            self._table.update_item(
                Key={"campaignId": campaign_id, "sk": sk},
                UpdateExpression="SET #s = :pending",
                ConditionExpression=Attr("status").eq("DISPATCHING"),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":pending": "PENDING"},
            )
        except self._table.meta.client.exceptions.ConditionalCheckFailedException:
            pass  # already transitioned by another invocation — safe to ignore

    def mark_outcome(self, campaign_id: str, sk: str, outcome: str) -> None:
        """Persist call outcome on a DIALED item (idempotent).

        outcome: 'answered' | 'voicemail' | 'busy' | 'no_answer'
        Only writes if outcome is not already set to avoid overwriting with a stale
        DescribeContact result when a concurrent invocation already resolved it.
        """
        try:
            self._table.update_item(
                Key={"campaignId": campaign_id, "sk": sk},
                UpdateExpression="SET #o = :o",
                ConditionExpression="attribute_not_exists(#o)",
                ExpressionAttributeNames={"#o": "outcome"},
                ExpressionAttributeValues={":o": outcome},
            )
        except self._table.meta.client.exceptions.ConditionalCheckFailedException:
            pass  # already set by a concurrent invocation — safe to ignore

    def get_phone(self, campaign_id: str, sk: str) -> str | None:
        """Return the phone number for a contact item. Returns None if item not found.

        Used by handler_caller to read the destination phone from DynamoDB instead of
        carrying it in the SQS message body — keeps PHI out of SQS and the DLQ.
        HIPAA: the returned value is PHI; the caller must not log it.
        """
        resp = self._table.get_item(Key={"campaignId": campaign_id, "sk": sk})
        item = resp.get("Item")
        if item is None:
            return None
        return item.get("phone")
