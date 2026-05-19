"""Tests for SegmentGroupsTranslator — locks both directions.

Verify uses aws_to_rules to evaluate local records against the same filters.
Reconcile uses customer_ids_to_segment_groups to build the static list segment.
Both directions share the same canonical FilterRule shape so the tests live
together.
"""
from __future__ import annotations

import pytest

from vip_shared.domain.entities.filter_rule import FilterOperator
from vip_shared.domain.services.segment_groups_translator import (
    SegmentGroupsTranslator,
    matches_group,
)


@pytest.fixture
def translator() -> SegmentGroupsTranslator:
    return SegmentGroupsTranslator()


class TestAwsToRules:
    def test_translates_single_attribute_dimension(self, translator):
        aws = {
            "Include": "ALL",
            "Groups": [
                {
                    "Type": "ALL",
                    "Dimensions": [
                        {
                            "ProfileAttributes": {
                                "Attributes": {
                                    "location": {
                                        "DimensionType": "BEGINS_WITH",
                                        "Values": ["NJ -"],
                                    }
                                }
                            }
                        }
                    ],
                }
            ],
        }
        rules, combinator = translator.aws_to_rules(aws)
        assert combinator == "ALL"
        assert len(rules) == 1
        assert rules[0].field == "location"
        assert rules[0].operator is FilterOperator.STARTS_WITH
        assert rules[0].values == ("NJ -",)

    def test_translates_ANY_group_preserving_combinator(self, translator):
        aws = {
            "Groups": [
                {
                    "Type": "ANY",
                    "Dimensions": [
                        {
                            "ProfileAttributes": {
                                "Attributes": {
                                    "available": {
                                        "DimensionType": "EQUAL",
                                        "Values": [True],
                                    }
                                }
                            }
                        }
                    ],
                }
            ],
        }
        rules, combinator = translator.aws_to_rules(aws)
        assert combinator == "ANY"

    def test_maps_INCLUSIVE_and_EXCLUSIVE_correctly(self, translator):
        aws = {
            "Groups": [
                {
                    "Type": "ALL",
                    "Dimensions": [
                        {
                            "ProfileAttributes": {
                                "Attributes": {
                                    "customerid": {
                                        "DimensionType": "INCLUSIVE",
                                        "Values": ["a", "b"],
                                    }
                                }
                            }
                        },
                        {
                            "ProfileAttributes": {
                                "Attributes": {
                                    "campaign": {
                                        "DimensionType": "EXCLUSIVE",
                                        "Values": ["legacy"],
                                    }
                                }
                            }
                        },
                    ],
                }
            ],
        }
        rules, _ = translator.aws_to_rules(aws)
        ops = {r.field: r.operator for r in rules}
        assert ops == {
            "customerid": FilterOperator.IN,
            "campaign": FilterOperator.NOT_IN,
        }

    def test_skips_unknown_dimension_types(self, translator):
        aws = {
            "Groups": [
                {
                    "Type": "ALL",
                    "Dimensions": [
                        {
                            "ProfileAttributes": {
                                "Attributes": {
                                    "birthDate": {
                                        "DimensionType": "BEFORE",
                                        "Values": ["2000-01-01"],
                                    },
                                    "location": {
                                        "DimensionType": "EQUAL",
                                        "Values": ["NJ"],
                                    },
                                }
                            }
                        }
                    ],
                }
            ],
        }
        rules, _ = translator.aws_to_rules(aws)
        assert [r.field for r in rules] == ["location"]

    def test_handles_lowercase_camel_keys(self, translator):
        """Frontend-posted segmentGroups use camelCase; backend must still parse."""
        aws = {
            "groups": [
                {
                    "type": "ALL",
                    "dimensions": [
                        {
                            "profileAttributes": {
                                "attributes": {
                                    "available": {
                                        "dimensionType": "EQUAL",
                                        "values": ["true"],
                                    }
                                }
                            }
                        }
                    ],
                }
            ],
        }
        rules, combinator = translator.aws_to_rules(aws)
        assert combinator == "ALL"
        assert rules[0].field == "available"

    def test_empty_groups_returns_empty(self, translator):
        rules, combinator = translator.aws_to_rules({})
        assert rules == []
        assert combinator == "ALL"


