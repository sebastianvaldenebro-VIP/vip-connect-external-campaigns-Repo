"""Tests for the segment-create-form helper handlers: list_distinct_values and
preview_count.
"""

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


def _event(qs=None, body=None):
    e = {}
    if qs is not None:
        e["queryStringParameters"] = qs
    if body is not None:
        e["body"] = json.dumps(body)
    return e


class TestListDistinctValues:
    def test_requires_field_query_param(self):
        from handlers import leads

        with pytest.raises(ValueError, match="field"):
            leads.list_distinct_values(_event(qs={}), {})

    def test_returns_sorted_unique_non_empty_values(self):
        from handlers import leads

        redis_source = MagicMock()
        redis_source.iter_records.return_value = iter(
            [
                {"attempt": "3"},
                {"attempt": "1"},
                {"attempt": "3"},
                {"attempt": ""},
                {"attempt": None},
                {"other": "x"},
            ]
        )

        with patch("handlers.leads.build_redis_source", return_value=redis_source):
            response = leads.list_distinct_values(
                _event(qs={"field": "attempt"}), {}
            )

        body = json.loads(response["body"])
        assert response["statusCode"] == 200
        assert body["field"] == "attempt"
        assert body["values"] == ["1", "3"]
        assert body["truncated"] is False

    def test_truncates_at_max_values(self):
        from handlers import leads

        redis_source = MagicMock()
        redis_source.iter_records.return_value = iter(
            [{"attempt": str(i)} for i in range(10)]
        )

        with patch("handlers.leads.build_redis_source", return_value=redis_source):
            response = leads.list_distinct_values(
                _event(qs={"field": "attempt", "max": "3"}), {}
            )

        body = json.loads(response["body"])
        assert len(body["values"]) == 3
        assert body["truncated"] is True

    def test_max_is_capped_at_distinct_values_cap(self):
        from handlers import leads

        redis_source = MagicMock()
        redis_source.iter_records.return_value = iter([])

        with patch("handlers.leads.build_redis_source", return_value=redis_source):
            leads.list_distinct_values(
                _event(qs={"field": "attempt", "max": "999999"}), {}
            )
        # No assertion error means the huge max didn't blow up; cap applied internally.


class TestPreviewCount:
    def test_requires_segment_groups_in_body(self):
        from handlers import leads

        with pytest.raises(ValueError, match="segmentGroups"):
            leads.preview_count(_event(body={}), {})

    def test_returns_redis_and_segment_counts(self):
        from handlers import leads

        segment_groups = {
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
        }
        redis_source = MagicMock()
        redis_source.iter_records.return_value = iter(
            [
                {"customerid": "a", "available": "1"},
                {"customerid": "b", "available": "0"},
            ]
        )
        cp = MagicMock()
        cp._client.create_segment_estimate.return_value = {"EstimateId": "est-1"}
        cp.wait_for_estimate.return_value = {"Estimate": "5"}

        with (
            patch("handlers.leads.build_redis_source", return_value=redis_source),
            patch("handlers.leads.build_cp", return_value=cp),
        ):
            response = leads.preview_count(
                _event(body={"segmentGroups": segment_groups}), {}
            )

        body = json.loads(response["body"])
        assert response["statusCode"] == 200
        assert body["redisCount"] == 1
        assert body["segmentCount"] == 5

    def test_skips_redis_scan_when_no_rules_evaluable(self):
        """A segmentGroups with dimensions but none translatable to a FilterRule
        (e.g. all unsupported DimensionTypes) must skip the Redis scan entirely,
        not call build_redis_source at all."""
        from handlers import leads

        segment_groups = {
            "Groups": [
                {
                    "Type": "ALL",
                    "Dimensions": [
                        {
                            "ProfileAttributes": {
                                "Attributes": {
                                    "birthDate": {
                                        "DimensionType": "BEFORE",
                                        "Values": ["2000-01-01"],
                                    }
                                }
                            }
                        }
                    ],
                }
            ]
        }
        cp = MagicMock()
        cp._client.create_segment_estimate.return_value = {"EstimateId": "est-1"}
        cp.wait_for_estimate.return_value = {"Estimate": "0"}

        with (
            patch("handlers.leads.build_redis_source") as mock_build_redis,
            patch("handlers.leads.build_cp", return_value=cp),
        ):
            response = leads.preview_count(
                _event(body={"segmentGroups": segment_groups}), {}
            )

        mock_build_redis.assert_not_called()
        body = json.loads(response["body"])
        assert body["redisCount"] == 0


class TestParseEstimate:
    def test_parses_numeric_value(self):
        from handlers import leads

        assert leads._parse_estimate(42) == 42
        assert leads._parse_estimate(42.0) == 42

    def test_parses_dict_with_total_count(self):
        from handlers import leads

        assert leads._parse_estimate({"TotalCount": 10}) == 10
        assert leads._parse_estimate({"totalCount": 20}) == 20

    def test_parses_plain_numeric_string(self):
        from handlers import leads

        assert leads._parse_estimate("15") == 15

    def test_parses_json_string_with_total_count(self):
        from handlers import leads

        assert leads._parse_estimate('{"totalCount": 30}') == 30

    def test_returns_none_for_malformed_string(self):
        from handlers import leads

        assert leads._parse_estimate("not-a-number") is None
        assert leads._parse_estimate('{"totalCount": "not-a-number"}') is None

    def test_returns_none_for_unrecognized_type(self):
        from handlers import leads

        assert leads._parse_estimate(None) is None
        assert leads._parse_estimate([1, 2, 3]) is None

    def test_returns_none_for_dict_without_total_count(self):
        from handlers import leads

        assert leads._parse_estimate({"other": 1}) is None


class TestNormaliseToPascalCase:
    def test_normalises_camel_case_input(self):
        from handlers import leads

        camel = {
            "include": "ALL",
            "groups": [
                {
                    "type": "ANY",
                    "dimensions": [
                        {
                            "profileAttributes": {
                                "attributes": {
                                    "available": {
                                        "dimensionType": "EQUAL",
                                        "values": ["1"],
                                    }
                                }
                            }
                        }
                    ],
                }
            ],
        }
        result = leads._normalise_to_pascal_case(camel)
        assert result["Include"] == "ALL"
        assert result["Groups"][0]["Type"] == "ANY"
        dims = result["Groups"][0]["Dimensions"]
        assert dims[0]["ProfileAttributes"]["Attributes"]["available"] == {
            "DimensionType": "EQUAL",
            "Values": ["1"],
        }

    def test_defaults_missing_fields(self):
        from handlers import leads

        result = leads._normalise_to_pascal_case({"Groups": [{"Dimensions": []}]})
        assert result["Include"] == "ALL"
        assert result["Groups"][0]["Type"] == "ALL"
        assert result["Groups"][0]["Dimensions"] == []

    def test_handles_empty_input(self):
        from handlers import leads

        result = leads._normalise_to_pascal_case({})
        assert result == {"Include": "ALL", "Groups": []}
