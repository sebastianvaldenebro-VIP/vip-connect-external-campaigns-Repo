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

import re
from datetime import datetime, timezone
from typing import Any

import boto3

# ── State → location values (mirrors stateLocationMap.ts) ────────────────────

_STATE_LOCATIONS: dict[str, list[str]] = {
    "SCA": [
        "CA - Arcadia",
        "CA - Encino",
        "CA - Huntington Beach",
        "CA - Irvine",
        "CA - Long Beach",
        "CA - National City",
        "CA - Newport Beach",
        "CA - Poway",
        "CA - San Diego",
        "CA - Temecula",
        "CA - Torrance",
        "California",
    ],
    "NCA": ["CA - Palo Alto", "CA - Sacramento", "CA - San Jose"],
    "CT": ["Connecticut", "CT - Farmington", "CT - Hamden", "CT - Stamford"],
    "MD": ["DC", "Maryland", "MD - Bethesda", "MD - Bowie", "MD - Maple Lawn Office"],
    "NJ": [
        "New Jersey",
        "NJ - Clifton",
        "NJ - Edgewater",
        "NJ - Harrison Office",
        "NJ - Hoboken",
        "NJ - Marlton",
        "NJ - Morris County Office",
        "NJ - Morristown",
        "NJ - Paramus",
        "NJ - Princeton",
        "NJ - Scotch Plains",
        "NJ - West Orange Office",
        "NJ - West Orange Office (NEW)",
        "NJ - Woodbridge",
        "NJ - Woodland Park Office",
    ],
    "NY": [
        "New York",
        "NY - Brighton Beach",
        "NY - Bronx",
        "NY - Forest Hills",
        "NY - Hartsdale",
        "NY - Upper East Side",
        "NY - Yonkers",
        "NYC - Astoria",
        "NYC - Bronx",
        "NYC - Brooklyn - Williamsburg",
        "NYC - Downtown Brooklyn",
        "NYC - FiDi Manhattan",
        "NYC - Midtown Manhattan",
        "NYC - Staten Island",
        "NYC - Williamsburg",
    ],
    "LI": [
        "Long Island",
        "NY - LI Hampton Bays",
        "NY - LI Jericho",
        "NY - LI Port Jefferson",
        "NY - LI Rockville",
        "NY - LI West Islip",
    ],
    "TX": [
        "Texas",
        "TX - Addison",
        "TX - Arlington",
        "TX - Cedar Park",
        "TX - Cibolo Creek",
        "TX - Dallas - Addison",
        "TX - Flower Mound",
        "TX - Fort Worth",
        "TX - Kyle",
        "TX - Medical Center",
        "TX - Spring Branch",
        "TX - Sugar Land",
    ],
}


def locations_for_state_codes(codes: list[str]) -> list[str]:
    out: list[str] = []
    for code in codes:
        out.extend(_STATE_LOCATIONS.get(code, []))
    return out


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
    """Return the ARN of the canonical journey flow (Test-Journey-Flow).

    Looks for a CAMPAIGN-type flow named exactly ``Test-Journey-Flow``.
    Returns None if not found (caller's fail-fast guard fires).
    Unlike campaign flows, this one is never auto-created — it must exist in Connect.
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

    match = next((f for f in flows if f["Name"] == _JOURNEY_FLOW_NAME), None)
    if match:
        return match["Arn"]

    logger.error(
        "journey flow '%s' not found in Connect instance %s",
        _JOURNEY_FLOW_NAME,
        connect_instance_id,
    )
    return None


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
