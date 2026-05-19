"""Tests for AuditRecorder — verify 6-year TTL + shape."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from vip_shared.infrastructure.persistence.audit import (
    AUDIT_RETENTION_DAYS,
    AuditRecorder,
)


def test_record_creates_correct_shape():
    mock_table = MagicMock()
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    recorder = AuditRecorder(table_name="AdminAuditLog", dynamodb_resource=mock_resource)

    recorder.record(
        entity_type="segment",
        entity_id="nj-1st",
        action="create",
        actor_sub="user-1",
        actor_email="u@x.com",
        before=None,
        after={"name": "nj-1st", "displayName": "NJ 1st"},
        ip_address="1.2.3.4",
        user_agent="test",
    )

    mock_table.put_item.assert_called_once()
    item = mock_table.put_item.call_args.kwargs["Item"]

    assert item["entity_id"] == "segment/nj-1st"
    assert item["entity_type"] == "segment"
    assert item["resource_id"] == "nj-1st"
    assert item["action"] == "create"
    assert item["actor_sub"] == "user-1"
    assert item["actor_email"] == "u@x.com"
    assert item["ip_address"] == "1.2.3.4"
    assert item["user_agent"] == "test"
    assert "before" not in item  # None wasn't serialized
    assert item["after"]  # was serialized as JSON string


def test_record_sets_6year_ttl():
    mock_table = MagicMock()
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    recorder = AuditRecorder(table_name="AdminAuditLog", dynamodb_resource=mock_resource)

    recorder.record(
        entity_type="campaign",
        entity_id="camp-1",
        action="start",
        actor_sub="u",
        actor_email="u@x.com",
    )

    item = mock_table.put_item.call_args.kwargs["Item"]

    # TTL should be 6 years in the future (±1 hour slack)
    ttl = item["ttl"]
    now = datetime.now(tz=timezone.utc)
    expected_min = now + timedelta(days=AUDIT_RETENTION_DAYS - 1)
    expected_max = now + timedelta(days=AUDIT_RETENTION_DAYS + 1)

    ttl_dt = datetime.fromtimestamp(ttl, tz=timezone.utc)
    assert expected_min <= ttl_dt <= expected_max


def test_record_timestamp_is_iso_8601_z():
    mock_table = MagicMock()
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    recorder = AuditRecorder(table_name="AdminAuditLog", dynamodb_resource=mock_resource)

    recorder.record(
        entity_type="segment",
        entity_id="x",
        action="delete",
        actor_sub="u",
        actor_email="u@x.com",
    )

    item = mock_table.put_item.call_args.kwargs["Item"]
    ts = item["timestamp"]

    assert ts.endswith("Z")
    # Must parse as ISO 8601
    parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None


def test_optional_fields_omitted_when_none():
    mock_table = MagicMock()
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    recorder = AuditRecorder(table_name="AdminAuditLog", dynamodb_resource=mock_resource)

    recorder.record(
        entity_type="segment",
        entity_id="x",
        action="create",
        actor_sub="u",
        actor_email="u@x.com",
    )

    item = mock_table.put_item.call_args.kwargs["Item"]
    assert "before" not in item
    assert "after" not in item
    assert "ip_address" not in item
    assert "user_agent" not in item
    assert "extra" not in item
