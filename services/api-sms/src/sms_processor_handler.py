"""
SMS Processor Lambda — consumes one SQS message at a time, calls EUM SMS
SendTextMessage, and updates VipSmsCampaignQueue + VipSmsCampaignRuns.

No PHI in logs. Only campaignId, messageId, status, and error type are logged.
Phone numbers are never logged.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import boto3

_QUEUE_TABLE = os.environ["SMS_CAMPAIGN_QUEUE_TABLE"]
_RUNS_TABLE = os.environ["SMS_CAMPAIGN_RUNS_TABLE"]
_CONFIG_SET = os.environ.get("SMS_CONFIG_SET_NAME", "")
_OPT_OUT_LIST = os.environ.get("SMS_OPT_OUT_LIST_NAME", "")

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
        )


def _process(
    campaign_id: str,
    sk: str,
    phone: str,
    message_template: str,
    origination_arn: str,
    plan_id: str,
    run_id: str,
) -> None:
    sent_at = datetime.now(timezone.utc).isoformat()

    # Claim this item PENDING → SENDING before sending (H-A1: idempotency guard).
    # If another invocation already claimed it, this raises ConditionalCheckFailedException
    # and we return early — preventing duplicate sends for the same SQS message.
    try:
        _ddb.Table(_QUEUE_TABLE).update_item(
            Key={"campaignId": campaign_id, "sk": sk},
            UpdateExpression="SET #s = :sending, updatedAt = :t",
            ConditionExpression="#s = :pending",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":sending": "SENDING",
                ":pending": "PENDING",
                ":t": sent_at,
            },
        )
    except _ddb.meta.client.exceptions.ConditionalCheckFailedException:
        # Already claimed or processed by another invocation — skip to avoid duplicate send
        print(f"sms_processor: skipped duplicate campaign={campaign_id} sk={sk[:20]}")
        return

    try:
        kwargs: dict = {
            "DestinationPhoneNumber": phone,
            "MessageBody": message_template,
            "OriginationIdentity": origination_arn,
            "MessageType": "TRANSACTIONAL",
        }
        if _CONFIG_SET:
            kwargs["ConfigurationSetName"] = _CONFIG_SET
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
