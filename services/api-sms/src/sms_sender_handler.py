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

from vip_shared.domain.services.quiet_hours import (
    is_within_quiet_hours as _is_within_quiet_hours,
)
from vip_shared.domain.services.sms_template import (
    ALLOWED_FIELDS,
    extract_placeholders,
    render as _render,
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
_TTL_SECONDS = 30 * 24 * 3600  # 30 days
# Claim records (see _process_recipients' claim gate) are a short-lived DB-level
# dedup guard, not a long-lived audit record — 15 minutes is comfortably longer
# than this Lambda's own timeout (5 min) plus margin, so a claim from a crashed
# invocation self-heals reasonably promptly even without the explicit release
# in _flush_sms_batch. Deliberately NOT _TTL_SECONDS (30 days) — that lifetime
# is wrong for a claim, whose only purpose is to survive one overlapping tick.
_CLAIM_TTL_SECONDS = 15 * 60

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
        },
        ConditionExpression="attribute_not_exists(sk)",
    )

    # Extract recipients (phone + allowlisted render fields) from CP segment
    recipients = _get_segment_recipients(segment_name)

    queue_table = _ddb.Table(_QUEUE_TABLE)
    enqueued, failed, opted_out, outside_quiet_hours, rejected_fields = (
        _process_recipients(
            recipients,
            campaign_id=campaign_id,
            plan_id=event["planId"],
            run_id=event["runId"],
            message_tmpl=message_tmpl,
            clinic_name=event.get("clinicName", ""),
            origination_arn=origination_arn,
            already_sent_phones=set(),  # first pass — nothing sent yet
            now_iso=now_iso,
            ttl=ttl,
            queue_table=queue_table,
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

    # Update enqueued/skipped-opt-out/sqs-send-failed counts.
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
            "SET totalEnqueued = :n, totalSqsSendFailed = :f, "
            "totalSkippedOptOut = :o, totalSkippedQuietHours = :q, updatedAt = :t"
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
) -> tuple[int, int, int, int, set[str] | None]:
    """Per-recipient opt-out/quiet-hours/render/enqueue loop, shared by the
    first-pass send (lambda_handler, already_sent_phones=set()) and the
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
            continue
        if _opt_out.is_blocked(phone):
            opted_out += 1
            continue
        # TCPA: the recipient's own local time, not the call-center's. This is
        # the per-patient gate; executor.py's COT workingHours check is about
        # whether our Bogota staff are on shift and does not answer this.
        if not _is_within_quiet_hours(phone):
            outside_quiet_hours += 1
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
        # sk uses a CLAIM# prefix, never colliding with a real message item's
        # f"{iso_timestamp}#{random_hex}" sk. _normalize_phone is idempotent on
        # an already-E.164 phone, so the same phone always maps to the same key.
        try:
            queue_table.put_item(
                Item={
                    "campaignId": campaign_id,
                    "sk": f"CLAIM#{_normalize_phone(phone)}",
                    "claimedAt": now_iso,
                    "ttl": int(time.time()) + _CLAIM_TTL_SECONDS,
                },
                ConditionExpression="attribute_not_exists(sk)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                # Another concurrent execution (or a duplicate profile within
                # this same pass) already claimed this phone — skip silently,
                # same treatment as already_sent_phones. This is the actual
                # race-closing guarantee; the already_sent_phones pre-check
                # above is only a cheap optimization to avoid attempting a
                # claim at all for someone almost certainly already sent.
                continue
            raise

        try:
            body = _render(
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

    return enqueued, failed, opted_out, outside_quiet_hours, rejected_fields


def _flush_sms_batch(
    sqs_batch: list[dict],
    ddb_items_by_id: dict[str, dict],
    queue_table,
    campaign_id: str,
) -> tuple[int, int]:
    """Send one SQS batch, then write its DDB items reflecting the real outcome.

    send_message_batch does not raise on a partial failure — some entries can fail
    while the call itself returns 200. Items whose SQS entry failed are written as
    SQS_SEND_FAILED (visible in the queue table and counted in totalSqsSendFailed,
    not totalFailed — see the note at the call site) instead of PENDING, since no
    message exists for them to ever be picked up.
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

    # Release the claim for every genuinely-failed send (2026-09
    # adversarial-review Finding, Important, part 2): no message was ever
    # actually delivered for these phones, so the claim gate in
    # _process_recipients must not go on blocking a later retry from
    # re-attempting them. A delete failure here is not fatal — worst case is a
    # stale claim that self-heals via _CLAIM_TTL_SECONDS, one fewer retry
    # attempt until it expires, not a correctness issue — so log and continue
    # rather than raising.
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
            # until TTL expiry," never to a crashed/retried batch flush that
            # could itself risk re-processing this same batch.
            _logger.warn(
                "sms_sender_claim_release_failed",
                campaign_id=campaign_id,
                error=type(exc).__name__,
            )

    return len(ddb_items_by_id) - len(failed_ids), len(failed_ids)


# ── Quiet-hours retry entry point ─────────────────────────────────────────────
# Closes a finding from the 2026-09 adversarial code review: the pre-call SMS
# quiet-hours check above (in _process_recipients, via lambda_handler) runs
# exactly once, at bucket activation. The paired Connect Campaigns V2 voice
# campaign, by contrast, uses localTimeZoneDetection=AREA_CODE + openHours,
# which Connect's own campaign engine re-evaluates CONTINUOUSLY for as long as
# the campaign stays "running" — so a recipient outside their local quiet-hours
# window at activation could still get dialed hours later (once their window
# opens) having never received the pre-call text.
#
# retry_quiet_hours_skipped closes that gap by giving the SMS side the same
# continuous re-evaluation, invoked repeatedly from executor.py's tick() poll
# loop for as long as the paired voice campaign remains "running" (see
# executor._invoke_sms_retry_quiet_hours) — the same active window Connect's
# own AREA_CODE detection uses, so retries stop the instant the voice campaign
# does, with no separate bookkeeping needed for that bound.
_TERMINAL_RUN_STATUSES = frozenset({"COMPLETED", "ABORTED"})


def retry_quiet_hours_skipped(event: dict, context: object) -> dict:
    """
    Re-attempt sends for recipients skipped for quiet hours on an earlier pass
    (the original send, or a prior retry) of this precall SMS campaign.

    event = {"campaignId": str, "planId": str, "runId": str}  # smsCampaignId

    Deliberately cheap when there's nothing to do — this is invoked on every
    tick of an active precall-SMS-enabled campaign, and most ticks will have
    zero recipients newly eligible. No-ops (zero DDB/CP calls beyond the one
    get_item) when: the run record doesn't exist, its status is already
    terminal (COMPLETED/ABORTED), or totalSkippedQuietHours is already 0.

    Returns: {"retried": int, "stillSkipped": int}
    """
    campaign_id = event["campaignId"]
    plan_id = event["planId"]
    run_id = event["runId"]
    runs_table = _ddb.Table(_RUNS_TABLE)

    resp = runs_table.get_item(Key={"planId": plan_id, "sk": f"{run_id}#{campaign_id}"})
    record = resp.get("Item")
    if not record:
        return {"retried": 0, "stillSkipped": 0}
    if record.get("status") in _TERMINAL_RUN_STATUSES:
        return {"retried": 0, "stillSkipped": 0}
    if int(record.get("totalSkippedQuietHours") or 0) == 0:
        return {"retried": 0, "stillSkipped": 0}

    now_iso = datetime.now(timezone.utc).isoformat()
    ttl = int(time.time()) + _TTL_SECONDS

    recipients = _get_segment_recipients(record.get("segmentName", ""))
    already_sent_phones = _get_already_sent_phones(campaign_id)
    queue_table = _ddb.Table(_QUEUE_TABLE)

    enqueued, failed, _opted_out, outside_quiet_hours, rejected_fields = (
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
            "ADD totalEnqueued :n SET totalSkippedQuietHours = :q, updatedAt = :t"
        ),
        ExpressionAttributeValues={
            ":n": enqueued,
            ":q": outside_quiet_hours,
            ":t": now_iso,
        },
    )

    _logger.info(
        "precall_sms_quiet_hours_retry",
        campaign_id=campaign_id,
        newly_enqueued=enqueued,
        sqs_send_failed=failed,
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

    This is purely a cheap pre-check optimization to avoid attempting a claim
    at all for someone almost certainly already sent — the actual
    correctness guarantee against a duplicate send is the claim gate in
    _process_recipients, not this query.

    Mirrors the campaignId-keyed Query pattern used elsewhere for this table
    (see executor.py's _count_sms_queue), selecting phone + status via
    ProjectionExpression instead of Select=COUNT.
    """
    table = _ddb.Table(_QUEUE_TABLE)
    kwargs: dict = {
        "KeyConditionExpression": "campaignId = :cid",
        "ExpressionAttributeValues": {":cid": campaign_id},
        "ProjectionExpression": "#p, #s",
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


_BATCH_GET_PROFILE_MAX = 100  # CP API limit per BatchGetProfile call


def _get_segment_recipients(segment_name: str) -> list[dict]:
    """Read recipients (phone + allowlisted render fields) from a CP segment via
    GetSegmentMembership.

    Collects all profile IDs per membership page, then calls BatchGetProfile
    in groups of up to 100 — reducing API calls from O(n) to O(n/100).
    """
    recipients: list[dict] = []
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
                        # Minimum necessary: carry ONLY the allowlisted render
                        # fields out of the profile, never the whole record.
                        recipients.append(
                            {
                                "phone": _normalize_phone(raw),
                                "FirstName": profile.get("FirstName") or "",
                            }
                        )
            next_token = resp.get("NextToken")
            if not next_token:
                break
            kwargs["NextToken"] = next_token
    except Exception as exc:
        _logger.warn("sms_sender_get_segment_phones_failed", error=type(exc).__name__)
    return recipients


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
