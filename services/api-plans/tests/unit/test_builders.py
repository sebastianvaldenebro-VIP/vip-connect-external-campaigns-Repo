"""Tests for builders.py — segment name, SegmentGroups, and campaign params."""

from __future__ import annotations

import sys
import os
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from builders import (  # noqa: E402
    _abbreviate,
    all_known_locations,
    build_attempts_part,
    build_segment_groups,
    build_segment_name,
    build_campaign_params,
    get_all_location_groups,
    locations_for_state_codes,
)

# ── DynamoDB stub — used by all tests that touch the location mapping ──────────

_STUB_BY_CODE: dict = {
    "SCA": ["California", "CA - Arcadia", "CA - Encino", "CA - Huntington Beach",
            "CA - Irvine", "CA - Long Beach", "CA - National City", "CA - Newport Beach",
            "CA - Poway", "CA - San Diego", "CA - Temecula", "CA - Torrance"],
    "NCA": ["CA - Palo Alto", "CA - Sacramento", "CA - San Jose"],
    "CT":  ["Connecticut", "CT - Farmington", "CT - Hamden", "CT - Stamford"],
    "MD":  ["DC", "Maryland", "MD - Bethesda", "MD - Bowie", "MD - Maple Lawn Office"],
    "NJ":  ["New Jersey", "NJ - Clifton", "NJ - Edgewater", "NJ - Harrison Office",
            "NJ - Hoboken", "NJ - Marlton", "NJ - Morris County Office", "NJ - Morristown",
            "NJ - Paramus", "NJ - Princeton", "NJ - Scotch Plains",
            "NJ - West Orange Office", "NJ - West Orange Office (NEW)",
            "NJ - Woodbridge", "NJ - Woodland Park Office"],
    "NY":  ["New York", "NY - Brighton Beach", "NY - Bronx", "NY - Forest Hills",
            "NY - Hartsdale", "NY - Upper East Side", "NY - Yonkers",
            "NYC - Astoria", "NYC - Bronx", "NYC - Brooklyn - Williamsburg",
            "NYC - Downtown Brooklyn", "NYC - FiDi Manhattan", "NYC - Midtown Manhattan",
            "NYC - Staten Island", "NYC - Williamsburg"],
    "LI":  ["Long Island", "NY - LI Hampton Bays", "NY - LI Jericho",
            "NY - LI Port Jefferson", "NY - LI Rockville", "NY - LI West Islip"],
    "TX":  ["Texas", "TX - Addison", "TX - Arlington", "TX - Cedar Park",
            "TX - Cibolo Creek", "TX - Cinco Ranch", "TX - Dallas - Addison",
            "TX - East Dallas", "TX - Flower Mound", "TX - Fort Worth",
            "TX - Kyle", "TX - Medical Center", "TX - Spring Branch", "TX - Sugar Land"],
}
_STUB_GROUPS = [
    {"state": code_meta[0], "slug": code_meta[1], "code": code, "locations": locs}
    for code, locs, code_meta in [
        ("SCA", _STUB_BY_CODE["SCA"], ("South CA", "SouthCA")),
        ("NCA", _STUB_BY_CODE["NCA"], ("North CA", "NorthCA")),
        ("CT",  _STUB_BY_CODE["CT"],  ("Connecticut", "Connecticut")),
        ("MD",  _STUB_BY_CODE["MD"],  ("Maryland", "Maryland")),
        ("NJ",  _STUB_BY_CODE["NJ"],  ("New Jersey", "NewJersey")),
        ("NY",  _STUB_BY_CODE["NY"],  ("New York", "NewYork")),
        ("LI",  _STUB_BY_CODE["LI"],  ("Long Island", "LongIsland")),
        ("TX",  _STUB_BY_CODE["TX"],  ("Texas", "Texas")),
    ]
]
_STUB_ALL = frozenset(loc for locs in _STUB_BY_CODE.values() for loc in locs)
_STUB_MAPPING = (_STUB_BY_CODE, _STUB_GROUPS, _STUB_ALL)


@pytest.fixture(autouse=True)
def _mock_ddb_location():
    """Patch DynamoDB-backed location mapping for all tests in this module."""
    with patch("builders._load_location_mapping", return_value=_STUB_MAPPING):
        yield


# ── abbreviate ────────────────────────────────────────────────────────────────


def test_abbreviate_multiword():
    assert _abbreviate("New Lead") == "NL"


