"""Extras detection — async snapshot + diff against Redis.

Why async: ``CreateSegmentSnapshot`` takes minutes to complete and API
Gateway's integration timeout is 30s, so we can't block. The UI first POSTs
to start the snapshot, gets back a ``snapshotId``, and then polls GET until
the status flips to ``COMPLETED`` — at which point this handler reads the
CSV under the snapshot prefix and returns the ids that are in CP but no
longer match the operator's Redis filter.

Separate from ``/verify`` because the filter evaluation there already works
without a snapshot (Redis is the source of truth). Extras is the
harder-to-detect side of the diff: profiles the segment still contains that
Redis says shouldn't be members anymore.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from vip_shared.application.http import extract_caller, json_response
from vip_shared.domain.services.segment_groups_translator import (
    SegmentGroupsTranslator,
    matches_group,
)
from vip_shared.infrastructure.persistence.audit import build_from_env as build_audit
from vip_shared.infrastructure.persistence.customer_profiles_client import (
    build_from_env as build_cp,
)
from vip_shared.infrastructure.persistence.redis_lead_source import (
    build_from_env as build_redis_source,
)
from vip_shared.infrastructure.persistence.segment_filter_config import (
    build_from_env as build_filter_config_store,
)
from vip_shared.infrastructure.persistence.snapshot_reader import SnapshotReader

MAX_EXTRAS_RETURNED = 2_500  # keep response under the 6 MB API GW cap


def start_extras_detection(event: dict, path_params: dict) -> dict:
    """POST /segments/{id}/verify/extras — kick off the CP snapshot."""
    name = path_params["id"]
    caller = extract_caller(event)

    cp = build_cp()
    definition = cp.get_segment_definition(name)
    tags = definition.get("Tags") or {}
    family = tags.get("VipFamily") or name

    config = build_filter_config_store().get(family)
    if config is None:
        translator = SegmentGroupsTranslator()
        probe_rules, _ = translator.aws_to_rules(definition.get("SegmentGroups") or {})
        if not probe_rules:
            raise ValueError(
                "Segment has no evaluable filters — cannot run extras detection"
            )

    bucket = os.environ["SNAPSHOT_BUCKET"]
    role_arn = os.environ["SNAPSHOT_ROLE_ARN"]
    kms_arn = os.environ.get("DATA_KEY_ARN")

    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination_uri = f"s3://{bucket}/{name}/extras-{ts}/"

    response = cp.create_segment_snapshot(
        name=name,
        destination_uri=destination_uri,
        data_format="CSV",
        role_arn=role_arn,
        encryption_key_arn=kms_arn,
    )
    snapshot_id = response["SnapshotId"]

    build_audit().record(
        entity_type="segment",
        entity_id=name,
        action="verify-extras-start",
        actor_sub=caller.sub,
        actor_email=caller.email,
        ip_address=caller.ip_address,
        user_agent=caller.user_agent,
        extra={"snapshotId": snapshot_id, "destination": destination_uri},
    )

    return json_response(
        202,
        {
            "snapshotId": snapshot_id,
            "destinationUri": destination_uri,
            "status": response.get("Status", "IN_PROGRESS"),
        },
    )


def get_extras_detection(event: dict, path_params: dict) -> dict:
    """GET /segments/{id}/verify/extras/{snapshotId} — poll; diff when done."""
    name = path_params["id"]
    snapshot_id = path_params["snapshotId"]

    cp = build_cp()
    snap = cp.get_segment_snapshot(snapshot_id=snapshot_id, name=name)
    status = snap.get("Status")

    payload: dict = {
        "snapshotId": snapshot_id,
        "status": status,
        "destinationUri": snap.get("DestinationUri"),
    }
    if status == "FAILED":
        payload["statusMessage"] = snap.get("StatusMessage")
        return json_response(200, payload)
    if status != "COMPLETED":
        return json_response(200, payload)

    destination_uri = snap.get("DestinationUri")
    if not destination_uri:
        raise RuntimeError("Snapshot reported COMPLETED but has no DestinationUri")

    # 1. Load the snapshot CSV and pull the customer ids CP currently serves.
    reader = SnapshotReader()
    rows = reader.load_members(destination_uri)
    cp_ids = reader.extract_customer_ids(rows, field="customerid")
    if not cp_ids:
        # older snapshots may use ID casing instead
        cp_ids = reader.extract_customer_ids(rows, field="ID")

    # 2. Re-evaluate Redis against the canonical filter so we're diffing
    #    against operator intent, not against the frozen ID list the segment
    #    currently stores.
    definition = cp.get_segment_definition(name)
    tags = definition.get("Tags") or {}
    family = tags.get("VipFamily") or name

    config = build_filter_config_store().get(family)
    if config is not None:
        rules = list(config.rules)
        combinator = config.combinator
    else:
        # Legacy segment: no persisted filter config. Fall back to translating
        # the current SegmentGroups. For reconciled (vN) segments this will be
        # the frozen static ID list — evaluating Redis against it produces a
        # meaningless diff (it just checks whether those specific IDs still
        # exist in Redis, not whether they still match the original filter).
        # Refuse to proceed: the operator should recreate the segment so the
        # original filter gets persisted in VipAdminSegmentFilterConfig.
        try:
            current_version = int(tags.get("VipVersion") or "1")
        except (TypeError, ValueError):
            current_version = 1
        if current_version > 1:
            raise ValueError(
                "This segment was rebuilt before filter persistence was enabled. "
                "Its SegmentGroups is a static ID list, not the original filter — "
                "extras detection would diff against that list, not operator intent. "
                "Recreate the segment with the original filter to enable extras detection."
            )
        translator = SegmentGroupsTranslator()
        rules, combinator = translator.aws_to_rules(
            definition.get("SegmentGroups") or {}
        )

    if not rules:
        raise ValueError(
            "Segment has no evaluable filters — cannot compute extras against Redis"
        )

    redis_ids = _scan_redis_ids(rules, combinator)
    # Both diff directions: with the CP snapshot now in hand we can compute
    # the *real* missing count too (Redis IDs that aren't in CP yet). Verify
    # alone reports redis_count as missing, which is misleading once the
    # segment has populated — once extras detection finishes we can override
    # that with the actual diff.
    extras = sorted(cp_ids - redis_ids)
    missing = sorted(redis_ids - cp_ids)

    caller = extract_caller(event)
    build_audit().record(
        entity_type="segment",
        entity_id=name,
        action="verify-extras-complete",
        actor_sub=caller.sub,
        actor_email=caller.email,
        ip_address=caller.ip_address,
        user_agent=caller.user_agent,
        extra={
            "snapshotId": snapshot_id,
            "cpCount": len(cp_ids),
            "redisCount": len(redis_ids),
            "extras": len(extras),
            "missing": len(missing),
        },
    )

    payload.update(
        {
            "cpCount": len(cp_ids),
            "redisCount": len(redis_ids),
            "totalExtras": len(extras),
            "extraCustomerIds": extras[:MAX_EXTRAS_RETURNED],
            "totalMissing": len(missing),
            "missingCustomerIds": missing[:MAX_EXTRAS_RETURNED],
            "computedAt": _now_iso(),
        }
    )
    return json_response(200, payload)


def _scan_redis_ids(rules, combinator: str) -> set[str]:
    source = build_redis_source()
    ids: set[str] = set()
    for record in source.iter_records():
        if matches_group(record, rules, combinator):
            customer_id = str(record.get("customerid") or record.get("id") or "").strip()
            if customer_id:
                ids.add(customer_id)
    return ids


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
