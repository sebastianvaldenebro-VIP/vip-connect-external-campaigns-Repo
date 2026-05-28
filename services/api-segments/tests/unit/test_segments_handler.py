"""Tests for segments CRUD handler — with mocked dependencies."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PROFILES_DOMAIN_NAME", "amazon-connect-vipmedicalgroup")
    monkeypatch.setenv("AUDIT_TABLE", "AdminAuditLog")
    monkeypatch.setenv("DATA_KEY_ARN", "arn:aws:kms:us-east-1:123:key/abc")
    monkeypatch.setenv("SNAPSHOT_BUCKET", "vip-admin-segment-snapshots-123")
    monkeypatch.setenv("SNAPSHOT_ROLE_ARN", "arn:aws:iam::123:role/snapshot")
    monkeypatch.setenv("SEGMENT_FILTER_CONFIG_TABLE", "VipAdminSegmentFilterConfig")
    monkeypatch.setenv("AWS_REGION", "us-east-1")


def _no_config_store():
    store = MagicMock()
    store.get.return_value = None
    return store


def _caller_event(body: dict | None = None) -> dict:
    return {
        "body": json.dumps(body) if body else None,
        "requestContext": {
            "authorizer": {
                "jwt": {"claims": {"sub": "user-1", "email": "user@medwork.io"}}
            },
            "http": {"sourceIp": "1.2.3.4", "userAgent": "test"},
        },
    }


def test_list_segments_returns_normalized_shape():
    from handlers import segments

    mock_cp = MagicMock()
    mock_cp.list_segment_definitions.return_value = {
        "Items": [
            {
                "SegmentDefinitionName": "NJ-1st",
                "DisplayName": "NJ 1st",
                "Description": "NJ 1st attempt",
                "SegmentDefinitionArn": "arn:aws:profile:...NJ-1st",
                "CreatedAt": "2026-04-01T00:00:00Z",
                "Tags": {"env": "prod"},
            }
        ],
        "NextToken": None,
    }

    with patch("handlers.segments.build_cp", return_value=mock_cp):
        response = segments.list_segments({"queryStringParameters": None}, {})

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert len(body["segments"]) == 1
    assert body["segments"][0]["name"] == "NJ-1st"
    assert body["segments"][0]["displayName"] == "NJ 1st"


def test_create_segment_calls_cp_and_audit():
    from handlers import segments

    mock_cp = MagicMock()
    mock_cp.create_segment_definition.return_value = {
        "SegmentDefinitionName": "new-seg",
        "DisplayName": "New segment",
        "SegmentDefinitionArn": "arn:aws:profile:...new-seg",
        "CreatedAt": "2026-04-23T00:00:00Z",
    }
    mock_audit = MagicMock()

    event = _caller_event(
        {
            "name": "new-seg",
            "displayName": "New segment",
            "segmentGroups": {"Groups": []},
        }
    )

    with (
        patch("handlers.segments.build_cp", return_value=mock_cp),
        patch("handlers.segments.build_audit", return_value=mock_audit),
        patch(
            "handlers.segments.build_filter_config_store",
            return_value=_no_config_store(),
        ),
    ):
        response = segments.create_segment(event, {})

    assert response["statusCode"] == 201
    mock_cp.create_segment_definition.assert_called_once()
    mock_audit.record.assert_called_once()

    audit_call = mock_audit.record.call_args.kwargs
    assert audit_call["entity_type"] == "segment"
    assert audit_call["entity_id"] == "new-seg"
    assert audit_call["action"] == "create"
    assert audit_call["actor_sub"] == "user-1"
    assert audit_call["actor_email"] == "user@medwork.io"


def test_create_segment_rejects_missing_fields():
    from handlers import segments

    event = _caller_event({"name": "incomplete"})

    with pytest.raises(ValueError, match="Missing required fields"):
        segments.create_segment(event, {})


def test_delete_segment_captures_before_state():
    from handlers import segments

    mock_cp = MagicMock()
    mock_cp.get_segment_definition.return_value = {
        "SegmentDefinitionName": "old-seg",
        "DisplayName": "Old segment",
        "SegmentGroups": {"Groups": []},
    }
    mock_audit = MagicMock()

    with (
        patch("handlers.segments.build_cp", return_value=mock_cp),
        patch("handlers.segments.build_audit", return_value=mock_audit),
        patch(
            "handlers.segments.build_filter_config_store",
            return_value=_no_config_store(),
        ),
    ):
        response = segments.delete_segment(_caller_event(), {"id": "old-seg"})

    assert response["statusCode"] == 204
    mock_cp.delete_segment_definition.assert_called_once_with("old-seg")
    audit_call = mock_audit.record.call_args.kwargs
    assert audit_call["action"] == "delete"
    assert audit_call["before"]["name"] == "old-seg"
