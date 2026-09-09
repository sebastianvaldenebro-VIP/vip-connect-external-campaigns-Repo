"""Tests for builders._load_location_mapping() — the real DynamoDB-backed
implementation. test_builders.py mocks this function away entirely for every
other builders test (autouse fixture), so it needs its own dedicated,
genuine coverage here.
"""

from __future__ import annotations

import os
import sys
import time
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

import builders  # noqa: E402


def _reset_cache():
    builders._cache_by_code = None
    builders._cache_groups = None
    builders._cache_all_locations = None
    builders._cache_ts = 0


def _fake_items():
    return [
        {"location": "NJ - Hoboken", "stateCode": "NJ", "stateName": "New Jersey", "slug": "NewJersey", "stateSortOrder": "1"},
        {"location": "NJ - Newark", "stateCode": "NJ", "stateName": "New Jersey", "slug": "NewJersey", "stateSortOrder": "1"},
        {"location": "TX - Austin", "stateCode": "TX", "stateName": "Texas", "slug": "Texas", "stateSortOrder": "2"},
    ]


def test_scans_table_and_builds_grouped_caches():
    _reset_cache()
    mock_table = MagicMock()
    mock_table.scan.return_value = {"Items": _fake_items()}
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch("boto3.resource", return_value=mock_resource):
        by_code, groups, all_locations = builders._load_location_mapping()

    assert by_code["NJ"] == ["NJ - Hoboken", "NJ - Newark"]
    assert by_code["TX"] == ["TX - Austin"]
    assert {g["code"] for g in groups} == {"NJ", "TX"}
    nj_group = next(g for g in groups if g["code"] == "NJ")
    assert nj_group["state"] == "New Jersey"
    assert nj_group["slug"] == "NewJersey"
    assert nj_group["locations"] == ["NJ - Hoboken", "NJ - Newark"]
    assert all_locations == frozenset({"NJ - Hoboken", "NJ - Newark", "TX - Austin"})
    _reset_cache()


def test_groups_sorted_by_state_sort_order():
    _reset_cache()
    items = [
        {"location": "TX - Austin", "stateCode": "TX", "stateName": "Texas", "slug": "Texas", "stateSortOrder": "5"},
        {"location": "NJ - Hoboken", "stateCode": "NJ", "stateName": "New Jersey", "slug": "NewJersey", "stateSortOrder": "1"},
    ]
    mock_table = MagicMock()
    mock_table.scan.return_value = {"Items": items}
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch("boto3.resource", return_value=mock_resource):
        _, groups, _ = builders._load_location_mapping()

    assert [g["code"] for g in groups] == ["NJ", "TX"]
    _reset_cache()


def test_defaults_state_sort_order_to_99_when_missing():
    _reset_cache()
    items = [
        {"location": "ZZ - Somewhere", "stateCode": "ZZ", "stateName": "Zeta", "slug": "Zeta"},
    ]
    mock_table = MagicMock()
    mock_table.scan.return_value = {"Items": items}
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch("boto3.resource", return_value=mock_resource):
        _, groups, _ = builders._load_location_mapping()

    assert groups[0]["stateSortOrder"] == 99
    _reset_cache()


def test_paginates_through_multiple_scan_pages():
    _reset_cache()
    mock_table = MagicMock()
    mock_table.scan.side_effect = [
        {
            "Items": [_fake_items()[0]],
            "LastEvaluatedKey": {"location": "NJ - Hoboken"},
        },
        {"Items": [_fake_items()[1], _fake_items()[2]]},
    ]
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch("boto3.resource", return_value=mock_resource):
        by_code, _, all_locations = builders._load_location_mapping()

    assert mock_table.scan.call_count == 2
    second_call_kwargs = mock_table.scan.call_args_list[1].kwargs
    assert second_call_kwargs["ExclusiveStartKey"] == {"location": "NJ - Hoboken"}
    assert len(all_locations) == 3
    _reset_cache()


def test_uses_cached_value_within_ttl_without_rescanning():
    _reset_cache()
    mock_table = MagicMock()
    mock_table.scan.return_value = {"Items": _fake_items()}
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch("boto3.resource", return_value=mock_resource):
        builders._load_location_mapping()
        builders._load_location_mapping()  # second call within TTL

    mock_table.scan.assert_called_once()
    _reset_cache()


def test_rescans_after_ttl_expires():
    _reset_cache()
    mock_table = MagicMock()
    mock_table.scan.return_value = {"Items": _fake_items()}
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch("boto3.resource", return_value=mock_resource):
        builders._load_location_mapping()
        # Force the cache to look stale.
        builders._cache_ts = time.monotonic() - builders._CACHE_TTL - 1
        builders._load_location_mapping()

    assert mock_table.scan.call_count == 2
    _reset_cache()


def test_locations_for_state_codes_uses_real_load_function():
    _reset_cache()
    mock_table = MagicMock()
    mock_table.scan.return_value = {"Items": _fake_items()}
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch("boto3.resource", return_value=mock_resource):
        result = builders.locations_for_state_codes(["NJ"])

    assert result == ["NJ - Hoboken", "NJ - Newark"]
    _reset_cache()
