"""Deny-list (blocked caller numbers) read/write handlers.

Backs ``vip-connect-deny-list`` — the same DynamoDB table that
``connectcampaign_denylist_check`` reads on every inbound call and
``connectcampaign_denylist_write`` writes from the agent's "Block Number"
Quick Connect. This Lambda is the manual-entry path for when an agent can't
transfer the live call to that Quick Connect.

The check Lambda does ``get_item(Key={"ContactNumber": caller})`` where
``caller`` is Connect's ``CustomerEndpoint.Address`` — always E.164. A
manually-typed number must be normalized to that exact format or it will
silently never match.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone

import boto3

from vip_shared.application.http import extract_caller, json_response, parse_body
from vip_shared.infrastructure.persistence.audit import build_from_env as build_audit
from vip_shared.infrastructure.telemetry.structured_logger import StructuredLogger

TABLE_NAME = os.environ.get("DENY_LIST_TABLE", "vip-connect-deny-list")
DEFAULT_LIST_LIMIT = 500

_logger = StructuredLogger(service="api-deny-list")


def _table():
    return boto3.resource("dynamodb").Table(TABLE_NAME)


def _mask(phone: str) -> str:
    """Last-4 mask for anything that leaves this process as a log line."""
    digits = "".join(c for c in (phone or "") if c.isdigit())
    return f"****{digits[-4:]}" if len(digits) >= 4 else "****"


def normalize_phone(raw: str) -> str | None:
    """Normalize agent input to the E.164 form Connect writes as
    CustomerEndpoint.Address, so this matches what the check Lambda looks up.
    Accepts 10-digit US numbers, 11-digit with leading 1, or already-E.164.

    Uses re.ASCII so Unicode "digit" characters (e.g. full-width U+FF10-19,
    which Python's \\d/\\D match by default) are rejected rather than
    silently passed through into a non-None but non-ASCII value that looks
    like a successful E.164 normalization but can never match a real
    caller's CustomerEndpoint.Address.
    """
    digits = re.sub(r"\D", "", raw or "", flags=re.ASCII)
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) != 11 or not digits.startswith("1"):
        return None
    return "+" + digits


def _record_audit_or_log(*, action: str, entity_id: str, **kwargs) -> None:
    try:
        build_audit().record(
            entity_type="deny_list", entity_id=entity_id, action=action, **kwargs
        )
    except Exception as exc:  # noqa: BLE001
        _logger.error(
            "audit_write_failed",
            action=action,
            entity_type="deny_list",
            entity_id=entity_id,
            error=str(exc),
        )


def list_blocked_numbers(event: dict, _path_params: dict) -> dict:
    """GET /deny-list?limit=<N> — most-recently-added first.

    Scans the FULL table before sorting/truncating. Scan(Limit=N) truncates
    in DynamoDB's internal hash-partition order, unrelated to addedAt, so
    sorting only that one page silently produces a wrong "most recent" list
    once the table has more than N rows. At current scale (a few hundred
    rows) a full scan is cheap; move to a GSI (addedAt sort key) + Query if
    the table grows enough for this to matter.
    """
    qs = event.get("queryStringParameters") or {}
    try:
        limit = int(qs.get("limit", DEFAULT_LIST_LIMIT))
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    limit = max(1, min(limit, 1000))

    table = _table()
    items: list[dict] = []
    scan_kwargs: dict = {}
    while True:
        response = table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key

    items.sort(key=lambda i: i.get("addedAt", ""), reverse=True)
    items = items[:limit]

    return json_response(
        200,
        {
            "blockedNumbers": [_serialize(i) for i in items],
            "count": len(items),
        },
    )


def add_blocked_number(event: dict, _path_params: dict) -> dict:
    """POST /deny-list — body: {"phoneNumber": str, "reason"?: str}."""
    body = parse_body(event)
    raw_phone = body.get("phoneNumber")
    if not raw_phone:
        raise ValueError("Missing required field: phoneNumber")
    if not isinstance(raw_phone, str):
        raise ValueError("phoneNumber must be a string")

    phone = normalize_phone(raw_phone)
    if not phone:
        raise ValueError(
            "phoneNumber must be a 10-digit US number or already E.164 (+1XXXXXXXXXX)"
        )

    raw_reason = body.get("reason")
    if raw_reason is not None and not isinstance(raw_reason, str):
        raise ValueError("reason must be a string")
    reason = (raw_reason or "").strip()[:500]

    caller = extract_caller(event)
    table = _table()

    existing = table.get_item(Key={"ContactNumber": phone}).get("Item")
    already_blocked = existing is not None

    # put_item below is a full replace, not a merge — if this call didn't
    # supply a new reason, keep whatever was already recorded instead of
    # silently dropping it.
    if not reason and already_blocked:
        reason = existing.get("reason", "")

    item = {
        "ContactNumber": phone,
        "addedAt": datetime.now(timezone.utc).isoformat(),
        "addedBy": caller.email,
        "contactId": "manual-entry",
        "source": "manual-ui",
    }
    if reason:
        item["reason"] = reason
    table.put_item(Item=item)

    _logger.info(
        "blocked_number_added",
        number=_mask(phone),
        already_blocked=already_blocked,
        actor=caller.email,
    )
    _record_audit_or_log(
        entity_id=_mask(phone),
        action="update" if already_blocked else "create",
        actor_sub=caller.sub,
        actor_email=caller.email,
        ip_address=caller.ip_address,
        user_agent=caller.user_agent,
        before=(
            {"phoneNumberLast4": _mask(phone), "reason": existing.get("reason")}
            if already_blocked
            else None
        ),
        after={"phoneNumberLast4": _mask(phone), "reason": reason or None},
    )

    return json_response(
        201,
        {
            "phoneNumber": phone,
            "alreadyBlocked": already_blocked,
        },
    )


def _serialize(item: dict) -> dict:
    # Rows written by the sibling Quick Connect Lambda never set `source`
    # either, and today's real rows are 100% bulk-import — "legacy" is an
    # honest label for "we don't know the provenance", unlike guessing
    # "quick-connect" for something that's demonstrably not that.
    return {
        "phoneNumber": item.get("ContactNumber"),
        "addedAt": item.get("addedAt"),
        "addedBy": item.get("addedBy"),
        "reason": item.get("reason"),
        "source": item.get("source", "legacy"),
    }
