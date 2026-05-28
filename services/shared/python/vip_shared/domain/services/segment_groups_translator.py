"""Translate between Customer Profiles SegmentGroups and FilterRule.

Customer Profiles' ``SegmentGroups`` is a nested PascalCase structure with
multiple typed dimensions. Our FilterRule model is a flat list of
``(field, operator, values)`` triples. Going each direction lets us:

1. Evaluate a CP segment's filters locally against Redis leads (verify).
2. Build a synthetic ``customerid IN [list]`` segment when rebuilding
   a reconciled snapshot (reconcile).
"""

from __future__ import annotations

from typing import Any

from vip_shared.domain.entities.filter_rule import FilterOperator, FilterRule

# AWS DimensionType → our FilterOperator. Anything not in this map is
# unsupported for verify (we can still build segments with it, just can't
# evaluate locally against Redis).
_AWS_TO_OPERATOR: dict[str, FilterOperator] = {
    "EQUAL": FilterOperator.EQ,
    "NOT_EQUAL": FilterOperator.NEQ,
    "INCLUSIVE": FilterOperator.IN,
    "EXCLUSIVE": FilterOperator.NOT_IN,
    "BEGINS_WITH": FilterOperator.STARTS_WITH,
    "ENDS_WITH": FilterOperator.ENDS_WITH,
    "CONTAINS": FilterOperator.CONTAINS,
    "GREATER_THAN_OR_EQUAL": FilterOperator.GTE,
    "LESS_THAN_OR_EQUAL": FilterOperator.LTE,
}


class SegmentGroupsTranslator:
    """Convert between AWS SegmentGroups and our FilterRule list."""

    def aws_to_rules(
        self, segment_groups: dict[str, Any]
    ) -> tuple[list[FilterRule], str]:
        """Return (rules, combinator) from a CP ``SegmentGroups`` response.

        Assumes the simple single-group shape produced by this UI. For groups
        coming from the AWS console with multiple groups we only read the
        first — callers should validate.
        """
        groups = segment_groups.get("Groups") or segment_groups.get("groups") or []
        if not groups:
            return [], "ALL"

        group = groups[0]
        combinator = str(group.get("Type") or group.get("type") or "ALL").upper()
        dimensions = group.get("Dimensions") or group.get("dimensions") or []

        rules: list[FilterRule] = []
        for dim in dimensions:
            profile_attrs = (
                dim.get("ProfileAttributes") or dim.get("profileAttributes") or {}
            )
            attrs = (
                profile_attrs.get("Attributes") or profile_attrs.get("attributes") or {}
            )
            for field_name, attr_dim in attrs.items():
                rule = self._dimension_to_rule(field_name, attr_dim)
                if rule is not None:
                    rules.append(rule)

        return rules, combinator

    def customer_ids_to_segment_groups(
        self, customer_ids: list[str], *, field: str = "ID", chunk_size: int = 50
    ) -> dict[str, Any]:
        """Build a SegmentGroups definition that matches exactly the given IDs.

        AWS caps ``AttributeDimension.Values`` (docs list 50 as max). To include
        N > 50 customerIds we emit one dimension per chunk and combine them
        with ``Type=ANY`` so the group evaluates as an OR.
        """
        if not customer_ids:
            # Use an impossible ID value so CP builds a 0-member segment rather
            # than an empty-Dimensions one — CP interprets Dimensions:[] as
            # "match all profiles", which is the wrong behaviour for a 0-result
            # rebuild. Callers should guard against calling this with an empty
            # list (reconcile raises before reaching here), but this keeps the
            # method safe if that guard is ever bypassed.
            return {
                "Include": "ALL",
                "Groups": [
                    {
                        "Type": "ALL",
                        "Dimensions": [
                            {
                                "ProfileAttributes": {
                                    "Attributes": {
                                        field: {
                                            "DimensionType": "INCLUSIVE",
                                            "Values": ["__no_records__"],
                                        }
                                    }
                                }
                            }
                        ],
                    }
                ],
            }

        chunks = [
            customer_ids[i : i + chunk_size]
            for i in range(0, len(customer_ids), chunk_size)
        ]
        dimensions = [
            {
                "ProfileAttributes": {
                    "Attributes": {
                        field: {"DimensionType": "INCLUSIVE", "Values": chunk},
                    }
                }
            }
            for chunk in chunks
        ]
        return {
            "Include": "ALL",
            "Groups": [{"Type": "ANY", "Dimensions": dimensions}],
        }

    # ── internal helpers ──────────────────────────────────────────────

    @staticmethod
    def _dimension_to_rule(
        field_name: str, attr_dim: dict[str, Any]
    ) -> FilterRule | None:
        aws_op = attr_dim.get("DimensionType") or attr_dim.get("dimensionType")
        values = attr_dim.get("Values") or attr_dim.get("values") or []
        if not aws_op or not values:
            return None
        op = _AWS_TO_OPERATOR.get(str(aws_op).upper())
        if op is None:
            return None
        return FilterRule(field=field_name, operator=op, values=tuple(values))


def matches_group(
    record: dict[str, Any], rules: list[FilterRule], combinator: str
) -> bool:
    """Evaluate a record against rules with the given ALL/ANY combinator.

    Kept at module scope so callers can avoid depending on FilterEvaluator
    directly when the combinator is dynamic.
    """
    if not rules:
        return True  # empty segment definition matches everything
    from vip_shared.domain.services.filter_evaluator import FilterEvaluator

    evaluator = FilterEvaluator()
    if combinator.upper() == "ANY":
        return any(evaluator.matches(record, [rule]) for rule in rules)
    return evaluator.matches(record, rules)
