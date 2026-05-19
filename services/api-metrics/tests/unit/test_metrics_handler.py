"""Tests for metrics handler."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CONNECT_INSTANCE_ID", "instance-1")
    monkeypatch.setenv("AUDIT_TABLE", "AdminAuditLog")


def test_get_campaign_metrics_returns_totals_and_series():
    from handlers import metrics

    mock_cw = MagicMock()
    mock_cw.get_campaign_totals.return_value = {"Delivery": 1247, "ContactsAnswered": 612}
    mock_cw.get_campaign_metric_series.return_value = [
        {"timestamp": "2026-04-22T14:00:00+00:00", "value": 100.0},
    ]

    with patch("handlers.metrics.build_cw", return_value=mock_cw):
        response = metrics.get_campaign_metrics(
            {"queryStringParameters": {"lookbackHours": "24", "period": "60"}},
            {"id": "c-1"},
        )

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["campaignId"] == "c-1"
    assert body["totals"]["Delivery"] == 1247
    assert "Delivery" in body["series"]


def test_get_current_realtime_requires_queue_id():
    from handlers import metrics

    with pytest.raises(ValueError, match="queueId"):
        metrics.get_current_realtime({"queryStringParameters": None}, {})


def test_get_queue_realtime_flattens_metric_collections():
    from handlers import metrics

    mock_client = MagicMock()
    mock_client.get_current_metric_data.return_value = [
        {
            "Collections": [
                {"Metric": {"Name": "AGENTS_AVAILABLE"}, "Value": 3.0},
                {"Metric": {"Name": "AGENTS_ONLINE"}, "Value": 24.0},
            ]
        }
    ]

    with patch("handlers.metrics.build_connect", return_value=mock_client):
        response = metrics.get_queue_realtime({}, {"queueId": "q-1"})

    body = json.loads(response["body"])
    assert body["queueId"] == "q-1"
    assert body["metrics"] == {"AGENTS_AVAILABLE": 3.0, "AGENTS_ONLINE": 24.0}


def test_get_dispositions_requires_campaign_id():
    from handlers import metrics

    with pytest.raises(ValueError, match="campaignId"):
        metrics.get_dispositions({"queryStringParameters": {}}, {})


def test_get_dispositions_groups_by_disconnect_reason():
    from handlers import metrics

    mock_client = MagicMock()
    mock_client.search_contacts.return_value = [
        {"Id": "1", "DisconnectReason": "CUSTOMER_DISCONNECT"},
        {"Id": "2", "DisconnectReason": "CUSTOMER_DISCONNECT"},
        {"Id": "3", "DisconnectReason": "AMD_UNANSWERED"},
    ]

    with patch("handlers.metrics.build_connect", return_value=mock_client):
        response = metrics.get_dispositions(
            {"queryStringParameters": {"campaignId": "c-1", "lookbackHours": "24"}},
            {},
        )

    body = json.loads(response["body"])
    assert body["totalContacts"] == 3
    # Sorted by count desc
    assert body["breakdown"][0]["disconnectReason"] == "CUSTOMER_DISCONNECT"
    assert body["breakdown"][0]["count"] == 2
    assert body["breakdown"][0]["percent"] == 66.67
