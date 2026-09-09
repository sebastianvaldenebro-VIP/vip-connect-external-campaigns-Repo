"""Tests for the verify handler — count-based diff (no snapshot)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PROFILES_DOMAIN_NAME", "amazon-connect-vipmedicalgroup")
    monkeypatch.setenv("AUDIT_TABLE", "AdminAuditLog")
    monkeypatch.setenv("DATA_KEY_ARN", "arn:aws:kms:us-east-1:123:key/abc")
    monkeypatch.setenv("REDIS_HOST", "fake")
    monkeypatch.setenv("REDIS_PORT", "6379")
    monkeypatch.setenv("TEAM", "BASIC_TEAM")
    monkeypatch.setenv("SEGMENT_FILTER_CONFIG_TABLE", "VipAdminSegmentFilterConfig")
    monkeypatch.setenv("AWS_REGION", "us-east-1")


def _no_config_store():
    """Stub: table lookup returns None so tests fall through to legacy path."""
    store = MagicMock()
    store.get.return_value = None
    return store


def _event() -> dict:
    return {
        "body": None,
        "requestContext": {
            "authorizer": {"jwt": {"claims": {"sub": "u1", "email": "u@x.com"}}},
            "http": {"sourceIp": "1.2.3.4", "userAgent": "test"},
        },
    }


def _manual_definition(name: str, arn: str = "arn:aws:profile:...") -> dict:
    return {
        "SegmentDefinitionName": name,
        "SegmentDefinitionArn": arn,
        "DisplayName": name,
        "Tags": {"VipSyncMode": "manual", "VipFamily": name, "VipVersion": "1"},
        "SegmentGroups": {
            "Include": "ALL",
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
            ],
        },
    }


def test_rejects_segment_without_filters():
    from handlers import verify

    cp = MagicMock()
    definition = _manual_definition("x")
    definition["SegmentGroups"] = {"Include": "ALL", "Groups": []}
    cp.get_segment_definition.return_value = definition

    with (
        patch("handlers.verify.build_cp", return_value=cp),
        patch(
            "handlers.verify.build_filter_config_store", return_value=_no_config_store()
        ),
    ):
        with pytest.raises(ValueError, match="no evaluable filters"):
            verify.verify_segment(_event(), {"id": "x"})


def test_returns_counts_from_estimate_and_redis_scan():
    from handlers import verify

    cp = MagicMock()
    cp.get_segment_definition.return_value = _manual_definition("nj-v1")
    cp.create_segment_estimate.return_value = {"EstimateId": "est-1"}
    cp.wait_for_estimate.return_value = {"Status": "SUCCEEDED", "Estimate": "2"}

    redis_source = MagicMock()
    redis_source.iter_records.return_value = iter(
        [
            {"id": "cust-a", "customerid": "cust-a", "phone": "+1", "available": "1"},
            {"id": "cust-b", "customerid": "cust-b", "phone": "+2", "available": "1"},
            {
                "id": "cust-new",
                "customerid": "cust-new",
                "phone": "+3",
                "available": "1",
            },
        ]
    )

    audit = MagicMock()

    with (
        patch("handlers.verify.build_cp", return_value=cp),
        patch("handlers.verify.build_redis_source", return_value=redis_source),
        patch("handlers.verify.build_audit", return_value=audit),
        patch(
            "handlers.verify.build_filter_config_store",
            return_value=_no_config_store(),
        ),
    ):
        response = verify.verify_segment(_event(), {"id": "nj-v1"})

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["redisCount"] == 3
    assert body["segmentCount"] == 2
    assert set(body["missingCustomerIds"]) == {"cust-a", "cust-b", "cust-new"}
    assert body["extraCustomerIds"] == []
    # Extras detection moved to POST /segments/{id}/verify/extras — verify
    # itself no longer carries a placeholder note for it.
    assert "extrasDetectionDisabled" not in body["notes"]
    audit.record.assert_called_once()


def test_uses_persisted_filter_config_when_present():
    from handlers import verify
    from vip_shared.domain.entities.filter_rule import FilterOperator, FilterRule

    cp = MagicMock()
    definition = _manual_definition("nj-v1")
    definition["SegmentGroups"] = {"Groups": []}  # frozen — must NOT be used
    cp.get_segment_definition.return_value = definition
    cp.create_segment_estimate.return_value = {"EstimateId": "est-1"}
    cp.wait_for_estimate.return_value = {"Estimate": "1"}

    config = MagicMock()
    config.rules = [FilterRule(field="available", operator=FilterOperator.EQ, values=("1",))]
    config.combinator = "ALL"
    config_store = MagicMock()
    config_store.get.return_value = config

    redis_source = MagicMock()
    redis_source.iter_records.return_value = iter(
        [{"id": "cust-a", "customerid": "cust-a", "available": "1"}]
    )

    with (
        patch("handlers.verify.build_cp", return_value=cp),
        patch("handlers.verify.build_redis_source", return_value=redis_source),
        patch("handlers.verify.build_audit", return_value=MagicMock()),
        patch("handlers.verify.build_filter_config_store", return_value=config_store),
    ):
        response = verify.verify_segment(_event(), {"id": "nj-v1"})

    body = json.loads(response["body"])
    assert body["redisCount"] == 1
    assert body["notes"] == {}  # no legacy warning when config is persisted


def test_legacy_warning_present_when_no_config_and_rebuilt():
    from handlers import verify

    cp = MagicMock()
    definition = _manual_definition("nj-v3")
    definition["Tags"]["VipVersion"] = "3"
    cp.get_segment_definition.return_value = definition
    cp.create_segment_estimate.return_value = {"EstimateId": "est-1"}
    cp.wait_for_estimate.return_value = {"Estimate": "0"}

    redis_source = MagicMock()
    redis_source.iter_records.return_value = iter([])

    with (
        patch("handlers.verify.build_cp", return_value=cp),
        patch("handlers.verify.build_redis_source", return_value=redis_source),
        patch("handlers.verify.build_audit", return_value=MagicMock()),
        patch("handlers.verify.build_filter_config_store", return_value=_no_config_store()),
    ):
        response = verify.verify_segment(_event(), {"id": "nj-v3"})

    body = json.loads(response["body"])
    assert "legacyFilter" in body["notes"]
    assert "Recreate with the original filter" in body["notes"]["legacyFilter"]


def test_legacy_warning_for_never_rebuilt_v1_segment():
    from handlers import verify

    cp = MagicMock()
    definition = _manual_definition("nj-v1")
    cp.get_segment_definition.return_value = definition
    cp.create_segment_estimate.return_value = {"EstimateId": "est-1"}
    cp.wait_for_estimate.return_value = {"Estimate": "0"}

    redis_source = MagicMock()
    redis_source.iter_records.return_value = iter([])

    with (
        patch("handlers.verify.build_cp", return_value=cp),
        patch("handlers.verify.build_redis_source", return_value=redis_source),
        patch("handlers.verify.build_audit", return_value=MagicMock()),
        patch("handlers.verify.build_filter_config_store", return_value=_no_config_store()),
    ):
        response = verify.verify_segment(_event(), {"id": "nj-v1"})

    body = json.loads(response["body"])
    assert "will keep working until first rebuild" in body["notes"]["legacyFilter"]


def test_scan_redis_skips_blank_customer_ids():
    from handlers import verify
    from vip_shared.domain.entities.filter_rule import FilterOperator, FilterRule

    redis_source = MagicMock()
    redis_source.iter_records.return_value = iter(
        [
            {"customerid": "", "available": "1"},
            {"customerid": "cust-a", "available": "1"},
        ]
    )
    rules = [FilterRule(field="available", operator=FilterOperator.EQ, values=("1",))]

    with patch("handlers.verify.build_redis_source", return_value=redis_source):
        ids, sample_index = verify._scan_redis(rules, "ALL")

    assert ids == {"cust-a"}
    assert set(sample_index.keys()) == {"cust-a"}


class TestParseEstimate:
    def test_parses_numeric_value(self):
        from handlers import verify

        assert verify._parse_estimate(42) == 42
        assert verify._parse_estimate(42.5) == 42

    def test_parses_dict_with_total_count(self):
        from handlers import verify

        assert verify._parse_estimate({"TotalCount": 10}) == 10
        assert verify._parse_estimate({"totalCount": 20}) == 20

    def test_parses_numeric_string(self):
        from handlers import verify

        assert verify._parse_estimate("15") == 15

    def test_returns_none_for_malformed_string(self):
        from handlers import verify

        assert verify._parse_estimate("not-a-number") is None

    def test_returns_none_for_none_input(self):
        from handlers import verify

        assert verify._parse_estimate(None) is None


class TestTagHelpers:
    def test_sync_mode_from_tags_defaults_to_live(self):
        from handlers import verify

        assert verify._sync_mode_from_tags({}) == "live"
        assert verify._sync_mode_from_tags({"VipSyncMode": "MANUAL"}) == "manual"
        assert verify._sync_mode_from_tags(None) == "live"

    def test_family_from_tags_returns_none_when_absent(self):
        from handlers import verify

        assert verify._family_from_tags({}) is None
        assert verify._family_from_tags({"VipFamily": "nj"}) == "nj"
        assert verify._family_from_tags(None) is None

    def test_version_from_tags_falls_back_to_1_on_malformed_value(self):
        from handlers import verify

        assert verify._version_from_tags({"VipVersion": "bad"}) == 1
        assert verify._version_from_tags({}) == 1
        assert verify._version_from_tags({"VipVersion": "4"}) == 4


def test_filters_out_leads_that_do_not_match_segment_rules():
    from handlers import verify

    cp = MagicMock()
    cp.get_segment_definition.return_value = _manual_definition("nj-v1")
    cp.create_segment_estimate.return_value = {"EstimateId": "est-1"}
    cp.wait_for_estimate.return_value = {"Status": "SUCCEEDED", "Estimate": "0"}

    redis_source = MagicMock()
    # Only cust-on matches `available=1`; cust-off is excluded by the filter.
    redis_source.iter_records.return_value = iter(
        [
            {"id": "cust-on", "customerid": "cust-on", "available": "1"},
            {"id": "cust-off", "customerid": "cust-off", "available": "0"},
        ]
    )

    with (
        patch("handlers.verify.build_cp", return_value=cp),
        patch("handlers.verify.build_redis_source", return_value=redis_source),
        patch("handlers.verify.build_audit", return_value=MagicMock()),
        patch(
            "handlers.verify.build_filter_config_store",
            return_value=_no_config_store(),
        ),
    ):
        response = verify.verify_segment(_event(), {"id": "nj-v1"})

    body = json.loads(response["body"])
    assert body["redisCount"] == 1
    assert body["missingCustomerIds"] == ["cust-on"]
