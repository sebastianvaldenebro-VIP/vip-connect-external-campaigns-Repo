"""POST /segments/{id}/reconcile.

Rebuild a manual-sync segment to match the exact Redis truth:
  1. Scan Redis with the segment's filters — Redis is source of truth.
  2. Create ``{family}-v{N+1}`` with ``customerid IN [redis_ids]`` partitioned.
  3. Retarget campaigns that referenced the old segment ARN.
  4. Delete the old segment and record audit.

Note: snapshot diffing was removed in favour of a pure-rebuild approach because
``CreateSegmentSnapshot`` requires ``iam:PassRole`` which our boundary denies.
Removed-count therefore reports as 0; the rebuild still produces the correct
final membership because we replace the segment entirely with the Redis set.
"""

from __future__ import annotations

import re
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
from vip_shared.infrastructure.persistence.outbound_campaigns_client import (
    build as build_oc,
)
from vip_shared.infrastructure.persistence.redis_lead_source import (
    build_from_env as build_redis_source,
)
from vip_shared.infrastructure.persistence.segment_filter_config import (
    build_from_env as build_filter_config_store,
)

VERSION_SUFFIX_RE = re.compile(r"-v(\d+)$")


def reconcile_segment(event: dict, path_params: dict) -> dict:
    name = path_params["id"]
    caller = extract_caller(event)

    cp = build_cp()
    definition = cp.get_segment_definition(name)
    tags = definition.get("Tags") or {}
    # Manual/live distinction retired — every segment is reconcilable now.
    family = tags.get("VipFamily") or _family_from_name(name)
    current_version = _version_from_tags(tags, fallback_name=name)

    # Prefer the authoritative filter persisted at create-time; fall back to
    # the segment's own segmentGroups for legacy segments. Legacy segments
    # that have been rebuilt can't meaningfully reconcile — the stored
    # filter would be the frozen customerid list — so we refuse with a
    # clear message pointing to recreate.
    config_store = build_filter_config_store()
    config = config_store.get(family)
    if config is not None:
        rules = list(config.rules)
        combinator = config.combinator
    else:
        if current_version > 1:
            raise ValueError(
                "This segment was rebuilt before filter persistence was enabled. "
                "Its current segmentGroups is a static customerId list, not the "
                "original filter. Recreate the segment with the original filter "
                "to enable reconcile."
            )
        translator = SegmentGroupsTranslator()
        rules, combinator = translator.aws_to_rules(
            definition.get("SegmentGroups") or {}
        )

    if not rules:
        raise ValueError(
            "Segment has no evaluable filters — nothing to reconcile against Redis"
        )
    new_version = current_version + 1
    new_name = f"{family}-v{new_version}"
    display_name = definition.get("DisplayName") or name
    # Prefer the v1 description persisted in the filter config so the
    # original intent survives every rebuild — `definition.Description` only
    # carries whatever the *previous* version had, which itself may have
    # been a rebuilt-shaped placeholder. The config row is the canonical
    # source of "what was this segment originally for".
    description = (
        config.description if config is not None else None
    ) or definition.get("Description")

    # 1. Redis is source of truth. Pure rebuild — we don't diff against a CP
    # snapshot because CreateSegmentSnapshot requires iam:PassRole which is
    # blocked by the boundary.
    redis_ids = _collect_redis_ids(rules, combinator)

    # Guard: 0 matches means the filter has no leads right now. Proceeding
    # would create a segment with Dimensions:[] which CP evaluates as "match
    # all profiles" — far worse than keeping the current segment untouched.
    if len(redis_ids) == 0:
        filter_summary = " AND ".join(f"{r.field} IN {list(r.values)}" for r in rules)
        raise ValueError(
            f"No Redis records match the current filters ({filter_summary}). "
            "Reconcile is blocked to prevent creating an empty segment. "
            "Verify the filters match your data, or wait until matching leads "
            "exist in Redis before rebuilding."
        )

    # CP limits: 60 dimensions per segment × 50 values per AttributeDimension
    # = 3000 customerIds. Above that the CreateSegmentDefinition returns
    # "maximum limit of 60", so fail fast with a clear error so the caller
    # knows to narrow the filter instead of retrying blindly.
    MAX_REBUILD_MEMBERS = 3000
    if len(redis_ids) > MAX_REBUILD_MEMBERS:
        raise ValueError(
            f"Cannot rebuild: segment would hold {len(redis_ids):,} members, "
            f"over the CP hard limit of {MAX_REBUILD_MEMBERS:,}. "
            "Narrow the filter or split into smaller segments."
        )

    added = len(redis_ids)  # everything matching Redis is what the new segment holds
    removed = 0  # unknown without a snapshot; audit only reports adds

    # 2. Create v{N+1} with ID IN [redis_ids].
    # The CP object type maps Redis `_source.id` to `_profile.Attributes.ID`
    # (uppercase). Filtering by `customerid` would silently return 0 matches
    # because that attribute does not exist in the profile schema.
    new_segment_groups = SegmentGroupsTranslator().customer_ids_to_segment_groups(
        sorted(redis_ids), field="ID"
    )
    new_tags = _merged_tags(tags, family=family, version=new_version)
    create_response = cp.create_segment_definition(
        name=new_name,
        display_name=display_name,
        segment_groups=new_segment_groups,
        description=description,
        tags=new_tags,
    )
    new_arn = create_response["SegmentDefinitionArn"]
    old_arn = definition["SegmentDefinitionArn"]

    # 3-5: retarget campaigns, bump the config version, delete the old segment.
    # CP segment names are unique, so a failure anywhere in here that isn't rolled
    # back leaves {family}-v{new_version} permanently occupied by an orphan — every
    # future reconcile recomputes the exact same new_name (current_version is only
    # bumped by mark_rebuilt below, which hasn't run yet) and create_segment_definition
    # rejects it as a duplicate forever, with no code path that ever frees the name.
    oc = build_oc()
    campaigns_updated: list[str] = []
    try:
        _retarget_campaigns(
            oc=oc, old_arn=old_arn, new_arn=new_arn, updated=campaigns_updated
        )

        # 4. Bump the config version so future verify/reconcile calls line up with
        # the new segment name. Only persist if we already had a config row
        # (legacy segments stay legacy until recreated).
        if config is not None:
            config_store.mark_rebuilt(
                family=family,
                new_version=new_version,
                rebuilt_by=caller.email or caller.sub,
            )

        # 5. Delete the old segment (after retarget so campaigns don't break mid-flight).
        cp.delete_segment_definition(name)
    except Exception as exc:
        rollback_ok = True
        for campaign_id in campaigns_updated:
            try:
                oc.update_campaign_source(
                    campaign_id, {"customerProfilesSegmentArn": old_arn}
                )
            except Exception:  # noqa: BLE001
                rollback_ok = False
        try:
            cp.delete_segment_definition(new_name)
        except Exception:  # noqa: BLE001
            rollback_ok = False
        if rollback_ok:
            raise
        raise RuntimeError(
            f"reconcile failed and rollback was incomplete — segment {new_name} "
            f"and/or its retargeted campaigns may need manual cleanup: {exc}"
        ) from exc

    build_audit().record(
        entity_type="segment",
        entity_id=new_name,
        action="reconcile",
        actor_sub=caller.sub,
        actor_email=caller.email,
        ip_address=caller.ip_address,
        user_agent=caller.user_agent,
        before={"segmentName": name, "segmentArn": old_arn, "version": current_version},
        after={
            "segmentName": new_name,
            "segmentArn": new_arn,
            "version": new_version,
            "targetCount": len(redis_ids),
            "added": added,
            "removed": removed,
            "campaignsUpdated": campaigns_updated,
        },
    )

    return json_response(
        200,
        {
            "newSegmentName": new_name,
            "newSegmentArn": new_arn,
            "newVersion": new_version,
            "targetCount": len(redis_ids),
            "added": added,
            "removed": removed,
            "campaignsUpdated": campaigns_updated,
            "oldSegmentDeleted": True,
            "completedAt": _now_iso(),
        },
    )


