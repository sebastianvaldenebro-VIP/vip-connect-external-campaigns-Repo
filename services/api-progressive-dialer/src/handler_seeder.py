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
import logging
import os
import time
import uuid
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

_LOG = logging.getLogger(__name__)
_LOG.setLevel(logging.INFO)

_ddb_table = None
_cp_client = None

_PROFILES_DOMAIN = os.environ["PROFILES_DOMAIN_NAME"]
_QUEUE_TABLE = os.environ["CAMPAIGN_QUEUE_TABLE"]
_BATCH_SIZE = 20  # CP BatchGetProfile hard limit per call


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


def _extract_phones_from_filter(segment_groups: dict) -> list:
    """Extract phone numbers from phone-filter segments.

    Handles segments where the filter IS the phone list:
      Groups[].Dimensions[].ProfileAttributes.PhoneNumber.DimensionType=INCLUSIVE Values[...]
    These are created manually in the CP console (not by reconcile.py).
    Phone numbers are PHI — not logged, only counts appear in logs.
    """
    phones = []
    for group in (segment_groups.get("Groups") or []):
        for dimension in (group.get("Dimensions") or []):
            pa = dimension.get("ProfileAttributes") or {}
            phone_dim = pa.get("PhoneNumber") or {}
            if phone_dim.get("DimensionType") == "INCLUSIVE":
                phones.extend(phone_dim.get("Values") or [])
    return phones


def _fetch_phones(profile_ids: list) -> list:
    """Return phone numbers for the given profile IDs using BatchGetProfile.

    Profiles without PhoneNumber are silently skipped (no logging of PHI).
    """
    if not profile_ids:
        return []
    phones = []
    cp = _get_cp()
    for i in range(0, len(profile_ids), _BATCH_SIZE):
        # BatchGetProfile requires 32-char hex without hyphens
        chunk = [pid.replace("-", "") for pid in profile_ids[i:i + _BATCH_SIZE]]
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

    _t0 = time.monotonic()
    segment_name = body.get("segmentName")
    if not segment_name:
        err = {"error": "missing segmentName"}
        return {"statusCode": 400, "body": json.dumps(err)} if _http_mode else err

    _LOG.info(json.dumps({
        "event": "seeder_invoked",
        "campaign_id": campaign_id,
        "segment_name": segment_name,
        "mode": "http" if _http_mode else "direct",
    }))

    # 1. Get segment definition and extract profile IDs
    try:
        resp = _get_cp().get_segment_definition(
            DomainName=_PROFILES_DOMAIN,
            SegmentDefinitionName=segment_name,
        )
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if _http_mode:
            if code == "ResourceNotFoundException":
                return {"statusCode": 404, "body": json.dumps({"error": "segment not found"})}
            if code == "AccessDeniedException":
                return {"statusCode": 403, "body": json.dumps({"error": "access denied to segment"})}
            return {"statusCode": 500, "body": json.dumps({"error": "failed to read segment"})}
        # Direct-invoke mode: raise so executor can handle as a real error
        raise RuntimeError(f"segment lookup failed: {type(exc).__name__}")

    segment_groups = resp.get("SegmentGroups") or {}
    profile_ids = _extract_profile_ids(segment_groups)
    if profile_ids:
        # ID-list segment (built by reconcile.py) — resolve phones via BatchGetProfile
        phones = _fetch_phones(profile_ids)
        _LOG.info(json.dumps({
            "event": "segment_resolved",
            "campaign_id": campaign_id,
            "path": "id_list",
            "profile_ids_found": len(profile_ids),
            "phones_resolved": len(phones),
        }))
    else:
        # Fallback: phone-filter segment (created manually or by executor._create_segment)
        phones = _extract_phones_from_filter(segment_groups)
        _LOG.info(json.dumps({
            "event": "segment_resolved",
            "campaign_id": campaign_id,
            "path": "phone_filter",
            "phones_found": len(phones),
        }))
        if not phones:
            err = {"error": "segment has no members"}
            return {"statusCode": 400, "body": json.dumps(err)} if _http_mode else {"seeded": 0}

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

    _LOG.info(json.dumps({
        "event": "seeder_complete",
        "campaign_id": campaign_id,
        "seeded": written,
        "duration_ms": round((time.monotonic() - _t0) * 1000),
    }))

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
