"""
SMS Processor Lambda — consumes one SQS message at a time, calls EUM SMS
SendTextMessage, and updates VipSmsCampaignQueue + VipSmsCampaignRuns.

No PHI in logs. Only campaignId, messageId, status, and error type are logged.
Phone numbers are never logged.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import boto3

from vip_shared.domain.services.sms_campaign import (
    SmsCampaignError,
    validate_body as validate_campaign_body,
    validate_version as validate_campaign_version,
)

_QUEUE_TABLE = os.environ["SMS_CAMPAIGN_QUEUE_TABLE"]
_RUNS_TABLE = os.environ["SMS_CAMPAIGN_RUNS_TABLE"]
_CONFIG_SET = os.environ.get("SMS_CONFIG_SET_NAME", "")
_OPT_OUT_LIST = os.environ.get("SMS_OPT_OUT_LIST_NAME", "")

# 2x this Lambda's own 30s timeout (api-sms-stack.ts B5 comment), comfortably below
# the queue's 180s VisibilityTimeout. A SENDING claim older than this cannot belong
# to a still-running invocation (it would have already timed out) — it can only be
# an orphan left behind by a crash/timeout, since nothing else ever moves an item
# out of SENDING. Without this, a redelivered message for an orphaned claim hits the
# ConditionalCheckFailedException below, is skipped as a "duplicate", and its SQS
# message is deleted on the way out — permanently stranding it in SENDING forever.
_STALE_SENDING_SECONDS = 60

_ddb = boto3.resource("dynamodb")
_sms = boto3.client("pinpoint-sms-voice-v2", region_name="us-east-1")


def lambda_handler(event: dict, context: object) -> None:
    for record in event.get("Records", []):
        body = json.loads(record["body"])
        _process(
            campaign_id=body["campaignId"],
            sk=body["sk"],
            phone=body["phone"],
            message_template=body["messageTemplate"],
            origination_arn=body["originationNumberArn"],
            plan_id=body.get("planId", ""),
            run_id=body.get("runId", ""),
            precall_policy=body.get("precallPolicy"),
            sms_template_version=body.get("smsTemplateVersion"),
            sms_template_version_present="smsTemplateVersion" in body,
        )


def _process(
    campaign_id: str,
    sk: str,
    phone: str,
    message_template: str,
    origination_arn: str,
    plan_id: str,
    run_id: str,
    precall_policy: dict | None = None,
    sms_template_version: str | None = None,
    sms_template_version_present: bool = False,
) -> None:
    sent_at = datetime.now(timezone.utc).isoformat()

    # Claim this item PENDING → SENDING before sending (H-A1: idempotency guard).
    # Also allows re-claiming a SENDING item whose claim has gone stale (see
    # _STALE_SENDING_SECONDS) — recovering an orphan left by a crashed/timed-out
    # invocation. If another invocation holds a fresh claim, this raises
    # ConditionalCheckFailedException and we return early to avoid a duplicate send.
    stale_before = (
        datetime.now(timezone.utc) - timedelta(seconds=_STALE_SENDING_SECONDS)
    ).isoformat()
    try:
        claim = _ddb.Table(_QUEUE_TABLE).update_item(
            Key={"campaignId": campaign_id, "sk": sk},
            UpdateExpression="SET #s = :sending, updatedAt = :t",
            ConditionExpression=(
                "#s = :pending OR (#s = :sending AND updatedAt < :stale_before)"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":sending": "SENDING",
                ":pending": "PENDING",
                ":stale_before": stale_before,
                ":t": sent_at,
            },
            ReturnValues="ALL_OLD",
        )
    except _ddb.meta.client.exceptions.ConditionalCheckFailedException:
        # Already claimed by a still-live invocation, or already terminal — skip
        # to avoid duplicate send.
        print(f"sms_processor: skipped duplicate campaign={campaign_id} sk={sk[:20]}")
        return

    try:
        previous = claim.get("Attributes") or {}
        campaign_message = sms_template_version_present or sms_template_version is not None
        campaign_row = "smsTemplateVersion" in previous
        managed = precall_policy is not None or campaign_message or campaign_row
        if campaign_message or campaign_row:
            # The durable queue marker prevents a truncated/altered payload
            # from silently downgrading to legacy TRANSACTIONAL or stale retry
            # semantics. Settle invalid contracts once; do not strand PENDING
            # work until the SQS DLQ catches it. Legacy rows have no marker.
            try:
                validate_campaign_version(sms_template_version)
                if previous.get("smsTemplateVersion") != sms_template_version or precall_policy is not None:
                    raise SmsCampaignError("conflicting_sms_modes")
            except SmsCampaignError:
                _update_queue_item(campaign_id, sk, "FAILED", error_code="INVALID_SMS_CONTRACT", sent_at=sent_at)
                _increment_runs_counter(campaign_id, plan_id, run_id, "totalFailed")
                print(f"sms_processor: FAILED campaign={campaign_id} error=INVALID_SMS_CONTRACT")
                return
        if managed and previous.get("status") == "SENDING":
            # A stale worker may have reached EUM before it crashed. Managed
            # copy must not be sent twice to repair that unknown
            # outcome. Settle it as an explicit failure; legacy retry behavior
            # remains unchanged.
            _update_queue_item(campaign_id, sk, "FAILED", error_code="PROVIDER_OUTCOME_UNKNOWN", sent_at=sent_at)
            _increment_runs_counter(campaign_id, plan_id, run_id, "totalFailed")
            print(f"sms_processor: FAILED campaign={campaign_id} error=PROVIDER_OUTCOME_UNKNOWN")
            return
        kwargs: dict = {
            "DestinationPhoneNumber": phone,
            "MessageBody": message_template,
            "OriginationIdentity": origination_arn,
            "MessageType": "PROMOTIONAL" if sms_template_version is not None else "TRANSACTIONAL",
        }
        if _CONFIG_SET:
            kwargs["ConfigurationSetName"] = _CONFIG_SET
        if managed:
            # The parent campaign may have ended (or its pre-call gate timed
            # out) while this message waited in SQS.
            # Recheck immediately before EUM and fail closed on policy drift.
            run = _ddb.Table(_RUNS_TABLE).get_item(
                Key={"planId": plan_id, "sk": f"{run_id}#{campaign_id}"},
                ConsistentRead=True,
            ).get("Item")
            if (
                not run or run.get("status") != "RUNNING"
                or run.get("precallPolicy") != precall_policy
                or run.get("smsTemplateVersion") != sms_template_version
            ):
                _update_queue_item(campaign_id, sk, "CANCELLED", sent_at=sent_at)
                if run:
                    _increment_runs_counter(campaign_id, plan_id, run_id, "totalCancelled")
                print(f"sms_processor: CANCELLED campaign={campaign_id}")
                return
        if sms_template_version is not None:
            validate_campaign_body(message_template)
        if precall_policy is not None:
            # This copy announces an imminent call; do not retain it in the
            # provider queue for the default 72 hours. Acceptance is not a
            # guarantee of carrier delivery within this interval.
            kwargs["TimeToLive"] = 300
        # Opt-out enforcement: EUM SMS automatically checks the phone number's
        # configured opt-out list (Default) — where STOP replies are recorded.
        # Passing a separate opt-out list here would bypass real opt-outs.

        resp = _sms.send_text_message(**kwargs)
        message_id = resp["MessageId"]

        _update_queue_item(campaign_id, sk, "SENT", message_id=message_id, sent_at=sent_at)
        _increment_runs_counter(campaign_id, plan_id, run_id, "totalSent")
        print(f"sms_processor: SENT campaign={campaign_id} messageId={message_id}")

    except _sms.exceptions.ValidationException as exc:
        if "OptedOut" in str(exc):
            _update_queue_item(campaign_id, sk, "OPTED_OUT", error_code="OPTED_OUT", sent_at=sent_at)
            _increment_runs_counter(campaign_id, plan_id, run_id, "totalOptedOut")
            print(f"sms_processor: OPTED_OUT campaign={campaign_id}")
            # Do NOT re-raise — opted-out numbers are expected, not errors
        else:
            # Store only the error class name — never the raw exception message (may contain PHI phone number)
            _update_queue_item(campaign_id, sk, "FAILED", error_code=type(exc).__name__, sent_at=sent_at)
            _increment_runs_counter(campaign_id, plan_id, run_id, "totalFailed")
            print(f"sms_processor: FAILED campaign={campaign_id} error=ValidationException")
            # Strip the original exception to prevent EUM's DestinationPhoneNumber from reaching CloudWatch
            raise RuntimeError(
                f"sms send failed campaign={campaign_id} err=ValidationException"
            ) from None

    except Exception as exc:
        _update_queue_item(campaign_id, sk, "FAILED", error_code=type(exc).__name__, sent_at=sent_at)
        _increment_runs_counter(campaign_id, plan_id, run_id, "totalFailed")
        print(f"sms_processor: FAILED campaign={campaign_id} error={type(exc).__name__}")
        raise RuntimeError(
            f"sms send failed campaign={campaign_id} err={type(exc).__name__}"
        ) from None


def _update_queue_item(
    campaign_id: str,
    sk: str,
    status: str,
    *,
    message_id: str | None = None,
    error_code: str | None = None,
    sent_at: str | None = None,
) -> None:
    expr = "SET #s = :s, sentAt = :t, updatedAt = :t"
    names = {"#s": "status"}
    vals: dict = {":s": status, ":t": sent_at}
    if message_id:
        expr += ", messageId = :m"
        vals[":m"] = message_id
    if error_code:
        expr += ", errorCode = :e"
        vals[":e"] = error_code
    _ddb.Table(_QUEUE_TABLE).update_item(
        Key={"campaignId": campaign_id, "sk": sk},
        UpdateExpression=expr,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=vals,
    )


def _increment_runs_counter(
    campaign_id: str,
    plan_id: str,
    run_id: str,
    counter_field: str,
) -> None:
    """Increment a counter on VipSmsCampaignRuns using the primary key directly.

    plan_id and run_id come from the SQS message body (set by the sender Lambda),
    eliminating the need for a scan — solves H-B1.
    """
    if not plan_id or not run_id:
        return
    try:
        _ddb.Table(_RUNS_TABLE).update_item(
            Key={"planId": plan_id, "sk": f"{run_id}#{campaign_id}"},
            UpdateExpression=f"ADD {counter_field} :one SET updatedAt = :t",
            ExpressionAttributeValues={
                ":one": 1,
                ":t": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as exc:
        # Counter increment failure is non-fatal — the SMS was already sent/recorded
        print(f"sms_processor: _increment_runs_counter error type={type(exc).__name__}")
