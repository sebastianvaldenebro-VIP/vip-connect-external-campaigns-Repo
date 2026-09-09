"""Tests for POST /segments/{id}/verify/extras and GET .../{snapshotId}."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PROFILES_DOMAIN_NAME", "amazon-connect-vipmedicalgroup")
    monkeypatch.setenv("SNAPSHOT_BUCKET", "vip-segment-snapshots")
    monkeypatch.setenv("SNAPSHOT_ROLE_ARN", "arn:aws:iam::123:role/snapshot-role")
    monkeypatch.setenv("DATA_KEY_ARN", "arn:aws:kms:us-east-1:123:key/abc")
    monkeypatch.setenv("AUDIT_TABLE", "AdminAuditLog")
    monkeypatch.setenv("REDIS_HOST", "fake")
    monkeypatch.setenv("REDIS_PORT", "6379")
    monkeypatch.setenv("TEAM", "BASIC_TEAM")
    monkeypatch.setenv("SEGMENT_FILTER_CONFIG_TABLE", "VipAdminSegmentFilterConfig")


def _event():
    return {
        "requestContext": {
            "authorizer": {"jwt": {"claims": {"sub": "u1", "email": "u@example.com"}}},
            "http": {"sourceIp": "10.0.0.1", "userAgent": "test-agent"},
        }
    }


def _definition(name="nj-v1", version="1"):
    return {
        "Tags": {"VipFamily": name, "VipVersion": version},
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


def _no_config_store():
    store = MagicMock()
    store.get.return_value = None
    return store


class TestStartExtrasDetection:
    def test_starts_snapshot_and_records_audit(self):
        from handlers import verify_extras

        cp = MagicMock()
        cp.get_segment_definition.return_value = _definition()
        cp.create_segment_snapshot.return_value = {
            "SnapshotId": "snap-1",
            "Status": "IN_PROGRESS",
        }
        audit = MagicMock()

        with (
            patch("handlers.verify_extras.build_cp", return_value=cp),
            patch("handlers.verify_extras.build_filter_config_store", return_value=_no_config_store()),
            patch("handlers.verify_extras.build_audit", return_value=audit),
        ):
            response = verify_extras.start_extras_detection(_event(), {"id": "nj-v1"})

        body = json.loads(response["body"])
        assert response["statusCode"] == 202
        assert body["snapshotId"] == "snap-1"
        assert "extras-" in body["destinationUri"]
        audit.record.assert_called_once()
        assert audit.record.call_args.kwargs["action"] == "verify-extras-start"

    def test_raises_when_no_config_and_no_evaluable_filters(self):
        from handlers import verify_extras

        cp = MagicMock()
        definition = _definition()
        definition["SegmentGroups"] = {"Groups": []}
        cp.get_segment_definition.return_value = definition

        with (
            patch("handlers.verify_extras.build_cp", return_value=cp),
            patch("handlers.verify_extras.build_filter_config_store", return_value=_no_config_store()),
        ):
            with pytest.raises(ValueError, match="no evaluable filters"):
                verify_extras.start_extras_detection(_event(), {"id": "nj-v1"})

    def test_skips_probe_when_persisted_config_exists(self):
        """When a SegmentFilterConfig row exists, the handler must not even
        attempt to derive rules from (possibly frozen) SegmentGroups."""
        from handlers import verify_extras

        cp = MagicMock()
        definition = _definition()
        definition["SegmentGroups"] = {"Groups": []}  # frozen ID-list — would fail probe
        cp.get_segment_definition.return_value = definition
        cp.create_segment_snapshot.return_value = {"SnapshotId": "snap-2"}

        config_store = MagicMock()
        config_store.get.return_value = MagicMock()  # config exists

        with (
            patch("handlers.verify_extras.build_cp", return_value=cp),
            patch("handlers.verify_extras.build_filter_config_store", return_value=config_store),
            patch("handlers.verify_extras.build_audit", return_value=MagicMock()),
        ):
            response = verify_extras.start_extras_detection(_event(), {"id": "nj-v1"})

        assert response["statusCode"] == 202


class TestGetExtrasDetection:
    def test_returns_status_only_when_not_completed(self):
        from handlers import verify_extras

        cp = MagicMock()
        cp.get_segment_snapshot.return_value = {"Status": "IN_PROGRESS"}

        with patch("handlers.verify_extras.build_cp", return_value=cp):
            response = verify_extras.get_extras_detection(
                _event(), {"id": "nj-v1", "snapshotId": "snap-1"}
            )

        body = json.loads(response["body"])
        assert response["statusCode"] == 200
        assert body["status"] == "IN_PROGRESS"
        assert "cpCount" not in body

    def test_returns_status_message_when_failed(self):
        from handlers import verify_extras

        cp = MagicMock()
        cp.get_segment_snapshot.return_value = {
            "Status": "FAILED",
            "StatusMessage": "Access denied to role",
        }

        with patch("handlers.verify_extras.build_cp", return_value=cp):
            response = verify_extras.get_extras_detection(
                _event(), {"id": "nj-v1", "snapshotId": "snap-1"}
            )

        body = json.loads(response["body"])
        assert body["status"] == "FAILED"
        assert body["statusMessage"] == "Access denied to role"

    def test_raises_when_completed_without_destination_uri(self):
        from handlers import verify_extras

        cp = MagicMock()
        cp.get_segment_snapshot.return_value = {"Status": "COMPLETED"}

        with patch("handlers.verify_extras.build_cp", return_value=cp):
            with pytest.raises(RuntimeError, match="no DestinationUri"):
                verify_extras.get_extras_detection(
                    _event(), {"id": "nj-v1", "snapshotId": "snap-1"}
                )

    def test_computes_extras_and_missing_when_completed(self):
        from handlers import verify_extras

        cp = MagicMock()
        cp.get_segment_snapshot.return_value = {
            "Status": "COMPLETED",
            "DestinationUri": "s3://bucket/nj-v1/extras-1/",
        }
        cp.get_segment_definition.return_value = _definition()

        reader = MagicMock()
        reader.load_members.return_value = [{"customerid": "cust-extra"}]
        reader.extract_customer_ids.return_value = {"cust-extra"}

        redis_source = MagicMock()
        redis_source.iter_records.return_value = iter(
            [{"customerid": "cust-missing", "available": "1"}]
        )
        audit = MagicMock()

        with (
            patch("handlers.verify_extras.build_cp", return_value=cp),
            patch("handlers.verify_extras.SnapshotReader", return_value=reader),
            patch("handlers.verify_extras.build_filter_config_store", return_value=_no_config_store()),
            patch("handlers.verify_extras.build_redis_source", return_value=redis_source),
            patch("handlers.verify_extras.build_audit", return_value=audit),
        ):
            response = verify_extras.get_extras_detection(
                _event(), {"id": "nj-v1", "snapshotId": "snap-1"}
            )

        body = json.loads(response["body"])
        assert body["cpCount"] == 1
        assert body["redisCount"] == 1
        assert body["extraCustomerIds"] == ["cust-extra"]
        assert body["missingCustomerIds"] == ["cust-missing"]
        assert "computedAt" in body
        audit.record.assert_called_once()
        assert audit.record.call_args.kwargs["action"] == "verify-extras-complete"

    def test_falls_back_to_uppercase_id_field_when_customerid_empty(self):
        from handlers import verify_extras

        cp = MagicMock()
        cp.get_segment_snapshot.return_value = {
            "Status": "COMPLETED",
            "DestinationUri": "s3://bucket/nj-v1/extras-1/",
        }
        cp.get_segment_definition.return_value = _definition()

        reader = MagicMock()
        reader.load_members.return_value = [{"ID": "cust-legacy"}]
        reader.extract_customer_ids.side_effect = [set(), {"cust-legacy"}]

        redis_source = MagicMock()
        redis_source.iter_records.return_value = iter([])

        with (
            patch("handlers.verify_extras.build_cp", return_value=cp),
            patch("handlers.verify_extras.SnapshotReader", return_value=reader),
            patch("handlers.verify_extras.build_filter_config_store", return_value=_no_config_store()),
            patch("handlers.verify_extras.build_redis_source", return_value=redis_source),
            patch("handlers.verify_extras.build_audit", return_value=MagicMock()),
        ):
            response = verify_extras.get_extras_detection(
                _event(), {"id": "nj-v1", "snapshotId": "snap-1"}
            )

        body = json.loads(response["body"])
        assert body["cpCount"] == 1
        assert reader.extract_customer_ids.call_count == 2

    def test_raises_for_rebuilt_segment_without_persisted_config(self):
        """A vN (rebuilt) segment with no persisted filter config must refuse
        to run extras detection against its frozen ID-list SegmentGroups."""
        from handlers import verify_extras

        cp = MagicMock()
        cp.get_segment_snapshot.return_value = {
            "Status": "COMPLETED",
            "DestinationUri": "s3://bucket/nj-v1/extras-1/",
        }
        cp.get_segment_definition.return_value = _definition(version="2")

        reader = MagicMock()
        reader.load_members.return_value = []
        reader.extract_customer_ids.return_value = set()

        with (
            patch("handlers.verify_extras.build_cp", return_value=cp),
            patch("handlers.verify_extras.SnapshotReader", return_value=reader),
            patch("handlers.verify_extras.build_filter_config_store", return_value=_no_config_store()),
        ):
            with pytest.raises(ValueError, match="rebuilt before filter persistence"):
                verify_extras.get_extras_detection(
                    _event(), {"id": "nj-v1", "snapshotId": "snap-1"}
                )

    def test_treats_malformed_version_tag_as_v1(self):
        """A non-numeric VipVersion tag must not crash — treated as v1, which
        (with no persisted config) falls through to the legacy translate path
        rather than raising the rebuilt-segment guard."""
        from handlers import verify_extras

        cp = MagicMock()
        cp.get_segment_snapshot.return_value = {
            "Status": "COMPLETED",
            "DestinationUri": "s3://bucket/nj-v1/extras-1/",
        }
        cp.get_segment_definition.return_value = _definition(version="not-a-number")

        reader = MagicMock()
        reader.load_members.return_value = []
        reader.extract_customer_ids.return_value = set()

        redis_source = MagicMock()
        redis_source.iter_records.return_value = iter([])

        with (
            patch("handlers.verify_extras.build_cp", return_value=cp),
            patch("handlers.verify_extras.SnapshotReader", return_value=reader),
            patch("handlers.verify_extras.build_filter_config_store", return_value=_no_config_store()),
            patch("handlers.verify_extras.build_redis_source", return_value=redis_source),
            patch("handlers.verify_extras.build_audit", return_value=MagicMock()),
        ):
            response = verify_extras.get_extras_detection(
                _event(), {"id": "nj-v1", "snapshotId": "snap-1"}
            )

        assert response["statusCode"] == 200

    def test_raises_when_no_evaluable_filters_at_all(self):
        from handlers import verify_extras

        cp = MagicMock()
        cp.get_segment_snapshot.return_value = {
            "Status": "COMPLETED",
            "DestinationUri": "s3://bucket/nj-v1/extras-1/",
        }
        definition = _definition()
        definition["SegmentGroups"] = {"Groups": []}
        cp.get_segment_definition.return_value = definition

        reader = MagicMock()
        reader.load_members.return_value = []
        reader.extract_customer_ids.return_value = set()

        with (
            patch("handlers.verify_extras.build_cp", return_value=cp),
            patch("handlers.verify_extras.SnapshotReader", return_value=reader),
            patch("handlers.verify_extras.build_filter_config_store", return_value=_no_config_store()),
        ):
            with pytest.raises(ValueError, match="no evaluable filters"):
                verify_extras.get_extras_detection(
                    _event(), {"id": "nj-v1", "snapshotId": "snap-1"}
                )

    def test_uses_persisted_config_rules_when_present(self):
        from handlers import verify_extras
        from vip_shared.domain.entities.filter_rule import FilterOperator, FilterRule

        cp = MagicMock()
        cp.get_segment_snapshot.return_value = {
            "Status": "COMPLETED",
            "DestinationUri": "s3://bucket/nj-v1/extras-1/",
        }
        definition = _definition()
        definition["SegmentGroups"] = {"Groups": []}  # frozen — must NOT be used
        cp.get_segment_definition.return_value = definition

        reader = MagicMock()
        reader.load_members.return_value = []
        reader.extract_customer_ids.return_value = set()

        config = MagicMock()
        config.rules = [FilterRule(field="available", operator=FilterOperator.EQ, values=("1",))]
        config.combinator = "ALL"
        config_store = MagicMock()
        config_store.get.return_value = config

        redis_source = MagicMock()
        redis_source.iter_records.return_value = iter(
            [{"customerid": "cust-a", "available": "1"}]
        )

        with (
            patch("handlers.verify_extras.build_cp", return_value=cp),
            patch("handlers.verify_extras.SnapshotReader", return_value=reader),
            patch("handlers.verify_extras.build_filter_config_store", return_value=config_store),
            patch("handlers.verify_extras.build_redis_source", return_value=redis_source),
            patch("handlers.verify_extras.build_audit", return_value=MagicMock()),
        ):
            response = verify_extras.get_extras_detection(
                _event(), {"id": "nj-v1", "snapshotId": "snap-1"}
            )

        body = json.loads(response["body"])
        assert body["redisCount"] == 1


def test_scan_redis_ids_dedupes_and_strips():
    from handlers import verify_extras
    from vip_shared.domain.entities.filter_rule import FilterOperator, FilterRule

    redis_source = MagicMock()
    redis_source.iter_records.return_value = iter(
        [
            {"customerid": " cust-a ", "available": "1"},
            {"id": "cust-b", "available": "1"},
            {"customerid": "", "available": "1"},  # blank id — must be skipped
        ]
    )
    rules = [FilterRule(field="available", operator=FilterOperator.EQ, values=("1",))]

    with patch("handlers.verify_extras.build_redis_source", return_value=redis_source):
        result = verify_extras._scan_redis_ids(rules, "ALL")

    assert result == {"cust-a", "cust-b"}
