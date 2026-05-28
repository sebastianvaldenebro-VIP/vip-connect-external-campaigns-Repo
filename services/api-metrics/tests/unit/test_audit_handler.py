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
