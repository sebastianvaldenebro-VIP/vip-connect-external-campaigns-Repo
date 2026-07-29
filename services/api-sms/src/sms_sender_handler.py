"""
SMS Sender Lambda — reads CP segment phone numbers and enqueues them into
VipSmsCampaignQueue (DDB) and SQS vip-sms-campaign-queue for async delivery.

PHI rule:
  - Only E.164 phone numbers are stored in VipSmsCampaignQueue.
  - No names, DOBs, conditions, diagnoses, or any other PHI.
  - Phone numbers are NOT logged.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime, timezone

import boto3

_QUEUE_TABLE = os.environ["SMS_CAMPAIGN_QUEUE_TABLE"]
_RUNS_TABLE = os.environ["SMS_CAMPAIGN_RUNS_TABLE"]
_SQS_QUEUE_URL = os.environ["SMS_SQS_QUEUE_URL"]
_DOMAIN = os.environ["PROFILES_DOMAIN_NAME"]
_TTL_SECONDS = 30 * 24 * 3600  # 30 days

_ddb = boto3.resource("dynamodb")
_sqs = boto3.client("sqs")
_cp = boto3.client("customer-profiles")

# US 10-digit numbers in E.164 format only
_E164_RE = re.compile(r"^\+1\d{10}$")


def lambda_handler(event: dict, context: object) -> dict:
    """
    event = {
      "campaignId": str,               # smsCampaignId
      "planId": str,
      "runId": str,
      "planName": str,
      "segmentArn": str,               # existing CP segment ARN
      "segmentName": str,
      "messageTemplate": str,          # PHI-free template, max 160 chars
      "originationNumberArn": str,     # EUM SMS phone number ARN
      "originationNumber": str,        # friendly E.164 (e.g. +15125551234)
    }
    Returns: {"enqueued": int}
    """
    campaign_id = event["campaignId"]
    segment_arn = event["segmentArn"]
    segment_name = event.get("segmentName", segment_arn.split("/")[-1])
    message_tmpl = event["messageTemplate"]
    origination_arn = event["originationNumberArn"]
    now_epoch = int(time.time())
    now_iso = datetime.now(timezone.utc).isoformat()
    ttl = now_epoch + _TTL_SECONDS

    # Write start record to VipSmsCampaignRuns
    _ddb.Table(_RUNS_TABLE).put_item(
        Item={
            "planId": event["planId"],
            "sk": f"{event['runId']}#{campaign_id}",
            "smsCampaignId": campaign_id,
            "planName": event.get("planName", ""),
            "segmentName": segment_name,
            "segmentArn": segment_arn,
            "messageTemplate": message_tmpl,
            "originationNumberArn": origination_arn,
            "originationNumber": event.get("originationNumber", ""),
            "status": "RUNNING",
            "startedAt": now_iso,
            "totalEnqueued": 0,
            "totalSent": 0,
            "totalFailed": 0,
            "totalOptedOut": 0,
            "createdAt": now_iso,
            "updatedAt": now_iso,
            "pipelineVersion": "v1",
        },
        ConditionExpression="attribute_not_exists(sk)",
    )

    # Extract phone numbers from CP segment
    phones = _get_segment_phones(segment_name)

    # Batch-write to DDB and SQS
    enqueued = 0
    queue_table = _ddb.Table(_QUEUE_TABLE)
    sqs_batch: list[dict] = []
    ddb_batch: list[dict] = []

    for phone in phones:
        if not _E164_RE.match(phone):
            continue
        item_sk = f"{now_iso}#{uuid.uuid4().hex[:8]}"
        sqs_batch.append(
            {
                "Id": uuid.uuid4().hex[:8],
                "MessageBody": json.dumps(
                    {
                        "campaignId": campaign_id,
                        "sk": item_sk,
                        "phone": phone,
                        "messageTemplate": message_tmpl,
                        "originationNumberArn": origination_arn,
                        "planId": event["planId"],
                        "runId": event["runId"],
                    }
                ),
            }
        )
        ddb_batch.append(
            {
                "PutRequest": {
                    "Item": {
                        "campaignId": campaign_id,
                        "sk": item_sk,
                        "phone": phone,
                        "status": "PENDING",
                        "createdAt": now_iso,
                        "updatedAt": now_iso,
                        "ttl": ttl,
                    }
                }
            }
        )
        enqueued += 1

        if len(sqs_batch) == 10:
            _sqs.send_message_batch(QueueUrl=_SQS_QUEUE_URL, Entries=sqs_batch)
            sqs_batch = []
        if len(ddb_batch) == 25:
            with queue_table.batch_writer() as bw:
                for req in ddb_batch:
                    bw.put_item(Item=req["PutRequest"]["Item"])
            ddb_batch = []

    if sqs_batch:
        _sqs.send_message_batch(QueueUrl=_SQS_QUEUE_URL, Entries=sqs_batch)
    if ddb_batch:
        with queue_table.batch_writer() as bw:
            for req in ddb_batch:
                bw.put_item(Item=req["PutRequest"]["Item"])

    # Update enqueued count
    _ddb.Table(_RUNS_TABLE).update_item(
        Key={"planId": event["planId"], "sk": f"{event['runId']}#{campaign_id}"},
        UpdateExpression="SET totalEnqueued = :n, updatedAt = :t",
        ExpressionAttributeValues={":n": enqueued, ":t": now_iso},
    )

    print(f"sms_sender: enqueued={enqueued} campaign={campaign_id}")
    return {"enqueued": enqueued}


_BATCH_GET_PROFILE_MAX = 100  # CP API limit per BatchGetProfile call


def _get_segment_phones(segment_name: str) -> list[str]:
    """Read all phone numbers from a CP segment via GetSegmentMembership.

    Collects all profile IDs per membership page, then calls BatchGetProfile
    in groups of up to 100 — reducing API calls from O(n) to O(n/100).
    """
    phones: list[str] = []
    try:
        kwargs: dict = {
            "DomainName": _DOMAIN,
            "SegmentDefinitionName": segment_name,
            "MaxResults": 250,
        }
        while True:
            resp = _cp.get_segment_membership(**kwargs)
            # Collect all profile IDs from this page
            profile_ids = [
                (e if isinstance(e, str) else e.get("ProfileId", ""))
                for e in resp.get("Profiles", [])
            ]
            profile_ids = [pid for pid in profile_ids if pid]
            # BatchGetProfile in groups of up to 100
            for i in range(0, len(profile_ids), _BATCH_GET_PROFILE_MAX):
                batch_ids = profile_ids[i : i + _BATCH_GET_PROFILE_MAX]
                batch_resp = _cp.batch_get_profile(
                    DomainName=_DOMAIN,
                    ProfileIds=batch_ids,
                )
                for profile in batch_resp.get("Profiles", []):
                    raw = profile.get("PhoneNumber") or profile.get("MobilePhoneNumber") or ""
                    if raw:
                        phones.append(_normalize_phone(raw))
            next_token = resp.get("NextToken")
            if not next_token:
                break
            kwargs["NextToken"] = next_token
    except Exception as exc:
        print(f"sms_sender: _get_segment_phones error type={type(exc).__name__}")
    return phones


def _normalize_phone(raw: str) -> str:
    """Normalize a phone number to E.164 format (+1XXXXXXXXXX)."""
    if raw.startswith("+"):
        return raw
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return raw
