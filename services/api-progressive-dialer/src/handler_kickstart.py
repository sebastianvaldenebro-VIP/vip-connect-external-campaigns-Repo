"""DynamoDB Streams kickstart — dispatches to already-AVAILABLE agents on contact insert.

Problem solved: the consumer reacts only to agent state-change events (AVAILABLE transitions).
If an agent is already in AVAILABLE state when a contact is seeded, no dispatch fires until
that agent transitions away and back to AVAILABLE. This Lambda triggers on INSERT events to
VipProgressiveCampaignQueue and immediately dispatches to any currently-available agent via
connect:GetCurrentUserData (real-time, not event-driven).

Falls back silently: if no available agents exist, the regular consumer handles dispatch on
the next AVAILABLE state transition — no duplicate work, no race conditions.
"""
from __future__ import annotations

import json
import logging
import os
import uuid

import boto3

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
_FIRSTORION_SECRET_NAME = os.environ["FIRSTORION_SECRET_NAME"]
_SQS_DELAY_SECONDS = 22

_lock_store: AgentLock | None = None
_queue_store: CampaignQueue | None = None
_fo_client: FirstOrionClient | None = None
_connect_client = None
_sqs_client = None
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


def _get_connect():
    global _connect_client
    if _connect_client is None:
        _connect_client = boto3.client("connect")
    return _connect_client


def _get_sqs():
    global _sqs_client
    if _sqs_client is None:
        _sqs_client = boto3.client("sqs")
    return _sqs_client


def _get_ddb():
    global _ddb_client
    if _ddb_client is None:
        _ddb_client = boto3.client("dynamodb")
    return _ddb_client


def _get_campaign_config(campaign_id: str) -> dict | None:
    """Scan VipActiveBrandedCampaigns for the given campaignId.

    Scan is acceptable here — typically 1-5 active campaigns at any time.
    Returns the raw DynamoDB item dict or None if the campaign is no longer active.
    """
    resp = _get_ddb().scan(
        TableName=_ACTIVE_CAMPAIGNS_TABLE,
        FilterExpression="campaignId = :cid",
        ExpressionAttributeValues={":cid": {"S": campaign_id}},
    )
    items = resp.get("Items", [])
    return items[0] if items else None


def _get_available_agents(queue_id: str) -> list[str]:
    """Return agent ARNs currently in Available state for this Connect queue.

    Uses connect:GetCurrentUserData which reflects real-time agent state,
    unlike the Kinesis agent event stream which only fires on state transitions.
    Paginates through all results.
    Mirrors agent_event_filter.is_agent_available()'s NextStatus check — an agent
    who queued a break (NextStatus set to a non-Available status) will go offline
    after their current contact, so kickstart must skip them like the consumer does.
    HIPAA: agent ARNs are not PHI — no masking required.
    """
    agents: list[str] = []
    # AgentStatuses is not supported by the Lambda runtime's boto3 version —
    # filter by status client-side instead.
    kwargs: dict = {
        "InstanceId": _CONNECT_INSTANCE_ID,
        "Filters": {"Queues": [queue_id]},
    }
    while True:
        resp = _get_connect().get_current_user_data(**kwargs)
        for user in resp.get("UserDataList", []):
            status_name = user.get("Status", {}).get("StatusName", "")
            if status_name != "Available":
                continue
            next_status = user.get("NextStatus")
            if next_status and next_status != "Available":
                continue
            arn = user.get("User", {}).get("Arn")
            if arn:
                agents.append(arn)
        next_token = resp.get("NextToken")
        if not next_token:
            break
        kwargs["NextToken"] = next_token
    return agents


def _try_dispatch(
    *,
    agent_arn: str,
    campaign_id: str,
    queue_arn: str,
    contact_flow_id: str,
    source_phone: str,
) -> bool:
    """Acquire agent lock and dispatch one contact from the campaign queue.

    Returns True if a contact was successfully enqueued to SQS, False otherwise.
    Releases the lock on any failure so the agent remains available for the consumer.
    """
    if not _get_lock().acquire(agent_arn, campaign_id=campaign_id):
        logger.info("kickstart lock already held agent=...%s", agent_arn[-12:])
        return False

    contact = _get_queue().dequeue(campaign_id)
    if contact is None:
        _get_lock().release(agent_arn)
        logger.info("kickstart queue empty after lock acquire campaign_id=%s", campaign_id)
        return False

    correlation_id = str(uuid.uuid4())[:8]

    pushed = _get_fo().push(a_number=source_phone, b_number=contact.phone)
    if not pushed:
        logger.warning("kickstart First Orion push failed correlation_id=%s", correlation_id)

    _get_sqs().send_message(
        QueueUrl=_SQS_QUEUE_URL,
        MessageBody=json.dumps({
            "agentArn": agent_arn,
            "queueArn": queue_arn,
            "campaignId": campaign_id,
            "contactSk": contact.sk,
            "instanceId": _CONNECT_INSTANCE_ID,
            "contactFlowId": contact_flow_id,
            "sourcePhone": source_phone,
            "correlationId": correlation_id,
        }),
        DelaySeconds=_SQS_DELAY_SECONDS,
    )
    logger.info(
        "kickstart SQS enqueued correlation_id=%s campaign_id=%s",
        correlation_id,
        campaign_id,
    )
    return True


def _process_insert(new_image: dict) -> None:
    """Handle one INSERT event from the VipProgressiveCampaignQueue stream."""
    status = new_image.get("status", {}).get("S")
    if status != "PENDING":
        return

    campaign_id = new_image.get("campaignId", {}).get("S")
    if not campaign_id:
        logger.warning("kickstart INSERT missing campaignId — skipping")
        return

    campaign = _get_campaign_config(campaign_id)
    if not campaign:
        # Campaign may have ended between seeding and stream processing — normal edge case.
        logger.info("kickstart campaign_id=%s not active — skipping", campaign_id)
        return

    queue_arn: str = campaign["queueArn"]["S"]
    contact_flow_id: str = campaign["contactFlowId"]["S"]
    source_phone: str = campaign["sourcePhone"]["S"]
    queue_id = queue_arn.split("/")[-1]

    agents = _get_available_agents(queue_id)
    if not agents:
        logger.info(
            "kickstart no available agents campaign_id=%s — consumer will handle on next AVAILABLE event",
            campaign_id,
        )
        return

    logger.info(
        "kickstart found %d available agent(s) for campaign_id=%s — attempting dispatch",
        len(agents),
        campaign_id,
    )

    for agent_arn in agents:
        if _try_dispatch(
            agent_arn=agent_arn,
            campaign_id=campaign_id,
            queue_arn=queue_arn,
            contact_flow_id=contact_flow_id,
            source_phone=source_phone,
        ):
            return  # one dispatch per INSERT — remaining contacts handled by consumer


def lambda_handler(event: dict, _context) -> dict:
    processed = 0
    for record in event.get("Records", []):
        if record.get("eventName") != "INSERT":
            continue
        new_image = record.get("dynamodb", {}).get("NewImage", {})
        _process_insert(new_image)
        processed += 1
    return {"statusCode": 200, "processed": processed}
