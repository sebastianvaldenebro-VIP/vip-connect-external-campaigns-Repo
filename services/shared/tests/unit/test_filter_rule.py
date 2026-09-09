"""Tests for the FilterRule entity and its from_dict factory."""

from __future__ import annotations

import pytest

from vip_shared.domain.entities.filter_rule import FilterOperator, FilterRule


def test_from_dict_builds_rule_with_operator_and_values():
    rule = FilterRule.from_dict("location", {"op": "starts_with", "values": ["NJ -"]})

    assert rule.field == "location"
    assert rule.operator is FilterOperator.STARTS_WITH
    assert rule.values == ("NJ -",)


def test_from_dict_raises_when_values_missing():
    with pytest.raises(ValueError, match="location"):
        FilterRule.from_dict("location", {"op": "eq"})


def test_from_dict_raises_when_values_empty():
    with pytest.raises(ValueError, match="at least one value"):
        FilterRule.from_dict("location", {"op": "eq", "values": []})


def test_from_dict_raises_on_unknown_operator():
    with pytest.raises(ValueError):
        FilterRule.from_dict("location", {"op": "not_a_real_op", "values": ["x"]})
