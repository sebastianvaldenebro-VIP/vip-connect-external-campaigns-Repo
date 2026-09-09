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

from vip_shared.infrastructure.persistence.opt_out import (
    build_from_env as build_opt_out_from_env,
)
from vip_shared.infrastructure.telemetry.structured_logger import StructuredLogger

_logger = StructuredLogger(service="api-sms-sender")

_QUEUE_TABLE = os.environ["SMS_CAMPAIGN_QUEUE_TABLE"]
_RUNS_TABLE = os.environ["SMS_CAMPAIGN_RUNS_TABLE"]
_SQS_QUEUE_URL = os.environ["SMS_SQS_QUEUE_URL"]
_DOMAIN = os.environ["PROFILES_DOMAIN_NAME"]
_TTL_SECONDS = 30 * 24 * 3600  # 30 days

_ddb = boto3.resource("dynamodb")
_sqs = boto3.client("sqs")
_cp = boto3.client("customer-profiles")
_opt_out = build_opt_out_from_env()

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
            "totalSkippedOptOut": 0,
            "createdAt": now_iso,
            "updatedAt": now_iso,
            "pipelineVersion": "v1",
        },
        ConditionExpression="attribute_not_exists(sk)",
    )

    # Extract phone numbers from CP segment
    phones = _get_segment_phones(segment_name)

    # Batch-write to DDB and SQS. Both batches are flushed together at the same
    # size (10, SQS's own hard cap) and keyed by the SQS entry Id, so a partial
    # send_message_batch failure can be mapped back to its exact DDB item —
    # previously the two batches flushed independently (10 vs 25) with the SQS
    # response discarded entirely, so a partially-failed send left its DDB row
    # written as PENDING with no message ever in the queue: permanently stuck,
    # invisible, and never retried.
    enqueued = 0
    failed = 0
    opted_out = 0
    queue_table = _ddb.Table(_QUEUE_TABLE)
    sqs_batch: list[dict] = []
    ddb_items_by_id: dict[str, dict] = {}

    for phone in phones:
        if not _E164_RE.match(phone):
            continue
        if _opt_out.is_blocked(phone):
            opted_out += 1
            continue
        item_sk = f"{now_iso}#{uuid.uuid4().hex[:8]}"
        entry_id = uuid.uuid4().hex[:8]
        sqs_batch.append(
            {
                "Id": entry_id,
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
        ddb_items_by_id[entry_id] = {
            "campaignId": campaign_id,
            "sk": item_sk,
            "phone": phone,
            "status": "PENDING",
            "createdAt": now_iso,
            "updatedAt": now_iso,
            "ttl": ttl,
        }

        if len(sqs_batch) == 10:
            batch_ok, batch_failed = _flush_sms_batch(
                sqs_batch, ddb_items_by_id, queue_table, campaign_id
            )
            enqueued += batch_ok
            failed += batch_failed
            sqs_batch = []
            ddb_items_by_id = {}

    if sqs_batch:
        batch_ok, batch_failed = _flush_sms_batch(
            sqs_batch, ddb_items_by_id, queue_table, campaign_id
        )
        enqueued += batch_ok
        failed += batch_failed

    # Update enqueued/failed/skipped-opt-out counts.
    #
    # NOTE: this writes totalSkippedOptOut, NOT totalOptedOut. totalOptedOut is a
    # different counter owned by sms_processor_handler.py — it means "we enqueued
    # this contact and EUM's own managed suppression list rejected the send" (those
    # contacts ARE inside totalEnqueued). totalSkippedOptOut means "we never
    # enqueued this contact at all — skipped before send using our own opt-out
    # list". Writing to totalOptedOut here would race with the processor's atomic
    # ADD (SQS-driven sends can start firing while this loop is still running) and
    # would conflate two different populations in downstream reporting/UI.
    _ddb.Table(_RUNS_TABLE).update_item(
        Key={"planId": event["planId"], "sk": f"{event['runId']}#{campaign_id}"},
        UpdateExpression="SET totalEnqueued = :n, totalFailed = :f, totalSkippedOptOut = :o, updatedAt = :t",
        ExpressionAttributeValues={
            ":n": enqueued,
            ":f": failed,
            ":o": opted_out,
            ":t": now_iso,
        },
    )

    _logger.info(
        "sms_sender_enqueued",
        campaign_id=campaign_id,
        enqueued=enqueued,
        failed=failed,
        skipped_opt_out=opted_out,
    )
    return {"enqueued": enqueued, "failed": failed}


def _flush_sms_batch(
    sqs_batch: list[dict],
    ddb_items_by_id: dict[str, dict],
    queue_table,
    campaign_id: str,
) -> tuple[int, int]:
    """Send one SQS batch, then write its DDB items reflecting the real outcome.

    send_message_batch does not raise on a partial failure — some entries can fail
    while the call itself returns 200. Items whose SQS entry failed are written as
    SQS_SEND_FAILED (visible in the queue table and counted in totalFailed) instead
    of PENDING, since no message exists for them to ever be picked up.
    """
    resp = _sqs.send_message_batch(QueueUrl=_SQS_QUEUE_URL, Entries=sqs_batch)
    failed_entries = resp.get("Failed", [])
    failed_ids = {f["Id"] for f in failed_entries}
    if failed_entries:
        # PHI rule: no phone numbers here — only the SQS-assigned Id and error code.
        _logger.warn(
            "sms_sender_batch_partial_failure",
            campaign_id=campaign_id,
            failed_count=len(failed_entries),
            batch_size=len(sqs_batch),
            codes=sorted({f.get("Code", "") for f in failed_entries}),
        )
    with queue_table.batch_writer() as bw:
        for entry_id, item in ddb_items_by_id.items():
            if entry_id in failed_ids:
                item["status"] = "SQS_SEND_FAILED"
            bw.put_item(Item=item)
    return len(ddb_items_by_id) - len(failed_ids), len(failed_ids)


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
        _logger.warn("sms_sender_get_segment_phones_failed", error=type(exc).__name__)
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
