"""POST /segments/{id}/verify.

Compare a manual-sync segment against Redis truth.

Note: CP's ``CreateSegmentSnapshot`` requires a ``RoleArn`` to be passed, and
our boundary denies ``iam:PassRole``. Until an admin grants that action, we
cannot use snapshots to enumerate the exact members CP currently has. This
handler therefore compares *counts* (Redis scan vs. on-demand segment
estimate) and returns the Redis-matching customerIds as "source of truth".
Extras (profiles in CP that no longer match Redis) are not detectable in this
mode — the UI surfaces that limitation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

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

SAMPLE_SIZE = 20
MAX_IDS_RETURNED = 2_500  # cap so the response body stays under 6 MB API GW limit


def verify_segment(event: dict, path_params: dict) -> dict:
    """Run the verify workflow and return diff details."""
    name = path_params["id"]
    caller = extract_caller(event)

    cp = build_cp()
    definition = cp.get_segment_definition(name)
    tags = definition.get("Tags") or {}
    # Manual/live distinction was retired — all segments can be verified and
    # reconciled. The `VipSyncMode` tag is still emitted on new segments for
    # backwards compat but no longer gates this endpoint.
    family = tags.get("VipFamily") or name
    legacy_warning: str | None = None

    # Prefer the authoritative filter stored in DDB so verify stays meaningful
    # after rebuilds replace segmentGroups with a static customerId list.
    config = build_filter_config_store().get(family)
    if config is not None:
        rules = list(config.rules)
        combinator = config.combinator
    else:
        # Legacy path: segments created before this table existed only have
        # their filter stored inside segmentGroups. For a never-rebuilt
        # segment this still works; for a rebuilt one it'll re-evaluate the
        # frozen customerid list (can't detect new matches). We flag that so
        # the UI can nudge the operator to recreate.
        translator = SegmentGroupsTranslator()
        rules, combinator = translator.aws_to_rules(
            definition.get("SegmentGroups") or {}
        )
        version_tag = tags.get("VipVersion", "1")
        legacy_warning = (
            "Legacy segment without persisted filter config. "
            + (
                "Recreate with the original filter to enable full drift detection."
                if version_tag != "1"
                else "Verify works this time; will keep working until first rebuild."
            )
        )

    if not rules:
        raise ValueError(
            "Segment has no evaluable filters — nothing to verify against Redis"
        )

    # 1. Ask CP for the current estimated count via the segment's own definition.
    estimate_response = cp.create_segment_estimate(name)
    estimate_info = cp.wait_for_estimate(estimate_response["EstimateId"])
    segment_count = _parse_estimate(estimate_info.get("Estimate"))

    # 2. Scan Redis and apply the same filters locally.
    redis_ids, redis_sample = _scan_redis(rules, combinator)

    missing = sorted(redis_ids)
    sample = _build_sample(missing, redis_sample)

    build_audit().record(
        entity_type="segment",
        entity_id=name,
        action="verify",
        actor_sub=caller.sub,
        actor_email=caller.email,
        ip_address=caller.ip_address,
        user_agent=caller.user_agent,
        extra={
            "redisCount": len(redis_ids),
            "segmentCount": segment_count,
        },
    )

    # Extras detection is its own async path now (POST /segments/{id}/verify/extras),
    # so we no longer need the `extrasDetectionDisabled` placeholder here.
    notes: dict[str, str] = {}
    if legacy_warning is not None:
        notes["legacyFilter"] = legacy_warning

    return json_response(
        200,
        {
            "segmentName": name,
            "family": family,
            "version": _version_from_tags(definition.get("Tags", {})),
            "redisCount": len(redis_ids),
            "segmentCount": segment_count,
            "missingCustomerIds": missing[:MAX_IDS_RETURNED],
            "extraCustomerIds": [],  # undetectable without snapshot support
            "sample": sample,
            "verifiedAt": _now_iso(),
            "notes": notes,
        },
    )


def _scan_redis(rules, combinator: str) -> tuple[set[str], dict[str, dict[str, Any]]]:
    """Return (matching customerIds, lookup index by id for sampling)."""
    source = build_redis_source()
    matching_ids: set[str] = set()
    sample_index: dict[str, dict[str, Any]] = {}

    for record in source.iter_records():
        if matches_group(record, rules, combinator):
            customer_id = str(record.get("customerid") or record.get("id") or "").strip()
            if not customer_id:
                continue
            matching_ids.add(customer_id)
            if len(sample_index) < SAMPLE_SIZE * 5:
                sample_index[customer_id] = record

    return matching_ids, sample_index


def _build_sample(
    customer_ids: list[str], redis_index: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for customer_id in customer_ids[:SAMPLE_SIZE]:
        record = redis_index.get(customer_id, {})
        out.append(
            {
                "customerId": customer_id,
                "phone": record.get("phone"),
                "name": _full_name(record),
                "status": "missing",  # "source of truth per Redis"
            }
        )
    return out


def _full_name(record: dict[str, Any]) -> str | None:
    parts = [record.get("first_name"), record.get("last_name")]
    joined = " ".join(p for p in parts if p).strip()
    return joined or None


def _parse_estimate(raw: Any) -> int | None:
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, dict):
        total = raw.get("TotalCount") or raw.get("totalCount")
        if isinstance(total, (int, float)):
            return int(total)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _sync_mode_from_tags(tags: dict[str, str]) -> str:
    return (tags or {}).get("VipSyncMode", "live").lower()


def _family_from_tags(tags: dict[str, str]) -> str | None:
    return (tags or {}).get("VipFamily")


def _version_from_tags(tags: dict[str, str]) -> int:
    try:
        return int((tags or {}).get("VipVersion", "1"))
    except (TypeError, ValueError):
        return 1


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
