"""Estimate handlers for segments — forces on-demand recompute.

This is the key feature that mitigates the 24h lag of segment snapshots.
Whenever the operator hits "Refresh" in the UI, we call
`CreateSegmentEstimate` which computes the member count against live
Profile Attributes (not the cached snapshot).
"""

from __future__ import annotations

from vip_shared.application.http import extract_caller, json_response
from vip_shared.infrastructure.persistence.audit import build_from_env as build_audit
from vip_shared.infrastructure.persistence.customer_profiles_client import (
    build_from_env as build_cp,
)


def create_estimate(event: dict, path_params: dict) -> dict:
    """POST /segments/{id}/estimate — kick off an async recompute."""
    name = path_params["id"]
    caller = extract_caller(event)
    cp = build_cp()

    response = cp.create_segment_estimate(name)
    estimate_id = response["EstimateId"]

    build_audit().record(
        entity_type="segment",
        entity_id=name,
        action="estimate",
        actor_sub=caller.sub,
        actor_email=caller.email,
        ip_address=caller.ip_address,
        user_agent=caller.user_agent,
        extra={"estimateId": estimate_id},
    )

    return json_response(
        202,
        {
            "estimateId": estimate_id,
            "status": response.get("Status", "IN_PROGRESS"),
        },
    )


def get_estimate(event: dict, path_params: dict) -> dict:
    """GET /segments/{id}/estimate/{estimateId} — poll status + result."""
    estimate_id = path_params["estimateId"]
    cp = build_cp()
    response = cp.get_segment_estimate(estimate_id)

    body: dict = {
        "estimateId": estimate_id,
        "status": response.get("Status"),
    }

    if response.get("Status") == "SUCCEEDED":
        # Estimate is delivered as a string like "5000" or "{\"estimate\":5000}".
        # Normalize to int when possible.
        raw = response.get("Estimate")
        body["estimate"] = _normalize_estimate(raw)

    if response.get("StatusCode"):
        body["statusCode"] = response["StatusCode"]
    if response.get("Message"):
        body["message"] = response["Message"]

    return json_response(200, body)


def _normalize_estimate(raw) -> dict:
    """Return estimate as {totalCount: int} regardless of AWS format."""
    if isinstance(raw, dict):
        total = raw.get("TotalCount") or raw.get("totalCount")
        if isinstance(total, (int, float)):
            return {"totalCount": int(total)}
    try:
        return {"totalCount": int(raw)}
    except (TypeError, ValueError):
        return {"totalCount": None, "raw": raw}
