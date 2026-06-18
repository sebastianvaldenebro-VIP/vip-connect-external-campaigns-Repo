# services/api-progressive-dialer/src/handler_seeder.py
"""Seed VipProgressiveCampaignQueue from a Customer Profiles segment.

Flow:
  POST /campaigns/{id}/seed-branded {"segmentName": "my-segment"}
  1. GetSegmentDefinition(segmentName) → SegmentGroups
  2. Parse all profile IDs from SegmentGroups (ID IN [...] filter built by reconcile.py)
  3. BatchGetProfile in chunks of 100 → profile["PhoneNumber"]
  4. BatchWriteItem to VipProgressiveCampaignQueue

HIPAA: phone numbers are not logged. Only counts appear in logs.
"""
import json
import os
import time
import uuid
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

_ddb_table = None
_cp_client = None

_PROFILES_DOMAIN = os.environ["PROFILES_DOMAIN_NAME"]
_QUEUE_TABLE = os.environ["CAMPAIGN_QUEUE_TABLE"]
_BATCH_SIZE = 100  # CP BatchGetProfile max per call


def _get_table():
    global _ddb_table
    if _ddb_table is None:
        _ddb_table = boto3.resource("dynamodb").Table(_QUEUE_TABLE)
    return _ddb_table


def _get_cp():
    global _cp_client
    if _cp_client is None:
        _cp_client = boto3.client("customer-profiles")
    return _cp_client


def _extract_profile_ids(segment_groups: dict) -> list:
    """Parse all customer/profile IDs from SegmentGroups.

    reconcile.py builds segments via SegmentGroupsTranslator.customer_ids_to_segment_groups,
    which produces this nesting:
      Groups[].Dimensions[].ProfileAttributes.Attributes.ID.Values[...]
    Note the Attributes key between ProfileAttributes and the field name — a direct
    ProfileAttributes.ID lookup always returns None and yields an empty list.
    """
    ids = []
    for group in (segment_groups.get("Groups") or []):
        for dimension in (group.get("Dimensions") or []):
            attrs = (dimension.get("ProfileAttributes") or {}).get("Attributes") or {}
            id_dim = attrs.get("ID") or {}
            ids.extend(id_dim.get("Values") or [])
    return ids


def _fetch_phones(profile_ids: list) -> list:
    """Return phone numbers for the given profile IDs using BatchGetProfile.

    Profiles without PhoneNumber are silently skipped (no logging of PHI).
    """
    if not profile_ids:
        return []
    phones = []
    cp = _get_cp()
    for i in range(0, len(profile_ids), _BATCH_SIZE):
        chunk = profile_ids[i:i + _BATCH_SIZE]
        resp = cp.batch_get_profile(
            DomainName=_PROFILES_DOMAIN,
            ProfileIds=chunk,
        )
        for profile in (resp.get("Profiles") or []):
            phone = profile.get("PhoneNumber")
            if phone:
                phones.append(phone)
    return phones


def lambda_handler(event: dict, _context) -> dict:
    # Support both HTTP API Gateway shape and direct Lambda invocation from executor.
    if "pathParameters" in event:                    # HTTP shape (API Gateway)
        path_params = event.get("pathParameters") or {}
        campaign_id = path_params.get("id")
        body: dict = {}
        raw = event.get("body")
        if raw:
            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return {"statusCode": 400, "body": json.dumps({"error": "invalid JSON body"})}
        _http_mode = True
    else:                                            # Direct Lambda invocation shape
        campaign_id = event.get("campaignId")
        body = event
        _http_mode = False

    if not campaign_id:
        if _http_mode:
            return {"statusCode": 400, "body": json.dumps({"error": "missing campaign id"})}
        raise ValueError("missing campaignId in direct-invoke payload")

    segment_name = body.get("segmentName")
    if not segment_name:
        err = {"error": "missing segmentName"}
        return {"statusCode": 400, "body": json.dumps(err)} if _http_mode else err

    # 1. Get segment definition and extract profile IDs
    try:
        resp = _get_cp().get_segment_definition(
            DomainName=_PROFILES_DOMAIN,
            SegmentDefinitionName=segment_name,
        )
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "ResourceNotFoundException":
            return {"statusCode": 404, "body": json.dumps({"error": "segment not found"})}
        if code == "AccessDeniedException":
            return {"statusCode": 403, "body": json.dumps({"error": "access denied to segment"})}
        return {"statusCode": 500, "body": json.dumps({"error": "failed to read segment"})}

    profile_ids = _extract_profile_ids(resp.get("SegmentGroups") or {})
    if not profile_ids:
        return {"statusCode": 400, "body": json.dumps({"error": "segment has no members"})}

    # 2. Fetch phones via BatchGetProfile
    phones = _fetch_phones(profile_ids)

    # 3. Write to DynamoDB queue (phone is PHI — encrypted at rest via KMS CMK on the table)
    table = _get_table()
    ttl = int(time.time()) + 86400  # 24h TTL
    written = 0

    with table.batch_writer() as batch:
        for phone in phones:
            contact_uuid = str(uuid.uuid4())
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            batch.put_item(Item={
                "campaignId": campaign_id,
                "sk": f"{ts}#{contact_uuid}",
                "contactUUID": contact_uuid,
                "phone": phone,
                "status": "PENDING",
                "ttl": ttl,
            })
            written += 1

    result = {"seeded": written}
    if _http_mode:
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "seeded": written,
                "campaignId": campaign_id,
                "profilesFound": len(profile_ids),
                "contactsWithPhone": written,
            }),
        }
    return result
