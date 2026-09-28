"""
SMS Sender Lambda — reads CP segment phone numbers and enqueues them into
VipSmsCampaignQueue (DDB) and SQS vip-sms-campaign-queue for async delivery.

PHI rule:
  - Only E.164 phone numbers are stored in VipSmsCampaignQueue (DDB) — the
    long-lived, 30-day-TTL record. No names, DOBs, conditions, diagnoses, or
    any other PHI ever lands there.
  - The SQS message body DOES carry the rendered message text (which may
    include the recipient's first name, per
    vip_shared.domain.services.sms_template's allowlist) — this is transient,
    consumed once by sms_processor_handler.py, and never written to DDB.
  - Phone numbers and rendered text are NOT logged.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from segment_recipients import (
    SegmentRecipientsPending,
    load_segment_recipients as _load_segment_recipients,
)

from vip_shared.domain.services.quiet_hours import (
    is_within_quiet_hours as _is_within_quiet_hours,
)
from vip_shared.domain.services.sms_template import (
    ALLOWED_FIELDS,
    extract_placeholders,
    render as _render,
)
from vip_shared.domain.services.precall_sms import (
    PersonalizationError,
    normalize_policy,
    personalize,
    validate_policy,
)
from vip_shared.domain.services.sms_campaign import (
    SmsCampaignError,
    render as render_campaign,
    validate_template as validate_campaign_template,
    validate_version as validate_campaign_version,
)
from vip_shared.domain.services.sms_origination import (
    SmsOriginationError,
    validate_promotional_origin,
)
from vip_shared.infrastructure.persistence.opt_out import (
    build_from_env as build_opt_out_from_env,
)
from vip_shared.infrastructure.telemetry.structured_logger import StructuredLogger

_logger = StructuredLogger(service="api-sms-sender")

_QUEUE_TABLE = os.environ["SMS_CAMPAIGN_QUEUE_TABLE"]
_RUNS_TABLE = os.environ["SMS_CAMPAIGN_RUNS_TABLE"]
_SQS_QUEUE_URL = os.environ["SMS_SQS_QUEUE_URL"]
_DOMAIN = os.environ["PROFILES_DOMAIN_NAME"]
_SNAPSHOT_BUCKET = os.environ.get("SMS_SNAPSHOT_BUCKET", "")
_SNAPSHOT_ROLE_ARN = os.environ.get("SMS_SNAPSHOT_ROLE_ARN", "")
_SNAPSHOT_KEY_ARN = os.environ.get("SMS_SNAPSHOT_KEY_ARN", "")
_TTL_SECONDS = 30 * 24 * 3600  # 30 days
# Claim records (see _process_recipients' claim gate) are a short-lived DB-level
# dedup guard, not a long-lived audit record. This ttl attribute is now ONLY a
# distant backstop for eventual physical cleanup of very old, no-longer-relevant
# claim rows — it is NOT what determines when a stale claim can be safely
# reclaimed (see _CLAIM_STALE_SECONDS below for that). A prior version of this
# comment assumed a claim from a crashed invocation would "self-heal" once this
# ttl expired; that was wrong — DynamoDB's TTL deletion is a background sweep
# with no delivery-time guarantee (AWS documents "typically within 48 hours" of
# expiry, not 15 minutes), so relying on it to unblock a retry could strand a
# phone for far longer than intended. Deliberately NOT _TTL_SECONDS (30 days) —
# that lifetime is wrong even for a backstop on a record whose only purpose is
# to survive one overlapping tick.
_CLAIM_TTL_SECONDS = 15 * 60
# Threshold for atomically "stealing" a stale claim (2026-09 adversarial-review
# Finding, Important — see _CLAIM_TTL_SECONDS above for the debunked TTL-based
# assumption this replaces). A claim's OWN recorded age (claimedAtEpoch) — not
# whether DynamoDB has gotten around to deleting the row — is what determines
# reclaimability. Same idiom as sms_processor_handler.py's
# _STALE_SENDING_SECONDS and executor.py's _dispatch_ready_campaigns 5-minute
# stale-"creating"-claim reset. SmsRetryQuietHoursFunction (api-sms-stack.ts),
# the Lambda that invokes retry_quiet_hours_skipped, has a real timeout of 5
# minutes — 10 minutes gives 2x margin above that, comfortably ruling out
# mistaking a still-running invocation's fresh claim for an orphan.
_CLAIM_STALE_SECONDS = 10 * 60

_ddb = boto3.resource("dynamodb")
_sqs = boto3.client("sqs")
_cp = boto3.client("customer-profiles")
_s3 = boto3.client("s3")
_opt_out = build_opt_out_from_env()
# Legacy/precall paths do not create or query an origination inventory client.
_origin_client = None

# US 10-digit numbers in E.164 format only
_E164_RE = re.compile(r"^\+1\d{10}$")


def _is_managed_run(record: dict) -> bool:
    return record.get("precallPolicy") is not None or "smsTemplateVersion" in record


def _validate_managed_run(record: dict) -> None:
    if "smsTemplateVersion" in record:
        validate_campaign_version(record["smsTemplateVersion"])
        if record.get("precallPolicy") is not None:
            raise SmsCampaignError("conflicting_sms_modes")
        validate_campaign_template(record.get("messageTemplate"))
    else:
        validate_policy(record["precallPolicy"])


def _validate_campaign_origin(record: dict) -> None:
    if "smsTemplateVersion" not in record:
        return
    global _origin_client
    if _origin_client is None:
        _origin_client = boto3.client("pinpoint-sms-voice-v2")
    validate_promotional_origin(record["originationNumberArn"], client=_origin_client)


def _bind_plan_schedule_source(record: dict, runs_table, event: dict) -> dict:
    """Adopt Plans scheduling once for an unfinished legacy SMS run.

    scheduleSource is an internal contract with the IAM-authorized Plans
    invoker, not proof of an AWS caller's identity. Merely having planId/runId
    does not opt a standalone invocation into this behavior. A persisted
    source is immutable, and sealed managed runs cannot gain late SMS work.
    """
    if (
        event.get("scheduleSource") != "plans"
        or "scheduleSource" in record
        or record.get("status") != "RUNNING"
        or record.get("initializationComplete") is True
    ):
        return record
    key = {"planId": record["planId"], "sk": record["sk"]}
    try:
        runs_table.update_item(
            Key=key,
            UpdateExpression="SET scheduleSource = :source",
            ConditionExpression=(
                "attribute_not_exists(scheduleSource) AND #status = :running "
                "AND (attribute_not_exists(initializationComplete) "
                "OR initializationComplete = :initializing)"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":source": "plans", ":running": "RUNNING", ":initializing": False,
            },
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    # Use the winner, including a concurrent source, cancellation or seal.
    current = runs_table.get_item(Key=key, ConsistentRead=True).get("Item")
    if not current:
        raise RuntimeError("SMS run disappeared during schedule source recovery")
    return current


def lambda_handler(event: dict, context: object) -> dict:
    """
    event = {
      "campaignId": str,               # smsCampaignId
      "planId": str,
      "runId": str,
      "planName": str,
      "segmentArn": str,               # existing CP segment ARN
      "segmentName": str,
      "messageTemplate": str,          # allowlisted-placeholder template, e.g.
                                        # "Hi {{FirstName}}! This is {{ClinicName}}."
                                        # — rendered once per recipient below, max
                                        # 160 chars once rendered (see
                                        # vip_shared.domain.services.sms_template)
      "clinicName": str,               # optional; interpolated into {{ClinicName}}.
                                        # If the template contains {{ClinicName}}
                                        # and this key is omitted, it renders empty
                                        # and the patient reads "This is .".
      "originationNumberArn": str,     # EUM SMS phone number ARN
      "originationNumber": str,        # friendly E.164 (e.g. +15125551234)
      "scheduleSource": "plans",      # internal Plans invocations only; optional
      "smsTemplateVersion": "campaign-v1",  # optional SMS-only editable contract
    }
    Returns enqueue/failure counts. A snapshot still being generated adds
    "pending": true; a run that has ended adds "terminal": true and exitReason.
    """
    campaign_id = event["campaignId"]
    segment_arn = event["segmentArn"]
    segment_name = event.get("segmentName", segment_arn.split("/")[-1])
    requested_policy = event.get("precallPolicy")
    if "precallPolicy" in event:
        requested_policy = normalize_policy(requested_policy)
    if "smsTemplateVersion" in event:
        validate_campaign_version(event["smsTemplateVersion"])
        if requested_policy is not None:
            raise SmsCampaignError("conflicting_sms_modes")
        validate_campaign_template(event.get("messageTemplate"))
    message_tmpl = event.get("messageTemplate", "") if requested_policy is not None else event["messageTemplate"]
    origination_arn = event["originationNumberArn"]
    now_epoch = int(time.time())
    now_iso = datetime.now(timezone.utc).isoformat()
    ttl = now_epoch + _TTL_SECONDS

    # The runs row is an immutable campaign identity, not a completion claim.
    # A prior invocation may have created it and then failed before enqueueing.
    runs_table = _ddb.Table(_RUNS_TABLE)
    record = {
        "planId": event["planId"],
        "sk": f"{event['runId']}#{campaign_id}",
        "smsCampaignId": campaign_id,
        "planName": event.get("planName", ""),
        "segmentName": segment_name,
        "segmentArn": segment_arn,
        "messageTemplate": message_tmpl,
        "clinicName": event.get("clinicName", ""),
        "originationNumberArn": origination_arn,
        "originationNumber": event.get("originationNumber", ""),
        "status": "RUNNING",
        "startedAt": now_iso,
        "totalEnqueued": 0,
        "totalSent": 0,
        "totalFailed": 0,
        "totalOptedOut": 0,
        "totalSkippedOptOut": 0,
        "totalSkippedQuietHours": 0,
        "totalSqsSendFailed": 0,
        "createdAt": now_iso,
        "updatedAt": now_iso,
        "pipelineVersion": "v1",
    }
    if event.get("scheduleSource") == "plans":
        record["scheduleSource"] = "plans"
    if requested_policy is not None:
        record["messageTemplate"] = ""
        record["clinicName"] = ""
        record["precallPolicy"] = requested_policy
    if "smsTemplateVersion" in event:
        record["smsTemplateVersion"] = event["smsTemplateVersion"]
        record["clinicName"] = ""
    if _is_managed_run(record):
        record["initializationComplete"] = False
        record["activeEnqueueBatches"] = 0
        record["enqueueRevision"] = 0
        record["totalSkippedPersonalization"] = 0
        record["totalCancelled"] = 0
    origin_prevalidated = False
    if "smsTemplateVersion" in record:
        # Reject a new incompatible campaign before creating an empty RUNNING
        # row. Existing rows own their origin, even when the replay payload
        # names a different number; sealed/terminal rows remain observable.
        key = {"planId": record["planId"], "sk": record["sk"]}
        existing = runs_table.get_item(Key=key, ConsistentRead=True).get("Item")
        if not existing:
            try:
                _validate_campaign_origin(record)
            except SmsOriginationError:
                # Another initializer may have persisted a winner while EUM
                # was queried. Recover that winner instead of rejecting its
                # replay based on the losing candidate's origin.
                if not runs_table.get_item(Key=key, ConsistentRead=True).get("Item"):
                    raise
            else:
                origin_prevalidated = True
    already_sent_phones: set[str] = set()
    try:
        runs_table.put_item(Item=record, ConditionExpression="attribute_not_exists(sk)")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        origin_prevalidated = False
        record = runs_table.get_item(
            Key={"planId": event["planId"], "sk": f"{event['runId']}#{campaign_id}"},
            ConsistentRead=True,
        ).get("Item")
        if not record:
            raise RuntimeError("SMS run disappeared during sender recovery") from None
        record = _bind_plan_schedule_source(record, runs_table, event)
        if record.get("status") in _TERMINAL_RUN_STATUSES:
            return {
                "terminal": True,
                "exitReason": record.get("exitReason", ""),
                "enqueued": 0,
                "failed": 0,
            }
        if _is_managed_run(record) and record.get("initializationComplete") is True:
            _validate_managed_run(record)
            return _managed_result(event["planId"], event["runId"], campaign_id)
        # A caller replay cannot replace the original audience or message.
        segment_name = record["segmentName"]
        message_tmpl = record["messageTemplate"]
        origination_arn = record["originationNumberArn"]
        already_sent_phones = _get_already_sent_phones(campaign_id)

    if _is_managed_run(record):
        _validate_managed_run(record)
    if not origin_prevalidated:
        _validate_campaign_origin(record)

    # Extract recipients (phone + allowlisted render fields) from CP segment
    try:
        recipients = _get_segment_recipients(
            segment_name,
            plan_id=event["planId"],
            run_id=event["runId"],
            campaign_id=campaign_id,
        )
    except SegmentRecipientsPending:
        if _is_managed_run(record):
            return _managed_result(event["planId"], event["runId"], campaign_id)
        return {"pending": True, "enqueued": 0, "failed": 0}
    except _SmsRunInactive as exc:
        return {
            "terminal": True,
            "exitReason": exc.record.get("exitReason", ""),
            "enqueued": 0,
            "failed": 0,
        }

    if _is_managed_run(record):
        return _process_managed_run(
            recipients, record=record, campaign_id=campaign_id,
            plan_id=event["planId"], run_id=event["runId"],
            already_sent_phones=already_sent_phones, now_iso=now_iso, ttl=ttl,
        )

    queue_table = _ddb.Table(_QUEUE_TABLE)
    enqueued, failed, opted_out, outside_quiet_hours, rejected_fields = (
        _process_recipients(
            recipients,
            campaign_id=campaign_id,
            plan_id=event["planId"],
            run_id=event["runId"],
            message_tmpl=message_tmpl,
            clinic_name=record.get("clinicName", ""),
            origination_arn=origination_arn,
            already_sent_phones=already_sent_phones,
            now_iso=now_iso,
            ttl=ttl,
            queue_table=queue_table,
            schedule_source=record.get("scheduleSource"),
        )
    )

    if rejected_fields is not None:
        # PHI rule: log the offending field NAMES only — never the template body
        # or any recipient value.
        _logger.warn(
            "sms_sender_template_rejected_non_allowlisted_placeholder",
            campaign_id=campaign_id,
            fields=sorted(rejected_fields),
        )

    # ADD preserves prior attempts and concurrent retry/processor increments;
    # suppression counters describe the current pass rather than accumulating
    # the same blocked recipient on every retry.
    #
    # NOTE: this writes totalSkippedOptOut and totalSqsSendFailed, NOT totalOptedOut
    # or totalFailed. Those two are owned by sms_processor_handler.py and mean
    # "we enqueued this contact, then EUM/DDB rejected it after the fact" — those
    # contacts ARE inside totalEnqueued. totalSkippedOptOut and totalSqsSendFailed
    # mean "we never enqueued this contact at all" — skipped before send (our own
    # opt-out list) or rejected by send_message_batch itself. Writing to
    # totalOptedOut/totalFailed here would race with the processor's atomic ADD
    # (SQS-driven sends can start firing while this loop is still running) and would
    # conflate two different populations in downstream reporting/UI — the exact bug
    # this split exists to avoid.
    _ddb.Table(_RUNS_TABLE).update_item(
        Key={"planId": event["planId"], "sk": f"{event['runId']}#{campaign_id}"},
        UpdateExpression=(
            "ADD totalEnqueued :n, totalSqsSendFailed :f "
            "SET totalSkippedOptOut = :o, totalSkippedQuietHours = :q, updatedAt = :t"
        ),
        ExpressionAttributeValues={
            ":n": enqueued,
            ":f": failed,
            ":o": opted_out,
            ":q": outside_quiet_hours,
            ":t": now_iso,
        },
    )

    _logger.info(
        "sms_sender_enqueued",
        campaign_id=campaign_id,
        enqueued=enqueued,
        sqs_send_failed=failed,
        skipped_opt_out=opted_out,
        skipped_quiet_hours=outside_quiet_hours,
    )
    return {"enqueued": enqueued, "failed": failed}


def _managed_result(plan_id: str, run_id: str, campaign_id: str, *, enqueued: int = 0, failed: int = 0) -> dict:
    """Fresh non-PHI aggregates; initialization and provider settlement are distinct."""
    record = _ddb.Table(_RUNS_TABLE).get_item(
        Key={"planId": plan_id, "sk": f"{run_id}#{campaign_id}"}, ConsistentRead=True,
    ).get("Item")
    if not record or record.get("status") != "RUNNING":
        return {"terminal": True, "exitReason": (record or {}).get("exitReason", ""), "enqueued": 0, "failed": 0}
    counters = {
        name: int(record.get(name) or 0) for name in (
            "totalEnqueued", "totalSent", "totalFailed", "totalOptedOut", "totalCancelled",
            "totalSkippedOptOut", "totalSkippedQuietHours", "totalSkippedPersonalization", "totalSqsSendFailed",
        )
    }
    initialized = record.get("initializationComplete") is True
    outstanding = counters["totalEnqueued"] - sum(counters[key] for key in (
        "totalSent", "totalFailed", "totalOptedOut", "totalCancelled",
    ))
    return {
        "enqueued": enqueued, "failed": failed, **counters,
        "initializationComplete": initialized,
        "pending": not initialized or outstanding > 0 or int(record.get("activeEnqueueBatches") or 0) > 0,
    }


def _process_managed_run(
    recipients: list[dict], *, record: dict, campaign_id: str, plan_id: str,
    run_id: str, already_sent_phones: set[str], now_iso: str, ttl: int,
) -> dict:
    _validate_managed_run(record)
    policy = record.get("precallPolicy")
    template_version = record.get("smsTemplateVersion")
    # Resolve the whole cohort before claiming a phone. A shared phone with
    # conflicting rendered identities must never pick the first profile.
    grouped: dict[str, list[dict]] = {}
    for recipient in recipients:
        grouped.setdefault(recipient["phone"], []).append(recipient)
    prepared: list[dict] = []
    reasons: dict[str, int] = {}
    opted_out_before = 0
    for phone, profiles in grouped.items():
        if phone in already_sent_phones:
            continue
        if not _E164_RE.fullmatch(phone):
            reasons["invalid_phone"] = reasons.get("invalid_phone", 0) + 1
            continue
        if _opt_out.is_blocked(phone):
            opted_out_before += 1
            continue
        bodies: set[str] = set()
        invalid: set[str] = set()
        for profile in profiles:
            try:
                bodies.add(
                    render_campaign(record["messageTemplate"], recipient=profile)
                    if template_version is not None else personalize(profile, policy).body
                )
            except (PersonalizationError, SmsCampaignError) as exc:
                invalid.add(exc.reason)
        reason = None
        if len(bodies) > 1 or (bodies and invalid):
            reason = "conflicting_profile"
        elif invalid:
            reason = sorted(invalid)[0]
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1
            continue
        prepared.append({**profiles[0], "_managedBody": bodies.pop()})
    # An earlier PENDING observation may become SQS_SEND_FAILED while this
    # invocation works. Reconcile those phones again before sealing too.
    pending = {recipient["phone"] for recipient in prepared} | (set(grouped) & already_sent_phones)
    enqueued, failed, opted_out, quiet, _ = _process_recipients(
        prepared, campaign_id=campaign_id, plan_id=plan_id, run_id=run_id,
        message_tmpl="", clinic_name="", origination_arn=record["originationNumberArn"],
        already_sent_phones=already_sent_phones, now_iso=now_iso, ttl=ttl,
        queue_table=_ddb.Table(_QUEUE_TABLE), precall_policy=policy, pending_phones=pending,
        sms_template_version=template_version,
        schedule_source=record.get("scheduleSource"),
    )
    # PENDING/SENDING/SENT rows have reserved aggregate counts, even if a
    # previous invocation crashed after reservation. Their unresolved counts
    # continue blocking acceptance; never automatically resend to fix them.
    runs_table = _ddb.Table(_RUNS_TABLE)
    key = {"planId": plan_id, "sk": f"{run_id}#{campaign_id}"}
    before_history = runs_table.get_item(Key=key, ConsistentRead=True).get("Item") or {}
    pending.difference_update(_get_already_sent_phones(campaign_id))
    values = {":o": opted_out_before + opted_out, ":q": quiet, ":p": sum(reasons.values()),
              ":reasons": reasons, ":t": now_iso, ":running": "RUNNING", ":initializing": False}
    expression = ("SET totalSkippedOptOut = :o, totalSkippedQuietHours = :q, "
                  "totalSkippedPersonalization = :p, personalizationSkipReasons = :reasons, updatedAt = :t")
    condition = "#status = :running AND initializationComplete = :initializing"
    # A legacy worker may have evaluated recipient hours while a concurrent
    # Plans invocation adopted scheduling ownership. It cannot seal that stale
    # suppression; leave initialization pending for a pass using the winner.
    if "scheduleSource" in record:
        condition += " AND scheduleSource = :schedule_source"
        values[":schedule_source"] = record["scheduleSource"]
    else:
        condition += " AND attribute_not_exists(scheduleSource)"
    if not pending and int(before_history.get("activeEnqueueBatches") or 0) == 0:
        expression += ", initializationComplete = :complete"
        values[":complete"] = True
        values[":no_batches"] = 0
        values[":revision"] = int(before_history.get("enqueueRevision") or 0)
        # Zero active batches alone is insufficient: another initializer can
        # start and finish a rejected enqueue between our history read and
        # this write. The revision makes that stale history unable to seal.
        condition += (
            " AND (attribute_not_exists(activeEnqueueBatches) OR activeEnqueueBatches = :no_batches)"
            " AND (attribute_not_exists(enqueueRevision) OR enqueueRevision = :revision)"
        )
    try:
        runs_table.update_item(
            Key=key,
            UpdateExpression=expression,
            ConditionExpression=condition,
            ExpressionAttributeNames={"#status": "status"}, ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    return _managed_result(plan_id, run_id, campaign_id, enqueued=enqueued, failed=failed)


def _process_recipients(
    recipients: list[dict],
    *,
    campaign_id: str,
    plan_id: str,
    run_id: str,
    message_tmpl: str,
    clinic_name: str,
    origination_arn: str,
    already_sent_phones: set[str],
    now_iso: str,
    ttl: int,
    queue_table,
    precall_policy: dict | None = None,
    sms_template_version: str | None = None,
    pending_phones: set[str] | None = None,
    schedule_source: str | None = None,
) -> tuple[int, int, int, int, set[str] | None]:
    """Per-recipient opt-out/quiet-hours/render/enqueue loop, shared by the
    initial/resumed send (lambda_handler) and the
    quiet-hours retry pass (retry_quiet_hours_skipped, already_sent_phones
    populated from a live VipSmsCampaignQueue query).

    Two behaviors beyond the original inline loop:
      - A phone already in already_sent_phones is skipped silently (not
        counted as opted-out or quiet-hours-skipped) — a cheap optimization
        for the common case of a retry re-scanning mostly-already-sent
        recipients.
      - Immediately before rendering+enqueueing, every recipient must win an
        atomic DB-level claim (conditional put_item on queue_table) for their
        phone. This — not already_sent_phones — is what actually prevents two
        overlapping executions (e.g. two concurrent tick-driven retry
        invocations for the same campaign) from both sending to the same
        phone.

    Returns (enqueued, failed, opted_out, skipped_quiet_hours, rejected_fields).
    rejected_fields is None unless the template itself was rejected (a
    campaign-level, not per-recipient, problem — see the `except ValueError`
    below), in which case the loop aborts early.
    """
    enqueued = 0
    failed = 0
    opted_out = 0
    outside_quiet_hours = 0
    sqs_batch: list[dict] = []
    ddb_items_by_id: dict[str, dict] = {}

    # Set only if rendering rejects the template (see the `except ValueError`
    # below) — a template-level problem, not a per-recipient one, since every
    # recipient renders the same template.
    rejected_fields: set[str] | None = None

    for recipient in recipients:
        phone = recipient["phone"]
        if not _E164_RE.match(phone):
            continue
        if phone in already_sent_phones:
            # Already sent on a prior pass (the original send or an earlier
            # retry) — skip silently, no counting either way.
            if pending_phones is not None:
                pending_phones.discard(phone)
            continue
        if _opt_out.is_blocked(phone):
            opted_out += 1
            if pending_phones is not None:
                pending_phones.discard(phone)
            continue
        # Plans already owns scheduling for its campaigns. Standalone sends
        # retain the recipient-hours gate; absent/unknown sources never skip it.
        if schedule_source != "plans" and not _is_within_quiet_hours(phone):
            outside_quiet_hours += 1
            if pending_phones is not None:
                # Profile initialization resolves a quiet-hours suppression
                # immediately. It never delays the paired voice campaign to
                # wait for SMS hours, or sends this copy after voice begins.
                pending_phones.discard(phone)
            continue

        # DB-level claim gate (2026-09 adversarial-review Finding, Critical):
        # already_sent_phones above is a read-then-decide in-memory check, not a
        # correctness guarantee — this Lambda's 5-minute timeout outlives the
        # ~1-minute tick cadence that invokes retry_quiet_hours_skipped, so two
        # overlapping executions can both read an empty/stale already_sent_phones
        # for the same phone and both reach this point. This conditional put_item
        # is what actually prevents a duplicate send: only one execution can ever
        # win the claim for a given (campaignId, phone). Mirrors the "claim before
        # act" idiom executor.py already uses for Connect campaign creation
        # (_dispatch_ready_campaigns's Phase 3 — claim, then act).
        #
        # A follow-up finding (Important) identified that an orphaned claim —
        # left behind by a crashed invocation, or written just before a
        # template-rejection `break` (see the `except ValueError` below) aborts
        # the rest of this loop — was wrongly assumed to self-heal once ttl
        # expired. It doesn't: DynamoDB TTL deletion has no delivery-time
        # guarantee, so the row can outlive ttl by far longer than any retry
        # cadence, permanently blocking that phone. The ConditionExpression
        # below therefore also allows atomically RECLAIMING a claim whose own
        # recorded age (claimedAtEpoch) exceeds _CLAIM_STALE_SECONDS — the same
        # "reclaim by recorded timestamp, not TTL sweep timing" idiom as
        # sms_processor_handler.py's SENDING-claim recovery and executor.py's
        # stale-"creating"-claim reset in _dispatch_ready_campaigns.
        #
        # sk uses a CLAIM# prefix, never colliding with a real message item's
        # f"{iso_timestamp}#{random_hex}" sk. _normalize_phone is idempotent on
        # an already-E.164 phone, so the same phone always maps to the same key.
        now_epoch = int(time.time())
        try:
            queue_table.put_item(
                Item={
                    "campaignId": campaign_id,
                    "sk": f"CLAIM#{_normalize_phone(phone)}",
                    "claimedAt": now_iso,
                    "claimedAtEpoch": now_epoch,
                    "ttl": now_epoch + _CLAIM_TTL_SECONDS,
                },
                ConditionExpression=(
                    "attribute_not_exists(sk) OR claimedAtEpoch < :stale_before"
                ),
                ExpressionAttributeValues={
                    ":stale_before": now_epoch - _CLAIM_STALE_SECONDS,
                },
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                # Another concurrent execution (or a duplicate profile within
                # this same pass) holds a still-fresh claim on this phone — skip
                # silently, same treatment as already_sent_phones. This is the
                # actual race-closing guarantee; the already_sent_phones
                # pre-check above is only a cheap optimization to avoid
                # attempting a claim at all for someone almost certainly
                # already sent.
                continue
            raise

        try:
            body = recipient["_managedBody"] if (precall_policy is not None or sms_template_version is not None) else _render(
                message_tmpl,
                recipient=recipient,
                campaign={"clinicName": clinic_name},
            )
        except ValueError:
            # Defense in depth: _validate_sms_campaign (api-plans) should already
            # have rejected any template with a non-allowlisted placeholder. If
            # one slipped through anyway, sending it would deliver literal
            # `{{...}}` braces to a patient — worse than sending nothing. The
            # template (not this recipient) is what's broken, so every remaining
            # recipient would fail identically — abandon the whole campaign
            # rather than skip just this one.
            rejected_fields = extract_placeholders(message_tmpl) - ALLOWED_FIELDS
            break
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
                        # Key name retained deliberately: it now carries the
                        # rendered message, not the raw template. Renaming it
                        # (e.g. to "messageBody") would strand every in-flight
                        # message across the deploy boundary — the processor
                        # would KeyError on messages enqueued by an older sender.
                        "messageTemplate": body,
                        "originationNumberArn": origination_arn,
                        "planId": plan_id,
                        "runId": run_id,
                        **({"precallPolicy": precall_policy} if precall_policy is not None else {}),
                        **({"smsTemplateVersion": sms_template_version} if sms_template_version is not None else {}),
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
            **({"smsTemplateVersion": sms_template_version} if sms_template_version is not None else {}),
        }

        if len(sqs_batch) == 10:
            batch_ok, batch_failed = _flush_sms_batch(
                sqs_batch, ddb_items_by_id, queue_table, campaign_id,
                precall_policy=precall_policy, sms_template_version=sms_template_version,
            )
            enqueued += batch_ok
            failed += batch_failed
            sqs_batch = []
            ddb_items_by_id = {}

    if sqs_batch:
        batch_ok, batch_failed = _flush_sms_batch(
            sqs_batch, ddb_items_by_id, queue_table, campaign_id,
            precall_policy=precall_policy, sms_template_version=sms_template_version,
        )
        enqueued += batch_ok
        failed += batch_failed

    return enqueued, failed, opted_out, outside_quiet_hours, rejected_fields


def _flush_sms_batch(
    sqs_batch: list[dict],
    ddb_items_by_id: dict[str, dict],
    queue_table,
    campaign_id: str,
    *,
    precall_policy: dict | None = None,
    sms_template_version: str | None = None,
) -> tuple[int, int]:
    """Send one SQS batch, then write its DDB items reflecting the real outcome.

    send_message_batch does not raise on a partial failure — some entries can fail
    while the call itself returns 200. Items whose SQS entry failed are written as
    SQS_SEND_FAILED (visible in the queue table and counted in totalSqsSendFailed,
    not totalFailed — see the note at the call site) instead of PENDING, since no
    message exists for them to ever be picked up.
    """
    managed = precall_policy is not None or sms_template_version is not None
    if managed:
        # Managed modes reserve work before dispatch. Reserve the attempted
        # count before any external work, so a crash cannot report a false
        # empty success. A reservation stranded by a crash remains pending
        # until the executor's bounded initialization deadline; never resend
        # an ambiguous provider attempt just to repair counters.
        first = json.loads(sqs_batch[0]["MessageBody"])
        runs_key = {"planId": first["planId"], "sk": f"{first['runId']}#{campaign_id}"}
        try:
            _ddb.Table(_RUNS_TABLE).update_item(
                Key=runs_key,
                UpdateExpression="ADD totalEnqueued :reserved, activeEnqueueBatches :one, enqueueRevision :one",
                ConditionExpression="#status = :running AND initializationComplete = :initializing",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":reserved": len(sqs_batch), ":one": 1, ":running": "RUNNING", ":initializing": False},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            # A concurrent initializer sealed the cohort (or the executor
            # aborted it) while this invocation was still reading profiles.
            # No new reservation or SQS work can begin after that point.
            return 0, 0
        # PENDING must exist before SQS can dispatch to the processor. Do not
        # write these items again after SQS: a fast processor may already have
        # moved them to SENT/CANCELLED before send_message_batch returns.
        with queue_table.batch_writer() as bw:
            for item in ddb_items_by_id.values():
                bw.put_item(Item=item)
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
    if not managed:
        with queue_table.batch_writer() as bw:
            for entry_id, item in ddb_items_by_id.items():
                if entry_id in failed_ids:
                    item["status"] = "SQS_SEND_FAILED"
                bw.put_item(Item=item)
    elif failed_ids:
        # Only explicit SQS rejections are safe to compensate and retry.
        # Network/unknown outcomes retain their reserved count and claims.
        # Mark every rejected row first. If a write fails, the reservation
        # remains outstanding. Decrementing first would leave a PENDING row
        # that a retry could mistake for settled work with zero reservations.
        for entry_id in failed_ids:
            item = ddb_items_by_id[entry_id]
            queue_table.update_item(
                Key={"campaignId": campaign_id, "sk": item["sk"]},
                UpdateExpression="SET #status = :failed",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":failed": "SQS_SEND_FAILED"},
            )
        _ddb.Table(_RUNS_TABLE).update_item(
            Key=runs_key,
            UpdateExpression="ADD totalEnqueued :compensation, totalSqsSendFailed :failed, activeEnqueueBatches :closed",
            ExpressionAttributeValues={":compensation": -len(failed_ids), ":failed": len(failed_ids), ":closed": -1},
        )
    else:
        _ddb.Table(_RUNS_TABLE).update_item(
            Key=runs_key,
            UpdateExpression="ADD activeEnqueueBatches :closed",
            ExpressionAttributeValues={":closed": -1},
        )

    # Release the claim for every genuinely-failed send (2026-09
    # adversarial-review Finding, Important, part 2): no message was ever
    # actually delivered for these phones, so the claim gate in
    # _process_recipients must not go on blocking a later retry from
    # re-attempting them. A delete failure here is not fatal — worst case is a
    # stale claim recoverable after _CLAIM_STALE_SECONDS, a delayed retry
    # until its timestamp is old enough — so log and continue rather than raising.
    for entry_id in failed_ids:
        item = ddb_items_by_id.get(entry_id)
        if not item:
            continue
        try:
            queue_table.delete_item(
                Key={
                    "campaignId": campaign_id,
                    "sk": f"CLAIM#{_normalize_phone(item['phone'])}",
                }
            )
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup, see comment above
            # Deliberately broad: ANY failure releasing the claim (throttling,
            # network, IAM, whatever) must degrade to "one fewer retry attempt
            # until stale-claim recovery," never to a crashed/retried batch flush that
            # could itself risk re-processing this same batch.
            _logger.warn(
                "sms_sender_claim_release_failed",
                campaign_id=campaign_id,
                error=type(exc).__name__,
            )

    return len(ddb_items_by_id) - len(failed_ids), len(failed_ids)


# Keep the deployed entry-point name for compatibility. Plans retries recover
# unfinished reads/enqueues using the persisted scheduling source, without a
# second recipient-hours check. Standalone runs retain their original gate.
_TERMINAL_RUN_STATUSES = frozenset({"COMPLETED", "ABORTED"})


def retry_quiet_hours_skipped(event: dict, context: object) -> dict:
    """
    Recover unresolved recipients of an active SMS campaign.

    event = {"campaignId": str, "planId": str, "runId": str,
             "scheduleSource": "plans"}  # source is optional for standalone

    Invoked while the paired campaign is active. Missing or terminal runs
    are no-ops. Quiet-hours counts are reporting only: a zero value does not
    mean that unfinished claims, failed enqueues, or incomplete reads are done.
    Every active pass rechecks the audience and deduplicates actual queue items.

    Returns: {"retried": int, "stillSkipped": int}
    """
    campaign_id = event["campaignId"]
    plan_id = event["planId"]
    run_id = event["runId"]
    runs_table = _ddb.Table(_RUNS_TABLE)

    resp = runs_table.get_item(
        Key={"planId": plan_id, "sk": f"{run_id}#{campaign_id}"},
        ConsistentRead=True,
    )
    record = resp.get("Item")
    if not record:
        return {"retried": 0, "stillSkipped": 0}
    record = _bind_plan_schedule_source(record, runs_table, event)
    if record.get("status") in _TERMINAL_RUN_STATUSES:
        return {"retried": 0, "stillSkipped": 0}
    if _is_managed_run(record):
        _validate_managed_run(record)
        if record.get("initializationComplete") is True:
            result = _managed_result(plan_id, run_id, campaign_id)
            result["retried"] = 0
            result["stillSkipped"] = result.get("totalSkippedQuietHours", 0)
            return result
    _validate_campaign_origin(record)

    now_iso = datetime.now(timezone.utc).isoformat()
    ttl = int(time.time()) + _TTL_SECONDS

    try:
        recipients = _get_segment_recipients(
            record.get("segmentName", ""),
            plan_id=plan_id,
            run_id=run_id,
            campaign_id=campaign_id,
        )
    except SegmentRecipientsPending:
        if _is_managed_run(record):
            return _managed_result(plan_id, run_id, campaign_id)
        return {
            "pending": True,
            "retried": 0,
            "stillSkipped": int(record.get("totalSkippedQuietHours") or 0),
        }
    except _SmsRunInactive:
        return {"retried": 0, "stillSkipped": 0}
    already_sent_phones = _get_already_sent_phones(campaign_id)
    if _is_managed_run(record):
        result = _process_managed_run(
            recipients, record=record, campaign_id=campaign_id,
            plan_id=plan_id, run_id=run_id, already_sent_phones=already_sent_phones,
            now_iso=now_iso, ttl=ttl,
        )
        result["retried"] = result.get("enqueued", 0)
        result["stillSkipped"] = result.get("totalSkippedQuietHours", 0)
        return result
    queue_table = _ddb.Table(_QUEUE_TABLE)

    enqueued, failed, opted_out, outside_quiet_hours, rejected_fields = (
        _process_recipients(
            recipients,
            campaign_id=campaign_id,
            plan_id=plan_id,
            run_id=run_id,
            message_tmpl=record.get("messageTemplate", ""),
            clinic_name=record.get("clinicName", ""),
            origination_arn=record.get("originationNumberArn", ""),
            already_sent_phones=already_sent_phones,
            now_iso=now_iso,
            ttl=ttl,
            queue_table=queue_table,
            schedule_source=record.get("scheduleSource"),
        )
    )

    if rejected_fields is not None:
        # PHI rule: log the offending field NAMES only — never the template body.
        _logger.warn(
            "sms_sender_template_rejected_non_allowlisted_placeholder",
            campaign_id=campaign_id,
            fields=sorted(rejected_fields),
        )

    # totalEnqueued is cumulative across every pass (ADD) — this call's
    # `enqueued` is only the NEW sends from this pass. totalSkippedQuietHours
    # is NOT cumulative (SET) — it is recomputed fresh every pass and means
    # "how many are still stuck outside quiet hours right now", not "how many
    # have ever been skipped" (a recipient counted here on one pass and then
    # sent on the next must disappear from this count, not accumulate in it).
    runs_table.update_item(
        Key={"planId": plan_id, "sk": f"{run_id}#{campaign_id}"},
        UpdateExpression=(
            "ADD totalEnqueued :n, totalSqsSendFailed :f "
            "SET totalSkippedQuietHours = :q, totalSkippedOptOut = :o, updatedAt = :t"
        ),
        ExpressionAttributeValues={
            ":n": enqueued,
            ":f": failed,
            ":o": opted_out,
            ":q": outside_quiet_hours,
            ":t": now_iso,
        },
    )

    _logger.info(
        "precall_sms_quiet_hours_retry",
        campaign_id=campaign_id,
        newly_enqueued=enqueued,
        sqs_send_failed=failed,
        skipped_opt_out=opted_out,
        still_skipped_quiet_hours=outside_quiet_hours,
    )
    return {"retried": enqueued, "stillSkipped": outside_quiet_hours}


def _get_already_sent_phones(campaign_id: str) -> set[str]:
    """Every phone with a genuinely-attempted VipSmsCampaignQueue message item
    for this campaignId — i.e. was already sent (or enqueued) on a prior pass,
    so a retry must not send to it again.

    Excludes two things a naive "any item for this phone" check would wrongly
    fold in:
      - CLAIM# records (written by _process_recipients' claim gate) — these
        carry no "phone" attribute and must never be mistaken for a sent
        message.
      - Phones whose ONLY queue item is status SQS_SEND_FAILED (2026-09
        adversarial-review Finding, Important, part 1): send_message_batch
        rejected them before any message ever reached SQS, so nothing was
        actually delivered. Treating that as "already sent" would silently
        and permanently exclude the phone from every future retry, even
        though the retry feature's whole purpose is to eventually deliver to
        it. A phone with at least one non-failed item IS still treated as
        sent, even if it also has an unrelated failed item from another pass.

    This strongly consistent history check prevents resending after a claim
    has aged out. The conditional claim gate in _process_recipients closes
    the remaining race between two overlapping passes reading the same history.

    Mirrors the campaignId-keyed Query pattern used elsewhere for this table
    (see executor.py's _count_sms_queue), selecting phone + status via
    ProjectionExpression instead of Select=COUNT.
    """
    table = _ddb.Table(_QUEUE_TABLE)
    kwargs: dict = {
        "KeyConditionExpression": "campaignId = :cid",
        "ExpressionAttributeValues": {":cid": campaign_id},
        "ProjectionExpression": "#p, #s",
        "ConsistentRead": True,
        "ExpressionAttributeNames": {"#p": "phone", "#s": "status"},
    }
    phones: set[str] = set()
    while True:
        resp = table.query(**kwargs)
        for item in resp.get("Items", []):
            phone = item.get("phone")
            if not phone or item.get("status") == "SQS_SEND_FAILED":
                continue
            phones.add(phone)
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    return phones


class _SmsRunInactive(RuntimeError):
    """The campaign ended while its asynchronous audience was being prepared."""

    def __init__(self, record: dict | None = None):
        super().__init__("SMS run is no longer active")
        self.record = record or {}


def _get_segment_recipients(
    segment_name: str, *, plan_id: str, run_id: str, campaign_id: str
) -> list[dict]:
    """Load a complete audience using this run's immutable snapshot metadata.

    Only the snapshot identifier/location are stored in the runs table. The
    reader retrieves recipient data from the encrypted export and Profiles.
    Snapshot creation can outlive one invocation; a pending snapshot is work
    in progress and must not be counted as an empty audience.
    """
    runs_table = _ddb.Table(_RUNS_TABLE)
    key = {"planId": plan_id, "sk": f"{run_id}#{campaign_id}"}

    def active_record() -> dict:
        record = runs_table.get_item(Key=key, ConsistentRead=True).get("Item")
        if not record or record.get("status") != "RUNNING":
            raise _SmsRunInactive(record)
        if record.get("segmentName") != segment_name:
            raise RuntimeError("SMS run audience changed during recipient read")
        return record

    def load_snapshot() -> dict | None:
        return active_record().get("recipientSnapshot")

    def publish_snapshot(candidate: dict) -> dict:
        # Concurrent callers can create separate exports, but only one becomes
        # this run's source. Never replace its winner or revive a terminal run.
        try:
            runs_table.update_item(
                Key=key,
                UpdateExpression="SET recipientSnapshot = :snapshot",
                ConditionExpression=(
                    "attribute_exists(sk) AND #status = :running "
                    "AND segmentName = :segment "
                    "AND attribute_not_exists(recipientSnapshot)"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":snapshot": candidate,
                    ":running": "RUNNING",
                    ":segment": segment_name,
                },
            )
            return candidate
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            winner = active_record().get("recipientSnapshot")
            if not winner:
                raise RuntimeError("SMS snapshot publication lost its run") from None
            return winner

    try:
        recipients = _load_segment_recipients(
            cp=_cp,
            s3=_s3,
            domain=_DOMAIN,
            segment_name=segment_name,
            snapshot_bucket=_SNAPSHOT_BUCKET,
            snapshot_role_arn=_SNAPSHOT_ROLE_ARN,
            encryption_key_arn=_SNAPSHOT_KEY_ARN,
            load_snapshot=load_snapshot,
            publish_snapshot=publish_snapshot,
        )
        # The run may have been aborted while waiting for a completed export.
        # Check again before any claim, SQS message, or counter update is made.
        active_record()
        return [
            {
                "phone": _normalize_phone(recipient["phone"]),
                "ProfileId": recipient.get("ProfileId"),
                "FirstName": recipient.get("FirstName") or "",
                "Attributes": recipient.get("Attributes") or {},
            }
            for recipient in recipients
        ]
    except (SegmentRecipientsPending, _SmsRunInactive):
        raise
    except Exception as exc:
        _logger.warn("sms_sender_get_segment_phones_failed", error=type(exc).__name__)
        raise RuntimeError("SMS recipient read failed") from None


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