class TestCustomerIdsToSegmentGroups:
    def test_single_chunk_when_under_cap(self, translator):
        ids = [f"cust-{i}" for i in range(10)]
        groups = translator.customer_ids_to_segment_groups(ids)
        dimensions = groups["Groups"][0]["Dimensions"]
        assert len(dimensions) == 1
        attrs = dimensions[0]["ProfileAttributes"]["Attributes"]
        # Default field is "ID" (matches CP Attributes.ID casing).
        assert attrs["ID"]["DimensionType"] == "INCLUSIVE"
        assert attrs["ID"]["Values"] == ids

    def test_partitions_into_chunks_of_50(self, translator):
        ids = [f"cust-{i}" for i in range(125)]
        groups = translator.customer_ids_to_segment_groups(ids)
        dimensions = groups["Groups"][0]["Dimensions"]
        # 125 / 50 → 3 dimensions (50, 50, 25)
        assert len(dimensions) == 3
        sizes = [
            len(d["ProfileAttributes"]["Attributes"]["ID"]["Values"])
            for d in dimensions
        ]
        assert sizes == [50, 50, 25]

    def test_uses_ANY_combinator_so_chunks_OR(self, translator):
        ids = [f"cust-{i}" for i in range(51)]
        groups = translator.customer_ids_to_segment_groups(ids)
        assert groups["Groups"][0]["Type"] == "ANY"

    def test_empty_ids_produces_no_match_sentinel(self, translator):
        # Empty list must produce a non-empty Dimensions with an impossible
        # value rather than Dimensions:[] — CP interprets an empty group as
        # "match all profiles", which is dangerous for a 0-member rebuild.
        groups = translator.customer_ids_to_segment_groups([])
        dims = groups["Groups"][0]["Dimensions"]
        assert len(dims) == 1
        attrs = dims[0]["ProfileAttributes"]["Attributes"]
        field_spec = next(iter(attrs.values()))
        assert field_spec["Values"] == ["__no_records__"]


class TestMatchesGroup:
    def test_ALL_combinator_requires_every_rule(self):
        rules, _ = SegmentGroupsTranslator().aws_to_rules(
            {
                "Groups": [
                    {
                        "Type": "ALL",
                        "Dimensions": [
                            {
                                "ProfileAttributes": {
                                    "Attributes": {
                                        "location": {
                                            "DimensionType": "BEGINS_WITH",
                                            "Values": ["NJ"],
                                        },
                                        "available": {
                                            "DimensionType": "EQUAL",
                                            "Values": [True],
                                        },
                                    }
                                }
                            }
                        ],
                    }
                ]
            }
        )
        match = {"location": "NJ - Newark", "available": True}
        partial = {"location": "NJ - Newark", "available": False}
        assert matches_group(match, rules, "ALL") is True
        assert matches_group(partial, rules, "ALL") is False

    def test_ANY_combinator_passes_on_first_matching_rule(self):
        rules, _ = SegmentGroupsTranslator().aws_to_rules(
            {
                "Groups": [
                    {
                        "Type": "ANY",
                        "Dimensions": [
                            {
                                "ProfileAttributes": {
                                    "Attributes": {
                                        "location": {
                                            "DimensionType": "BEGINS_WITH",
                                            "Values": ["NJ"],
                                        }
                                    }
                                }
                            },
                            {
                                "ProfileAttributes": {
                                    "Attributes": {
                                        "location": {
                                            "DimensionType": "BEGINS_WITH",
                                            "Values": ["FL"],
                                        }
                                    }
                                }
                            },
                        ],
                    }
                ]
            }
        )
        assert matches_group({"location": "FL - Miami"}, rules, "ANY") is True
        assert matches_group({"location": "TX - Austin"}, rules, "ANY") is False

    def test_empty_rules_matches_everything(self):
        assert matches_group({"any": "value"}, [], "ALL") is True
