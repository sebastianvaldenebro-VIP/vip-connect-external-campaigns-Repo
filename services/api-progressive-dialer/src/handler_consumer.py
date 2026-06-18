# services/api-progressive-dialer/src/handler_consumer.py
"""Kinesis Agent Event Stream consumer.

Flow per record:
1. Decode + filter: only STATE_CHANGE with ROUTABLE Available + no pending break
2. Query VipActiveBrandedCampaigns GSI by queueArn — campaigns sorted by priority ASC, createdAt ASC
3. Acquire agent lock (atomic conditional write: attribute_not_exists OR stale TTL) — skip if another invocation won
4. Try each campaign queue in priority order until a contact is found
5. Fire First Orion push (single call, not polling)
6. Enqueue SQS message with DelaySeconds=22
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
import uuid

import boto3

from agent_event_filter import extract_agent_info, is_agent_available, is_queue_allowed
from agent_lock import AgentLock
from campaign_queue import CampaignQueue
from first_orion_client import FirstOrionClient

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_CAMPAIGN_QUEUE_TABLE = os.environ["CAMPAIGN_QUEUE_TABLE"]
_AGENT_LOCK_TABLE = os.environ["AGENT_LOCK_TABLE"]
_SQS_QUEUE_URL = os.environ["SQS_QUEUE_URL"]
_CONNECT_INSTANCE_ID = os.environ["CONNECT_INSTANCE_ID"]
_ACTIVE_CAMPAIGNS_TABLE = os.environ["ACTIVE_CAMPAIGNS_TABLE"]
_ACTIVE_CAMPAIGNS_GSI   = os.environ.get("ACTIVE_CAMPAIGNS_GSI", "queueArn-index")
_FIRSTORION_SECRET_NAME = os.environ["FIRSTORION_SECRET_NAME"]
_SQS_DELAY_SECONDS = 22
_CW_NAMESPACE = "VipConnect/ProgressiveDialer"
# Optional comma-separated queue ARNs to restrict which agents trigger dispatch.
# Empty = all queues served. Set to limit branded dialing to specific outbound queues.
_ALLOWED_QUEUE_ARNS: set[str] = {
    arn.strip()
    for arn in os.environ.get("ALLOWED_QUEUE_ARNS", "").split(",")
    if arn.strip()
}

# Module-level singletons — re-used across warm invocations
_lock_store: AgentLock | None = None
_queue_store: CampaignQueue | None = None
_fo_client: FirstOrionClient | None = None
_sqs_client = None
_cw_client = None
_ddb_client = None


def _get_lock() -> AgentLock:
    global _lock_store
    if _lock_store is None:
        _lock_store = AgentLock(_AGENT_LOCK_TABLE)
    return _lock_store


def _get_queue() -> CampaignQueue:
    global _queue_store
    if _queue_store is None:
        _queue_store = CampaignQueue(_CAMPAIGN_QUEUE_TABLE)
    return _queue_store


def _get_fo() -> FirstOrionClient:
    global _fo_client
    if _fo_client is None:
        _fo_client = FirstOrionClient.build_from_secret(_FIRSTORION_SECRET_NAME)
    return _fo_client


def _get_sqs():
    global _sqs_client
    if _sqs_client is None:
        _sqs_client = boto3.client("sqs")
    return _sqs_client


def _get_cw():
    global _cw_client
    if _cw_client is None:
        _cw_client = boto3.client("cloudwatch")
    return _cw_client


def _get_ddb():
    global _ddb_client
    if _ddb_client is None:
        _ddb_client = boto3.client("dynamodb")
    return _ddb_client


def _emit_metric(metric_name: str, value: float = 1.0) -> None:
    """Emit a custom metric to VipConnect/ProgressiveDialer namespace.

    Failures are logged but not raised — metric emission must never abort a dispatch.
    """
    try:
        _get_cw().put_metric_data(
            Namespace=_CW_NAMESPACE,
            MetricData=[{"MetricName": metric_name, "Value": value, "Unit": "Count"}],
        )
    except Exception as exc:
        logger.warning("Failed to emit metric %s: %s", metric_name, type(exc).__name__)


def _get_active_campaigns(queue_arn: str) -> list[dict]:
    """Query VipActiveBrandedCampaigns GSI for all active campaigns on this queue.

    Paginates through all pages so queues with many campaigns are fully retrieved.
    Returns items sorted by priority ASC then createdAt ASC (oldest high-priority first).
    """
    items: list[dict] = []
    kwargs = dict(
        TableName=_ACTIVE_CAMPAIGNS_TABLE,
        IndexName=_ACTIVE_CAMPAIGNS_GSI,
        KeyConditionExpression="queueArn = :q",
        ExpressionAttributeValues={":q": {"S": queue_arn}},
    )
    while True:
        resp = _get_ddb().query(**kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    return sorted(
        items,
        key=lambda x: (int(x["priority"]["N"]), x["createdAt"]["S"]),
    )


def _process_record(record: dict) -> None:
    raw = base64.b64decode(record["kinesis"]["data"]).decode("utf-8")
    agent_event = json.loads(raw)

    if not is_agent_available(agent_event):
        return

    info = extract_agent_info(agent_event)
    agent_arn = info["agent_arn"]
    queue_arn = info["queue_arn"]

    if not agent_arn or not queue_arn:
        logger.warning("Agent event missing ARN or queue_arn — skipping")
        return

    if not is_queue_allowed(queue_arn, _ALLOWED_QUEUE_ARNS):
        return  # agent serves a different queue, not our branded campaign

    campaigns = _get_active_campaigns(queue_arn)
    if not campaigns:
        return  # no active branded campaigns for this queue

    # Acquire agent lock using the first (highest-priority) campaign id as metadata.
    # No blind release before acquire — that would create a race where two concurrent
    # invocations both delete the lock then both acquire it, causing double-dispatch.
    first_campaign_id = campaigns[0]["campaignId"]["S"]
    lock = _get_lock()
    if not lock.acquire(agent_arn, campaign_id=first_campaign_id):
        logger.info("Lock already held for agent — skipping dispatch")
        return

    # Try each campaign queue in priority order until a contact is found
    contact = None
    campaign_id = None
    contact_flow_id = None
    source_phone = None

    for camp in campaigns:
        c_id = camp["campaignId"]["S"]
        c = _get_queue().dequeue(c_id)
        if c is not None:
            contact = c
            campaign_id = c_id
            contact_flow_id = camp["contactFlowId"]["S"]
            source_phone = camp["sourcePhone"]["S"]
            break

    if contact is None:
        lock.release(agent_arn)
        logger.info("All campaign queues empty — releasing lock")
        return

    # Generate a short correlation ID once per dispatch. Used in all subsequent log lines
    # and propagated in the SQS body so the caller Lambda shares the same trace ID in CW.
    correlation_id = str(uuid.uuid4())[:8]

    # Fire First Orion push — does NOT log phone numbers (PHI rule)
    pushed = _get_fo().push(a_number=source_phone, b_number=contact.phone)
    if not pushed:
        logger.warning(
            "First Orion push failed — will retry via SQS caller correlation_id=%s",
            correlation_id,
        )
        _emit_metric("FirstOrionPushFailed")

    # Enqueue SQS with 22s delay regardless of push result
    # (caller Lambda fires StartOutboundVoiceContact, not dependent on push success)
    # destinationPhone (PHI) is intentionally NOT included in the SQS message.
    # The caller Lambda reads it from DynamoDB (encrypted at rest via KMS CMK).
    # This keeps PHI out of SQS and prevents it from sitting in the DLQ for 14 days.
    message = {
        "agentArn": agent_arn,
        "queueArn": queue_arn,
        "campaignId": campaign_id,
        "contactSk": contact.sk,
        "sourcePhone": source_phone,
        "contactFlowId": contact_flow_id,
        "instanceId": _CONNECT_INSTANCE_ID,
        "correlationId": correlation_id,
    }
    _get_sqs().send_message(
        QueueUrl=_SQS_QUEUE_URL,
        MessageBody=json.dumps(message),
        DelaySeconds=_SQS_DELAY_SECONDS,
    )
    logger.info(
        "SQS message enqueued correlation_id=%s campaign_id=%s",
        correlation_id,
        campaign_id,
    )


def lambda_handler(event: dict, _context) -> dict:
    dispatched_count = 0
    for record in event.get("Records", []):
        try:
            _process_record(record)
            dispatched_count += 1
        except Exception as exc:
            logger.error("Failed to process record: %s", type(exc).__name__)
            raise
    return {"dispatched_count": dispatched_count}
