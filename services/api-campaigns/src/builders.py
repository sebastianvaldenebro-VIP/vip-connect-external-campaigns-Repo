"""Transformers from UI-friendly request bodies to AWS V2 API payloads.

The UI sends a simpler, flatter shape; this module produces the nested
`channelSubtypeConfig` / `source` / `schedule` structures that
`connectcampaignsv2:CreateCampaign` expects.
"""

from __future__ import annotations

from typing import Any

# TCPA quiet hours are a property of the *recipient's* local time, not of a
# timezone the operator picks once per campaign. Connect Campaigns V2 resolves
# the recipient's timezone itself; we only declare the window.
#
# AREA_CODE over ZIP_CODE: every phone number has an area code, whereas
# ZIP_CODE needs a populated Customer Profiles address we have not confirmed.
#
# The window is the FULL statutory TCPA span on the hours axis (08:00-21:00
# recipient-local) and stricter than statute on the day axis: Monday-Saturday
# only. TCPA does not exempt Sunday; excluding it is a VIP business choice.
#
# The "T" prefix is mandatory — Iso8601Time's pattern is T\d{2}:\d{2}. Note
# that botocore does NOT enforce string patterns: a bare "08:00" validates
# clean locally and is sent to the service. The unit tests are the only guard.
#
# SUNDAY is excluded by OMITTING the key from dailyHours. An empty list
# ("SUNDAY": []) is equally valid per the model but its semantics are
# undocumented; see the shape table in this task.
_QUIET_HOURS_START = "T08:00"
_QUIET_HOURS_END = "T21:00"
_CONTACT_DAYS = (
    "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY",
)


def _open_hours() -> dict[str, Any]:
    """A TimeWindow gating one channel to the recipient-local quiet-hours window."""
    return {
        "openHours": {
            "dailyHours": {
                day: [{"startTime": _QUIET_HOURS_START, "endTime": _QUIET_HOURS_END}]
                for day in _CONTACT_DAYS
            }
        }
    }


def build_create_campaign_params(
    body: dict,
    *,
    connect_instance_id: str,
    profiles_domain_arn: str,
    instance_arn: str | None = None,
) -> dict[str, Any]:
    """Translate UI body → V2 CreateCampaign kwargs.

    Body (from UI form):
    {
      "name": "...",
      "segmentArn": "arn:aws:profile:...",   # optional — if present, segment source
      "queueId": "...",
      "contactFlowId": "...",
      "campaignFlowArn": "...",
      "sourcePhoneNumber": "+1...",
      "dialer": {"type": "progressive"|"predictive"|"agentless", "bandwidthAllocation": float, "dialingCapacity": float},
      "answerMachineDetection": {"enabled": bool, "awaitPrompt": bool},
      "schedule": {"startTime": "...Z", "endTime": "...Z"},
      "communicationTime": {"timezone": "..."},  # ambiguous-area-code fallback only
      "communicationLimits": {"perDay": int, "perWeek": int, "perMonth": int} (optional),
      "tags": {...} (optional)
    }
    """
    # connectCampaignFlowArn is optional in V2 connectcampaignsv2 — campaigns
    # that drive plain-voice agent dialing don't need a campaign-type contact
    # flow. We only forward it to AWS when the caller actually provided one.
    _require(
        body,
        ("name", "queueId", "contactFlowId", "sourcePhoneNumber", "dialer", "schedule"),
    )

    dialer = body["dialer"]
    dialer_type = dialer["type"]
    if dialer_type not in {"predictive", "progressive", "agentless"}:
        raise ValueError(f"Invalid dialer type: {dialer_type}")

    outbound_mode = {
        dialer_type: {
            "bandwidthAllocation": float(dialer.get("bandwidthAllocation", 1.0))
        }
    }

    amd = body.get("answerMachineDetection", {"enabled": True, "awaitPrompt": True})

    channel_subtype_config = {
        "telephony": {
            "capacity": float(dialer.get("dialingCapacity", 1.0)),
            "connectQueueId": body["queueId"],
            "outboundMode": outbound_mode,
            "defaultOutboundConfig": {
                "connectContactFlowId": body["contactFlowId"],
                "connectSourcePhoneNumber": body["sourcePhoneNumber"],
                "answerMachineDetectionConfig": {
                    "enableAnswerMachineDetection": bool(amd.get("enabled", True)),
                    "awaitAnswerMachinePrompt": bool(amd.get("awaitPrompt", True)),
                },
            },
        }
    }

    source = _build_source(body, profiles_domain_arn=profiles_domain_arn)

    schedule = {
        "startTime": body["schedule"]["startTime"],
        "endTime": body["schedule"]["endTime"],
    }

    params: dict[str, Any] = {
        "name": body["name"],
        "connectInstanceId": connect_instance_id,
        "channelSubtypeConfig": channel_subtype_config,
        "source": source,
        "schedule": schedule,
    }

    if body.get("campaignFlowArn"):
        params["connectCampaignFlowArn"] = body["campaignFlowArn"]

    # communicationTimeConfig only valid for segment-source campaigns, not event-trigger
    if "segmentArn" in body:
        comm_time = body.get("communicationTime") or {}
        params["communicationTimeConfig"] = {
            "localTimeZoneConfig": {
                # Fallback only — used when Connect cannot resolve the area code.
                "defaultTimeZone": comm_time.get("timezone", "America/New_York"),
                "localTimeZoneDetection": ["AREA_CODE"],
            },
            "telephony": _open_hours(),
        }

    if body.get("communicationLimits"):
        limits = body["communicationLimits"]
        params["communicationLimitsOverride"] = {
            "allChannelSubtypes": {
                "communicationLimitsList": [
                    _build_limit(limits, "perDay", "DAYS", 1),
                    _build_limit(limits, "perWeek", "DAYS", 7),
                    _build_limit(limits, "perMonth", "DAYS", 30),
                ]
            }
        }

    # Always tag the campaign with the owning Connect instance ARN. The
    # AWSServiceRoleForAmazonConnect SLR's HighVolumeOutboundCommunicationAccess
    # policy gates `connect-campaigns:DescribeCampaign` (and others) on this
    # tag matching the instance ARN — without it, the Connect console returns
    # 403 when an operator opens the campaign. AWS Connect's own console adds
    # this tag automatically; we have to do it manually because we create
    # campaigns directly via the V2 API.
    tags: dict[str, str] = dict(body.get("tags") or {})
    if instance_arn:
        tags["owner"] = instance_arn
    params["tags"] = tags

    return params


def _build_source(body: dict, *, profiles_domain_arn: str) -> dict:
    """Choose source type based on request body."""
    if body.get("segmentArn"):
        return {"customerProfilesSegmentArn": body["segmentArn"]}
    # eventTrigger default (rarely used by the UI, but supported)
    return {"eventTrigger": {"customerProfilesDomainArn": profiles_domain_arn}}


def _build_limit(limits: dict, key: str, unit: str, value: int) -> dict:
    max_count = int(limits.get(key, 0))
    return {
        "maxCountPerRecipient": max_count,
        "frequency": {"unit": unit, "value": value},
    }


def _require(body: dict, fields: tuple[str, ...]) -> None:
    missing = [f for f in fields if f not in body or body[f] is None]
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(missing)}")
