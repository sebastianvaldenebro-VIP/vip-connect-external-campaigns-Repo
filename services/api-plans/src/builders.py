"""Build CP segment groups and Connect campaign params from bucket/campaign definitions.

Supports two schemas:
  - Legacy (v1): bucket has segmentFilters + campaignConfig (one campaign per bucket)
  - Current (v2): bucket has campaigns[] each with states/group/attempts; campaignConfig at bucket level

Mirrors the logic in:
  frontend/src/lib/segmentGroups.ts  — SegmentGroups construction
  frontend/src/pages/SegmentNew.tsx  — name building
  services/api-campaigns/src/builders.py — campaign params
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import boto3

# ── State → location values — loaded from DynamoDB VipLocationMapping ─────────
# Table PK: location (String). Each item also has stateCode, stateName, slug,
# stateSortOrder. Cached in-process for _CACHE_TTL seconds to avoid per-request
# scans while still picking up new locations without a Lambda deploy.

_LOCATION_TABLE = os.environ.get("LOCATION_MAPPING_TABLE", "VipLocationMapping")
_CACHE_TTL = 3600  # 1 hour

# Module-level cache — shared across warm Lambda invocations.
_cache_by_code: dict[str, list[str]] | None = None
_cache_groups: list[dict] | None = None
_cache_all_locations: frozenset[str] | None = None
_cache_ts: float = 0


def _load_location_mapping() -> tuple[dict[str, list[str]], list[dict], frozenset[str]]:
    """Scan VipLocationMapping and rebuild in-process caches.

    Returns (by_code, groups, all_locations_set).
    Thread-safety: Lambda is single-threaded per invocation; a concurrent
    warm-start race is harmless — worst case it scans twice.
    """
    global _cache_by_code, _cache_groups, _cache_all_locations, _cache_ts

    now = time.monotonic()
    if _cache_by_code is not None and (now - _cache_ts) < _CACHE_TTL:
        return _cache_by_code, _cache_groups, _cache_all_locations  # type: ignore[return-value]

    table = boto3.resource("dynamodb").Table(_LOCATION_TABLE)
    resp = table.scan()
    items: list[dict] = resp.get("Items", [])
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))

    by_code: dict[str, list[str]] = {}
    groups_map: dict[str, dict] = {}
    for item in items:
        code = item["stateCode"]
        loc = item["location"]
        if code not in by_code:
            by_code[code] = []
            groups_map[code] = {
                "state": item["stateName"],
                "slug": item["slug"],
                "code": code,
                "stateSortOrder": int(item.get("stateSortOrder", 99)),
                "locations": [],
            }
        by_code[code].append(loc)
        groups_map[code]["locations"].append(loc)

    groups = sorted(groups_map.values(), key=lambda g: g["stateSortOrder"])

    _cache_by_code = by_code
    _cache_groups = groups
    _cache_all_locations = frozenset(items[i]["location"] for i in range(len(items)))
    _cache_ts = now
    return _cache_by_code, _cache_groups, _cache_all_locations


def locations_for_state_codes(codes: list[str]) -> list[str]:
    by_code, _, _ = _load_location_mapping()
    out: list[str] = []
    for code in codes:
        out.extend(by_code.get(code, []))
    return out


def get_all_location_groups() -> list[dict]:
    """Return all state groups ordered by stateSortOrder (for the API endpoint)."""
    _, groups, _ = _load_location_mapping()
    # Strip internal-only stateSortOrder from the API response
    return [
        {k: v for k, v in g.items() if k != "stateSortOrder"}
        for g in groups
    ]


def all_known_locations() -> frozenset[str]:
    """Return the flat set of all known location strings (for unknown-location detection)."""
    _, _, known = _load_location_mapping()
    return known


# ── V2 campaign model → segment filter translator ─────────────────────────────

_GROUP_LABEL_MAP: dict[str, str] = {
    "New lead": "New Lead",
    "Cancellation": "Cancellation",
    "No show": "No Show",
    "Follow-up": "Follow Up",
    "Reschedule": "Reschedule",
}

_ORDINAL: dict[int, str] = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th"}


def campaign_to_segment_filters(campaign: dict) -> dict:
    """Translate a v2 Campaign into the legacy segmentFilters shape.

    Accepts both new-style (groups: [str]) and old-style (group + attempts: [int]).
    The legacy shape is what _bucket_filters_to_rules and build_segment_groups consume.
    """
    groups = campaign.get("groups")
    if not groups:
        # Backward compat: old plans stored group + attempts separately
        group_label = _GROUP_LABEL_MAP.get(
            campaign.get("group", ""), campaign.get("group", "")
        )
        attempts = campaign.get("attempts", [])
        groups = [
            f"{group_label} / {_ORDINAL[n]} Attempt"
            for n in sorted(attempts)
            if n in _ORDINAL
        ]
    return {
        "state": campaign.get("states", []),
        "groups": groups,
        "attempts": [],
        "available": campaign.get("available") or "True",
    }


# ── Segment name building (mirrors buildAutoName in SegmentNew.tsx) ───────────


def build_segment_name(bucket: dict, campaign: dict | None = None) -> str:
    """Build a unique segment name.

    When called with a v2 campaign dict, derives state/attempts from the campaign.
    When called with a legacy bucket dict (no campaign), falls back to bucket.segmentFilters.
    """
    now = datetime.now(timezone.utc)
    d = str(now.day)
    m = str(now.month)
    yy = str(now.year)[-2:]
    date_part = f"{d}-{m}-{yy}"

    if campaign is not None:
        filters = campaign_to_segment_filters(campaign)
    else:
        filters = bucket.get("segmentFilters", {})

    state_codes = filters.get("state", [])
    states_part = "_".join(state_codes) if state_codes else "all"

    groups = filters.get("groups", [])
    attempts = filters.get("attempts", [])
    attempts_part = build_attempts_part(groups + attempts) or "any"

    hh = str(now.hour).zfill(2)
    mn = str(now.minute).zfill(2)
    raw = f"{date_part}-{states_part}-{attempts_part}-{hh}{mn}"
    return _sanitize_segment_name(raw)


def build_attempts_part(attempts: list[str]) -> str:
    """Group attempt labels by type abbreviation.

    Examples:
      ["New Lead / 1st Attempt", "New Lead / 2nd Attempt"] → "NL_1-2"
      ["Cancellation / 3rd Attempt"] → "Can-3"
    """
    grouped: dict[str, list[str]] = {}
    for value in attempts:
        parts = [p.strip() for p in value.split("/") if p.strip()]
        number_match = re.search(r"\d+", value)
        number = number_match.group(0) if number_match else ""
        # Find the non-numeric part
        category = next((p for p in parts if not re.search(r"\d", p)), None)
        if not category and parts:
            category = re.sub(r"\b\w*\d+\w*\b", "", parts[0]).strip()
        abbr = _abbreviate(category or value)
        if not abbr:
            continue
        if abbr not in grouped:
            grouped[abbr] = []
        if number and number not in grouped[abbr]:
            grouped[abbr].append(number)

    if not grouped:
        return ""

    parts_out: list[str] = []
    for abbr, nums in grouped.items():
        if not nums:
            parts_out.append(abbr)
        elif len(nums) == 1:
            parts_out.append(f"{abbr}-{nums[0]}")
        else:
            parts_out.append(
                f"{abbr}_{'–'.join(nums)}" if False else f"{abbr}_{'-'.join(nums)}"
            )
    return "_".join(parts_out)


def _abbreviate(text: str) -> str:
    words = [w for w in re.split(r"[^a-zA-Z]+", text) if w]
    if len(words) >= 2:
        return "".join(w[0].upper() for w in words)[:4]
    if len(words) == 1:
        w = words[0]
        return w[0].upper() + w[1:3].lower()
    return "att"


def _sanitize_segment_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)


# ── CP SegmentGroups builder (mirrors buildSegmentGroups in segmentGroups.ts) ─


def build_segment_groups(bucket: dict) -> dict[str, Any]:
    filters = bucket.get("segmentFilters", {})
    dimensions: list[dict] = []

    state_codes = filters.get("state", [])
    locations = locations_for_state_codes(state_codes)
    if locations:
        dimensions.append(_dimension("location", "INCLUSIVE", locations))

    available = filters.get("available", "")
    if available in ("True", "False"):
        dimensions.append(_dimension("available", "INCLUSIVE", [available]))

    groups = filters.get("groups", [])
    if groups:
        dimensions.append(_dimension("groups", "INCLUSIVE", groups))

    return {
        "Include": "ALL",
        "Groups": [{"Type": "ALL", "Dimensions": dimensions}],
    }


def _dimension(field: str, dim_type: str, values: list[str]) -> dict:
    return {
        "ProfileAttributes": {
            "Attributes": {field: {"DimensionType": dim_type, "Values": values}}
        }
    }


# ── Campaign flow live resolver ───────────────────────────────────────────────

_CANONICAL_FLOW_CONTENT = (
    '{"Version":"2019-10-30","StartAction":"FirstMessageSend",'
    '"Actions":[{"Identifier":"FirstMessageSend","Type":"PutDialRequest",'
    '"Transitions":{"NextAction":"EndCampaignFlowId","Conditions":[],"Errors":[]},'
    '"Parameters":{}},{"Parameters":{},"Identifier":"EndCampaignFlowId",'
    '"Type":"EndFlowExecution","Transitions":{}}],"Metadata":{}}'
)


_JOURNEY_FLOW_NAME = "Test-Journey-Flow"


def resolve_journey_flow_arn(connect_instance_id: str) -> str | None:
    """Return the latest-version ARN of the canonical journey flow (Test-Journey-Flow).

    Journey campaigns require a versioned ARN (e.g. arn:...:contact-flow/{id}:{version}).
    The base ARN from list_contact_flows lacks the version suffix and Connect rejects it
    with ValidationException "due to missing version".

    Resolution order:
    1. JOURNEY_FLOW_ARN env var (set this if the Lambda role lacks ListContactFlowVersions)
    2. list_contact_flow_versions API (requires connect:ListContactFlowVersions IAM action)
    3. Falls back to base ARN and logs an error visible in CloudWatch

    Returns None if flow not found (caller's fail-fast guard fires).
    """
    import logging
    import os

    logger = logging.getLogger(__name__)

    # Fast path: operator-pinned versioned ARN via env var (avoids ListContactFlowVersions
    # IAM requirement while the CDK stack is updated to include that permission).
    pinned = os.environ.get("JOURNEY_FLOW_ARN", "").strip()
    if pinned:
        return pinned

    connect = boto3.client("connect")
    flows: list[dict] = []
    kwargs: dict = {"InstanceId": connect_instance_id, "ContactFlowTypes": ["CAMPAIGN"]}
    while True:
        resp = connect.list_contact_flows(**kwargs)
        flows.extend(resp.get("ContactFlowSummaryList", []))
        token = resp.get("NextToken")
        if not token:
            break
        kwargs["NextToken"] = token

    match = next((f for f in flows if f["Name"] == _JOURNEY_FLOW_NAME), None)
    if not match:
        logger.error(
            "journey flow '%s' not found in Connect instance %s",
            _JOURNEY_FLOW_NAME,
            connect_instance_id,
        )
        return None

    # Extract flow ID from the base ARN and fetch the latest published version.
    # Journey CreateCampaign requires a versioned ARN; the base ARN is rejected.
    base_arn = match["Arn"]
    flow_id = base_arn.rsplit("/", 1)[-1]
    try:
        versions: list[dict] = []
        ver_kwargs: dict = {"InstanceId": connect_instance_id, "ContactFlowId": flow_id}
        while True:
            ver_resp = connect.list_contact_flow_versions(**ver_kwargs)
            versions.extend(ver_resp.get("ContactFlowVersionSummaryList", []))
            token = ver_resp.get("NextToken")
            if not token:
                break
            ver_kwargs["NextToken"] = token

        if versions:
            latest = max(versions, key=lambda v: int(v.get("Version", 0)))
            return latest["Arn"]
    except Exception as exc:
        # Use StructuredLogger so this failure is visible in CloudWatch.
        # Common cause: Lambda role missing connect:ListContactFlowVersions —
        # set JOURNEY_FLOW_ARN env var with the full versioned ARN as a workaround.
        try:
            from vip_shared.infrastructure.telemetry.structured_logger import (
                StructuredLogger as _SL,
            )

            _SL(service="api-plans").error(
                "resolve_journey_flow_arn_versions_failed",
                flow_id=flow_id,
                error=str(exc),
                error_type=type(exc).__name__,
                hint="Set JOURNEY_FLOW_ARN env var or grant connect:ListContactFlowVersions",
            )
        except Exception:
            logger.error(
                "resolve_journey_flow_arn: failed to fetch versions for flow %s: %s",
                flow_id,
                exc,
            )

    return base_arn


def resolve_campaign_flow_arn(
    state_codes: list[str], connect_instance_id: str
) -> str | None:
    """Return the canonical campaign-flow ARN for the given states.

    Looks for a flow named exactly ``campaign-<STATE>`` in the live Connect instance.
    If none is found, auto-creates it so the caller never receives a stale or missing ARN.
    Returns None only if creation also fails (caller's fail-fast guard fires).
    """
    import logging

    logger = logging.getLogger(__name__)

    connect = boto3.client("connect")
    flows: list[dict] = []
    kwargs: dict = {"InstanceId": connect_instance_id, "ContactFlowTypes": ["CAMPAIGN"]}
    while True:
        resp = connect.list_contact_flows(**kwargs)
        flows.extend(resp.get("ContactFlowSummaryList", []))
        token = resp.get("NextToken")
        if not token:
            break
        kwargs["NextToken"] = token

    for code in state_codes:
        canonical_name = f"campaign-{code}"
        match = next((f for f in flows if f["Name"] == canonical_name), None)
        if match:
            return match["Arn"]

    # No canonical flow found for any state — auto-create for the first one.
    target_code = state_codes[0]
    canonical_name = f"campaign-{target_code}"
    try:
        resp = connect.create_contact_flow(
            InstanceId=connect_instance_id,
            Name=canonical_name,
            Type="CAMPAIGN",
            Content=_CANONICAL_FLOW_CONTENT,
        )
        flow_arn = resp["ContactFlowArn"]
        connect.tag_resource(
            resourceArn=flow_arn,
            tags={"do-not-delete": "true", "managed-by": "vip-plans"},
        )
        logger.info(
            "auto-created canonical campaign flow %s -> %s", canonical_name, flow_arn
        )
        return flow_arn
    except Exception as exc:
        logger.error("failed to auto-create campaign flow %s: %s", canonical_name, exc)
        # A concurrent caller may have created it between our list and our
        # create attempt (this function now has 2 real callers: the
        # sequential executor, and the Enable-Campaign modal via HTTP —
        # a genuine race, not hypothetical). Re-list once before giving up.
        try:
            retry_resp = connect.list_contact_flows(
                InstanceId=connect_instance_id, ContactFlowTypes=["CAMPAIGN"],
            )
            retry_match = next(
                (f for f in retry_resp.get("ContactFlowSummaryList", [])
                 if f["Name"] == canonical_name),
                None,
            )
            if retry_match:
                return retry_match["Arn"]
        except Exception as retry_exc:
            logger.error(
                "resolve_campaign_flow_arn: retry list_contact_flows also failed for %s: %s",
                canonical_name,
                retry_exc,
            )
        return None


# ── Connect campaign params builder ──────────────────────────────────────────


def build_campaign_params(
    bucket: dict,
    *,
    segment_arn: str,
    connect_instance_id: str,
    profiles_domain_arn: str,
    instance_arn: str | None = None,
    start_time: str,
    end_time: str,
    campaign_name: str,
    campaign_flow_arn_override: str | None = None,
    campaign: dict | None = None,
    delivery_type: str = "campaign",
) -> dict[str, Any]:
    """Build Connect outbound campaign creation params.

    ``campaign`` is an optional v2 Campaign dict. If provided its ``campaignConfig``
    (or the bucket-level ``campaignConfig``) is used for dialer settings.
    Campaign-level config fields override bucket-level when both present.

    ``delivery_type`` controls the Connect campaign type:
      - "campaign" (default) → MANAGED, uses campaign-<STATE> flow
      - "journey"            → JOURNEY, uses Test-Journey-Flow
    """
    cfg = dict(bucket.get("campaignConfig", {}))
    if campaign:
        cfg.update(campaign.get("campaignConfig", {}))
    dialer_type = cfg.get("dialerType", "progressive")

    channel_subtype_config = {
        "telephony": {
            "capacity": float(cfg.get("dialingCapacity", 1.0)),
            "connectQueueId": cfg["queueId"],
            "outboundMode": {
                dialer_type: {
                    "bandwidthAllocation": float(cfg.get("bandwidthAllocation", 1.0))
                }
            },
            "defaultOutboundConfig": {
                "connectContactFlowId": cfg["contactFlowId"],
                "connectSourcePhoneNumber": cfg["sourcePhoneNumber"],
                "answerMachineDetectionConfig": {
                    "enableAnswerMachineDetection": bool(cfg.get("amdEnabled", True)),
                    "awaitAnswerMachinePrompt": bool(cfg.get("amdAwaitPrompt", True)),
                },
                **(
                    {"ringTimeout": int(cfg["ringTimeout"])}
                    if cfg.get("ringTimeout")
                    else {}
                ),
            },
        }
    }

    params: dict[str, Any] = {
        "name": campaign_name,
        "connectInstanceId": connect_instance_id,
        "channelSubtypeConfig": channel_subtype_config,
        "source": {"customerProfilesSegmentArn": segment_arn},
        "schedule": {"startTime": start_time, "endTime": end_time},
        "communicationTimeConfig": {
            "localTimeZoneConfig": {"defaultTimeZone": "America/New_York"}
        },
    }

    if delivery_type == "journey":
        params["type"] = "JOURNEY"
        params["communicationLimitsOverride"] = {
            "allChannelSubtypes": {"communicationLimitsList": []},
            "instanceLimitsHandling": "OPT_IN",
        }

    flow_arn = campaign_flow_arn_override
    if flow_arn:
        params["connectCampaignFlowArn"] = flow_arn

    tags: dict[str, str] = {}
    if instance_arn:
        tags["owner"] = instance_arn
    params["tags"] = tags

    return params
