"""Tests for POST /segments/{id}/diagnose — CP segment staleness evidence."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PROFILES_DOMAIN_NAME", "amazon-connect-vipmedicalgroup")
    monkeypatch.setenv("REDIS_HOST", "fake")
    monkeypatch.setenv("REDIS_PORT", "6379")
    monkeypatch.setenv("TEAM", "BASIC_TEAM")
    monkeypatch.setenv("SEGMENT_FILTER_CONFIG_TABLE", "VipAdminSegmentFilterConfig")


def _no_config_store():
    store = MagicMock()
    store.get.return_value = None
    return store


def _definition(name: str = "nj-v1") -> dict:
    return {
        "SegmentDefinitionName": name,
        "Tags": {"VipFamily": name},
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


def test_returns_422_when_segment_has_no_filters():
    from handlers import diagnose

    cp = MagicMock()
    definition = _definition()
    definition["SegmentGroups"] = {"Include": "ALL", "Groups": []}
    cp.get_segment_definition.return_value = definition

    with (
        patch("handlers.diagnose.build_cp", return_value=cp),
        patch("handlers.diagnose.build_filter_config_store", return_value=_no_config_store()),
    ):
        response = diagnose.diagnose_staleness({}, {"id": "nj-v1"})

    assert response["statusCode"] == 422


def test_returns_zero_sample_message_when_no_redis_matches():
    from handlers import diagnose

    cp = MagicMock()
    cp.get_segment_definition.return_value = _definition()
    redis_source = MagicMock()
    redis_source.iter_records.return_value = iter([])

    with (
        patch("handlers.diagnose.build_cp", return_value=cp),
        patch("handlers.diagnose.build_redis_source", return_value=redis_source),
        patch("handlers.diagnose.build_filter_config_store", return_value=_no_config_store()),
    ):
        response = diagnose.diagnose_staleness({}, {"id": "nj-v1"})

    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert body["sampledFromRedis"] == 0
    assert body["confirmedStaleCount"] == 0


def test_returns_no_staleness_message_when_all_sampled_are_members():
    from handlers import diagnose

    cp = MagicMock()
    cp.get_segment_definition.return_value = _definition()
    cp.get_segment_membership.return_value = {
        "Profiles": [{"ProfileId": "cust-1", "IsProfileInSegment": True}]
    }
    redis_source = MagicMock()
    redis_source.iter_records.return_value = iter(
        [{"id": "cust-1", "customerid": "cust-1", "available": "1"}]
    )

    with (
        patch("handlers.diagnose.build_cp", return_value=cp),
        patch("handlers.diagnose.build_redis_source", return_value=redis_source),
        patch("handlers.diagnose.build_filter_config_store", return_value=_no_config_store()),
    ):
        response = diagnose.diagnose_staleness({}, {"id": "nj-v1"})

    body = json.loads(response["body"])
    assert body["confirmedStaleCount"] == 0
    assert "already segment members" in body["message"]


def test_confirms_staleness_when_cp_attributes_match_filter_but_not_a_member():
    from handlers import diagnose

    cp = MagicMock()
    cp.get_segment_definition.return_value = _definition()
    cp.get_segment_membership.return_value = {
        "Profiles": [{"ProfileId": "cust-stale", "IsProfileInSegment": False}]
    }
    cp.batch_get_profile.return_value = {
        "Profiles": [
            {
                "ProfileId": "cust-stale",
                "Attributes": {"available": "1"},
                "LastUpdatedAt": "2026-08-27T15:00:00Z",
            }
        ]
    }
    redis_source = MagicMock()
    redis_source.iter_records.return_value = iter(
        [{"id": "cust-stale", "customerid": "cust-stale", "available": "1"}]
    )

    with (
        patch("handlers.diagnose.build_cp", return_value=cp),
        patch("handlers.diagnose.build_redis_source", return_value=redis_source),
        patch("handlers.diagnose.build_filter_config_store", return_value=_no_config_store()),
    ):
        response = diagnose.diagnose_staleness({}, {"id": "nj-v1"})

    body = json.loads(response["body"])
    assert body["confirmedStaleCount"] == 1
    assert body["confirmedStale"][0]["customerId"] == "cust-stale"
    assert "stale" in body["message"]


def test_reports_no_confirmed_staleness_when_cp_attributes_do_not_match():
    from handlers import diagnose

    cp = MagicMock()
    cp.get_segment_definition.return_value = _definition()
    cp.get_segment_membership.return_value = {
        "Profiles": [{"ProfileId": "cust-x", "IsProfileInSegment": False}]
    }
    cp.batch_get_profile.return_value = {
        "Profiles": [
            {
                "ProfileId": "cust-x",
                "Attributes": {"available": "0"},
            }
        ]
    }
    redis_source = MagicMock()
    redis_source.iter_records.return_value = iter(
        [{"id": "cust-x", "customerid": "cust-x", "available": "1"}]
    )

    with (
        patch("handlers.diagnose.build_cp", return_value=cp),
        patch("handlers.diagnose.build_redis_source", return_value=redis_source),
        patch("handlers.diagnose.build_filter_config_store", return_value=_no_config_store()),
    ):
        response = diagnose.diagnose_staleness({}, {"id": "nj-v1"})

    body = json.loads(response["body"])
    assert body["confirmedStaleCount"] == 0
    assert body["cpNoMatchCount"] == 1
    assert "No confirmed staleness" in body["message"]


def test_skips_non_member_missing_from_batch_get_profile_response():
    """A non-member ID absent from BatchGetProfile's response (ingestion lag)
    must be silently skipped, not crash or be miscounted."""
    from handlers import diagnose

    cp = MagicMock()
    cp.get_segment_definition.return_value = _definition()
    cp.get_segment_membership.return_value = {
        "Profiles": [{"ProfileId": "cust-missing", "IsProfileInSegment": False}]
    }
    cp.batch_get_profile.return_value = {"Profiles": []}  # nothing returned
    redis_source = MagicMock()
    redis_source.iter_records.return_value = iter(
        [{"id": "cust-missing", "customerid": "cust-missing", "available": "1"}]
    )

    with (
        patch("handlers.diagnose.build_cp", return_value=cp),
        patch("handlers.diagnose.build_redis_source", return_value=redis_source),
        patch("handlers.diagnose.build_filter_config_store", return_value=_no_config_store()),
    ):
        response = diagnose.diagnose_staleness({}, {"id": "nj-v1"})

    body = json.loads(response["body"])
    assert body["confirmedStaleCount"] == 0
    assert body["cpNoMatchCount"] == 0


def test_uses_persisted_filter_config_when_present():
    """When a SegmentFilterConfig row exists for the family, its rules must be
    used instead of re-deriving from the (possibly frozen) SegmentGroups."""
    from handlers import diagnose
    from vip_shared.domain.entities.filter_rule import FilterOperator, FilterRule

    cp = MagicMock()
    definition = _definition()
    definition["SegmentGroups"] = {"Include": "ALL", "Groups": []}  # frozen/ID-list
    cp.get_segment_definition.return_value = definition

    config = MagicMock()
    config.rules = [FilterRule(field="available", operator=FilterOperator.EQ, values=("1",))]
    config.combinator = "ALL"
    config_store = MagicMock()
    config_store.get.return_value = config

    redis_source = MagicMock()
    redis_source.iter_records.return_value = iter([])

    with (
        patch("handlers.diagnose.build_cp", return_value=cp),
        patch("handlers.diagnose.build_redis_source", return_value=redis_source),
        patch("handlers.diagnose.build_filter_config_store", return_value=config_store),
    ):
        response = diagnose.diagnose_staleness({}, {"id": "nj-v1"})

    assert response["statusCode"] == 200  # did NOT hit the "no evaluable filters" 422


def test_sample_redis_ids_stops_at_max_sample_cap():
    from handlers import diagnose
    from vip_shared.domain.entities.filter_rule import FilterOperator, FilterRule

    rules = [FilterRule(field="available", operator=FilterOperator.EQ, values=("1",))]
    records = [
        {"customerid": f"cust-{i}", "available": "1"} for i in range(diagnose.MAX_SAMPLE + 10)
    ]
    redis_source = MagicMock()
    redis_source.iter_records.return_value = iter(records)

    with patch("handlers.diagnose.build_redis_source", return_value=redis_source):
        ids = diagnose._sample_redis_ids(rules, "ALL", diagnose.MAX_SAMPLE)

    assert len(ids) == diagnose.MAX_SAMPLE


def test_check_membership_defaults_to_not_member_on_cp_error():
    from handlers import diagnose

    cp = MagicMock()
    cp.get_segment_membership.side_effect = RuntimeError("Throttled")

    result = diagnose._check_membership(cp, "nj-v1", ["cust-1", "cust-2"])

    assert result == {"cust-1": False, "cust-2": False}


def test_batch_get_profiles_swallows_errors_per_batch():
    from handlers import diagnose

    cp = MagicMock()
    cp.batch_get_profile.side_effect = RuntimeError("Throttled")

    result = diagnose._batch_get_profiles(cp, ["cust-1"])

    assert result == []
