"""Resource lookup handlers for form dropdowns (queues, flows, phones)."""

from __future__ import annotations

import os

from vip_shared.application.http import json_response
from vip_shared.infrastructure.persistence.connect_client import build_from_env


def list_queues(event: dict, _path_params: dict) -> dict:
    """GET /campaigns/resources/queues — STANDARD queues for dropdown."""
    client = build_from_env()
    items = client.list_queues(queue_types=["STANDARD"])
    return json_response(
        200,
        {
            "queues": [
                {
                    "id": q.get("Id"),
                    "arn": q.get("Arn"),
                    "name": q.get("Name"),
                    "queueType": q.get("QueueType"),
                }
                for q in items
            ]
        },
    )


def list_contact_flows(event: dict, _path_params: dict) -> dict:
    """GET /campaigns/resources/contact-flows — flows filtered by relevance.

    By default returns CONTACT_FLOW and CAMPAIGN types, plus OUTBOUND_WHISPER.
    The UI can filter further client-side by name pattern.
    """
    qs = event.get("queryStringParameters") or {}
    requested_types = qs.get("types")
    if requested_types:
        types = [t.strip() for t in requested_types.split(",") if t.strip()]
    else:
        types = ["CONTACT_FLOW", "CAMPAIGN", "OUTBOUND_WHISPER", "AGENT_WHISPER"]

    client = build_from_env()
    items = client.list_contact_flows(contact_flow_types=types)
    return json_response(
        200,
        {
            "contactFlows": [
                {
                    "id": f.get("Id"),
                    "arn": f.get("Arn"),
                    "name": f.get("Name"),
                    "contactFlowType": f.get("ContactFlowType"),
                }
                for f in items
            ]
        },
    )


def list_phone_numbers(event: dict, _path_params: dict) -> dict:
    """GET /campaigns/resources/phone-numbers — claimed numbers for caller ID dropdown."""
    instance_id = os.environ["CONNECT_INSTANCE_ID"]
    region = os.environ.get("AWS_REGION", "us-east-1")
    account = os.environ.get("AWS_ACCOUNT_ID", "")
    target_arn = f"arn:aws:connect:{region}:{account}:instance/{instance_id}"

    client = build_from_env()
    items = client.list_phone_numbers(target_arn=target_arn)
    return json_response(
        200,
        {
            "phoneNumbers": [
                {
                    "arn": p.get("PhoneNumberArn"),
                    "number": p.get("PhoneNumber"),
                    "type": p.get("PhoneNumberType"),
                    "country": p.get("PhoneNumberCountryCode"),
                }
                for p in items
            ]
        },
    )
