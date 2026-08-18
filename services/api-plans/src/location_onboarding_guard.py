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


def _is_first_occurrence_of_state(state_code: str, exclude_locations: set) -> bool:
    """True if no item OUTSIDE this batch's own inserts already has this stateCode.

    `exclude_locations` must contain every location belonging to this
    stateCode that was INSERTed in the *current* Streams batch (not just the
    single triggering record) — sibling INSERTs for a brand-new state land in
    the same batch and are already durably committed to the table by the
    time we scan, so excluding only one of them would make every sibling
    record see the others and wrongly conclude the state pre-existed.

    Known residual limitation (accepted, not hidden): if a new state's
    locations are onboarded in a burst LARGER than this Lambda's
    batchSize (10) and span multiple separate Streams batches/invocations,
    cross-batch siblings are NOT excluded here and the alert can still be
    suppressed for the batches that land after the first one. This Lambda
    is an ops alarm, not a safety-critical control, so cross-invocation
    dedup (e.g. a DynamoDB lock table) is intentionally not built for that
    edge case.
    """
    resp = _table().scan(
        FilterExpression="stateCode = :code",
        ExpressionAttributeValues={":code": state_code},
        ProjectionExpression="#loc",
        ExpressionAttributeNames={"#loc": "location"},
    )
    other_locations = {item["location"] for item in resp.get("Items", [])} - exclude_locations
    return len(other_locations) == 0


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
    inserts = []
    for record in event.get("Records", []):
        if record.get("eventName") != "INSERT":
            continue

        raw_image = record.get("dynamodb", {}).get("NewImage", {})
        image = {k: _deserializer.deserialize(v) for k, v in raw_image.items()}

        state_code = image.get("stateCode")
        location = image.get("location")
        if not state_code or not location:
            continue

        inserts.append((state_code, location, image.get("canonicalPhone")))

    # Group this batch's own inserted locations by stateCode so that
    # sibling INSERTs (e.g. a brand-new state's locations added together in
    # one BatchWriteItem) can all be excluded from the "does this state
    # already exist" scan below — not just the one record currently being
    # processed. See _is_first_occurrence_of_state docstring for why.
    locations_by_state: dict = {}
    for state_code, location, _ in inserts:
        locations_by_state.setdefault(state_code, set()).add(location)

    alerted_states = set()
    for state_code, location, canonical_phone in inserts:
        if canonical_phone:
            continue

        if state_code in alerted_states:
            continue

        if not _is_first_occurrence_of_state(state_code, locations_by_state[state_code]):
            continue

        alerted_states.add(state_code)
        _LOG.info(json.dumps({
            "event": "location_onboarding_missing_phone_detected",
            "state_code": state_code,
        }))
        _notify_missing_phone(state_code, location)
