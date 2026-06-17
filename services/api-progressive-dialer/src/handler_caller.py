"""SQS consumer — fires StartOutboundVoiceContact after the 22-second delay.

Each SQS message corresponds to one agent dispatch. The message was enqueued
by handler_consumer.py with DelaySeconds=22, which ensures First Orion's
branding window (10–30s) is active when the SIP INVITE is sent.

Throttle: StartOutboundVoiceContact is capped at 2 RPS / 5 burst per account+region.
Lambda reserved concurrency is set to 2 in the CDK stack.
"""
from __future__ import annotations

import json
import logging
import os

from agent_lock import AgentLock
from campaign_queue import CampaignQueue
from connect_caller import ConnectCaller

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_CAMPAIGN_QUEUE_TABLE = os.environ["CAMPAIGN_QUEUE_TABLE"]
_AGENT_LOCK_TABLE = os.environ["AGENT_LOCK_TABLE"]

_queue_store: CampaignQueue | None = None
_lock_store: AgentLock | None = None


def _get_queue() -> CampaignQueue:
    global _queue_store
    if _queue_store is None:
        _queue_store = CampaignQueue(_CAMPAIGN_QUEUE_TABLE)
    return _queue_store


def _get_lock() -> AgentLock:
    global _lock_store
    if _lock_store is None:
        _lock_store = AgentLock(_AGENT_LOCK_TABLE)
    return _lock_store


def _process_message(body: dict) -> None:
    agent_arn = body["agentArn"]
    queue_arn = body["queueArn"]
    campaign_id = body["campaignId"]
    contact_sk = body["contactSk"]
    instance_id = body["instanceId"]
    contact_flow_id = body["contactFlowId"]
    source_phone = body["sourcePhone"]

    # Extract queue ID from ARN (last segment)
    queue_id = queue_arn.split("/")[-1]

    # Read destination phone from DynamoDB — PHI is not carried in the SQS message body.
    # This keeps the phone number out of SQS and the DLQ (14-day retention).
    destination_phone = _get_queue().get_phone(campaign_id, contact_sk)
    if not destination_phone:
        logger.error(
            "Contact not found or missing phone campaign_id=%s correlation_id=%s",
            campaign_id,
            agent_arn[-8:],
        )
        return

    caller = ConnectCaller(
        instance_id=instance_id,
        contact_flow_id=contact_flow_id,
    )
    result = caller.dial(
        destination_phone=destination_phone,
        queue_id=queue_id,
        source_phone=source_phone,
        # contact_sk is a deterministic idempotency key — Connect deduplicates within ~7min,
        # preventing double-dials if SQS redelivers the message on Lambda failure.
        client_token=contact_sk,
    )

    if result.success:
        _get_queue().mark_dialed(campaign_id, contact_sk, result.contact_id)
        logger.info(
            "Dial success campaign_id=%s contact_id=%s correlation_id=%s",
            campaign_id,
            result.contact_id,
            agent_arn[-8:],
        )
    else:
        logger.warning(
            "Dial failed error_code=%s campaign_id=%s correlation_id=%s",
            result.error_code,
            campaign_id,
            agent_arn[-8:],
        )
        # Reset contact to PENDING so the next available agent can retry it.
        # Without this, the contact stays DISPATCHING forever (24h TTL wasted).
        try:
            _get_queue().reset_to_pending(campaign_id, contact_sk)
        except Exception:
            logger.error("Failed to reset contact to PENDING correlation_id=%s", agent_arn[-8:])
        # Release agent lock so the next AVAILABLE event can dispatch
        _get_lock().release(agent_arn)


def lambda_handler(event: dict, _context) -> dict:
    for record in event.get("Records", []):
        try:
            body = json.loads(record["body"])
            _process_message(body)
        except Exception as exc:
            logger.error("Failed to process SQS message: %s", type(exc).__name__)
    return {"status": "ok"}
