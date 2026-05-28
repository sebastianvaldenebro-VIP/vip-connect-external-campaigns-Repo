"""Tests for campaigns CRUD + lifecycle handlers."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CONNECT_INSTANCE_ID", "instance-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    monkeypatch.setenv("PROFILES_DOMAIN_NAME", "d")
    monkeypatch.setenv("AUDIT_TABLE", "AdminAuditLog")


def _event(body: dict | None = None) -> dict:
    return {
        "body": json.dumps(body) if body else None,
        "requestContext": {
            "authorizer": {"jwt": {"claims": {"sub": "user-1", "email": "u@x.com"}}},
            "http": {"sourceIp": "1.2.3.4"},
        },
    }


def test_list_campaigns_normalizes_summaries():
    from handlers import campaigns

    mock_oc = MagicMock()
    mock_oc.list_campaigns.return_value = {
        "campaignSummaryList": [
            {
                "id": "c-1",
                "arn": "arn:...",
                "name": "NJ 1st",
                "status": "Running",
                "schedule": {"startTime": "2026-04-22T14:00:00Z"},
                "channelSubtypes": ["telephony"],
            }
        ],
        "nextToken": None,
    }

    with patch("handlers.campaigns.build_oc", return_value=mock_oc):
        response = campaigns.list_campaigns({"queryStringParameters": None}, {})

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert len(body["campaigns"]) == 1
    assert body["campaigns"][0]["id"] == "c-1"
    assert body["campaigns"][0]["status"] == "Running"


def test_get_campaign_merges_describe_and_state():
    from handlers import campaigns

    mock_oc = MagicMock()
    mock_oc.describe_campaign.return_value = {"campaign": {"id": "c-1", "name": "x"}}
    mock_oc.get_campaign_state.return_value = {"state": "Running"}

    with patch("handlers.campaigns.build_oc", return_value=mock_oc):
        response = campaigns.get_campaign(_event(), {"id": "c-1"})

    body = json.loads(response["body"])
    assert body["campaign"]["id"] == "c-1"
    assert body["state"] == "Running"


def test_create_campaign_audits():
    from handlers import campaigns

    mock_oc = MagicMock()
    mock_oc.create_campaign.return_value = {"id": "new-c", "arn": "arn:new"}
    mock_audit = MagicMock()

    body = {
        "name": "test",
        "segmentArn": "arn:aws:profile:us-east-1:123:domains/d/segment-definitions/seg",
        "queueId": "q",
        "contactFlowId": "f",
        "campaignFlowArn": "arn:aws:connect:us-east-1:123:instance/i/contact-flow/cf",
        "sourcePhoneNumber": "+1",
        "dialer": {
            "type": "progressive",
            "bandwidthAllocation": 1.0,
            "dialingCapacity": 1.0,
        },
        "schedule": {
            "startTime": "2026-04-23T14:00:00Z",
            "endTime": "2026-04-23T22:00:00Z",
        },
    }

    with (
        patch("handlers.campaigns.build_oc", return_value=mock_oc),
        patch("handlers.campaigns.build_audit", return_value=mock_audit),
    ):
        response = campaigns.create_campaign(_event(body), {})

    assert response["statusCode"] == 201
    mock_oc.create_campaign.assert_called_once()
    mock_audit.record.assert_called_once()
    audit_call = mock_audit.record.call_args.kwargs
    assert audit_call["action"] == "create"
    assert audit_call["entity_type"] == "campaign"


def test_delete_campaign_stops_if_running():
    from handlers import campaigns

    mock_oc = MagicMock()
    mock_oc.get_campaign_state.return_value = {"state": "Running"}
    mock_audit = MagicMock()

    with (
        patch("handlers.campaigns.build_oc", return_value=mock_oc),
        patch("handlers.campaigns.build_audit", return_value=mock_audit),
    ):
        response = campaigns.delete_campaign(_event(), {"id": "c-1"})

    assert response["statusCode"] == 204
    mock_oc.stop_campaign.assert_called_once_with("c-1")
    mock_oc.delete_campaign.assert_called_once_with("c-1")
    mock_audit.record.assert_called_once()


def test_delete_campaign_skips_stop_if_not_running():
    from handlers import campaigns

    mock_oc = MagicMock()
    mock_oc.get_campaign_state.return_value = {"state": "Stopped"}
    mock_audit = MagicMock()

    with (
        patch("handlers.campaigns.build_oc", return_value=mock_oc),
        patch("handlers.campaigns.build_audit", return_value=mock_audit),
    ):
        campaigns.delete_campaign(_event(), {"id": "c-1"})

    mock_oc.stop_campaign.assert_not_called()
    mock_oc.delete_campaign.assert_called_once()


def test_update_campaign_applies_multiple_fields():
    from handlers import campaigns

    mock_oc = MagicMock()
    mock_audit = MagicMock()

    body = {
        "name": "new-name",
        "schedule": {
            "startTime": "2026-05-01T00:00:00Z",
            "endTime": "2026-05-01T12:00:00Z",
        },
    }

    with (
        patch("handlers.campaigns.build_oc", return_value=mock_oc),
        patch("handlers.campaigns.build_audit", return_value=mock_audit),
    ):
        response = campaigns.update_campaign(_event(body), {"id": "c-1"})

    assert response["statusCode"] == 200
    mock_oc.update_campaign_name.assert_called_once_with("c-1", "new-name")
    mock_oc.update_campaign_schedule.assert_called_once()


def test_update_campaign_rejects_empty_body():
    from handlers import campaigns

    with pytest.raises(ValueError, match="No updatable fields"):
        campaigns.update_campaign(_event({}), {"id": "c-1"})


def test_lifecycle_actions_call_right_method():
    from handlers import campaigns

    mock_oc = MagicMock()
    mock_oc.get_campaign_state.return_value = {"state": "Running"}
    mock_audit = MagicMock()

    with (
        patch("handlers.campaigns.build_oc", return_value=mock_oc),
        patch("handlers.campaigns.build_audit", return_value=mock_audit),
    ):
        response = campaigns.start_campaign(_event(), {"id": "c-1"})

    assert response["statusCode"] == 200
    mock_oc.start_campaign.assert_called_once_with("c-1")
    audit_call = mock_audit.record.call_args.kwargs
    assert audit_call["action"] == "start"
