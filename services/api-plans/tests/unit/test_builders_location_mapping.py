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

from botocore.exceptions import ClientError

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


# ── auto_onboard_known_state_locations ────────────────────────────────────

def _fake_items_with_ct():
    return [
        {
            "location": "CT - Farmington",
            "stateCode": "CT",
            "stateName": "Connecticut",
            "slug": "Connecticut",
            "stateSortOrder": "2",
            "canonicalPhone": "+18605551234",
            "areaCodes": {"860", "959"},
        },
    ]


class TestResolveKnownCode:
    def test_direct_match(self):
        groups_by_code = {"CT": {"code": "CT"}}
        assert builders._resolve_known_code("CT", groups_by_code) == "CT"

    def test_known_alias_nyc(self):
        groups_by_code = {"NY": {"code": "NY"}}
        assert builders._resolve_known_code("NYC", groups_by_code) == "NY"

    def test_known_alias_south_ca(self):
        groups_by_code = {"SCA": {"code": "SCA"}}
        assert builders._resolve_known_code("South CA", groups_by_code) == "SCA"

    def test_known_alias_north_ca(self):
        groups_by_code = {"NCA": {"code": "NCA"}}
        assert builders._resolve_known_code("North CA", groups_by_code) == "NCA"

    def test_ambiguous_bare_ca_returns_none(self):
        groups_by_code = {"SCA": {"code": "SCA"}, "NCA": {"code": "NCA"}}
        assert builders._resolve_known_code("CA", groups_by_code) is None

    def test_never_before_seen_code_returns_none(self):
        groups_by_code = {"CT": {"code": "CT"}}
        assert builders._resolve_known_code("VA", groups_by_code) is None


class TestAutoOnboardKnownStateLocations:
    def test_empty_input_returns_empty_set_with_no_dynamodb_calls(self):
        with patch("boto3.resource") as mock_boto_resource:
            result = builders.auto_onboard_known_state_locations(set())
        assert result == set()
        mock_boto_resource.assert_not_called()

    def test_already_known_code_is_onboarded_and_removed_from_result(self):
        _reset_cache()
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": _fake_items_with_ct()}
        mock_resource = MagicMock()
        mock_resource.Table.return_value = mock_table

        with patch("boto3.resource", return_value=mock_resource):
            result = builders.auto_onboard_known_state_locations({"CT - West Hartford"})

        assert result == set()
        mock_table.put_item.assert_called_once()
        put_kwargs = mock_table.put_item.call_args.kwargs
        item = put_kwargs["Item"]
        assert item["location"] == "CT - West Hartford"
        assert item["stateCode"] == "CT"
        assert item["stateName"] == "Connecticut"
        assert item["slug"] == "Connecticut"
        assert item["canonicalPhone"] == "+18605551234"
        assert item["areaCodes"] == {"860", "959"}
        assert isinstance(item["areaCodes"], set)
        assert item["stateSortOrder"] == 2
        assert put_kwargs["ConditionExpression"] == "attribute_not_exists(#loc)"
        _reset_cache()

    def test_ambiguous_label_left_unresolved_no_put_item(self):
        _reset_cache()
        mock_table = MagicMock()
        mock_table.scan.return_value = {
            "Items": [
                {"location": "CA - Irvine", "stateCode": "SCA", "stateName": "South CA", "slug": "SouthCA", "stateSortOrder": "0"},
                {"location": "CA - Palo Alto", "stateCode": "NCA", "stateName": "North CA", "slug": "NorthCA", "stateSortOrder": "1"},
            ]
        }
        mock_resource = MagicMock()
        mock_resource.Table.return_value = mock_table

        with patch("boto3.resource", return_value=mock_resource):
            result = builders.auto_onboard_known_state_locations({"CA - Glendale"})

        assert result == {"CA - Glendale"}
        mock_table.put_item.assert_not_called()
        _reset_cache()

    def test_location_with_no_separator_left_unresolved_no_exception(self):
        _reset_cache()
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": _fake_items_with_ct()}
        mock_resource = MagicMock()
        mock_resource.Table.return_value = mock_table

        with patch("boto3.resource", return_value=mock_resource):
            result = builders.auto_onboard_known_state_locations({"MYSTERY"})

        assert result == {"MYSTERY"}
        mock_table.put_item.assert_not_called()
        _reset_cache()

    def test_conditional_check_failed_is_swallowed_and_removed_from_result(self):
        _reset_cache()
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": _fake_items_with_ct()}
        mock_table.put_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "PutItem"
        )
        mock_resource = MagicMock()
        mock_resource.Table.return_value = mock_table

        with patch("boto3.resource", return_value=mock_resource):
            result = builders.auto_onboard_known_state_locations({"CT - West Hartford"})

        assert result == set()
        _reset_cache()

    def test_generic_put_item_failure_leaves_location_unresolved_no_raise(self):
        _reset_cache()
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": _fake_items_with_ct()}
        mock_table.put_item.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}}, "PutItem"
        )
        mock_resource = MagicMock()
        mock_resource.Table.return_value = mock_table

        with patch("boto3.resource", return_value=mock_resource):
            result = builders.auto_onboard_known_state_locations({"CT - West Hartford"})

        assert result == {"CT - West Hartford"}
        _reset_cache()

    def test_successful_onboard_clears_cache_by_code(self):
        _reset_cache()
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": _fake_items_with_ct()}
        mock_resource = MagicMock()
        mock_resource.Table.return_value = mock_table

        with patch("boto3.resource", return_value=mock_resource):
            builders._load_location_mapping()
            assert builders._cache_by_code is not None
            builders.auto_onboard_known_state_locations({"CT - West Hartford"})

        assert builders._cache_by_code is None
        _reset_cache()

    def test_no_successful_onboard_leaves_cache_by_code_untouched(self):
        _reset_cache()
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": _fake_items_with_ct()}
        mock_resource = MagicMock()
        mock_resource.Table.return_value = mock_table

        with patch("boto3.resource", return_value=mock_resource):
            builders._load_location_mapping()
            populated = builders._cache_by_code
            assert populated is not None
            builders.auto_onboard_known_state_locations({"CA - Glendale"})

        assert builders._cache_by_code is populated
        _reset_cache()
