"""PATCH /segments/{id} — currently used to toggle Live ↔ Manual sync mode.

Sync mode lives in the segment's tags (``VipSyncMode``). We reuse tags instead
of adding a dedicated metadata table because they're atomic with the segment
definition: delete the segment and the tag goes with it.
"""
from __future__ import annotations

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

ALLOWED_MODES = {"live", "manual"}


def update_sync_mode(event: dict, path_params: dict) -> dict:
    name = path_params["id"]
    caller = extract_caller(event)
    body = parse_body(event)

    sync_mode = str(body.get("syncMode", "")).lower()
    if sync_mode not in ALLOWED_MODES:
        raise ValueError(
            f"syncMode must be one of {sorted(ALLOWED_MODES)}; got {body.get('syncMode')!r}"
        )

    cp = build_cp()
    definition = cp.get_segment_definition(name)
    tags = definition.get("Tags") or {}
    previous_mode = tags.get("VipSyncMode", "live").lower()

    cp.tag_segment(
        segment_arn=definition["SegmentDefinitionArn"],
        tags={"VipSyncMode": sync_mode},
    )

    # Live → Manual: seed the filter config from the current segmentGroups so
    # verify/reconcile have a filter to evaluate against. No-op if we already
    # have a config row (preserves previous history).
    if previous_mode == "live" and sync_mode == "manual":
        family = tags.get("VipFamily") or name
        store = build_filter_config_store()
        if store.get(family) is None:
            rules, combinator = SegmentGroupsTranslator().aws_to_rules(
                definition.get("SegmentGroups") or {},
            )
            if rules:
                version = _int_tag(tags, "VipVersion", default=1)
                store.put(
                    family=family,
                    rules=list(rules),
                    combinator=combinator,
                    sync_mode=sync_mode,
                    created_by=caller.email or caller.sub,
                    current_version=version,
                    description=definition.get("Description"),
                )

    build_audit().record(
        entity_type="segment",
        entity_id=name,
        action="syncMode.update",
        actor_sub=caller.sub,
        actor_email=caller.email,
        ip_address=caller.ip_address,
        user_agent=caller.user_agent,
        before={"syncMode": previous_mode},
        after={"syncMode": sync_mode},
    )

    return json_response(
        200,
        {
            "name": name,
            "syncMode": sync_mode,
        },
    )


def _int_tag(tags: dict, key: str, default: int) -> int:
    try:
        return int(tags.get(key, default))
    except (TypeError, ValueError):
        return default
