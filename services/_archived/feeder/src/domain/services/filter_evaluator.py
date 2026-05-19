from __future__ import annotations

from collections.abc import Iterable

from ..entities.filter_rule import FilterOperator, FilterRule
from ..entities.lead import Lead


class FilterEvaluator:
    """Evaluates a set of filter rules against leads using AND semantics."""

    def matches(self, lead: Lead, rules: Iterable[FilterRule]) -> bool:
        return all(self._apply_rule(lead, rule) for rule in rules)

    def filter(self, leads: Iterable[Lead], rules: Iterable[FilterRule]) -> list[Lead]:
        rule_list = tuple(rules)
        if not rule_list:
            return list(leads)
        return [lead for lead in leads if self.matches(lead, rule_list)]

    def _apply_rule(self, lead: Lead, rule: FilterRule) -> bool:
        value = self._extract_value(lead, rule.field)
        op = rule.operator

        if op is FilterOperator.EQ:
            return value == rule.values[0]
        if op is FilterOperator.NEQ:
            return value != rule.values[0]
        if op is FilterOperator.IN:
            return value in rule.values
        if op is FilterOperator.NOT_IN:
            return value not in rule.values
        if op is FilterOperator.STARTS_WITH:
            return isinstance(value, str) and value.startswith(str(rule.values[0]))
        if op is FilterOperator.ENDS_WITH:
            return isinstance(value, str) and value.endswith(str(rule.values[0]))
        if op is FilterOperator.CONTAINS:
            return isinstance(value, str) and str(rule.values[0]) in value
        if op is FilterOperator.GTE:
            return self._compare(value, rule.values[0], lambda a, b: a >= b)
        if op is FilterOperator.LTE:
            return self._compare(value, rule.values[0], lambda a, b: a <= b)
        return False

    @staticmethod
    def _extract_value(lead: Lead, field_name: str):
        known = {
            "id": lead.id,
            "phone": lead.phone,
            "first_name": lead.first_name,
            "last_name": lead.last_name,
            "groups": lead.groups,
            "location": lead.location,
            "campaign": lead.campaign,
            "available": lead.available,
        }
        if field_name in known:
            return known[field_name]
        return lead.field(field_name)

    @staticmethod
    def _compare(left, right, op) -> bool:
        try:
            return op(left, right)
        except (TypeError, ValueError):
            return False
