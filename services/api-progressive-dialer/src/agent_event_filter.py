"""Parses Amazon Connect Agent Event Stream records to detect agent availability."""
from __future__ import annotations


def is_agent_available(event: dict) -> bool:
    """Return True only when the agent just became fully available for a new call.

    Filters:
    - EventType must be STATE_CHANGE (not HEART_BEAT / LOGIN / LOGOUT)
    - AgentStatus.Type must be ROUTABLE (built-in Available only)
    - AgentStatus.Name must be 'Available'
    - NextAgentStatus must NOT be set to a non-Available status
      (agent queued a break and will go offline after current contact)
    """
    if event.get("EventType") != "STATE_CHANGE":
        return False

    snapshot = event.get("CurrentAgentSnapshot") or event.get("AgentSnapshot") or {}
    status = snapshot.get("AgentStatus") or {}

    if status.get("Type") != "ROUTABLE":
        return False
    if status.get("Name") != "Available":
        return False

    next_status = snapshot.get("NextAgentStatus")
    if next_status and next_status.get("Name") != "Available":
        return False

    return True


def extract_agent_info(event: dict) -> dict:
    """Extract agent ARN and queue ARNs from an agent event.

    Returns both the DefaultOutboundQueue ARN and all InboundQueue ARNs so the
    consumer can match branded campaigns registered against any queue the agent serves.
    """
    snapshot = event.get("CurrentAgentSnapshot") or event.get("AgentSnapshot") or {}
    config = snapshot.get("Configuration") or {}
    routing_profile = config.get("RoutingProfile") or {}
    default_queue = routing_profile.get("DefaultOutboundQueue") or {}
    inbound_queues = routing_profile.get("InboundQueues") or []

    return {
        "agent_arn": event.get("AgentARN"),
        "queue_arn": default_queue.get("ARN"),
        "inbound_queue_arns": [q["ARN"] for q in inbound_queues if q.get("ARN")],
    }


def is_queue_allowed(queue_arn: str | None, allowed_arns: set[str]) -> bool:
    """Return True if this agent's queue should be served by the branded dialer.

    If allowed_arns is empty, all queues are allowed (no filter configured).
    If allowed_arns is non-empty, queue_arn must be in the set.
    Returns False when queue_arn is None regardless of allowed_arns.
    """
    if queue_arn is None:
        return False
    if not allowed_arns:
        return True  # no filter = accept all
    return queue_arn in allowed_arns