def _collect_redis_ids(rules, combinator: str) -> set[str]:
    source = build_redis_source()
    matching: set[str] = set()
    for record in source.iter_records():
        if matches_group(record, rules, combinator):
            customer_id = str(
                record.get("customerid") or record.get("id") or ""
            ).strip()
            if customer_id:
                matching.add(customer_id)
    return matching


def _retarget_campaigns(*, oc, old_arn: str, new_arn: str, updated: list[str]) -> None:
    """Find campaigns using the old segment ARN and point them at the new one.

    Appends each successfully retargeted campaign id to `updated` in place, so a
    caller still sees partial progress (and can roll it back) if this raises partway.
    """
    next_token: str | None = None
    while True:
        page = oc.list_campaigns(max_results=100, next_token=next_token)
        for summary in page.get("campaignSummaryList", []):
            source = summary.get("source") or {}
            if source.get("customerProfilesSegmentArn") == old_arn:
                oc.update_campaign_source(
                    summary["id"], {"customerProfilesSegmentArn": new_arn}
                )
                updated.append(summary["id"])
        next_token = page.get("nextToken")
        if not next_token:
            break


def _merged_tags(
    original: dict[str, str], *, family: str, version: int
) -> dict[str, str]:
    tags = dict(original or {})
    tags["VipFamily"] = family
    tags["VipVersion"] = str(version)
    tags["VipSyncMode"] = "manual"
    return tags


def _family_from_name(name: str) -> str:
    match = VERSION_SUFFIX_RE.search(name)
    return name[: match.start()] if match else name


def _version_from_tags(tags: dict[str, str], *, fallback_name: str) -> int:
    try:
        return int(tags.get("VipVersion", "0"))
    except (TypeError, ValueError):
        pass
    match = VERSION_SUFFIX_RE.search(fallback_name)
    return int(match.group(1)) if match else 1


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
