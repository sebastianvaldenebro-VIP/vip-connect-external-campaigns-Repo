"""Tests for the refactored, record-agnostic FilterEvaluator."""
from __future__ import annotations

from vip_shared.domain.entities.filter_rule import FilterOperator, FilterRule
from vip_shared.domain.services.filter_evaluator import FilterEvaluator


def _record(**overrides) -> dict:
    defaults = {
        "id": "6ae814a6",
        "phone": "+12017801027",
        "first_name": "patrina",
        "last_name": "crawford",
        "groups": "New Lead / New Lead",
        "location": "NJ - Hoboken",
        "available": True,
    }
    defaults.update(overrides)
    return defaults


def test_eq_match():
    rule = FilterRule("available", FilterOperator.EQ, (True,))
    assert FilterEvaluator().matches(_record(), [rule]) is True


def test_eq_mismatch():
    rule = FilterRule("available", FilterOperator.EQ, (False,))
    assert FilterEvaluator().matches(_record(available=True), [rule]) is False


def test_starts_with_match():
    rule = FilterRule("location", FilterOperator.STARTS_WITH, ("NJ -",))
    assert FilterEvaluator().matches(_record(), [rule]) is True


def test_contains_match():
    rule = FilterRule("location", FilterOperator.CONTAINS, ("Hoboken",))
    assert FilterEvaluator().matches(_record(), [rule]) is True


def test_in_match():
    rule = FilterRule(
        "groups",
        FilterOperator.IN,
        ("New Lead / New Lead", "New Lead / 1st Attempt"),
    )
    assert FilterEvaluator().matches(_record(), [rule]) is True


def test_not_in_match():
    rule = FilterRule(
        "groups",
        FilterOperator.NOT_IN,
        ("Not Interested",),
    )
    assert FilterEvaluator().matches(_record(), [rule]) is True


def test_and_semantics_excludes_when_one_fails():
    rules = [
        FilterRule("available", FilterOperator.EQ, (True,)),
        FilterRule("groups", FilterOperator.EQ, ("4th Attempt",)),
    ]
    assert FilterEvaluator().matches(_record(), rules) is False


def test_filter_returns_only_matching():
    rules = [FilterRule("location", FilterOperator.STARTS_WITH, ("NJ -",))]
    records = [
        _record(location="NJ - Hoboken"),
        _record(location="NY - Midtown"),
        _record(location="NJ - Morristown"),
    ]
    result = FilterEvaluator().filter(records, rules)
    assert len(result) == 2
    assert all(r["location"].startswith("NJ -") for r in result)


def test_empty_rules_returns_all():
    records = [_record(), _record(id="b")]
    result = FilterEvaluator().filter(records, [])
    assert len(result) == 2


def test_works_with_any_mapping_like_object():
    """Confirms record is treated as a dict — not coupled to Lead entity."""
    rule = FilterRule("custom_field", FilterOperator.EQ, ("abc",))
    assert FilterEvaluator().matches({"custom_field": "abc"}, [rule]) is True
