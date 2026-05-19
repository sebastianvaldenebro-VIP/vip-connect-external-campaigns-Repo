"""CRUD handlers for segments."""
from __future__ import annotations

from typing import Any

from vip_shared.application.http import (
    extract_caller,
    json_response,
    parse_body,
)
from vip_shared.domain.services.segment_groups_translator import SegmentGroupsTranslator
from vip_shared.infrastructure.persistence.audit import build_from_env as build_audit
from vip_shared.infrastructure.persistence.customer_profiles_client import (
    build_from_env as build_cp,
)
from vip_shared.infrastructure.persistence.segment_filter_config import (
    build_from_env as build_filter_config_store,
)


def list_segments(event: dict, _path_params: dict) -> dict:
    """GET /segments — list all segment definitions in the domain."""
    cp = build_cp()

    # Accept optional ?nextToken= and ?maxResults=
    qs = event.get("queryStringParameters") or {}
    max_results = int(qs.get("maxResults", 100))
    next_token = qs.get("nextToken")

    response = cp.list_segment_definitions(
        max_results=max_results, next_token=next_token
    )

    segments = [_summary(item) for item in response.get("Items", [])]

    return json_response(
        200,
        {
            "segments": segments,
            "nextToken": response.get("NextToken"),
        },
    )


def _summary(item: dict) -> dict:
    tags = item.get("Tags") or {}
    return {
        "name": item.get("SegmentDefinitionName"),
        "displayName": item.get("DisplayName"),
        "description": item.get("Description"),
        "segmentArn": item.get("SegmentDefinitionArn"),
        "createdAt": item.get("CreatedAt"),
        "tags": tags,
        "family": tags.get("VipFamily") or item.get("SegmentDefinitionName"),
        "version": _int_tag(tags, "VipVersion", default=1),
        "syncMode": tags.get("VipSyncMode", "live").lower(),
    }


def _int_tag(tags: dict, key: str, default: int) -> int:
    try:
        return int(tags.get(key, default))
    except (TypeError, ValueError):
        return default


def get_segment(event: dict, path_params: dict) -> dict:
    """GET /segments/{id} — full definition."""
    name = path_params["id"]
    cp = build_cp()
    response = cp.get_segment_definition(name)
    body = _summary(response)
    body["segmentGroups"] = response.get("SegmentGroups")
    return json_response(200, body)


def create_segment(event: dict, _path_params: dict) -> dict:
    """POST /segments — create a new segment definition.

    Body schema:
    {
      "name": "segment-name-kebab",           # required, must be unique
      "displayName": "Human readable",         # required
      "description": "...",                    # optional
      "segmentGroups": { ... },                # required, Customer Profiles segment groups
      "syncMode": "live" | "manual",          # optional, defaults to "live"
      "tags": { "k": "v" }                     # optional extra tags
    }
    """
    body = parse_body(event)
    caller = extract_caller(event)

    _require_fields(body, ("name", "displayName", "segmentGroups"))

    # Manual/live distinction retired — every segment is reconcilable now.
    # We still emit the `VipSyncMode=manual` tag for backwards compatibility
    # with anything that may still inspect it, but it doesn't gate behavior.
    sync_mode = "manual"

    # Identity tags are managed by the service so reconcile/verify can trust
    # them — never let a caller override them via `tags`.
    tags = dict(body.get("tags") or {})
    tags["VipSyncMode"] = sync_mode
    tags.setdefault("VipFamily", body["name"])
    tags.setdefault("VipVersion", "1")

    cp = build_cp()
    response = cp.create_segment_definition(
        name=body["name"],
        display_name=body["displayName"],
        segment_groups=body["segmentGroups"],
        description=body.get("description"),
        tags=tags,
    )

    # Persist the authoritative filter for every new segment so verify and
    # reconcile can keep evaluating against operator intent even after a
    # rebuild replaces the segment's own segmentGroups with a static list.
    translator = SegmentGroupsTranslator()
    rules, combinator = translator.aws_to_rules(body["segmentGroups"])
    if rules:
        build_filter_config_store().put(
            family=tags["VipFamily"],
            rules=list(rules),
            combinator=combinator,
            sync_mode=sync_mode,
            created_by=caller.email or caller.sub,
            current_version=int(tags["VipVersion"]),
            # Persist the original v1 description so reconcile can copy it
            # forward into every rebuilt v{N+1} segment — otherwise the
            # operator loses track of what the segment was originally for.
            description=body.get("description"),
        )

    build_audit().record(
        entity_type="segment",
        entity_id=body["name"],
        action="create",
        actor_sub=caller.sub,
        actor_email=caller.email,
        ip_address=caller.ip_address,
        user_agent=caller.user_agent,
        after={
            "name": body["name"],
            "displayName": body["displayName"],
            "description": body.get("description"),
            "segmentGroups": body["segmentGroups"],
            "syncMode": sync_mode,
        },
    )

    return json_response(
        201,
        {
            "name": response["SegmentDefinitionName"],
            "displayName": response["DisplayName"],
            "segmentArn": response["SegmentDefinitionArn"],
            "createdAt": response.get("CreatedAt"),
            "syncMode": sync_mode,
            "family": tags["VipFamily"],
            "version": int(tags["VipVersion"]),
        },
    )


def delete_segment(event: dict, path_params: dict) -> dict:
    """DELETE /segments/{id}. TODO: verify no active campaigns reference this."""
    name = path_params["id"]
    caller = extract_caller(event)

    cp = build_cp()
    # Capture state before deletion for audit
    before = cp.get_segment_definition(name)
    cp.delete_segment_definition(name)

    # Also remove the filter config row for manual-sync segments so abandoned
    # rows don't pile up when a family is retired.
    tags_before = before.get("Tags") or {}
    family = tags_before.get("VipFamily")
    if family and tags_before.get("VipSyncMode", "live").lower() == "manual":
        try:
            build_filter_config_store().delete(family)
        except Exception:  # noqa: BLE001
            # Don't fail the API call if config row is missing/locked.
            pass

    build_audit().record(
        entity_type="segment",
        entity_id=name,
        action="delete",
        actor_sub=caller.sub,
        actor_email=caller.email,
        ip_address=caller.ip_address,
        user_agent=caller.user_agent,
        before={
            "name": before["SegmentDefinitionName"],
            "displayName": before.get("DisplayName"),
            "segmentGroups": before.get("SegmentGroups"),
        },
    )

    return json_response(204, {})


def _require_fields(body: dict, fields: tuple[str, ...]) -> None:
    missing = [f for f in fields if f not in body or body[f] is None]
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(missing)}")
