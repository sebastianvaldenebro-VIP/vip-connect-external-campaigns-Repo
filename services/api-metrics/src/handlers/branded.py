"""Branded campaign monitoring endpoints.

GET /metrics/branded/today          — summary of campaigns run today (or a given date)
GET /metrics/branded/campaigns/{id}/metrics — time-series snapshots for a campaign
GET /metrics/branded/agents         — live agent roster (GetCurrentUserData)
GET /metrics/branded/history        — audit history for a plan (past N days)

PHI rule: no phone numbers in responses. All data is aggregate counts or agent identifiers.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta

import boto3
from boto3.dynamodb.conditions import Attr, Key

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_RUN_SUMMARY_TABLE = os.environ.get("BRANDED_RUN_SUMMARY_TABLE", "")
_METRICS_TABLE = os.environ.get("BRANDED_CAMPAIGN_METRICS_TABLE", "")
_CONNECT_INSTANCE_ID = os.environ.get("CONNECT_INSTANCE_ID", "")

_ddb = boto3.resource("dynamodb")
_connect = boto3.client("connect")

# Module-level cache: agentId → display name. Populated lazily via DescribeUser.
# Lives for the lifetime of the Lambda container (warm starts reuse it).
_user_name_cache: dict[str, str] = {}
_rp_name_cache: dict[str, str] = {}  # routingProfileId → name
_status_type_cache: dict[str, str] = {}  # agent status ARN → Type (ROUTABLE/OFFLINE/CUSTOM)

# Statuses that represent a planned multi-day absence rather than a break —
# must not trip the frontend's "Extended break" alert.
_INTENTIONAL_ABSENCE_STATUSES = {"Out of the Office", "Vacation"}

# A contact in state ENDED only reflects active after-call-work while recent.
# GetCurrentUserData can keep returning an ENDED contact long after the
# agent moved on, which would otherwise pin them in "ACW" indefinitely.
_ACW_MAX_AGE_SECONDS = 300


def _now():
    return datetime.now(timezone.utc)


def _parse_ts(raw) -> datetime | None:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def _agent_display_name(user_id: str) -> str:
    if not user_id:
        return ""
    if user_id in _user_name_cache:
        return _user_name_cache[user_id]
    try:
        resp = _connect.describe_user(UserId=user_id, InstanceId=_CONNECT_INSTANCE_ID)
        info = resp.get("User", {}).get("IdentityInfo", {})
        first = info.get("FirstName", "")
        last = info.get("LastName", "")
        username = resp.get("User", {}).get("Username", "")
        name = f"{first} {last}".strip() or username or user_id
        _user_name_cache[user_id] = name
        return name
    except Exception as exc:
        logger.warning("describe_user(%s): %s", user_id, type(exc).__name__)
        _user_name_cache[user_id] = user_id
        return user_id


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _ok(body: dict) -> dict:
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def _err(status: int, message: str) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": message}),
    }


def get_today_summary(event: dict, context: object) -> dict:
    """Return all branded campaign runs that started on the given date (default: today)."""
    qs = event.get("queryStringParameters") or {}
    date_str = qs.get("date", _today_str())
    day_start = f"{date_str}T00:00:00"
    day_end = f"{date_str}T23:59:59"

    if not _RUN_SUMMARY_TABLE:
        return _err(503, "BRANDED_RUN_SUMMARY_TABLE not configured")

    table = _ddb.Table(_RUN_SUMMARY_TABLE)
    items: list[dict] = []
    resp = table.scan(
        FilterExpression=Attr("startedAt").between(day_start, day_end),
    )
    items.extend(resp["Items"])
    while "LastEvaluatedKey" in resp:
        resp = table.scan(
            ExclusiveStartKey=resp["LastEvaluatedKey"],
            FilterExpression=Attr("startedAt").between(day_start, day_end),
        )
        items.extend(resp["Items"])

    active = sum(1 for i in items if i.get("status") == "RUNNING")
    completed = sum(1 for i in items if i.get("status") == "COMPLETED")
    contacts_dialed = sum(int(i.get("totalDialed", 0) or 0) for i in items)

    return _ok({
        "date": date_str,
        "total": len(items),
        "active": active,
        "completed": completed,
        "contactsDialed": contacts_dialed,
        "campaigns": items,
    })


def get_campaign_metrics(event: dict, context: object) -> dict:
    """Return time-series metric snapshots for a specific branded campaign."""
    campaign_id = (event.get("pathParameters") or {}).get("brandedCampaignId", "")
    if not campaign_id:
        return _err(400, "brandedCampaignId path parameter required")

    if not _METRICS_TABLE:
        return _err(503, "BRANDED_CAMPAIGN_METRICS_TABLE not configured")

    qs = event.get("queryStringParameters") or {}
    limit = min(int(qs.get("limit", "24")), 100)

    resp = _ddb.Table(_METRICS_TABLE).query(
        KeyConditionExpression=Key("brandedCampaignId").eq(campaign_id),
        ScanIndexForward=False,
        Limit=limit,
    )
    return _ok({"campaignId": campaign_id, "metrics": resp["Items"]})


def _routing_profile_ids() -> list[str]:
    """Return all routing profile IDs and populate the name cache as a side-effect."""
    paginator = _connect.get_paginator("list_routing_profiles")
    ids: list[str] = []
    for page in paginator.paginate(InstanceId=_CONNECT_INSTANCE_ID, MaxResults=100):
        for p in page.get("RoutingProfileSummaryList", []):
            pid = p["Id"]
            ids.append(pid)
            _rp_name_cache[pid] = p.get("Name", pid)
    return ids[:100]  # GetCurrentUserData accepts max 100


def _status_type_for_arn(status_arn: str) -> str:
    """Resolve an agent status ARN to its Type (ROUTABLE/OFFLINE/CUSTOM).

    GetCurrentUserData's AgentStatusReference never returns Type — only
    StatusArn, StatusName, StatusStartTimestamp. ListAgentStatuses is the only
    API that carries Type, keyed by status Id (the ARN's last path segment).
    Cached for the container's lifetime; refreshed once per cold cache miss.
    """
    if not _status_type_cache:
        try:
            resp = _connect.list_agent_statuses(InstanceId=_CONNECT_INSTANCE_ID, MaxResults=100)
            for s in resp.get("AgentStatusSummaryList", []):
                _status_type_cache[s["Id"]] = s.get("Type", "CUSTOM")
        except Exception as exc:
            logger.warning("list_agent_statuses: %s", type(exc).__name__)
    status_id = status_arn.rsplit("/", 1)[-1] if status_arn else ""
    return _status_type_cache.get(status_id, "CUSTOM")


def get_agent_roster(event: dict, context: object) -> dict:
    """Return live agent roster from Connect GetCurrentUserData."""
    if not _CONNECT_INSTANCE_ID:
        return _err(503, "CONNECT_INSTANCE_ID not configured")

    qs = event.get("queryStringParameters") or {}
    queue_id = qs.get("queueId", "")

    # GetCurrentUserData requires at least one non-empty filter field.
    # When a specific queue is requested, scope to that queue.
    # Otherwise, scope to all routing profiles (covers every agent in the instance).
    if queue_id:
        filters: dict = {"Queues": [queue_id]}
    else:
        profile_ids = _routing_profile_ids()
        if not profile_ids:
            return _ok({"agents": [], "queueId": queue_id})
        filters = {"RoutingProfiles": profile_ids}

    agents: list[dict] = []
    try:
        _CONNECTED = {"CONNECTED", "CONNECTED_ONHOLD", "INCOMING", "CONNECTING"}
        _ACW = {"ENDED"}
        now = _now()

        next_token: str | None = None
        while True:
            kwargs: dict = {
                "InstanceId": _CONNECT_INSTANCE_ID,
                "Filters": filters,
                "MaxResults": 100,
            }
            if next_token:
                kwargs["NextToken"] = next_token
            resp = _connect.get_current_user_data(**kwargs)

            for ud in resp.get("UserDataList", []):
                status = ud.get("Status", {})
                contacts = ud.get("Contacts", [])
                status_name = status.get("StatusName", "")
                status_type = _status_type_for_arn(status.get("StatusArn", ""))

                active = next((c for c in contacts if c.get("AgentContactState") in _CONNECTED), None)
                acw_candidate = next((c for c in contacts if c.get("AgentContactState") in _ACW), None)
                acw = None
                if acw_candidate:
                    started = _parse_ts(acw_candidate.get("StateStartTimestamp"))
                    if started and (now - started).total_seconds() <= _ACW_MAX_AGE_SECONDS:
                        acw = acw_candidate

                if active:
                    effective = "On Call"
                    # Use the contact's StateStartTimestamp so elapsed time reflects
                    # how long the agent has been on this specific call, not how long
                    # they've been in their Connect status (which doesn't reset per call).
                    effective_ts = active.get("StateStartTimestamp", status.get("StatusStartTimestamp", ""))
                elif acw:
                    effective = "ACW"
                    effective_ts = acw.get("StateStartTimestamp", status.get("StatusStartTimestamp", ""))
                elif status_type == "ROUTABLE":
                    effective = "Available"
                    effective_ts = status.get("StatusStartTimestamp", "")
                elif status_type == "OFFLINE":
                    effective = "Offline"
                    effective_ts = status.get("StatusStartTimestamp", "")
                else:
                    effective = "Unavailable"
                    effective_ts = status.get("StatusStartTimestamp", "")

                user_id = ud.get("User", {}).get("Id", "")
                rp_id = ud.get("RoutingProfile", {}).get("Id", "")
                agents.append({
                    "agentId": user_id,
                    "agentName": _agent_display_name(user_id),
                    "status": status_name,
                    "statusType": status_type,
                    "effectiveStatus": effective,
                    "statusStartTimestamp": str(effective_ts) if effective_ts else "",
                    "isIntentionalAbsence": status_name in _INTENTIONAL_ABSENCE_STATUSES,
                    "routingProfileId": rp_id,
                    "routingProfileName": _rp_name_cache.get(rp_id, rp_id),
                    "contactsCount": len(contacts),
                    "activeContactState": (active or acw or {}).get("AgentContactState", ""),
                })

            next_token = resp.get("NextToken")
            if not next_token:
                break
    except Exception as exc:
        logger.error("get_agent_roster: %s", type(exc).__name__)
        return _err(502, "Failed to fetch agent roster from Connect")

    # Build deduplicated routing profile list from agents returned
    seen_rps: dict[str, str] = {}
    for a in agents:
        rid = a["routingProfileId"]
        if rid and rid not in seen_rps:
            seen_rps[rid] = a["routingProfileName"]
    routing_profiles = sorted(
        [{"id": k, "name": v} for k, v in seen_rps.items()],
        key=lambda x: x["name"],
    )

    return _ok({
        "agents": agents,
        "queueId": queue_id,
        "routingProfiles": routing_profiles,
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
    })


def get_history(event: dict, context: object) -> dict:
    """Return branded campaign run history for a plan over the past N days."""
    if not _RUN_SUMMARY_TABLE:
        return _err(503, "BRANDED_RUN_SUMMARY_TABLE not configured")

    qs = event.get("queryStringParameters") or {}
    plan_id = qs.get("planId", "")
    if not plan_id:
        return _err(400, "planId query parameter required")

    days = min(int(qs.get("days", "30")), 90)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    resp = _ddb.Table(_RUN_SUMMARY_TABLE).query(
        KeyConditionExpression=Key("planId").eq(plan_id),
        FilterExpression=Attr("startedAt").gte(since),
        ScanIndexForward=False,
    )
    return _ok({"planId": plan_id, "days": days, "history": resp["Items"]})
