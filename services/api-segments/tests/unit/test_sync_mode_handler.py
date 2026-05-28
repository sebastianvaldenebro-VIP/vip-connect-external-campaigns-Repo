"""Tests for PATCH /segments/{id} — sync-mode toggle handler."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PROFILES_DOMAIN_NAME", "amazon-connect-vipmedicalgroup")
    monkeypatch.setenv("AUDIT_TABLE", "AdminAuditLog")
    monkeypatch.setenv("SEGMENT_FILTER_CONFIG_TABLE", "VipAdminSegmentFilterConfig")
    monkeypatch.setenv("AWS_REGION", "us-east-1")


def _no_config_store():
    store = MagicMock()
    store.get.return_value = None
    return store


def _event(body: dict) -> dict:
    return {
        "body": json.dumps(body),
        "requestContext": {
            "authorizer": {"jwt": {"claims": {"sub": "u1", "email": "u@x.com"}}},
            "http": {"sourceIp": "1.2.3.4", "userAgent": "test"},
        },
    }


def test_rejects_invalid_mode():
    from handlers import sync_mode

    with pytest.raises(ValueError, match="syncMode must be"):
        sync_mode.update_sync_mode(_event({"syncMode": "nope"}), {"id": "x"})


def test_applies_tag_on_segment_and_audits():
    from handlers import sync_mode

    cp = MagicMock()
    cp.get_segment_definition.return_value = {
        "SegmentDefinitionName": "nj",
        "SegmentDefinitionArn": "arn:nj",
        "Tags": {"VipSyncMode": "live"},
    }
    audit = MagicMock()

    with (
        patch("handlers.sync_mode.build_cp", return_value=cp),
        patch("handlers.sync_mode.build_audit", return_value=audit),
        patch(
            "handlers.sync_mode.build_filter_config_store",
            return_value=_no_config_store(),
        ),
    ):
        response = sync_mode.update_sync_mode(
            _event({"syncMode": "manual"}), {"id": "nj"}
        )

    body = json.loads(response["body"])
    assert body == {"name": "nj", "syncMode": "manual"}
    cp.tag_segment.assert_called_once_with(
        segment_arn="arn:nj", tags={"VipSyncMode": "manual"}
    )
    audit.record.assert_called_once()
    assert audit.record.call_args.kwargs["action"] == "syncMode.update"
    assert audit.record.call_args.kwargs["before"] == {"syncMode": "live"}
    assert audit.record.call_args.kwargs["after"] == {"syncMode": "manual"}


def test_create_persists_syncMode_and_identity_tags():
    """Sanity check that segments create wires the new tags.

    Lives here because the test shares the same handler package and keeps the
    verify/reconcile contract story in one place.
    """
    from handlers import segments

    cp = MagicMock()
    cp.create_segment_definition.return_value = {
        "SegmentDefinitionName": "nj",
        "DisplayName": "NJ",
        "SegmentDefinitionArn": "arn:nj",
    }

    with (
        patch("handlers.segments.build_cp", return_value=cp),
        patch("handlers.segments.build_audit", return_value=MagicMock()),
        patch(
            "handlers.segments.build_filter_config_store",
            return_value=_no_config_store(),
        ),
    ):
        response = segments.create_segment(
            _event(
                {
                    "name": "nj",
                    "displayName": "NJ",
                    "segmentGroups": {"Include": "ALL", "Groups": []},
                    "syncMode": "manual",
                }
            ),
            {},
        )

    body = json.loads(response["body"])
    assert body["syncMode"] == "manual"
    assert body["family"] == "nj"
    assert body["version"] == 1

    tags = cp.create_segment_definition.call_args.kwargs["tags"]
    assert tags["VipSyncMode"] == "manual"
    assert tags["VipFamily"] == "nj"
    assert tags["VipVersion"] == "1"