def test_abbreviate_single_word():
    assert _abbreviate("Cancellation") == "Can"


def test_abbreviate_caps_at_4():
    assert _abbreviate("No Show Left Voicemail") == "NSLV"


# ── build_attempts_part ───────────────────────────────────────────────────────


def test_attempts_part_single():
    result = build_attempts_part(["Cancellation / 3rd Attempt"])
    assert result == "Can-3"


def test_attempts_part_multi():
    result = build_attempts_part(
        [
            "Cancellation / 2nd Attempt",
            "Cancellation / 4th Attempt",
            "Cancellation / 6th Attempt",
        ]
    )
    assert result == "Can_2-4-6"


def test_attempts_part_mixed_types():
    result = build_attempts_part(
        [
            "New Lead / 1st Attempt",
            "No Show / 2nd Attempt",
        ]
    )
    assert "NL-1" in result
    assert "NS-2" in result


def test_attempts_part_empty():
    assert build_attempts_part([]) == ""


# ── locations_for_state_codes ─────────────────────────────────────────────────


def test_ny_locations_nonempty():
    locs = locations_for_state_codes(["NY"])
    assert "New York" in locs
    assert len(locs) > 5


def test_unknown_code_returns_empty():
    assert locations_for_state_codes(["ZZ"]) == []


def test_multiple_codes():
    locs = locations_for_state_codes(["NY", "NJ"])
    assert "New York" in locs
    assert "New Jersey" in locs


# ── build_segment_groups ──────────────────────────────────────────────────────


def _bucket(state=("NY",), groups=(), attempts=(), available=""):
    return {
        "segmentFilters": {
            "state": list(state),
            "groups": list(groups),
            "attempts": list(attempts),
            "available": available,
        }
    }


def test_segment_groups_structure():
    groups = build_segment_groups(_bucket(state=("NY",)))
    assert groups["Include"] == "ALL"
    assert len(groups["Groups"]) == 1
    dims = groups["Groups"][0]["Dimensions"]
    fields = set()
    for d in dims:
        fields.update(d["ProfileAttributes"]["Attributes"].keys())
    assert "location" in fields


def test_available_filter_included_when_set():
    groups = build_segment_groups(_bucket(state=("NY",), available="True"))
    dims = groups["Groups"][0]["Dimensions"]
    attrs = {}
    for d in dims:
        attrs.update(d["ProfileAttributes"]["Attributes"])
    assert "available" in attrs
    assert attrs["available"]["Values"] == ["True"]
    assert attrs["available"]["DimensionType"] == "INCLUSIVE"


def test_available_filter_omitted_when_empty():
    groups = build_segment_groups(_bucket(state=("NY",), available=""))
    dims = groups["Groups"][0]["Dimensions"]
    attrs = {}
    for d in dims:
        attrs.update(d["ProfileAttributes"]["Attributes"])
    assert "available" not in attrs


def test_groups_filter_included():
    groups = build_segment_groups(
        _bucket(state=("NY",), groups=["New Lead / 1st Attempt"])
    )
    dims = groups["Groups"][0]["Dimensions"]
    attrs = {}
    for d in dims:
        attrs.update(d["ProfileAttributes"]["Attributes"])
    assert "groups" in attrs
    assert "New Lead / 1st Attempt" in attrs["groups"]["Values"]


def test_attempts_folded_into_groups():
    # v2: attempts are baked into groups values, not a separate dimension
    groups = build_segment_groups(
        _bucket(state=("NY",), groups=["New Lead / 1st Attempt"])
    )
    dims = groups["Groups"][0]["Dimensions"]
    attrs = {}
    for d in dims:
        attrs.update(d["ProfileAttributes"]["Attributes"])
    assert "groups" in attrs
    assert "New Lead / 1st Attempt" in attrs["groups"]["Values"]
    assert "attempt" not in attrs


# ── build_segment_name ────────────────────────────────────────────────────────


def test_segment_name_no_special_chars():
    import re

    name = build_segment_name(_bucket(state=("NY",), groups=["New Lead / 1st Attempt"]))
    assert re.match(r"^[a-zA-Z0-9_-]+$", name), f"Invalid name: {name!r}"


def test_segment_name_contains_state_code():
    name = build_segment_name(_bucket(state=("NY",)))
    assert "NY" in name


# ── build_campaign_params ─────────────────────────────────────────────────────


