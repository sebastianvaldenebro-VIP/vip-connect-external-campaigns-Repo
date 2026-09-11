"""Tests for the UI-body → V2 API payload transformer."""

from __future__ import annotations

import sys

import pytest

from builders import build_create_campaign_params


def _base_body():
    return {
        "name": "test-campaign",
        "segmentArn": "arn:aws:profile:us-east-1:123:domains/d/segment-definitions/seg",
        "queueId": "queue-1",
        "contactFlowId": "flow-1",
        "campaignFlowArn": "arn:aws:connect:us-east-1:123:instance/i/contact-flow/cf",
        "sourcePhoneNumber": "+19734949660",
        "dialer": {
            "type": "progressive",
            "bandwidthAllocation": 1.0,
            "dialingCapacity": 1.0,
        },
        "answerMachineDetection": {"enabled": True, "awaitPrompt": True},
        "schedule": {
            "startTime": "2026-04-23T14:00:00Z",
            "endTime": "2026-04-23T22:00:00Z",
        },
        "communicationTime": {"timezone": "America/New_York"},
    }


def test_segment_source_produces_expected_nested_structure():
    params = build_create_campaign_params(
        _base_body(),
        connect_instance_id="instance-1",
        profiles_domain_arn="arn:aws:profile:us-east-1:123:domains/d",
    )

    assert params["name"] == "test-campaign"
    assert params["connectInstanceId"] == "instance-1"
    assert params["connectCampaignFlowArn"].startswith("arn:aws:connect:")
    assert params["source"]["customerProfilesSegmentArn"].endswith("/seg")

    telephony = params["channelSubtypeConfig"]["telephony"]
    assert telephony["connectQueueId"] == "queue-1"
    assert telephony["outboundMode"]["progressive"]["bandwidthAllocation"] == 1.0
    assert telephony["capacity"] == 1.0
    assert telephony["defaultOutboundConfig"]["connectContactFlowId"] == "flow-1"
    assert (
        telephony["defaultOutboundConfig"]["connectSourcePhoneNumber"] == "+19734949660"
    )
    assert "ringTimeout" not in telephony["defaultOutboundConfig"]

    amd = telephony["defaultOutboundConfig"]["answerMachineDetectionConfig"]
    assert amd["enableAnswerMachineDetection"] is True
    assert amd["awaitAnswerMachinePrompt"] is True

    assert params["schedule"]["startTime"] == "2026-04-23T14:00:00Z"
    assert params["communicationTimeConfig"]["localTimeZoneConfig"] == {
        "localTimeZoneDetection": ["AREA_CODE"]
    }


def test_owner_tag_is_added_when_instance_arn_provided():
    """The Connect SLR's HighVolumeOutboundCommunicationAccess policy gates
    DescribeCampaign on tag owner=<instance-arn>; without it the Connect
    console returns 403 when opening campaigns."""
    body = _base_body()
    params = build_create_campaign_params(
        body,
        connect_instance_id="instance-1",
        profiles_domain_arn="arn:aws:profile:us-east-1:123:domains/d",
        instance_arn="arn:aws:connect:us-east-1:165505826690:instance/abc-123",
    )
    assert (
        params["tags"]["owner"]
        == "arn:aws:connect:us-east-1:165505826690:instance/abc-123"
    )


def test_owner_tag_does_not_clobber_caller_tags():
    body = _base_body()
    body["tags"] = {"costCenter": "VIP-OPS"}
    params = build_create_campaign_params(
        body,
        connect_instance_id="instance-1",
        profiles_domain_arn="arn:aws:profile:us-east-1:123:domains/d",
        instance_arn="arn:aws:connect:us-east-1:165505826690:instance/abc-123",
    )
    assert params["tags"]["costCenter"] == "VIP-OPS"
    assert params["tags"]["owner"].endswith("instance/abc-123")


def test_create_without_campaign_flow_arn_omits_field():
    """campaignFlowArn is optional in V2; the builder must skip it cleanly."""
    body = _base_body()
    del body["campaignFlowArn"]

    params = build_create_campaign_params(
        body,
        connect_instance_id="instance-1",
        profiles_domain_arn="arn:aws:profile:us-east-1:123:domains/d",
    )

    # No KeyError, no field, no empty string — just absent.
    assert "connectCampaignFlowArn" not in params

    # Empty-string value should also be treated as absent (UI sends "" when blank).
    body_blank = _base_body()
    body_blank["campaignFlowArn"] = ""
    params_blank = build_create_campaign_params(
        body_blank,
        connect_instance_id="instance-1",
        profiles_domain_arn="arn:aws:profile:us-east-1:123:domains/d",
    )
    assert "connectCampaignFlowArn" not in params_blank


