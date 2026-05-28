"""Tests for builders.py — segment name, SegmentGroups, and campaign params."""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from builders import (  # noqa: E402
    _abbreviate,
    build_attempts_part,
    build_segment_groups,
    build_segment_name,
    build_campaign_params,
    locations_for_state_codes,
)


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
