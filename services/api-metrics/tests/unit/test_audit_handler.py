"""Tests for audit log read handler."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AUDIT_TABLE", "AdminAuditLog")


def test_list_audit_scans_when_no_filter():
    from handlers import audit

    mock_table = MagicMock()
    mock_table.scan.return_value = {
        "Items": [
            {
                "entity_id": "segment/nj-1st",
                "entity_type": "segment",
                "resource_id": "nj-1st",
                "timestamp": "2026-04-22T14:00:00Z",
                "actor_sub": "user-1",
                "actor_email": "u@x.com",
                "action": "create",
            }
        ],
        "LastEvaluatedKey": None,
    }

    with patch("handlers.audit._table", return_value=mock_table):
        response = audit.list_audit_entries(
            {"queryStringParameters": {"limit": "50"}}, {}
        )

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["count"] == 1
    assert body["entries"][0]["action"] == "create"
    mock_table.scan.assert_called_once()


def test_list_audit_uses_gsi_when_actor_filter():
    from handlers import audit

    mock_table = MagicMock()
    mock_table.query.return_value = {"Items": [], "LastEvaluatedKey": None}

    with patch("handlers.audit._table", return_value=mock_table):
        audit.list_audit_entries({"queryStringParameters": {"actor": "user-1"}}, {})

    mock_table.query.assert_called_once()
    call_kwargs = mock_table.query.call_args.kwargs
    assert call_kwargs["IndexName"] == "GSI1_ByActor"


def test_list_audit_uses_gsi_when_action_filter():
    from handlers import audit

    mock_table = MagicMock()
    mock_table.query.return_value = {"Items": [], "LastEvaluatedKey": None}

    with patch("handlers.audit._table", return_value=mock_table):
        audit.list_audit_entries({"queryStringParameters": {"action": "create"}}, {})

    call_kwargs = mock_table.query.call_args.kwargs
    assert call_kwargs["IndexName"] == "GSI2_ByAction"


def test_list_audit_accepts_valid_next_token():
    from handlers import audit

    mock_table = MagicMock()
    mock_table.scan.return_value = {"Items": [], "LastEvaluatedKey": None}
    token = json.dumps({"entity_id": "segment/x", "timestamp": "2026-04-22T14:00:00Z"})

    with patch("handlers.audit._table", return_value=mock_table):
        response = audit.list_audit_entries(
            {"queryStringParameters": {"nextToken": token}}, {}
        )

    assert response["statusCode"] == 200
    call_kwargs = mock_table.scan.call_args.kwargs
    assert call_kwargs["ExclusiveStartKey"] == {
        "entity_id": "segment/x",
        "timestamp": "2026-04-22T14:00:00Z",
    }


def test_list_audit_rejects_non_dict_next_token():
    from handlers import audit

    mock_table = MagicMock()
    token = json.dumps(["not", "a", "dict"])

    with patch("handlers.audit._table", return_value=mock_table):
        response = audit.list_audit_entries(
            {"queryStringParameters": {"nextToken": token}}, {}
        )

    assert response["statusCode"] == 400
    body = json.loads(response["body"])
    assert body["error"]["code"] == "INVALID_TOKEN"
    mock_table.scan.assert_not_called()


def test_list_audit_rejects_next_token_with_disallowed_keys():
    from handlers import audit

    mock_table = MagicMock()
    token = json.dumps({"entity_id": "segment/x", "some_other_key": "x"})

    with patch("handlers.audit._table", return_value=mock_table):
        response = audit.list_audit_entries(
            {"queryStringParameters": {"nextToken": token}}, {}
        )

    assert response["statusCode"] == 400
    body = json.loads(response["body"])
    assert body["error"]["code"] == "INVALID_TOKEN"
    mock_table.scan.assert_not_called()


def test_list_audit_filters_by_entity_type_on_full_scan():
    from handlers import audit

    mock_table = MagicMock()
    mock_table.scan.return_value = {"Items": [], "LastEvaluatedKey": None}

    with patch("handlers.audit._table", return_value=mock_table):
        audit.list_audit_entries(
            {"queryStringParameters": {"entityType": "segment"}}, {}
        )

    call_kwargs = mock_table.scan.call_args.kwargs
    assert call_kwargs["FilterExpression"] == "entity_type = :et"
    assert call_kwargs["ExpressionAttributeValues"] == {":et": "segment"}


def test_table_builds_resource_from_env(monkeypatch):
    from handlers import audit

    monkeypatch.setenv("AUDIT_TABLE", "AdminAuditLog")
    fake_table = MagicMock()
    fake_resource = MagicMock()
    fake_resource.Table.return_value = fake_table

    with patch("boto3.resource", return_value=fake_resource) as mock_boto_resource:
        result = audit._table()

    mock_boto_resource.assert_called_once_with("dynamodb")
    fake_resource.Table.assert_called_once_with("AdminAuditLog")
    assert result is fake_table


def test_maybe_parse_json_returns_raw_string_on_malformed_json():
    from handlers import audit

    assert audit._maybe_parse_json("{not valid json") == "{not valid json"


def test_get_entity_history_queries_by_partition_key():
    from handlers import audit

    mock_table = MagicMock()
    mock_table.query.return_value = {
        "Items": [
            {
                "entity_id": "segment/x",
                "timestamp": "2026-04-22T14:00:00Z",
                "action": "create",
            }
        ]
    }

    with patch("handlers.audit._table", return_value=mock_table):
        response = audit.get_entity_history({}, {"entityId": "segment/x"})

    body = json.loads(response["body"])
    assert body["entityId"] == "segment/x"
    assert len(body["entries"]) == 1


def test_serialize_item_parses_json_fields():
    from handlers import audit

    raw = {
        "entity_id": "segment/x",
        "entity_type": "segment",
        "timestamp": "2026-04-22T14:00:00Z",
        "actor_sub": "u",
        "action": "update",
        "before": '{"name": "old"}',
        "after": '{"name": "new"}',
    }
    result = audit._serialize_item(raw)
    assert result["before"] == {"name": "old"}
    assert result["after"] == {"name": "new"}