@pytest.mark.parametrize(
    "communication_time", [None, {"timezone": "America/Los_Angeles"}]
)
def test_event_trigger_source_strips_communication_time_config(communication_time):
    body = _base_body()
    del body["segmentArn"]  # no segment → falls back to event trigger
    if communication_time is None:
        body.pop("communicationTime")
    else:
        body["communicationTime"] = communication_time

    params = build_create_campaign_params(
        body,
        connect_instance_id="instance-1",
        profiles_domain_arn="arn:aws:profile:us-east-1:123:domains/d",
    )

    assert "eventTrigger" in params["source"]
    assert params["source"]["eventTrigger"]["customerProfilesDomainArn"].endswith(
        ":domains/d"
    )
    # eventTrigger campaigns don't allow communicationTimeConfig
    assert "communicationTimeConfig" not in params


def test_invalid_dialer_type_raises():
    body = _base_body()
    body["dialer"]["type"] = "hybrid"  # not valid

    with pytest.raises(ValueError, match="Invalid dialer type"):
        build_create_campaign_params(
            body,
            connect_instance_id="instance-1",
            profiles_domain_arn="arn:aws:profile:us-east-1:123:domains/d",
        )


def test_missing_required_field_raises():
    body = _base_body()
    del body["queueId"]

    with pytest.raises(ValueError, match="Missing required fields"):
        build_create_campaign_params(
            body,
            connect_instance_id="instance-1",
            profiles_domain_arn="arn:aws:profile:us-east-1:123:domains/d",
        )


def test_communication_limits_translation():
    body = _base_body()
    body["communicationLimits"] = {"perDay": 3, "perWeek": 10, "perMonth": 20}

    params = build_create_campaign_params(
        body,
        connect_instance_id="instance-1",
        profiles_domain_arn="arn:aws:profile:us-east-1:123:domains/d",
    )

    limits = params["communicationLimitsOverride"]["allChannelSubtypes"][
        "communicationLimitsList"
    ]
    assert len(limits) == 3
    # Each limit is a dict with maxCountPerRecipient + frequency
    per_day = next(lim for lim in limits if lim["frequency"]["value"] == 1)
    per_week = next(lim for lim in limits if lim["frequency"]["value"] == 7)
    per_month = next(lim for lim in limits if lim["frequency"]["value"] == 30)
    assert per_day["maxCountPerRecipient"] == 3
    assert per_week["maxCountPerRecipient"] == 10
    assert per_month["maxCountPerRecipient"] == 20


_CONTACT_DAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY"]


@pytest.mark.parametrize("legacy_timezone", ["America/New_York", "America/Los_Angeles"])
def test_communication_time_config_uses_per_recipient_area_code_detection(
    legacy_timezone,
):
    """AWS rejects defaultTimeZone together with localTimeZoneDetection.

    Legacy UI timezone values must not reintroduce a fixed zone when recipient
    detection is selected; botocore's shape validation does not catch this.
    """
    body = _base_body()
    body["communicationTime"] = {"timezone": legacy_timezone}
    params = build_create_campaign_params(
        body,
        connect_instance_id="instance-1",
        profiles_domain_arn="arn:aws:profile:us-east-1:123:domains/d",
    )
    ltz = params["communicationTimeConfig"]["localTimeZoneConfig"]
    assert ltz["localTimeZoneDetection"] == ["AREA_CODE"]
    assert "defaultTimeZone" not in ltz


def test_communication_time_config_sets_telephony_open_hours_monday_to_saturday():
    params = build_create_campaign_params(
        _base_body(),
        connect_instance_id="instance-1",
        profiles_domain_arn="arn:aws:profile:us-east-1:123:domains/d",
    )
    daily = params["communicationTimeConfig"]["telephony"]["openHours"]["dailyHours"]
    assert sorted(daily) == sorted(_CONTACT_DAYS)
    for day in _CONTACT_DAYS:
        assert daily[day] == [{"startTime": "T08:00", "endTime": "T21:00"}]


def test_sunday_key_is_absent_from_daily_hours():
    """No contact on Sunday. Encoded by OMITTING the key, not by an empty list —
    see the shape table: both are syntactically valid, and this is the one whose
    semantics we are betting on. Task 7 verifies it against a real dial attempt.
    """
    params = build_create_campaign_params(
        _base_body(),
        connect_instance_id="instance-1",
        profiles_domain_arn="arn:aws:profile:us-east-1:123:domains/d",
    )
    daily = params["communicationTimeConfig"]["telephony"]["openHours"]["dailyHours"]
    assert "SUNDAY" not in daily


