from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class FilterOperator(str, Enum):
    EQ = "eq"
    NEQ = "neq"
    IN = "in"
    NOT_IN = "not_in"
    STARTS_WITH = "starts_with"
    ENDS_WITH = "ends_with"
    CONTAINS = "contains"
    GTE = "gte"
    LTE = "lte"


@dataclass(frozen=True, slots=True)
class FilterRule:
    field: str
    operator: FilterOperator
    values: tuple[Any, ...]

    @staticmethod
    def from_dict(field: str, rule: dict[str, Any]) -> FilterRule:
        op = FilterOperator(rule["op"])
        values = tuple(rule.get("values", []))
        if not values:
            raise ValueError(f"FilterRule for field '{field}' must have at least one value")
        return FilterRule(field=field, operator=op, values=values)
