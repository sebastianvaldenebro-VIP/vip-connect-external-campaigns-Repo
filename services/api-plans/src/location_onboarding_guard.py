"""Alarm when a brand-new state appears in VipLocationMapping with no
canonical phone number configured.

Triggered by DynamoDB Streams INSERT events on VipLocationMapping.
Flow auto-creation for new states is handled elsewhere
(builders.resolve_campaign_flow_arn, called from executor.py) — this
Lambda does detection + SNS alerting ONLY, no Connect permissions.
"""
import json
import logging
import os

import boto3
from boto3.dynamodb.types import TypeDeserializer

_LOG = logging.getLogger(__name__)
_LOG.setLevel(logging.INFO)

_SNS_ALERTS_TOPIC_ARN = os.environ["SNS_ALERTS_TOPIC_ARN"]
_LOCATION_MAPPING_TABLE = os.environ["LOCATION_MAPPING_TABLE"]

_deserializer = TypeDeserializer()
_sns = None
_ddb_table = None


def _sns_client():
    global _sns
    if _sns is None:
        _sns = boto3.client("sns")
    return _sns


def _table():
    global _ddb_table
    if _ddb_table is None:
        _ddb_table = boto3.resource("dynamodb").Table(_LOCATION_MAPPING_TABLE)
    return _ddb_table


def _is_first_occurrence_of_state(state_code: str, exclude_location: str) -> bool:
    """True if no OTHER item in the table already has this stateCode."""
    resp = _table().scan(
        FilterExpression="stateCode = :code AND #loc <> :loc",
        ExpressionAttributeNames={"#loc": "location"},
        ExpressionAttributeValues={":code": state_code, ":loc": exclude_location},
        ProjectionExpression="#loc",
    )
    return len(resp.get("Items", [])) == 0


def _notify_missing_phone(state_code: str, location: str) -> None:
    subject = f"New state detected with no canonical phone: {state_code}"[:100]
    message = (
        f"A new location ({location}) introduced state code '{state_code}' to "
        f"VipLocationMapping, but no canonicalPhone attribute is set for it.\n\n"
        f"Set canonicalPhone (and areaCodes) on this state's VipLocationMapping "
        f"items before enabling campaigns for it — see "
        f"infra/scripts/backfill-location-canonical-phone.py for the pattern."
    )
    try:
        _sns_client().publish(
            TopicArn=_SNS_ALERTS_TOPIC_ARN,
            Subject=subject,
            Message=message,
            MessageAttributes={
                "stateCode": {"DataType": "String", "StringValue": state_code},
                "event": {"DataType": "String", "StringValue": "location_onboarding_missing_phone"},
            },
        )
    except Exception as exc:
        _LOG.warning(
            "location_onboarding_guard: SNS publish failed (topic=%s): %s",
            _SNS_ALERTS_TOPIC_ARN,
            exc,
        )


def lambda_handler(event: dict, _context) -> None:
    for record in event.get("Records", []):
        if record.get("eventName") != "INSERT":
            continue

        raw_image = record.get("dynamodb", {}).get("NewImage", {})
        image = {k: _deserializer.deserialize(v) for k, v in raw_image.items()}

        state_code = image.get("stateCode")
        location = image.get("location")
        if not state_code or not location:
            continue

        canonical_phone = image.get("canonicalPhone")

        if canonical_phone:
            continue

        if not _is_first_occurrence_of_state(state_code, location):
            continue

        _LOG.info(json.dumps({
            "event": "location_onboarding_missing_phone_detected",
            "state_code": state_code,
        }))
        _notify_missing_phone(state_code, location)