def test_open_hours_times_carry_the_iso8601_t_prefix():
    """Iso8601Time's pattern is T\\d{2}:\\d{2}.

    botocore does NOT enforce string patterns, so a missing T validates clean
    locally and is sent to the service — this test is the only thing standing
    between a typo and a rejected (or misread) CreateCampaign in production.
    Nothing else in this repo uses openHours, so there is no precedent to
    compare against.
    """
    params = build_create_campaign_params(
        _base_body(),
        connect_instance_id="instance-1",
        profiles_domain_arn="arn:aws:profile:us-east-1:123:domains/d",
    )
    daily = params["communicationTimeConfig"]["telephony"]["openHours"]["dailyHours"]
    for ranges in daily.values():
        for rng in ranges:
            assert rng["startTime"].startswith("T")
            assert rng["endTime"].startswith("T")


def test_builders_module_imports_and_builds_open_hours_with_phonenumbers_blocked():
    """Regression guard for the api-plans/api-campaigns cold-start outage:
    builders.py must import (and its openHours builder must still work) even
    when `phonenumbers` is not importable.

    builders.py imports `connect_open_hours` from
    vip_shared.domain.services.connect_open_hours, which is intentionally
    kept free of any phonenumbers dependency — unlike its sibling
    quiet_hours.py (the per-recipient SMS gate), which unconditionally
    `import phonenumbers` at module scope. api-campaigns's Lambda layer is
    built from plain requirements.txt (no phonenumbers; only api-sms's layer
    has it via requirements-sms.txt — see infra/lib/utils/shared-layer.ts).
    If builders.py ever re-imports from quiet_hours.py instead, every
    api-campaigns cold start crashes with ModuleNotFoundError.

    `phonenumbers` is ambiently pip-installed on dev machines, which is
    exactly how this bug shipped invisibly through a full green pytest run
    before. This test blocks it for real via `sys.modules['phonenumbers'] =
    None` and forces a FRESH import of both builders.py and
    connect_open_hours.py (popping any cached module first — a cached
    module's already-executed import statements wouldn't be re-run and would
    hide a regression).
    """
    _OPEN_HOURS_MODULE = "vip_shared.domain.services.connect_open_hours"
    saved_builders = sys.modules.pop("builders", None)
    saved_open_hours = sys.modules.pop(_OPEN_HOURS_MODULE, None)
    saved_phonenumbers = sys.modules.get("phonenumbers")
    sys.modules["phonenumbers"] = None  # type: ignore[assignment]
    try:
        import builders as fresh_builders

        params = fresh_builders.build_create_campaign_params(
            _base_body(),
            connect_instance_id="instance-1",
            profiles_domain_arn="arn:aws:profile:us-east-1:123:domains/d",
        )
    finally:
        del sys.modules["phonenumbers"]
        if saved_phonenumbers is not None:
            sys.modules["phonenumbers"] = saved_phonenumbers
        sys.modules.pop("builders", None)
        sys.modules.pop(_OPEN_HOURS_MODULE, None)
        if saved_builders is not None:
            sys.modules["builders"] = saved_builders
        if saved_open_hours is not None:
            sys.modules[_OPEN_HOURS_MODULE] = saved_open_hours

    daily = params["communicationTimeConfig"]["telephony"]["openHours"]["dailyHours"]
    assert sorted(daily) == sorted(_CONTACT_DAYS)
    assert daily["SATURDAY"] == [{"startTime": "T08:00", "endTime": "T21:00"}]
    assert "SUNDAY" not in daily


def test_communication_time_config_emitted_even_without_communication_time_key():
    """Previously a segment campaign created without body['communicationTime']
    got NO communicationTimeConfig at all — i.e. zero quiet-hours enforcement.
    That hole is the point of this change."""
    body = _base_body()
    body.pop("communicationTime", None)
    params = build_create_campaign_params(
        body,
        connect_instance_id="instance-1",
        profiles_domain_arn="arn:aws:profile:us-east-1:123:domains/d",
    )
    assert "communicationTimeConfig" in params
    ctc = params["communicationTimeConfig"]
    assert ctc["localTimeZoneConfig"] == {"localTimeZoneDetection": ["AREA_CODE"]}
    assert ctc["telephony"]["openHours"]["dailyHours"] == {
        day: [{"startTime": "T08:00", "endTime": "T21:00"}] for day in _CONTACT_DAYS
    }
