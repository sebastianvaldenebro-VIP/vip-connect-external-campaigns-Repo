"""Tests for the reconcile handler — v{N+1} + retarget + delete."""
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
    monkeypatch.setenv("REDIS_HOST", "fake")
    monkeypatch.setenv("REDIS_PORT", "6379")
    monkeypatch.setenv("TEAM", "BASIC_TEAM")
    monkeypatch.setenv("CONNECT_INSTANCE_ID", "abc")
    monkeypatch.setenv("SEGMENT_FILTER_CONFIG_TABLE", "VipAdminSegmentFilterConfig")
    monkeypatch.setenv("AWS_REGION", "us-east-1")


def _no_config_store():
    store = MagicMock()
    store.get.return_value = None
    return store


def _config_store_with(rules, combinator="ALL", version=3):
    """Fake store that returns a persisted filter config — the happy path
    reconcile takes after create_segment wrote one at create-time."""
    from vip_shared.domain.entities.filter_rule import FilterOperator, FilterRule

    config = MagicMock()
    config.rules = tuple(
        FilterRule(
            field=f,
            operator=FilterOperator(op),
            values=tuple(v),
        )
        for f, op, v in rules
    )
    config.combinator = combinator
    config.current_version = version

    store = MagicMock()
    store.get.return_value = config
    return store


def _event() -> dict:
    return {
        "body": None,
        "requestContext": {
            "authorizer": {"jwt": {"claims": {"sub": "u1", "email": "u@x.com"}}},
            "http": {"sourceIp": "1.2.3.4", "userAgent": "test"},
        },
    }


def _definition(name: str, *, version: int, arn: str) -> dict:
    return {
        "SegmentDefinitionName": name,
        "SegmentDefinitionArn": arn,
        "DisplayName": name,
        "Tags": {
            "VipSyncMode": "manual",
            "VipFamily": "nj-available-leads",
            "VipVersion": str(version),
        },
        "SegmentGroups": {
            "Groups": [
                {
                    "Type": "ALL",
                    "Dimensions": [
                        {
                            "ProfileAttributes": {
                                "Attributes": {
                                    "available": {
                                        "DimensionType": "EQUAL",
                                        "Values": ["1"],
                                    }
                                }
                            }
                        }
                    ],
                }
            ]
        },
    }


def test_creates_vNplus1_and_retargets_campaigns():
    from handlers import reconcile

    old_arn = "arn:aws:profile:...segment-definitions/nj-available-leads-v3"
    new_arn = "arn:aws:profile:...segment-definitions/nj-available-leads-v4"

    cp = MagicMock()
    cp.get_segment_definition.return_value = _definition("nj-available-leads-v3", version=3, arn=old_arn)
    cp.create_segment_definition.return_value = {"SegmentDefinitionArn": new_arn}

    # Redis truth: cust-a, cust-b, cust-new (3 profiles that should be members)
    redis_source = MagicMock()
    redis_source.iter_records.return_value = iter(
        [
            {"id": "cust-a", "customerid": "cust-a", "available": "1"},
            {"id": "cust-b", "customerid": "cust-b", "available": "1"},
            {"id": "cust-new", "customerid": "cust-new", "available": "1"},
        ]
    )

    oc = MagicMock()
    oc.list_campaigns.return_value = {
        "campaignSummaryList": [
            {"id": "cmp-uses-old", "source": {"customerProfilesSegmentArn": old_arn}},
            {"id": "cmp-other", "source": {"customerProfilesSegmentArn": "arn:some-other"}},
        ],
        "nextToken": None,
    }

    audit = MagicMock()

    with (
        patch("handlers.reconcile.build_cp", return_value=cp),
        patch("handlers.reconcile.build_oc", return_value=oc),
        patch("handlers.reconcile.build_redis_source", return_value=redis_source),
        patch("handlers.reconcile.build_audit", return_value=audit),
        patch(
            "handlers.reconcile.build_filter_config_store",
            return_value=_config_store_with(
                [("available", "eq", ["1"])], combinator="ALL", version=3
            ),
        ),
    ):
        response = reconcile.reconcile_segment(_event(), {"id": "nj-available-leads-v3"})

    body = json.loads(response["body"])
    assert body["newSegmentName"] == "nj-available-leads-v4"
    assert body["newVersion"] == 4
    assert body["targetCount"] == 3
    # Without snapshot we can't compute removed — report all Redis IDs as added.
    assert body["added"] == 3
    assert body["removed"] == 0
    assert body["campaignsUpdated"] == ["cmp-uses-old"]
    assert body["oldSegmentDeleted"] is True

    # New segment created with `ID` INCLUSIVE list (matches CP object type).
    create_kwargs = cp.create_segment_definition.call_args.kwargs
    assert create_kwargs["name"] == "nj-available-leads-v4"
    assert create_kwargs["tags"]["VipSyncMode"] == "manual"
    assert create_kwargs["tags"]["VipVersion"] == "4"
    assert create_kwargs["tags"]["VipFamily"] == "nj-available-leads"
    groups = create_kwargs["segment_groups"]["Groups"][0]
    attrs = groups["Dimensions"][0]["ProfileAttributes"]["Attributes"]
    assert attrs["ID"]["DimensionType"] == "INCLUSIVE"
    assert set(attrs["ID"]["Values"]) == {"cust-a", "cust-b", "cust-new"}

    # Campaign retarget.
    oc.update_campaign_source.assert_called_once_with(
        "cmp-uses-old", {"customerProfilesSegmentArn": new_arn}
    )

    # Old segment deleted after retarget.
    cp.delete_segment_definition.assert_called_once_with("nj-available-leads-v3")


def test_paginates_campaign_listing_when_retargeting():
    from handlers import reconcile

    old_arn = "arn:old"
    new_arn = "arn:new"
    cp = MagicMock()
    cp.get_segment_definition.return_value = _definition("nj-v3", version=3, arn=old_arn)
    cp.create_segment_definition.return_value = {"SegmentDefinitionArn": new_arn}

    redis_source = MagicMock()
    redis_source.iter_records.return_value = iter([
        {"id": "cust-a", "customerid": "cust-a", "available": "1"},
    ])

    oc = MagicMock()
    oc.list_campaigns.side_effect = [
        {
            "campaignSummaryList": [{"id": "page1", "source": {"customerProfilesSegmentArn": old_arn}}],
            "nextToken": "TOKEN",
        },
        {
            "campaignSummaryList": [{"id": "page2", "source": {"customerProfilesSegmentArn": old_arn}}],
            "nextToken": None,
        },
    ]

    with (
        patch("handlers.reconcile.build_cp", return_value=cp),
        patch("handlers.reconcile.build_oc", return_value=oc),
        patch("handlers.reconcile.build_redis_source", return_value=redis_source),
        patch("handlers.reconcile.build_audit", return_value=MagicMock()),
        patch(
            "handlers.reconcile.build_filter_config_store",
            return_value=_config_store_with(
                [("available", "eq", ["1"])], combinator="ALL", version=3
            ),
        ),
    ):
        response = reconcile.reconcile_segment(_event(), {"id": "nj-v3"})

    body = json.loads(response["body"])
    assert body["campaignsUpdated"] == ["page1", "page2"]
    assert oc.list_campaigns.call_count == 2
