"""Redis-backed helpers for the segment create form.

These endpoints exist so the UI can populate dropdowns with values actually
present in the leads list (e.g. the distinct `attempt` counts operators have
used) and preview a segment count before committing to create it.

Both handlers live in api-segments because that Lambda already has Redis
connectivity set up (VPC + SG) and the right Redis env vars.
"""
from __future__ import annotations

import json

from vip_shared.application.http import json_response, parse_body
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

# Cap the size of dropdowns — 200 distinct values is more than enough for any
# attribute we'd want to filter on and keeps the response small.
DISTINCT_VALUES_CAP = 200

# Cap the number of customer IDs we return — the UI just needs the count.
PREVIEW_LIMIT = 2_500


def list_distinct_values(event: dict, _path_params: dict) -> dict:
    """GET /leads/distinct-values?field=<name>&max=<N>.

    Scans Redis and returns the unique non-empty values seen for the given
    field. Meant for small-to-medium-cardinality fields like ``attempt`` where
    the real values live in operator-managed state.
    """
    qs = event.get("queryStringParameters") or {}
    field = qs.get("field")
    if not field:
        raise ValueError("Missing required query param: field")
    max_values = min(int(qs.get("max", DISTINCT_VALUES_CAP)), DISTINCT_VALUES_CAP)

    source = build_redis_source()
    seen: set[str] = set()
    for record in source.iter_records():
        value = record.get(field)
        if value is None or value == "":
            continue
        seen.add(str(value))
        if len(seen) >= max_values:
            break

    return json_response(
        200,
        {
            "field": field,
            "values": sorted(seen),
            "truncated": len(seen) >= max_values,
        },
    )


def preview_count(event: dict, _path_params: dict) -> dict:
    """POST /segments/preview-count body: {segmentGroups}.

    Returns the count a segment with these filters would have, computed two
    ways:
      - `redisCount`: local scan — trusted, fast (1-2s for 26k leads).
      - `segmentCount`: CreateSegmentEstimate on an ephemeral query
        (equivalent to what CP will return once the segment is materialised).
        Slower (5-30s) but surfaces CP's own view so the operator can see
        drift at create time.
    """
    body = parse_body(event)
    segment_groups = body.get("segmentGroups") or {}
    if not segment_groups.get("Groups") and not segment_groups.get("groups"):
        raise ValueError("Missing segmentGroups in request body")

    translator = SegmentGroupsTranslator()
    rules, combinator = translator.aws_to_rules(segment_groups)

    # 1. Redis count (our source of truth). Ignore filters we can't evaluate
    # locally — let CP handle those. For the current supported attributes this
    # covers the user flow end-to-end.
    redis_count = 0
    if rules:
        source = build_redis_source()
        for record in source.iter_records():
            if matches_group(record, rules, combinator):
                redis_count += 1

    # 2. CP estimate — pass the segmentGroups directly as a SegmentQuery.
    cp = build_cp()
    estimate_response = cp._client.create_segment_estimate(
        DomainName=cp._domain,
        SegmentQuery=_normalise_to_pascal_case(segment_groups),
    )
    estimate_info = cp.wait_for_estimate(
        estimate_response["EstimateId"], timeout_seconds=30
    )

    return json_response(
        200,
        {
            "redisCount": redis_count,
            "segmentCount": _parse_estimate(estimate_info.get("Estimate")),
        },
    )


def _parse_estimate(raw) -> int | None:
    """Normalise the estimate result to an int."""
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, dict):
        total = raw.get("TotalCount") or raw.get("totalCount")
        if isinstance(total, (int, float)):
            return int(total)
    if isinstance(raw, str):
        try:
            return int(json.loads(raw).get("totalCount", raw)) if raw.startswith(
                "{"
            ) else int(raw)
        except (ValueError, TypeError):
            return None
    return None


def _normalise_to_pascal_case(sg: dict) -> dict:
    """Accept camelCase or PascalCase input, always hand PascalCase to AWS."""
    groups = sg.get("Groups") or sg.get("groups") or []
    out_groups = []
    for group in groups:
        dims = group.get("Dimensions") or group.get("dimensions") or []
        out_dims = []
        for dim in dims:
            profile = (
                dim.get("ProfileAttributes") or dim.get("profileAttributes") or {}
            )
            attrs_in = profile.get("Attributes") or profile.get("attributes") or {}
            attrs_out: dict = {}
            for field, spec in attrs_in.items():
                attrs_out[field] = {
                    "DimensionType": spec.get("DimensionType")
                    or spec.get("dimensionType"),
                    "Values": spec.get("Values") or spec.get("values") or [],
                }
            out_dims.append({"ProfileAttributes": {"Attributes": attrs_out}})
        out_groups.append(
            {
                "Type": group.get("Type") or group.get("type") or "ALL",
                "Dimensions": out_dims,
            }
        )
    return {
        "Include": sg.get("Include") or sg.get("include") or "ALL",
        "Groups": out_groups,
    }
