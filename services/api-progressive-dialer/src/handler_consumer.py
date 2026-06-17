# services/api-progressive-dialer/src/handler_consumer.py
"""Kinesis Agent Event Stream consumer.

Flow per record:
1. Decode + filter: only STATE_CHANGE with ROUTABLE Available + no pending break
2. Acquire agent lock (atomic conditional write: attribute_not_exists OR stale TTL) — skip if another invocation won
3. Dequeue next PENDING contact from campaign queue (conditional update)
4. Fire First Orion push (single call, not polling)
5. Enqueue SQS message with DelaySeconds=22
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time

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
_CONTACT_FLOW_ID = os.environ["CONTACT_FLOW_ID"]
_SOURCE_PHONE = os.environ["SOURCE_PHONE"]
_ACTIVE_CAMPAIGN_ID = os.environ["ACTIVE_CAMPAIGN_ID"]
_FIRSTORION_SECRET_NAME = os.environ["FIRSTORION_SECRET_NAME"]
_SQS_DELAY_SECONDS = 22
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

    # Atomic acquire: succeeds if no lock exists OR the existing lock's TTL is expired.
    # No blind release before acquire — that would create a race where two concurrent
    # invocations both delete the lock then both acquire it, causing double-dispatch.
    lock = _get_lock()
    if not lock.acquire(agent_arn, campaign_id=_ACTIVE_CAMPAIGN_ID):
        logger.info("Lock already held for agent — skipping dispatch correlation_id=%s", f"{agent_arn[-8:]}:{int(time.time())}")
        return

    contact = _get_queue().dequeue(_ACTIVE_CAMPAIGN_ID)
    if contact is None:
        lock.release(agent_arn)
        logger.info("Campaign queue empty — releasing lock correlation_id=%s", f"{agent_arn[-8:]}:{int(time.time())}")
        return

    # Fire First Orion push — does NOT log phone numbers
    pushed = _get_fo().push(a_number=_SOURCE_PHONE, b_number=contact.phone)
    if not pushed:
        logger.warning("First Orion push failed — will retry via SQS caller")

    # Enqueue SQS with 22s delay regardless of push result
    # (caller Lambda fires StartOutboundVoiceContact, not dependent on push success)
    # destinationPhone (PHI) is intentionally NOT included in the SQS message.
    # The caller Lambda reads it from DynamoDB (encrypted at rest via KMS CMK).
    # This keeps PHI out of SQS and prevents it from sitting in the DLQ for 14 days.
    message = {
        "agentArn": agent_arn,
        "queueArn": queue_arn,
        "campaignId": contact.campaign_id,
        "contactSk": contact.sk,
        "sourcePhone": _SOURCE_PHONE,
        "contactFlowId": _CONTACT_FLOW_ID,
        "instanceId": _CONNECT_INSTANCE_ID,
    }
    _get_sqs().send_message(
        QueueUrl=_SQS_QUEUE_URL,
        MessageBody=json.dumps(message),
        DelaySeconds=_SQS_DELAY_SECONDS,
    )
    logger.info(
        "SQS message enqueued correlation_id=%s campaign_id=%s",
        f"{agent_arn[-8:]}:{int(time.time())}",
        contact.campaign_id,
    )


def lambda_handler(event: dict, _context) -> dict:
    dispatched_count = 0
    for record in event.get("Records", []):
        try:
            _process_record(record)
            dispatched_count += 1
        except Exception as exc:
            logger.error("Failed to process record: %s", type(exc).__name__)
    return {"dispatched_count": dispatched_count}
