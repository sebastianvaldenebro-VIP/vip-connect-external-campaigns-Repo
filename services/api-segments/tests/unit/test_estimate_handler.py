"""Tests for estimate handler — the on-demand recompute API."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PROFILES_DOMAIN_NAME", "amazon-connect-vipmedicalgroup")
    monkeypatch.setenv("AUDIT_TABLE", "AdminAuditLog")


def _caller_event() -> dict:
    return {
        "requestContext": {
            "authorizer": {"jwt": {"claims": {"sub": "user-1", "email": "u@x.com"}}},
            "http": {"sourceIp": "1.2.3.4"},
        }
    }


def test_create_estimate_returns_estimate_id_and_audits():
    from handlers import estimate

    mock_cp = MagicMock()
    mock_cp.create_segment_estimate.return_value = {
        "EstimateId": "estimate-abc-123",
        "Status": "IN_PROGRESS",
    }
    mock_audit = MagicMock()

    with patch("handlers.estimate.build_cp", return_value=mock_cp), patch(
        "handlers.estimate.build_audit", return_value=mock_audit
    ):
        response = estimate.create_estimate(_caller_event(), {"id": "seg-1"})

    assert response["statusCode"] == 202
    body = json.loads(response["body"])
    assert body["estimateId"] == "estimate-abc-123"
    assert body["status"] == "IN_PROGRESS"
    mock_audit.record.assert_called_once()


def test_get_estimate_succeeded_normalizes_count():
    from handlers import estimate

    mock_cp = MagicMock()
    mock_cp.get_segment_estimate.return_value = {
        "Status": "SUCCEEDED",
        "Estimate": "1472",
    }

    with patch("handlers.estimate.build_cp", return_value=mock_cp):
        response = estimate.get_estimate(
            _caller_event(), {"id": "seg-1", "estimateId": "e-1"}
        )

    body = json.loads(response["body"])
    assert body["status"] == "SUCCEEDED"
    assert body["estimate"] == {"totalCount": 1472}


def test_get_estimate_in_progress_omits_count():
    from handlers import estimate

    mock_cp = MagicMock()
    mock_cp.get_segment_estimate.return_value = {"Status": "IN_PROGRESS"}

    with patch("handlers.estimate.build_cp", return_value=mock_cp):
        response = estimate.get_estimate(
            _caller_event(), {"id": "seg-1", "estimateId": "e-1"}
        )

    body = json.loads(response["body"])
    assert body["status"] == "IN_PROGRESS"
    assert "estimate" not in body


def test_get_estimate_failed_surfaces_message():
    from handlers import estimate

    mock_cp = MagicMock()
    mock_cp.get_segment_estimate.return_value = {
        "Status": "FAILED",
        "Message": "Segment references missing field",
    }

    with patch("handlers.estimate.build_cp", return_value=mock_cp):
        response = estimate.get_estimate(
            _caller_event(), {"id": "seg-1", "estimateId": "e-1"}
        )

    body = json.loads(response["body"])
    assert body["status"] == "FAILED"
    assert body["message"] == "Segment references missing field"
