from domain.entities.filter_rule import FilterOperator, FilterRule
from domain.entities.lead import Lead
from domain.services.filter_evaluator import FilterEvaluator


def _lead(**overrides) -> Lead:
    defaults = {
        "id": "6ae814a6-94aa-4a5b-a69d-2eb6ee5dfb86",
        "phone": "2017801027",
        "first_name": "patrina",
        "last_name": "crawford",
        "groups": "New Lead / New Lead",
        "location": "NJ - Hoboken",
        "campaign": "New Jersey Vein",
        "available": True,
        "raw": {"id": "abc", "custom_field": "xyz"},
    }
    defaults.update(overrides)
    return Lead(**defaults)


def test_eq_match():
    rule = FilterRule("available", FilterOperator.EQ, (True,))
    assert FilterEvaluator().matches(_lead(), [rule]) is True


def test_eq_mismatch():
    rule = FilterRule("available", FilterOperator.EQ, (False,))
    assert FilterEvaluator().matches(_lead(available=True), [rule]) is False


def test_starts_with_match():
    rule = FilterRule("location", FilterOperator.STARTS_WITH, ("NJ -",))
    assert FilterEvaluator().matches(_lead(), [rule]) is True


def test_in_match():
    rule = FilterRule("groups", FilterOperator.IN, ("New Lead / New Lead", "New Lead / 1st Attempt"))
    assert FilterEvaluator().matches(_lead(), [rule]) is True


def test_and_semantics_excludes_when_one_fails():
    rules = [
        FilterRule("available", FilterOperator.EQ, (True,)),
        FilterRule("groups", FilterOperator.EQ, ("4th Attempt",)),
    ]
    assert FilterEvaluator().matches(_lead(groups="New Lead / New Lead"), rules) is False


def test_filter_returns_only_matching():
    rules = [FilterRule("location", FilterOperator.STARTS_WITH, ("NJ -",))]
    leads = [
        _lead(location="NJ - Hoboken"),
        _lead(location="NY - Midtown"),
        _lead(location="NJ - Morristown"),
    ]
    result = FilterEvaluator().filter(leads, rules)
    assert len(result) == 2
    assert all(l.location.startswith("NJ -") for l in result)
