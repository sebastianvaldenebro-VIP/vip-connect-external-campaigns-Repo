"""CRUD + lifecycle handlers for Outbound Campaigns V2."""

from __future__ import annotations

import os

from vip_shared.application.http import (
    extract_caller,
    json_response,
    parse_body,
)
from vip_shared.infrastructure.persistence.audit import build_from_env as build_audit
from vip_shared.infrastructure.persistence.outbound_campaigns_client import (
    build as build_oc,
)

from builders import build_create_campaign_params

INSTANCE_ID = os.environ.get("CONNECT_INSTANCE_ID", "")


# ── CRUD ───────────────────────────────────────────────────────────────


def list_campaigns(event: dict, _path_params: dict) -> dict:
    oc = build_oc()
    qs = event.get("queryStringParameters") or {}
    max_results = int(qs.get("maxResults", 25))
    next_token = qs.get("nextToken")

    response = oc.list_campaigns(max_results=max_results, next_token=next_token)
    return json_response(
        200,
        {
            "campaigns": [
                _serialize_summary(c) for c in response.get("campaignSummaryList", [])
            ],
            "nextToken": response.get("nextToken"),
        },
    )


def get_campaign(event: dict, path_params: dict) -> dict:
    campaign_id = path_params["id"]
    oc = build_oc()
    describe = oc.describe_campaign(campaign_id)
    state = oc.get_campaign_state(campaign_id)
    return json_response(
        200,
        {
            "campaign": describe.get("campaign"),
            "state": state.get("state"),
        },
    )


def create_campaign(event: dict, _path_params: dict) -> dict:
    body = parse_body(event)
    caller = extract_caller(event)

    profiles_domain_name = os.environ["PROFILES_DOMAIN_NAME"]
    profiles_domain_arn = _domain_arn(profiles_domain_name)

    params = build_create_campaign_params(
        body,
        connect_instance_id=INSTANCE_ID,
        profiles_domain_arn=profiles_domain_arn,
        instance_arn=_instance_arn(INSTANCE_ID),
    )

    oc = build_oc()
    response = oc.create_campaign(**params)

    build_audit().record(
        entity_type="campaign",
        entity_id=response["id"],
        action="create",
        actor_sub=caller.sub,
        actor_email=caller.email,
        ip_address=caller.ip_address,
        user_agent=caller.user_agent,
        after={
            "id": response["id"],
            "name": body["name"],
            "segmentArn": body.get("segmentArn"),
            "queueId": body["queueId"],
            "dialer": body["dialer"],
            "schedule": body["schedule"],
        },
    )

    return json_response(
        201,
        {
            "id": response["id"],
            "arn": response["arn"],
        },
    )


def delete_campaign(event: dict, path_params: dict) -> dict:
    campaign_id = path_params["id"]
    caller = extract_caller(event)
    oc = build_oc()

    before_state = oc.get_campaign_state(campaign_id).get("state")

    # V2 requires the campaign to be stopped before deletion
    if before_state == "Running":
        oc.stop_campaign(campaign_id)
    oc.delete_campaign(campaign_id)

    build_audit().record(
        entity_type="campaign",
        entity_id=campaign_id,
        action="delete",
        actor_sub=caller.sub,
        actor_email=caller.email,
        ip_address=caller.ip_address,
        user_agent=caller.user_agent,
        before={"state": before_state},
    )

    return json_response(204, {})


def update_campaign(event: dict, path_params: dict) -> dict:
    """Selective updates — only fields that V2 supports via Update* APIs."""
    campaign_id = path_params["id"]
    caller = extract_caller(event)
    body = parse_body(event)

    # Validate before touching AWS
    updatable_keys = {"name", "segmentArn", "schedule"}
    if not any(key in body for key in updatable_keys):
        raise ValueError("No updatable fields in request")

    oc = build_oc()
    applied: dict = {}

    if "name" in body:
        oc.update_campaign_name(campaign_id, body["name"])
        applied["name"] = body["name"]

    if "segmentArn" in body:
        source = {"customerProfilesSegmentArn": body["segmentArn"]}
        oc.update_campaign_source(campaign_id, source)
        applied["segmentArn"] = body["segmentArn"]

    if "schedule" in body:
        oc.update_campaign_schedule(campaign_id, body["schedule"])
        applied["schedule"] = body["schedule"]

    build_audit().record(
        entity_type="campaign",
        entity_id=campaign_id,
        action="update",
        actor_sub=caller.sub,
        actor_email=caller.email,
        ip_address=caller.ip_address,
        user_agent=caller.user_agent,
        after=applied,
    )

    return json_response(200, {"id": campaign_id, "updated": applied})


# ── Lifecycle ──────────────────────────────────────────────────────────


def start_campaign(event: dict, path_params: dict) -> dict:
    return _lifecycle_action(event, path_params, action="start")


def stop_campaign(event: dict, path_params: dict) -> dict:
    return _lifecycle_action(event, path_params, action="stop")


def pause_campaign(event: dict, path_params: dict) -> dict:
    return _lifecycle_action(event, path_params, action="pause")


def resume_campaign(event: dict, path_params: dict) -> dict:
    return _lifecycle_action(event, path_params, action="resume")


def _lifecycle_action(event: dict, path_params: dict, *, action: str) -> dict:
    campaign_id = path_params["id"]
    caller = extract_caller(event)
    oc = build_oc()

    method = getattr(oc, f"{action}_campaign")
    method(campaign_id)
    state = oc.get_campaign_state(campaign_id).get("state")

    build_audit().record(
        entity_type="campaign",
        entity_id=campaign_id,
        action=action,
        actor_sub=caller.sub,
        actor_email=caller.email,
        ip_address=caller.ip_address,
        user_agent=caller.user_agent,
        after={"state": state},
    )

    return json_response(200, {"id": campaign_id, "state": state})


# ── Helpers ────────────────────────────────────────────────────────────


def _serialize_summary(summary: dict) -> dict:
    return {
        "id": summary.get("id"),
        "arn": summary.get("arn"),
        "name": summary.get("name"),
        "status": summary.get("status"),
        "schedule": summary.get("schedule"),
        "source": summary.get("source"),
        "channelSubtypes": summary.get("channelSubtypes", []),
    }


def _domain_arn(domain_name: str) -> str:
    region = os.environ.get("AWS_REGION", "us-east-1")
    account = os.environ.get("AWS_ACCOUNT_ID", "")
    return f"arn:aws:profile:{region}:{account}:domains/{domain_name}"


def _instance_arn(instance_id: str) -> str:
    region = os.environ.get("AWS_REGION", "us-east-1")
    account = os.environ.get("AWS_ACCOUNT_ID", "")
    return f"arn:aws:connect:{region}:{account}:instance/{instance_id}"
