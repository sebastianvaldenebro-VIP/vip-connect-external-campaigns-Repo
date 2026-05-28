"""Tests for the UI-body → V2 API payload transformer."""

from __future__ import annotations

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
    assert (
        params["communicationTimeConfig"]["localTimeZoneConfig"]["defaultTimeZone"]
        == "America/New_York"
    )


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


def test_event_trigger_source_strips_communication_time_config():
    body = _base_body()
    del body["segmentArn"]  # no segment → falls back to event trigger

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
