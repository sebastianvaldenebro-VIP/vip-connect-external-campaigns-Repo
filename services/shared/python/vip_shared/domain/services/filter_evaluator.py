from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from vip_shared.domain.entities.filter_rule import FilterOperator, FilterRule


class FilterEvaluator:
    """Evaluates a set of filter rules against a record using AND semantics.

    The record is any Mapping (dict-like). Field lookups use `record.get(field_name)`
    so this works transparently for Lead entities, Customer Profile attributes,
    or any domain object that exposes a dict interface.
    """

    def matches(self, record: Mapping[str, Any], rules: Iterable[FilterRule]) -> bool:
        return all(self._apply_rule(record, rule) for rule in rules)

    def filter(
        self, records: Iterable[Mapping[str, Any]], rules: Iterable[FilterRule]
    ) -> list[Mapping[str, Any]]:
        rule_list = tuple(rules)
        if not rule_list:
            return list(records)
        return [r for r in records if self.matches(r, rule_list)]

    def _apply_rule(self, record: Mapping[str, Any], rule: FilterRule) -> bool:
        value = record.get(rule.field)
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
    def _compare(left, right, op) -> bool:
        try:
            return op(left, right)
        except (TypeError, ValueError):
            return False
