"""Targeted tests for remaining gaps in executor._create_segment: legacy-bucket
filter fallback, location/groups/createdAt filter rule construction, Redis-
rebuilding guard, unknown-location detection + CloudWatch metric emission
(success and failure), empty-entries / empty-phones guards, and the
"segment already exists" ClientError recovery (and re-raise for other codes).

Follows the exact sys.modules stubbing convention established in
TestCreateSegmentReconcileCounts (test_executor_v2.py) so _create_segment's
real body — not a mock — is exercised.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

sys.modules.setdefault("vip_shared", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.persistence", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.persistence.audit", MagicMock())
sys.modules.setdefault(
    "vip_shared.infrastructure.persistence.redis_lead_source", MagicMock()
)
sys.modules.setdefault(
    "vip_shared.infrastructure.persistence.customer_profiles_client", MagicMock()
)
sys.modules.setdefault("vip_shared.domain", MagicMock())
sys.modules.setdefault("vip_shared.domain.entities", MagicMock())
sys.modules.setdefault("vip_shared.domain.entities.filter_rule", MagicMock())
sys.modules.setdefault("vip_shared.domain.services", MagicMock())
sys.modules.setdefault(
    "vip_shared.domain.services.segment_groups_translator", MagicMock()
)
sys.modules[
    "vip_shared.domain.services.segment_groups_translator"
].matches_group.return_value = True

import executor  # noqa: E402


def _redis_source(records):
    rs = MagicMock()
    rs.is_ready.return_value = True
    rs.iter_records.return_value = records
    return rs


def _cp_client(segment_arn="arn:cp:seg1"):
    cp = MagicMock()
    cp.create_segment_definition.return_value = {"SegmentDefinitionArn": segment_arn}
    return cp


class TestLegacyBucketFilterFallback:
    def test_uses_bucket_segment_filters_when_campaign_is_legacy(self):
        campaign = {"_legacyBucket": True, "name": "legacy-c"}
        bucket = {"name": "b0", "segmentFilters": {"state": ["NY"]}}
        rs = _redis_source(
            [{"customerid": "c1", "phone": "5551234567", "location": "NY"}]
        )
        cp = _cp_client()
        with (
            patch(
                "vip_shared.infrastructure.persistence.redis_lead_source.build_from_env",
                return_value=rs,
            ),
            patch(
                "vip_shared.infrastructure.persistence.customer_profiles_client.build_from_env",
                return_value=cp,
            ),
            patch("executor.all_known_locations", return_value=frozenset({"NY"})),
            patch("executor.locations_for_state_codes", return_value=["loc-ny"]),
            patch("executor.campaign_to_segment_filters") as mock_translate,
        ):
            executor._create_segment(bucket, campaign)

        # Legacy bucket must skip campaign_to_segment_filters entirely.
        mock_translate.assert_not_called()


class TestLocationGroupsCreatedAtRules:
    def test_builds_location_groups_and_createdat_rules(self):
        campaign = {"name": "c0", "groups": ["g1"], "attempts": ["a1"], "maxLeadAgeMinutes": 30}
        bucket = {"name": "b0"}
        rs = _redis_source(
            [{"customerid": "c1", "phone": "5551234567", "location": "NY"}]
        )
        cp = _cp_client()
        with (
            patch(
                "vip_shared.infrastructure.persistence.redis_lead_source.build_from_env",
                return_value=rs,
            ),
            patch(
                "vip_shared.infrastructure.persistence.customer_profiles_client.build_from_env",
                return_value=cp,
            ),
            patch("executor.all_known_locations", return_value=frozenset({"NY"})),
            patch("executor.locations_for_state_codes", return_value=["loc-ny"]),
            patch(
                "executor.campaign_to_segment_filters",
                return_value={
                    "state": ["NY"],
                    "groups": ["g1"],
                    "attempts": ["a1"],
                    "maxLeadAgeMinutes": 30,
                },
            ),
        ):
            name, arn, expected, actual = executor._create_segment(bucket, campaign)

        assert expected == 1
        assert actual == 1


class TestRedisRebuildingGuard:
    def test_raises_when_redis_not_ready(self):
        campaign = {"name": "c0"}
        bucket = {"name": "b0", "segmentFilters": {}}
        rs = MagicMock()
        rs.is_ready.return_value = False
        with (
            patch(
                "vip_shared.infrastructure.persistence.redis_lead_source.build_from_env",
                return_value=rs,
            ),
            patch("executor.locations_for_state_codes", return_value=[]),
            patch("executor.campaign_to_segment_filters", return_value={}),
            pytest.raises(executor._RedisRebuildingError),
        ):
            executor._create_segment(bucket, campaign)


class TestUnknownLocationFetchFailure:
    def test_falls_back_to_empty_known_locs_on_exception(self):
        campaign = {"name": "c0"}
        bucket = {"name": "b0", "segmentFilters": {}}
        rs = _redis_source(
            [{"customerid": "c1", "phone": "5551234567", "location": "MYSTERY"}]
        )
        cp = _cp_client()
        with (
            patch(
                "vip_shared.infrastructure.persistence.redis_lead_source.build_from_env",
                return_value=rs,
            ),
            patch(
                "vip_shared.infrastructure.persistence.customer_profiles_client.build_from_env",
                return_value=cp,
            ),
            patch("executor.all_known_locations", side_effect=RuntimeError("DDB down")),
            patch("executor.locations_for_state_codes", return_value=[]),
            patch("executor.campaign_to_segment_filters", return_value={}),
            patch("executor.boto3.client") as mock_boto_client,
        ):
            executor._create_segment(bucket, campaign)
        # Metric emission for the unknown location must still be attempted.
        mock_boto_client.assert_called_once_with("cloudwatch")


class TestUnknownLocationMetricEmission:
    def test_emits_cloudwatch_metric_for_unknown_locations(self):
        campaign = {"name": "c0"}
        bucket = {"name": "b0", "segmentFilters": {}}
        rs = _redis_source(
            [{"customerid": "c1", "phone": "5551234567", "location": "MYSTERY"}]
        )
        cp = _cp_client()
        mock_cw = MagicMock()
        with (
            patch(
                "vip_shared.infrastructure.persistence.redis_lead_source.build_from_env",
                return_value=rs,
            ),
            patch(
                "vip_shared.infrastructure.persistence.customer_profiles_client.build_from_env",
                return_value=cp,
            ),
            patch("executor.all_known_locations", return_value=frozenset({"NY"})),
            patch("executor.locations_for_state_codes", return_value=[]),
            patch("executor.campaign_to_segment_filters", return_value={}),
            patch("executor.boto3.client", return_value=mock_cw),
        ):
            executor._create_segment(bucket, campaign)

        metric_names = {
            m["MetricName"]
            for call in mock_cw.put_metric_data.call_args_list
            for m in call.kwargs["MetricData"]
        }
        assert metric_names == {"UnknownLocation"}

    def test_swallows_cloudwatch_metric_emit_failure(self):
        campaign = {"name": "c0"}
        bucket = {"name": "b0", "segmentFilters": {}}
        rs = _redis_source(
            [{"customerid": "c1", "phone": "5551234567", "location": "MYSTERY"}]
        )
        cp = _cp_client()
        with (
            patch(
                "vip_shared.infrastructure.persistence.redis_lead_source.build_from_env",
                return_value=rs,
            ),
            patch(
                "vip_shared.infrastructure.persistence.customer_profiles_client.build_from_env",
                return_value=cp,
            ),
            patch("executor.all_known_locations", return_value=frozenset({"NY"})),
            patch("executor.locations_for_state_codes", return_value=[]),
            patch("executor.campaign_to_segment_filters", return_value={}),
            patch("executor.boto3.client", side_effect=RuntimeError("CloudWatch down")),
        ):
            name, arn, expected, actual = executor._create_segment(bucket, campaign)
        # Must not raise — metric emission failure is non-fatal.
        assert expected == 1


class TestEmptyEntriesGuard:
    def test_raises_empty_segment_when_nothing_matches_filters(self):
        campaign = {"name": "c0"}
        bucket = {"name": "b0", "segmentFilters": {}}
        rs = _redis_source([])
        with (
            patch(
                "vip_shared.infrastructure.persistence.redis_lead_source.build_from_env",
                return_value=rs,
            ),
            patch("executor.locations_for_state_codes", return_value=[]),
            patch("executor.campaign_to_segment_filters", return_value={}),
            pytest.raises(executor._EmptySegmentError, match="No Redis records match"),
        ):
            executor._create_segment(bucket, campaign)


class TestEmptyPhonesGuard:
    def test_raises_empty_segment_when_no_valid_phones(self):
        campaign = {"name": "c0"}
        bucket = {"name": "b0", "segmentFilters": {}}
        rs = _redis_source(
            [{"customerid": "c1", "phone": "", "location": "NY"}]
        )
        with (
            patch(
                "vip_shared.infrastructure.persistence.redis_lead_source.build_from_env",
                return_value=rs,
            ),
            patch("executor.all_known_locations", return_value=frozenset({"NY"})),
            patch("executor.locations_for_state_codes", return_value=[]),
            patch("executor.campaign_to_segment_filters", return_value={}),
            pytest.raises(executor._EmptySegmentError, match="No Redis records with valid phone"),
        ):
            executor._create_segment(bucket, campaign)


class TestSegmentAlreadyExistsRecovery:
    def test_reuses_existing_segment_definition_on_already_exists(self):
        campaign = {"name": "c0"}
        bucket = {"name": "b0", "segmentFilters": {}}
        rs = _redis_source(
            [{"customerid": "c1", "phone": "5551234567", "location": "NY"}]
        )
        cp = MagicMock()
        cp.create_segment_definition.side_effect = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "already exists"}},
            "CreateSegmentDefinition",
        )
        cp.get_segment_definition.return_value = {"SegmentDefinitionArn": "arn:cp:existing"}
        with (
            patch(
                "vip_shared.infrastructure.persistence.redis_lead_source.build_from_env",
                return_value=rs,
            ),
            patch(
                "vip_shared.infrastructure.persistence.customer_profiles_client.build_from_env",
                return_value=cp,
            ),
            patch("executor.all_known_locations", return_value=frozenset({"NY"})),
            patch("executor.locations_for_state_codes", return_value=[]),
            patch("executor.campaign_to_segment_filters", return_value={}),
        ):
            name, arn, expected, actual = executor._create_segment(bucket, campaign)

        assert arn == "arn:cp:existing"

    def test_reraises_other_client_errors(self):
        campaign = {"name": "c0"}
        bucket = {"name": "b0", "segmentFilters": {}}
        rs = _redis_source(
            [{"customerid": "c1", "phone": "5551234567", "location": "NY"}]
        )
        cp = MagicMock()
        cp.create_segment_definition.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "throttled"}},
            "CreateSegmentDefinition",
        )
        with (
            patch(
                "vip_shared.infrastructure.persistence.redis_lead_source.build_from_env",
                return_value=rs,
            ),
            patch(
                "vip_shared.infrastructure.persistence.customer_profiles_client.build_from_env",
                return_value=cp,
            ),
            patch("executor.all_known_locations", return_value=frozenset({"NY"})),
            patch("executor.locations_for_state_codes", return_value=[]),
            patch("executor.campaign_to_segment_filters", return_value={}),
            pytest.raises(ClientError),
        ):
            executor._create_segment(bucket, campaign)
