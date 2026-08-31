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
    campaign_to_segment_filters,
    get_all_location_groups,
    locations_for_state_codes,
    resolve_campaign_flow_arn,
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


# Regression (2026-08-27, adversarial review round 2): two campaigns sharing
# state+groups but differing only in maxLeadAgeMinutes used to collide on an
# identical segment name, and _create_segment's "already exists" recovery
# path silently reused whichever campaign's segment won the create race —
# dialing the wrong lead set with no error.


def test_segment_name_differs_for_campaigns_with_different_max_lead_age():
    bucket = {"name": "b1"}
    campaign_a = {
        "id": "c-a", "states": ["NY"], "groups": ["New Lead / New Lead"],
        "maxLeadAgeMinutes": 10,
    }
    campaign_b = {
        "id": "c-b", "states": ["NY"], "groups": ["New Lead / New Lead"],
        "maxLeadAgeMinutes": None,
    }
    name_a = build_segment_name(bucket, campaign_a)
    name_b = build_segment_name(bucket, campaign_b)
    assert name_a != name_b


def test_segment_name_stable_for_legacy_bucket_without_campaign():
    """Legacy (v1) buckets have no per-campaign id — behavior must be unchanged."""
    name = build_segment_name(_bucket(state=("NY",), groups=["New Lead / 1st Attempt"]))
    assert "None" not in name


# Regression (2026-08-27, adversarial review round 3): the round-2 campaign-id
# disambiguator added up to 13 chars with no length budget, pushing realistic
# multi-state/multi-attempt campaigns past AWS Customer Profiles'
# SegmentDefinitionName hard limit (verified against botocore's service
# model: max=64) — segment creation would fail for campaigns that worked
# before that patch.


def test_segment_name_never_exceeds_aws_64_char_limit():
    campaign = {
        "id": "campaign-id-with-a-very-long-descriptive-name-1234567890",
        "states": ["SCA", "NCA", "CT", "MD", "NJ", "NY", "LI", "TX"],
        "groups": [
            "New Lead / 1st Attempt",
            "No Show / 2nd Attempt",
            "Cancellation / 3rd Attempt",
            "Reschedule / 4th Attempt",
        ],
        "maxLeadAgeMinutes": 15,
    }
    name = build_segment_name({"name": "b1"}, campaign)
    assert len(name) <= 64


def test_segment_name_disambiguator_survives_truncation():
    """The campaign-id disambiguator (its first 12 sanitized chars) must never
    be the part that gets cut — losing it would silently reopen the collision
    it exists to prevent. Uses ids that differ within the first 12 chars, and
    enough states/groups (4 groups, not 2) to actually push the body past the
    budget on any calendar date — verified: this fixture's body is 47 chars
    against a ~37-38 char budget, so it genuinely exercises the truncation
    branch, unlike a 2-group fixture which never reaches it.
    """
    shared_extra = {
        "states": ["SCA", "NCA", "CT", "MD", "NJ", "NY", "LI", "TX"],
        "groups": [
            "New Lead / 1st Attempt",
            "No Show / 2nd Attempt",
            "Cancellation / 3rd Attempt",
            "Reschedule / 4th Attempt",
        ],
    }
    campaign_a = {"id": "campaign-a-" + "x" * 100, **shared_extra}
    campaign_b = {"id": "campaign-b-" + "x" * 100, **shared_extra}
    name_a = build_segment_name({"name": "b1"}, campaign_a)
    name_b = build_segment_name({"name": "b1"}, campaign_b)
    assert name_a != name_b
    assert len(name_a) <= 64
    assert len(name_b) <= 64


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


# ── resolve_campaign_flow_arn ────────────────────────────────────────────────
#
# Regression for the concurrent-create race: this function now has 2 real
# callers (the sequential executor, and the Enable-Campaign modal via the new
# HTTP endpoint) that can race for the same brand-new state. If our
# create_contact_flow loses the race (DuplicateResourceException or similar),
# we must re-list once and return the winner's ARN instead of None — a bare
# None from this path used to raise a ValueError that killed a live run even
# though the flow now actually exists.


class _FakeConnectClient:
    """Minimal boto3 Connect client stub for resolve_campaign_flow_arn tests."""

    def __init__(self, list_responses, create_side_effect=None):
        # list_responses: sequence of ContactFlowSummaryList payloads, consumed
        # in order across successive list_contact_flows calls.
        self._list_responses = list(list_responses)
        self._create_side_effect = create_side_effect

    def list_contact_flows(self, **kwargs):
        payload = self._list_responses.pop(0) if self._list_responses else []
        return {"ContactFlowSummaryList": payload}

    def create_contact_flow(self, **kwargs):
        if self._create_side_effect is not None:
            raise self._create_side_effect
        return {"ContactFlowArn": "arn:aws:connect:us-east-1:165505826690:instance/x/contact-flow/new"}

    def tag_resource(self, **kwargs):
        return {}


def test_resolve_campaign_flow_arn_returns_existing_match():
    fake_client = _FakeConnectClient(
        list_responses=[[{"Name": "campaign-PA", "Arn": "arn:existing"}]],
    )
    with patch("builders.boto3.client", return_value=fake_client):
        arn = resolve_campaign_flow_arn(["PA"], "instance-id")
    assert arn == "arn:existing"


def test_resolve_campaign_flow_arn_auto_creates_when_missing():
    fake_client = _FakeConnectClient(list_responses=[[]])
    with patch("builders.boto3.client", return_value=fake_client):
        arn = resolve_campaign_flow_arn(["PA"], "instance-id")
    assert arn == "arn:aws:connect:us-east-1:165505826690:instance/x/contact-flow/new"


def test_resolve_campaign_flow_arn_returns_winner_arn_on_concurrent_create_race():
    """create_contact_flow raises (a concurrent caller won the race). The
    fix re-lists once, finds the winner's flow, and returns its ARN instead
    of None.
    """
    fake_client = _FakeConnectClient(
        list_responses=[
            [],  # first list: nothing found -> attempt create
            [{"Name": "campaign-PA", "Arn": "arn:winner"}],  # retry list after create failure
        ],
        create_side_effect=Exception("DuplicateResourceException"),
    )
    with patch("builders.boto3.client", return_value=fake_client):
        arn = resolve_campaign_flow_arn(["PA"], "instance-id")
    assert arn == "arn:winner"


def test_resolve_campaign_flow_arn_returns_none_when_create_and_retry_both_fail():
    fake_client = _FakeConnectClient(
        list_responses=[[], []],
        create_side_effect=Exception("boom"),
    )
    with patch("builders.boto3.client", return_value=fake_client):
        arn = resolve_campaign_flow_arn(["PA"], "instance-id")
    assert arn is None


# ── campaign_to_segment_filters — maxLeadAgeMinutes passthrough (2026-08-28) ───


def test_campaign_to_segment_filters_passes_through_max_lead_age_minutes():
    campaign = {"states": ["NY"], "groups": ["New Lead / New Lead"], "maxLeadAgeMinutes": 15}
    filters = campaign_to_segment_filters(campaign)
    assert filters["maxLeadAgeMinutes"] == 15


def test_campaign_to_segment_filters_max_lead_age_minutes_none_by_default():
    campaign = {"states": ["NY"], "groups": ["New Lead / New Lead"]}
    filters = campaign_to_segment_filters(campaign)
    assert filters["maxLeadAgeMinutes"] is None
