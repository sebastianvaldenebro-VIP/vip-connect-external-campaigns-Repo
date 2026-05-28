"""Persistence for the authoritative filter definition of a manual-sync segment.

Why this table exists: once a manual-sync segment is rebuilt via the reconcile
endpoint, its ``SegmentGroups`` in Customer Profiles becomes a frozen
``ID INCLUSIVE [ids]`` list — the semantic filter that produced that list is
gone. Without it, follow-up verifies/rebuilds can't tell if new Redis leads
should be added to the segment, they can only confirm the existing members.

Storing the original ``filterRules`` + ``combinator`` here lets the verify
handler re-evaluate Redis against the operator's intent, not against the
current (post-rebuild) segment definition. Keyed by ``family`` (e.g.
``nj-available-leads``) which is stable across rebuilds even as the concrete
segment name cycles through ``-v1``, ``-v2``, ``-v3``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import boto3

from vip_shared.domain.entities.filter_rule import FilterOperator, FilterRule


@dataclass(frozen=True)
class SegmentFilterConfig:
    """In-memory view of a row in ``VipAdminSegmentFilterConfig``."""

    family: str
    rules: tuple[FilterRule, ...]
    combinator: str
    sync_mode: str
    current_version: int
    created_at: str
    created_by: str | None = None
    last_rebuilt_at: str | None = None
    last_rebuilt_by: str | None = None
    # Human-readable summary of the original (v1) intent behind this segment
    # family. Persisted at create-time and propagated to every rebuilt
    # version so the operator can still see what the segment was for after
    # several reconciles have rewritten its SegmentGroups into a frozen
    # customerId list.
    description: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


class SegmentFilterConfigStore:
    """Thin DDB wrapper with put/get/delete + version bump on rebuild."""

    def __init__(self, table_name: str, dynamodb_resource=None) -> None:
        self._table = (dynamodb_resource or boto3.resource("dynamodb")).Table(
            table_name
        )

    # ── reads ────────────────────────────────────────────────────

    def get(self, family: str) -> SegmentFilterConfig | None:
        response = self._table.get_item(Key={"family": family})
        item = response.get("Item")
        return _item_to_config(item) if item else None

    # ── writes ───────────────────────────────────────────────────

    def put(
        self,
        *,
        family: str,
        rules: list[FilterRule],
        combinator: str,
        sync_mode: str,
        created_by: str | None,
        current_version: int = 1,
        description: str | None = None,
    ) -> SegmentFilterConfig:
        now = _now_iso()
        item: dict[str, Any] = {
            "family": family,
            "filter_rules": json.dumps([_rule_to_dict(r) for r in rules]),
            "combinator": combinator,
            "sync_mode": sync_mode,
            "current_version": current_version,
            "created_at": now,
        }
        if created_by:
            item["created_by"] = created_by
        if description:
            item["description"] = description
        self._table.put_item(Item=item)
        return _item_to_config(item)

    def mark_rebuilt(
        self,
        *,
        family: str,
        new_version: int,
        rebuilt_by: str | None,
    ) -> None:
        update_expr = "SET current_version = :v, last_rebuilt_at = :ts" + (
            ", last_rebuilt_by = :by" if rebuilt_by else ""
        )
        values: dict[str, Any] = {":v": new_version, ":ts": _now_iso()}
        if rebuilt_by:
            values[":by"] = rebuilt_by
        self._table.update_item(
            Key={"family": family},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=values,
        )

    def delete(self, family: str) -> None:
        self._table.delete_item(Key={"family": family})


# ── helpers ────────────────────────────────────────────────────


def _rule_to_dict(rule: FilterRule) -> dict[str, Any]:
    return {
        "field": rule.field,
        "operator": rule.operator.value,
        "values": list(rule.values),
    }


def _rule_from_dict(raw: dict[str, Any]) -> FilterRule:
    return FilterRule(
        field=raw["field"],
        operator=FilterOperator(raw["operator"]),
        values=tuple(raw.get("values", [])),
    )


def _item_to_config(item: dict[str, Any]) -> SegmentFilterConfig:
    raw_rules = item.get("filter_rules", "[]")
    rule_list = json.loads(raw_rules) if isinstance(raw_rules, str) else raw_rules
    return SegmentFilterConfig(
        family=item["family"],
        rules=tuple(_rule_from_dict(r) for r in rule_list),
        combinator=item.get("combinator", "ALL"),
        sync_mode=item.get("sync_mode", "manual"),
        current_version=int(item.get("current_version", 1)),
        created_at=item.get("created_at", ""),
        created_by=item.get("created_by"),
        last_rebuilt_at=item.get("last_rebuilt_at"),
        last_rebuilt_by=item.get("last_rebuilt_by"),
        description=item.get("description"),
    )


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_from_env() -> SegmentFilterConfigStore:
    table = os.environ["SEGMENT_FILTER_CONFIG_TABLE"]
    return SegmentFilterConfigStore(table_name=table)