def _campaign_bucket():
    return {
        "segmentFilters": {
            "state": ["NY"],
            "groups": [],
            "attempts": [],
            "available": "",
        },
        "campaignConfig": {
            "queueId": "q-1",
            "contactFlowId": "cf-1",
            "sourcePhoneNumber": "+12125550100",
            "dialerType": "progressive",
            "bandwidthAllocation": 1.0,
            "dialingCapacity": 1.0,
            "amdEnabled": True,
            "amdAwaitPrompt": True,
            "campaignFlowArn": "",
        },
    }


def test_campaign_params_structure():
    params = build_campaign_params(
        _campaign_bucket(),
        segment_arn="arn:aws:profile:us-east-1:123:domains/d/segment-definitions/s",
        connect_instance_id="instance-1",
        profiles_domain_arn="arn:aws:profile:us-east-1:123:domains/d",
        instance_arn="arn:aws:connect:us-east-1:123:instance/i",
        start_time="2026-05-01T13:00:00Z",
        end_time="2026-05-01T21:00:00Z",
        campaign_name="test-campaign",
    )
    assert params["name"] == "test-campaign"
    assert params["connectInstanceId"] == "instance-1"
    assert params["source"]["customerProfilesSegmentArn"].endswith("/s")
    assert "communicationTimeConfig" in params
    assert "connectCampaignFlowArn" not in params  # empty string → omitted


def test_campaign_params_includes_flow_arn_via_override():
    # Flow ARN must come through campaign_flow_arn_override (resolver result), not cfg.
    bucket = _campaign_bucket()
    params = build_campaign_params(
        bucket,
        segment_arn="arn:aws:profile:us-east-1:123:domains/d/segment-definitions/s",
        connect_instance_id="instance-1",
        profiles_domain_arn="arn:aws:profile:us-east-1:123:domains/d",
        start_time="2026-05-01T13:00:00Z",
        end_time="2026-05-01T21:00:00Z",
        campaign_name="test-campaign",
        campaign_flow_arn_override="arn:aws:connect:us-east-1:123:contact-flow/f",
    )
    assert (
        params["connectCampaignFlowArn"]
        == "arn:aws:connect:us-east-1:123:contact-flow/f"
    )


def test_campaign_params_ignores_stored_flow_arn_in_config():
    # campaignFlowArn in bucket campaignConfig is no longer used — resolver is sole source.
    bucket = _campaign_bucket()
    bucket["campaignConfig"]["campaignFlowArn"] = (
        "arn:aws:connect:us-east-1:123:contact-flow/stale"
    )
    params = build_campaign_params(
        bucket,
        segment_arn="arn:aws:profile:us-east-1:123:domains/d/segment-definitions/s",
        connect_instance_id="instance-1",
        profiles_domain_arn="arn:aws:profile:us-east-1:123:domains/d",
        start_time="2026-05-01T13:00:00Z",
        end_time="2026-05-01T21:00:00Z",
        campaign_name="test-campaign",
    )
    assert "connectCampaignFlowArn" not in params


# ── get_all_location_groups ───────────────────────────────────────────────────


def test_get_all_location_groups_returns_list():
    groups = get_all_location_groups()
    assert isinstance(groups, list)
    assert len(groups) == 8


def test_get_all_location_groups_structure():
    groups = get_all_location_groups()
    for g in groups:
        assert "state" in g
        assert "slug" in g
        assert "code" in g
        assert "locations" in g
        assert "stateSortOrder" not in g  # internal field must be stripped


def test_get_all_location_groups_tx_includes_cinco_ranch():
    groups = get_all_location_groups()
    tx = next((g for g in groups if g["code"] == "TX"), None)
    assert tx is not None
    assert "TX - Cinco Ranch" in tx["locations"]
    assert "TX - East Dallas" in tx["locations"]


# ── all_known_locations ───────────────────────────────────────────────────────


def test_all_known_locations_is_frozenset():
    known = all_known_locations()
    assert isinstance(known, frozenset)


def test_all_known_locations_contains_expected():
    known = all_known_locations()
    assert "New York" in known
    assert "TX - Cinco Ranch" in known
    assert "NJ - Clifton" in known


def test_all_known_locations_does_not_contain_unknown():
    known = all_known_locations()
    assert "TX - Unknown Clinic" not in known
    assert "ZZ - Nowhere" not in known
