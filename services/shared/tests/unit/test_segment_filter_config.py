"""Tests for SegmentFilterConfigStore — DDB get/put/update semantics."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

from vip_shared.domain.entities.filter_rule import FilterOperator, FilterRule
from vip_shared.infrastructure.persistence.segment_filter_config import (
    SegmentFilterConfigStore,
)


def _store_with_table():
    table = MagicMock()
    resource = MagicMock()
    resource.Table.return_value = table
    return (
        SegmentFilterConfigStore(table_name="T", dynamodb_resource=resource),
        table,
    )


def test_put_writes_rules_as_json_string():
    store, table = _store_with_table()
    store.put(
        family="nj-available-leads",
        rules=[
            FilterRule(field="location", operator=FilterOperator.IN, values=("NJ",)),
        ],
        combinator="ALL",
        sync_mode="manual",
        created_by="user@medwork.io",
    )
    item = table.put_item.call_args.kwargs["Item"]
    assert item["family"] == "nj-available-leads"
    assert item["combinator"] == "ALL"
    assert item["sync_mode"] == "manual"
    assert item["current_version"] == 1
    assert item["created_by"] == "user@medwork.io"
    # rules are serialised as JSON so DDB doesn't have to fight the operator enum.
    parsed = json.loads(item["filter_rules"])
    assert parsed == [{"field": "location", "operator": "in", "values": ["NJ"]}]


def test_get_roundtrips_rules():
    store, table = _store_with_table()
    table.get_item.return_value = {
        "Item": {
            "family": "nj",
            "filter_rules": json.dumps(
                [{"field": "available", "operator": "eq", "values": ["1"]}]
            ),
            "combinator": "ALL",
            "sync_mode": "manual",
            "current_version": 2,
            "created_at": "2026-04-24T12:00:00Z",
        }
    }
    config = store.get("nj")
    assert config is not None
    assert config.family == "nj"
    assert config.combinator == "ALL"
    assert config.current_version == 2
    assert len(config.rules) == 1
    assert config.rules[0].field == "available"
    assert config.rules[0].operator is FilterOperator.EQ
    assert config.rules[0].values == ("1",)


def test_get_returns_none_for_missing_family():
    store, table = _store_with_table()
    table.get_item.return_value = {}
    assert store.get("does-not-exist") is None


def test_mark_rebuilt_bumps_version_and_timestamps():
    store, table = _store_with_table()
    store.mark_rebuilt(family="nj", new_version=4, rebuilt_by="u@x.com")
    kwargs = table.update_item.call_args.kwargs
    assert kwargs["Key"] == {"family": "nj"}
    # Encoded values carry the new version and the "by" field.
    values = kwargs["ExpressionAttributeValues"]
    assert values[":v"] == 4
    assert values[":by"] == "u@x.com"
    assert ":ts" in values


def test_delete_removes_row():
    store, table = _store_with_table()
    store.delete("nj")
    table.delete_item.assert_called_once_with(Key={"family": "nj"})
