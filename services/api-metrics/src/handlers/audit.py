"""Read-only handlers for AdminAuditLog table."""
from __future__ import annotations

import json
import os
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

from vip_shared.application.http import json_response


def list_audit_entries(event: dict, _path_params: dict) -> dict:
    """GET /audit?limit=50&nextToken=...

    Accepts optional filters: actor, action, entityType, from, to.
    Uses scan with FilterExpression for small volumes; for larger,
    use GSI1_ByActor or GSI2_ByAction if those filters are set.
    """
    qs = event.get("queryStringParameters") or {}
    limit = min(int(qs.get("limit", 50)), 100)
    next_token = qs.get("nextToken")

    table = _table()

    scan_kwargs: dict[str, Any] = {"Limit": limit}
    if next_token:
        scan_kwargs["ExclusiveStartKey"] = json.loads(next_token)

    # Prefer GSI if a unique filter is present
    if qs.get("actor"):
        scan_kwargs["IndexName"] = "GSI1_ByActor"
        scan_kwargs["KeyConditionExpression"] = Key("actor_sub").eq(qs["actor"])
        response = table.query(**scan_kwargs)
    elif qs.get("action"):
        scan_kwargs["IndexName"] = "GSI2_ByAction"
        scan_kwargs["KeyConditionExpression"] = Key("action").eq(qs["action"])
        response = table.query(**scan_kwargs)
    else:
        # Full scan with optional entityType client-side filter
        filter_parts: list = []
        expr_values: dict = {}
        if qs.get("entityType"):
            filter_parts.append("entity_type = :et")
            expr_values[":et"] = qs["entityType"]
        if filter_parts:
            scan_kwargs["FilterExpression"] = " AND ".join(filter_parts)
            scan_kwargs["ExpressionAttributeValues"] = expr_values
        response = table.scan(**scan_kwargs)

    items = [_serialize_item(item) for item in response.get("Items", [])]
    # Sort newest-first
    items.sort(key=lambda i: i["timestamp"], reverse=True)

    last_key = response.get("LastEvaluatedKey")
    return json_response(
        200,
        {
            "entries": items,
            "nextToken": json.dumps(last_key, default=str) if last_key else None,
            "count": len(items),
        },
    )


def get_entity_history(event: dict, path_params: dict) -> dict:
    """GET /audit/{entityId} — all actions for a specific entity (segment/camp)."""
    entity_id = path_params["entityId"]  # e.g. "segment/nj-1st" (URL-encoded /)
    table = _table()
    response = table.query(
        KeyConditionExpression=Key("entity_id").eq(entity_id),
        ScanIndexForward=False,  # newest first
        Limit=100,
    )
    return json_response(
        200,
        {
            "entityId": entity_id,
            "entries": [_serialize_item(item) for item in response.get("Items", [])],
        },
    )


def _table():
    return boto3.resource("dynamodb").Table(os.environ["AUDIT_TABLE"])


def _serialize_item(item: dict) -> dict:
    """Normalize DynamoDB item for JSON response."""
    return {
        "entityId": item.get("entity_id"),
        "entityType": item.get("entity_type"),
        "resourceId": item.get("resource_id"),
        "timestamp": item.get("timestamp"),
        "actorSub": item.get("actor_sub"),
        "actorEmail": item.get("actor_email"),
        "action": item.get("action"),
        "before": _maybe_parse_json(item.get("before")),
        "after": _maybe_parse_json(item.get("after")),
        "ipAddress": item.get("ip_address"),
        "userAgent": item.get("user_agent"),
        "extra": _maybe_parse_json(item.get("extra")),
    }


def _maybe_parse_json(raw):
    if raw is None:
        return None
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, ValueError):
        return raw
