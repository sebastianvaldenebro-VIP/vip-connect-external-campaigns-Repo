"""POST /segments/{id}/diagnose — CP segment staleness evidence.

Samples up to MAX_SAMPLE profiles from Redis that currently match the
segment's filter, then:
  1. Asks CP directly (GetSegmentMembership) whether each profile is a member.
  2. Fetches the CP attributes for the non-members via BatchGetProfile.
  3. Re-evaluates those CP attributes against the same filter rules.

Profiles where CP attributes satisfy the filter but CP segment membership
is False are *confirmed-stale*: CP has ingested the update, but the segment
re-evaluation has not run yet.

This produces self-contained, timestamped JSON evidence that can be sent
directly to AWS support without relying on any static profile ID.
"""

from __future__ import annotations

from datetime import datetime, timezone

from vip_shared.application.http import json_response
from vip_shared.domain.services.segment_groups_translator import (
    SegmentGroupsTranslator,
    matches_group,
)
from vip_shared.infrastructure.persistence.customer_profiles_client import (
    build_from_env as build_cp,
)
from vip_shared.infrastructure.persistence.redis_lead_source import (
    build_from_env as build_redis_source,
)
from vip_shared.infrastructure.persistence.segment_filter_config import (
    build_from_env as build_filter_config_store,
)

MAX_SAMPLE = 30
# GetSegmentMembership accepts max 100 IDs per call.
_MEMBERSHIP_BATCH = 100


def diagnose_staleness(event: dict, path_params: dict) -> dict:
    """POST /segments/{id}/diagnose"""
    name = path_params["id"]

    cp = build_cp()
    definition = cp.get_segment_definition(name)
    tags = definition.get("Tags") or {}
    family = tags.get("VipFamily") or name

    # ── 1. Resolve filter rules ──────────────────────────────────────────
    config = build_filter_config_store().get(family)
    if config is not None:
        rules = list(config.rules)
        combinator = config.combinator
    else:
        translator = SegmentGroupsTranslator()
        rules, combinator = translator.aws_to_rules(
            definition.get("SegmentGroups") or {}
        )

    if not rules:
        return json_response(
            422,
            {"error": "Segment has no evaluable filters — cannot diagnose"},
        )

    # ── 2. Sample IDs from Redis that match the filter ───────────────────
    sample_ids = _sample_redis_ids(rules, combinator, MAX_SAMPLE)
    if not sample_ids:
        return json_response(
            200,
            {
                "diagnosedAt": _now_iso(),
                "segmentName": name,
                "message": "No Redis profiles match the current filter — nothing to diagnose.",
                "sampledFromRedis": 0,
                "confirmedStaleCount": 0,
                "confirmedStale": [],
            },
        )

    # ── 3. Ask CP which of these are actual segment members ─────────────
    membership_map = _check_membership(cp, name, sample_ids)
    non_members = [pid for pid in sample_ids if not membership_map.get(pid, False)]

    if not non_members:
        return json_response(
            200,
            {
                "diagnosedAt": _now_iso(),
                "segmentName": name,
                "message": "All sampled Redis profiles are already segment members — no staleness detected.",
                "sampledFromRedis": len(sample_ids),
                "confirmedStaleCount": 0,
                "confirmedStale": [],
            },
        )

    # ── 4. Fetch CP attributes for non-members ───────────────────────────
    cp_profiles = _batch_get_profiles(cp, non_members)
    profile_map: dict[str, dict] = {p.get("ProfileId", ""): p for p in cp_profiles}

    # ── 5. Evaluate CP attributes against filter rules ───────────────────
    confirmed_stale = []
    cp_no_match = []

    for pid in non_members:
        profile = profile_map.get(pid)
        if profile is None:
            # CP returned no data for this ID — ingestion lag (different issue)
            continue

        cp_attrs = profile.get("Attributes") or {}
        last_updated = profile.get("LastUpdatedAt")

        # Normalise to the same record format the Redis evaluator uses.
        record = {k.lower(): v for k, v in cp_attrs.items()}
        record.setdefault("customerid", pid)

        cp_matches_filter = matches_group(record, rules, combinator)

        entry = {
            "customerId": pid,
            "cpLastUpdatedAt": str(last_updated) if last_updated else None,
            "cpAttributesMatchFilter": cp_matches_filter,
            "cpAttributes": cp_attrs,
            "isSegmentMember": False,
        }

        if cp_matches_filter:
            confirmed_stale.append(entry)
        else:
            cp_no_match.append(entry)

    message = (
        f"{len(confirmed_stale)} of {len(sample_ids)} sampled profiles have matching "
        f"CP attributes but are NOT segment members — segment membership is stale."
        if confirmed_stale
        else "No confirmed staleness in this sample. CP attributes do not satisfy the "
        "filter for any non-member (possible ingestion lag or attribute mismatch)."
    )

    return json_response(
        200,
        {
            "diagnosedAt": _now_iso(),
            "segmentName": name,
            "message": message,
            "sampledFromRedis": len(sample_ids),
            "nonMembersInSample": len(non_members),
            "confirmedStaleCount": len(confirmed_stale),
            "cpNoMatchCount": len(cp_no_match),
            "confirmedStale": confirmed_stale,
            "cpNoMatch": cp_no_match[:5],
        },
    )


# ── Helpers ───────────────────────────────────────────────────────────────


def _sample_redis_ids(rules, combinator: str, max_n: int) -> list[str]:
    source = build_redis_source()
    ids: list[str] = []
    for record in source.iter_records():
        if len(ids) >= max_n:
            break
        if matches_group(record, rules, combinator):
            cid = str(record.get("customerid") or record.get("id") or "").strip()
            if cid:
                ids.append(cid)
    return ids


def _check_membership(cp, segment_name: str, profile_ids: list[str]) -> dict[str, bool]:
    """Returns {profileId: isInSegment}. Handles batching."""
    result: dict[str, bool] = {}
    for i in range(0, len(profile_ids), _MEMBERSHIP_BATCH):
        batch = profile_ids[i : i + _MEMBERSHIP_BATCH]
        try:
            response = cp.get_segment_membership(name=segment_name, profile_ids=batch)
            for entry in response.get("Profiles", []):
                pid = entry.get("ProfileId", "")
                result[pid] = bool(entry.get("IsProfileInSegment", False))
        except Exception:
            # If the call fails for this batch, assume not members.
            for pid in batch:
                result.setdefault(pid, False)
    return result


def _batch_get_profiles(cp, profile_ids: list[str]) -> list[dict]:
    """BatchGetProfile, max 100 per call."""
    profiles: list[dict] = []
    for i in range(0, len(profile_ids), 100):
        batch = profile_ids[i : i + 100]
        try:
            response = cp.batch_get_profile(profile_ids=batch)
            profiles.extend(response.get("Profiles", []))
        except Exception:
            pass
    return profiles


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
