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

from vip_shared.infrastructure.persistence.opt_out import (
    build_from_env as build_opt_out_from_env,
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
_opt_out = build_opt_out_from_env()


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
    # Also allows re-claiming a SENDING item whose claim has gone stale (see
    # _STALE_SENDING_SECONDS) — recovering an orphan left by a crashed/timed-out
    # invocation. If another invocation holds a fresh claim, this raises
    # ConditionalCheckFailedException and we return early to avoid a duplicate send.
    stale_before = (
        datetime.now(timezone.utc) - timedelta(seconds=_STALE_SENDING_SECONDS)
    ).isoformat()
    try:
        _ddb.Table(_QUEUE_TABLE).update_item(
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
        )
    except _ddb.meta.client.exceptions.ConditionalCheckFailedException:
        # Already claimed by a still-live invocation, or already terminal — skip
        # to avoid duplicate send.
        print(f"sms_processor: skipped duplicate campaign={campaign_id} sk={sk[:20]}")
        return

    # VIP-02: final strongly-consistent opt-out recheck immediately before send.
    # sms_sender_handler.py already checked this same shared table at enqueue
    # time, but SQS delivery can lag behind a STOP reply recorded in the
    # meantime. EUM's own opt-out list (checked below by send_text_message
    # itself) is carrier-level only and has no idea about this cross-channel
    # list, so it cannot catch this gap on its own.
    try:
        opted_out_now = _opt_out.is_blocked(phone)
    except Exception as exc:
        # Fail closed: if we can't determine opt-out status, do NOT send.
        # This is a transient read failure, not a real opt-out — treat it as
        # retryable (see the retryable branch below) rather than mislabeling
        # the contact OPTED_OUT on what may just be a DynamoDB blip.
        _revert_claim_to_pending(campaign_id, sk)
        print(
            f"sms_processor: RETRY (opt-out check failed, failing closed) "
            f"campaign={campaign_id} error={type(exc).__name__}"
        )
        raise RuntimeError(
            f"sms send blocked — opt-out check failed campaign={campaign_id} "
            f"err={type(exc).__name__}"
        ) from None

    if opted_out_now:
        _update_queue_item(
            campaign_id, sk, "OPTED_OUT", error_code="OPTED_OUT", sent_at=sent_at
        )
        _increment_runs_counter(campaign_id, plan_id, run_id, "totalOptedOut")
        print(f"sms_processor: OPTED_OUT (pre-send recheck) campaign={campaign_id}")
        return

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

    # VIP-01: the send call is deliberately isolated from bookkeeping (queue
    # status + counters) below. A failure here means the SMS was NEVER sent —
    # safe to classify as terminal (ValidationException) or retryable (anything
    # else) and act accordingly. A failure AFTER a successful send (bookkeeping)
    # must never be treated the same way — see the try/except below this one.
    try:
        resp = _sms.send_text_message(**kwargs)
    except _sms.exceptions.ValidationException as exc:
        if "OptedOut" in str(exc):
            _update_queue_item(campaign_id, sk, "OPTED_OUT", error_code="OPTED_OUT", sent_at=sent_at)
            _increment_runs_counter(campaign_id, plan_id, run_id, "totalOptedOut")
            print(f"sms_processor: OPTED_OUT campaign={campaign_id}")
            return  # expected outcome, not an error — do NOT re-raise
        # Terminal: EUM rejected the request itself (bad number, bad template,
        # etc.) — an identical retry would fail identically. Store only the
        # error class name — never the raw exception message (may contain PHI
        # phone number) — then mark FAILED and let it stay FAILED.
        _update_queue_item(campaign_id, sk, "FAILED", error_code=type(exc).__name__, sent_at=sent_at)
        _increment_runs_counter(campaign_id, plan_id, run_id, "totalFailed")
        print(f"sms_processor: FAILED campaign={campaign_id} error=ValidationException")
        # Strip the original exception to prevent EUM's DestinationPhoneNumber from reaching CloudWatch
        raise RuntimeError(
            f"sms send failed campaign={campaign_id} err=ValidationException"
        ) from None
    except Exception as exc:
        # VIP-01: retryable. Anything other than a validation rejection —
        # network blip, ThrottlingException, an EUM 5xx, etc. — means the send
        # itself never happened and a later attempt could well succeed.
        # Previously this branch marked the item FAILED (terminal), so a
        # redelivered SQS message for it hit ConditionalCheckFailedException
        # on the claim above (status FAILED matches neither PENDING nor
        # stale-SENDING) and was skipped as "already claimed" — one attempt,
        # zero real retries, despite SQS redelivering. Revert the claim to
        # PENDING so the very next redelivery can reclaim it immediately
        # (rather than waiting out _STALE_SENDING_SECONDS), and raise so SQS
        # actually redelivers it.
        _revert_claim_to_pending(campaign_id, sk)
        print(f"sms_processor: RETRY campaign={campaign_id} error={type(exc).__name__}")
        raise RuntimeError(
            f"sms send failed (retryable) campaign={campaign_id} err={type(exc).__name__}"
        ) from None

    # Send succeeded. Bookkeeping is intentionally its own try/except: a
    # failure here (VIP-01) must never flip an already-sent message to FAILED
    # — the SMS is gone, that fact must not be lost — and must never re-raise,
    # since SQS redelivering would risk a duplicate send for a message whose
    # SMS has ALREADY gone out.
    message_id = resp["MessageId"]
    try:
        _update_queue_item(campaign_id, sk, "SENT", message_id=message_id, sent_at=sent_at)
        _increment_runs_counter(campaign_id, plan_id, run_id, "totalSent")
        print(f"sms_processor: SENT campaign={campaign_id} messageId={message_id}")
    except Exception as exc:
        print(
            f"sms_processor: SENT but bookkeeping failed campaign={campaign_id} "
            f"messageId={message_id} error={type(exc).__name__}"
        )


def _revert_claim_to_pending(campaign_id: str, sk: str) -> None:
    """Revert a SENDING claim back to PENDING after a retryable send failure
    (VIP-01), so the next SQS redelivery can reclaim it immediately instead of
    waiting out _STALE_SENDING_SECONDS.

    Best-effort: if this write itself fails, the stale-SENDING claim-recovery
    path in _process's initial claim step still recovers the item once
    _STALE_SENDING_SECONDS elapses — so a failure here delays retry, it does
    not lose the item.
    """
    try:
        _ddb.Table(_QUEUE_TABLE).update_item(
            Key={"campaignId": campaign_id, "sk": sk},
            UpdateExpression="SET #s = :pending, updatedAt = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":pending": "PENDING",
                ":t": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as exc:
        print(
            f"sms_processor: revert_to_pending failed campaign={campaign_id} "
            f"error={type(exc).__name__}"
        )


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
