"""Plan execution state machine — v2 (DAG campaigns per bucket).

Execution model
───────────────
start_run(plan_id)
  └─ _start_bucket(run, 0)
       └─ _dispatch_ready_campaigns(run, 0)
            ├─ stage-1 campaigns (empty dependsOn) → _start_one_campaign
            └─ stage-N campaigns wait for parents

tick(plan_id, run_id, bucket_index)  [EventBridge rate(1 min)]
  ├─ poll running campaigns → update state
  ├─ time-based: pre-start next bucket at (duration - 5 min)
  ├─ time-based: expire bucket at duration → _expire_bucket
  ├─ _dispatch_ready_campaigns (newly unblocked by parents completing)
  └─ all terminal → _advance_bucket → next bucket or run complete

Dependency semantics (AND, cross-bucket supported)
───────────────────────────────────────────────────
  dependsOn = []            → waits for entire PREVIOUS bucket to complete
  dependsOn = [c1, c2, ...]  → waits for ALL listed campaigns (any bucket)

Dependents always proceed regardless of parent exit status (no cascade-cancel).
A cancelled or errored parent is treated as "done" — the dependent will attempt
to start and will skip/error on its own if there are no leads or other issues.

Pre-start warming (prestart_next = true, time_based buckets only)
─────────────────────────────────────────────────────────────────
5 min before bucket expires: create Connect campaigns (not start) for
the next bucket's stage-1 (empty dependsOn) campaigns. When the bucket
officially advances, warming campaigns are started without re-creating.

on_plan_complete chaining
─────────────────────────
After the last bucket advances: call start_run_chained(plan_id) which
finds all plans triggered by this plan and fires them.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Final

import boto3
from botocore.exceptions import ClientError
from vip_shared.infrastructure.persistence.audit import build_from_env as build_audit

from builders import (
    _JOURNEY_FLOW_NAME,
    all_known_locations,
    build_campaign_params,
    build_segment_name,
    campaign_to_segment_filters,
    locations_for_state_codes,
    resolve_campaign_flow_arn,
    resolve_journey_flow_arn,
)
from store import (
    ConcurrentWriteError,
    create_run,
    find_plans_by_trigger_planid,
    get_plan,
    get_run,
    get_latest_run,
    list_plans,
    lock_plan_run,
    record_bucket_schedule_name,
    save_run,
    unlock_plan_run,
    update_plan_pending_warmup,
    update_plan_trigger,
)

logger = logging.getLogger(__name__)

try:
    from vip_shared.infrastructure.telemetry.structured_logger import (
        StructuredLogger as _SL,
    )

    _slog = _SL(service="api-plans")
except ImportError:

    class _NoopLogger:  # pragma: no cover
        def info(self, *a, **k):
            pass

        def warn(self, *a, **k):
            pass

        def error(self, *a, **k):
            pass

    _slog = _NoopLogger()  # type: ignore[assignment]

CONNECT_INSTANCE_ID: Final = os.environ.get("CONNECT_INSTANCE_ID", "")
PROFILES_DOMAIN_NAME: Final = os.environ.get("PROFILES_DOMAIN_NAME", "")
LAMBDA_FUNCTION_ARN: Final = os.environ.get("LAMBDA_FUNCTION_ARN", "")
SNS_ALERTS_TOPIC_ARN: Final = os.environ.get("SNS_ALERTS_TOPIC_ARN", "")

# ── Progressive branded dialer env vars (wired in CDK at Task 9) ──────────────
_PROGRESSIVE_DIALER_SEEDER_ARN: Final = os.environ.get("PROGRESSIVE_DIALER_SEEDER_ARN", "")
_ACTIVE_BRANDED_CAMPAIGNS_TABLE: Final = os.environ.get("ACTIVE_BRANDED_CAMPAIGNS_TABLE", "")
_CAMPAIGN_QUEUE_TABLE_BRANDED: Final = os.environ.get("CAMPAIGN_QUEUE_TABLE_BRANDED", "")
_BRANDED_RUN_SUMMARY_TABLE: Final = os.environ.get("BRANDED_RUN_SUMMARY_TABLE", "")

# Consecutive branded-poll failure tracking — keyed by brandedCampaignId.
# Resets on a successful poll; campaign transitions to error after this many consecutive failures.
_branded_poll_failures: dict[str, int] = {}
_BRANDED_POLL_FAILURE_LIMIT: Final = 5

# Pre-start window: create next-bucket campaigns this many minutes before expiry
_PRESTART_MINUTES: Final = 5

# Daily hard-stop hour in COT (UTC-5 fixed, consistent with all other time guards in tick()).
_DAILY_CUTOFF_HOUR: Final = 19  # 7 PM COT = 00:00 UTC

# Runs active longer than this many hours without completing are flagged as stuck.
_STUCK_RUN_HOURS: Final = 4

# A "running" bucket with zero campaigns in one of these statuses for
# _NO_ACTIVE_CAMPAIGN_MINUTES is flagged — catches a tick that crashed before
# ever creating the bucket's campaigns (see BD-013), long before StuckRun
# would (that only fires after _STUCK_RUN_HOURS of the whole run, not this
# specific bucket-level symptom).
_NO_ACTIVE_CAMPAIGN_MINUTES: Final = 5
_ACTIVE_CAMPAIGN_STATUSES: Final = frozenset({"creating", "warming", "running"})

# ── Campaign exit reasons ─────────────────────────────────────────────────────

REASON_COMPLETED: Final = "completed"
REASON_STOPPED: Final = "stopped"
REASON_EXPIRED: Final = "expired"
REASON_BUCKET_EXPIRED: Final = "bucket_expired"
REASON_ERROR: Final = "error"
REASON_SKIPPED_EMPTY: Final = "skipped_empty"
REASON_RECONCILE_FAILED: Final = "reconcile_failed"
REASON_CREATION_FAILED: Final = "creation_failed"
REASON_CANCELLED: Final = "cancelled"
REASON_PARENT_CANCELLED: Final = "parent_cancelled"
REASON_ABORTED: Final = "aborted"


def _is_branded(campaign: dict) -> bool:
    """Return True if this campaign uses the Progressive Branded Dialer channel.

    Discriminator is deliveryType='branded', NOT dialerType — dialerType is injected
    verbatim as a Connect V2 JSON key and would cause ValidationException if set to
    'branded'.
    """
    return campaign.get("deliveryType") == "branded"


def _is_sms(campaign: dict) -> bool:
    """Return True if this campaign uses the EUM SMS bulk delivery channel."""
    return campaign.get("deliveryType") == "sms"


# ── Branded dialer boto3 singletons ──────────────────────────────────────────

_lambda_client = None
_ddb_client_branded = None


def _get_lambda_client():
    global _lambda_client
    if _lambda_client is None:
        import boto3 as _boto3
        _lambda_client = _boto3.client("lambda")
    return _lambda_client


def _get_ddb_client():
    global _ddb_client_branded
    if _ddb_client_branded is None:
        import boto3 as _boto3
        _ddb_client_branded = _boto3.client("dynamodb")
    return _ddb_client_branded


def _emit_branded_metric(metric_name: str, value: float = 1.0) -> None:
    """Emit a count metric to VipConnect/ProgressiveDialer for branded dialer events.

    Fire-and-forget — failures are logged but never raised so metric emission
    never interrupts plan execution.
    """
    try:
        import boto3 as _boto3
        _boto3.client("cloudwatch").put_metric_data(
            Namespace="VipConnect/ProgressiveDialer",
            MetricData=[{
                "MetricName": metric_name,
                "Value": value,
                "Unit": "Count",
                "Dimensions": [{"Name": "DeliveryType", "Value": "branded"}],
            }],
        )
    except Exception as exc:
        logger.warning("_emit_branded_metric %s failed: %s", metric_name, type(exc).__name__)


def _emit_dispatch_stalled_metric(campaign_id: str) -> None:
    """Emit CampaignDispatchStalled to VIPPlans when a campaign reverts to "queued"
    instead of advancing (Redis rebuilding, empty segment retry). Applies to any
    delivery type, not just branded — unlike _emit_branded_metric.

    Emitted twice, same convention as ScheduledRunFallback: with CampaignId (for
    per-campaign drill-down) and without dimensions (aggregate, so a single CLI
    alarm can watch "any campaign stalled" without a Metric Math/SEARCH alarm).

    Fire-and-forget — failures are logged but never raised.
    """
    try:
        boto3.client("cloudwatch").put_metric_data(
            Namespace="VIPPlans",
            MetricData=[
                {
                    "MetricName": "CampaignDispatchStalled",
                    "Value": 1,
                    "Unit": "Count",
                    "Dimensions": [{"Name": "CampaignId", "Value": campaign_id}],
                },
                {
                    "MetricName": "CampaignDispatchStalled",
                    "Value": 1,
                    "Unit": "Count",
                    "Dimensions": [],
                },
            ],
        )
    except Exception as exc:
        logger.warning("_emit_dispatch_stalled_metric failed: %s", type(exc).__name__)


def _count_branded_queue(campaign_id: str) -> int:
    """Count PENDING+DISPATCHING items in VipProgressiveCampaignQueue for this campaign.

    Paginates through all pages so large queues are correctly counted.
    Uses eventual consistency — a count of 0 means the queue is drained.
    Callers must handle exceptions (transient DDB errors) without transitioning state.
    """
    ddb = _get_ddb_client()
    kwargs = dict(
        TableName=_CAMPAIGN_QUEUE_TABLE_BRANDED,
        KeyConditionExpression="campaignId = :c",
        FilterExpression="#s IN (:p, :d)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":c": {"S": campaign_id},
            ":p": {"S": "PENDING"},
            ":d": {"S": "DISPATCHING"},
        },
        Select="COUNT",
    )
    total = 0
    while True:
        resp = ddb.query(**kwargs)
        total += resp.get("Count", 0)
        if total > 0:
            return total  # early exit — non-zero means not drained
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    return total


def _count_sms_queue(campaign_id: str) -> int:
    """Count PENDING items in VipSmsCampaignQueue for polling SMS campaign completion.

    Returns 0 when the queue is drained (all messages sent/failed/opted-out).
    Paginates through all DDB pages — mirrors _count_branded_queue pattern.
    Callers must handle exceptions without transitioning state.
    """
    table = boto3.resource("dynamodb").Table(
        os.environ.get("SMS_CAMPAIGN_QUEUE_TABLE", "VipSmsCampaignQueue")
    )
    kwargs: dict = {
        "KeyConditionExpression": "campaignId = :cid",
        "FilterExpression": "#s IN (:p, :s)",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {
            ":cid": campaign_id,
            ":p": "PENDING",
            ":s": "SENDING",
        },
        "Select": "COUNT",
    }
    total = 0
    while True:
        resp = table.query(**kwargs)
        total += resp.get("Count", 0)
        if total > 0:
            return total  # early exit — non-zero means not drained
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    return total


def _stop_sms_campaign(cs: dict) -> None:
    """Mark the VipSmsCampaignRuns record as ABORTED for a stopped SMS campaign.

    Uses the primary key stored in cs (_smsRunsPlanId, _smsRunsSk) for a direct
    update_item — no table scan needed.
    Non-fatal — a failure here does not abort the plan-level stop.
    """
    campaign_id = cs.get("smsCampaignId", "")
    plan_id = cs.get("_smsRunsPlanId", "")
    sk = cs.get("_smsRunsSk", "")
    if not campaign_id or not plan_id or not sk:
        return
    try:
        now_iso = _now_utc().isoformat()
        boto3.resource("dynamodb").Table(
            os.environ.get("SMS_CAMPAIGN_RUNS_TABLE", "VipSmsCampaignRuns")
        ).update_item(
            Key={"planId": plan_id, "sk": sk},
            UpdateExpression="SET #s = :a, completedAt = :t, exitReason = :r, updatedAt = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":a": "ABORTED",
                ":t": now_iso,
                ":r": cs.get("exitReason", "aborted"),
            },
        )
    except Exception as exc:
        logger.warning("_stop_sms_campaign error: %s", type(exc).__name__)


def _complete_sms_campaign(cs: dict) -> None:
    """Mark the VipSmsCampaignRuns record as COMPLETED for a queue-drained campaign.

    Uses the primary key stored in cs (_smsRunsPlanId, _smsRunsSk) for a direct
    update_item — no table scan needed.
    Non-fatal — a failure here does not block plan-level completion.
    """
    campaign_id = cs.get("smsCampaignId", "")
    plan_id = cs.get("_smsRunsPlanId", "")
    sk = cs.get("_smsRunsSk", "")
    if not campaign_id or not plan_id or not sk:
        return
    try:
        now_iso = _now_utc().isoformat()
        boto3.resource("dynamodb").Table(
            os.environ.get("SMS_CAMPAIGN_RUNS_TABLE", "VipSmsCampaignRuns")
        ).update_item(
            Key={"planId": plan_id, "sk": sk},
            UpdateExpression="SET #s = :c, completedAt = :t, exitReason = :r, updatedAt = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":c": "COMPLETED",
                ":t": now_iso,
                ":r": cs.get("exitReason", "queue_drained"),
            },
        )
    except Exception as exc:
        logger.warning("_complete_sms_campaign error: %s", type(exc).__name__)


def _invoke_sms_sender(**kwargs: object) -> None:
    """Invoke the SMS Sender Lambda synchronously (mirrors _invoke_seeder pattern)."""
    import json as _json

    response = _get_lambda_client().invoke(
        FunctionName=os.environ["SMS_SENDER_FUNCTION_ARN"],
        InvocationType="RequestResponse",
        Payload=_json.dumps(kwargs).encode(),
    )
    if response.get("FunctionError"):
        payload_bytes = response["Payload"].read()
        raise RuntimeError(
            f"SMS Sender Lambda error: {payload_bytes[:200]!r}"
        )


def get_branded_queue_counts(branded_campaign_id: str) -> tuple[int, int]:
    """Return (pending_count, dialed_count) for a branded campaign queue.

    Queries VipProgressiveCampaignQueue twice — once filtered to PENDING/DISPATCHING,
    once to DIALED — and returns both counts. Used by the branded-progress endpoint.
    Raises on DDB errors; callers decide whether to swallow or propagate.
    """
    if not _CAMPAIGN_QUEUE_TABLE_BRANDED:
        return (0, 0)
    ddb = _get_ddb_client()
    base_kwargs = dict(
        TableName=_CAMPAIGN_QUEUE_TABLE_BRANDED,
        KeyConditionExpression="campaignId = :c",
        ExpressionAttributeValues={":c": {"S": branded_campaign_id}},
        Select="COUNT",
    )

    def _count_with_filter(filter_expr: str, attr_values: dict) -> int:
        kwargs = {**base_kwargs, "FilterExpression": filter_expr,
                  "ExpressionAttributeNames": {"#s": "status"}}
        kwargs["ExpressionAttributeValues"] = {**base_kwargs["ExpressionAttributeValues"], **attr_values}
        total = 0
        while True:
            resp = ddb.query(**kwargs)
            total += resp.get("Count", 0)
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                return total
            kwargs["ExclusiveStartKey"] = lek

    pending = _count_with_filter(
        "#s IN (:p, :d)",
        {":p": {"S": "PENDING"}, ":d": {"S": "DISPATCHING"}},
    )
    dialed = _count_with_filter("#s = :dl", {":dl": {"S": "DIALED"}})
    return (pending, dialed)


def get_branded_queue_items(branded_campaign_id: str, limit: int = 50) -> list[dict]:
    """Return up to `limit` queue items newest-first for a branded campaign.

    HIPAA: phone is PHI — only phone_last4 is returned, never the full number.
    Each item: {"phone_last4": str, "status": str, "seededAt": str}.
    """
    if not _CAMPAIGN_QUEUE_TABLE_BRANDED:
        return []
    ddb = _get_ddb_client()
    resp = ddb.query(
        TableName=_CAMPAIGN_QUEUE_TABLE_BRANDED,
        KeyConditionExpression="campaignId = :c",
        ExpressionAttributeValues={":c": {"S": branded_campaign_id}},
        ProjectionExpression="sk, phone, #s",
        ExpressionAttributeNames={"#s": "status"},
        ScanIndexForward=False,
        Limit=limit,
    )
    result = []
    for raw in resp.get("Items", []):
        sk = raw.get("sk", {}).get("S", "")
        phone = raw.get("phone", {}).get("S", "")
        status = raw.get("status", {}).get("S", "")
        seeded_at = sk.split("#")[0] if "#" in sk else sk
        result.append({
            "phone_last4": phone[-4:] if len(phone) >= 4 else phone,
            "status": status,
            "seededAt": seeded_at,
        })
    return result


def _expire_branded_queue_items(campaign_id: str) -> None:
    """Set TTL=now on all PENDING/DISPATCHING items in VipProgressiveCampaignQueue.

    DynamoDB TTL sweep will delete them within 48h. This stops the consumer from
    dequeuing contacts for a stopped/aborted campaign.
    Batch-writes up to 3000 items in pages of 25 — acceptable for segment max size.
    UnprocessedItems are retried up to 3 times with exponential backoff (0.1s base).
    """
    now_epoch = int(time.time())
    table = _CAMPAIGN_QUEUE_TABLE_BRANDED
    ddb = _get_ddb_client()
    last_key = None
    while True:
        kwargs = dict(
            TableName=table,
            KeyConditionExpression="campaignId = :c",
            FilterExpression="#s IN (:p, :d)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":c": {"S": campaign_id},
                ":p": {"S": "PENDING"},
                ":d": {"S": "DISPATCHING"},
            },
            ProjectionExpression="campaignId, sk",
        )
        if last_key:
            kwargs["ExclusiveStartKey"] = last_key
        resp = ddb.query(**kwargs)
        items = resp.get("Items", [])
        # Batch write in chunks of 25, retrying UnprocessedItems up to 3 times
        for i in range(0, len(items), 25):
            chunk = items[i : i + 25]
            batch = [
                {
                    "PutRequest": {
                        "Item": {
                            "campaignId": item["campaignId"],
                            "sk":         item["sk"],
                            "status":     {"S": "EXPIRED"},
                            "ttl":        {"N": str(now_epoch)},
                        }
                    }
                }
                for item in chunk
            ]
            attempts = 0
            while batch and attempts < 3:
                resp_bw = ddb.batch_write_item(RequestItems={table: batch})
                unprocessed = resp_bw.get("UnprocessedItems", {}).get(table, [])
                if unprocessed:
                    attempts += 1
                    time.sleep(0.1 * (2 ** attempts))
                    batch = unprocessed
                else:
                    break
            else:
                if batch:
                    logger.warning(
                        "_expire_branded_queue_items: %d items unprocessed after retries for %s",
                        len(batch), campaign_id,
                    )
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break


def _safe_expire_branded_queue(campaign_id: str, context: str) -> None:
    """Best-effort queue cleanup for branded error paths.

    Skips when the queue table is not configured (e.g. the env-var guard itself
    raised, so nothing was ever seeded) and NEVER raises — cleanup failure must
    not mask the caller's error-status transition or escape the function.
    """
    if not _CAMPAIGN_QUEUE_TABLE_BRANDED:
        return
    try:
        _expire_branded_queue_items(campaign_id)
    except Exception as exc:
        logger.error(
            "_safe_expire_branded_queue: cleanup failed [%s] for %s: %s",
            context, campaign_id, type(exc).__name__,
        )


def _stop_branded_campaign(cs: dict) -> None:
    """Remove branded campaign from VipActiveBrandedCampaigns and expire its queue.

    Must NEVER raise — log errors and continue so the calling abort/stop path
    completes even if cleanup partially fails.
    """
    campaign_id = cs.get("brandedCampaignId")
    queue_arn = cs.get("queueArn")
    if not campaign_id or not queue_arn:
        logger.warning("_stop_branded_campaign: missing campaign_id or queue_arn, skipping")
        return

    try:
        _get_ddb_client().delete_item(
            TableName=_ACTIVE_BRANDED_CAMPAIGNS_TABLE,
            Key={
                "pk": {"S": f"QUEUE#{queue_arn}"},
                "sk": {"S": f"CAMPAIGN#{campaign_id}"},
            },
            ConditionExpression="attribute_exists(pk)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # Another invocation already deleted the record — idempotent no-op
            logger.debug(
                "_stop_branded_campaign: record already deleted for %s (ConditionalCheckFailed)",
                campaign_id,
            )
        else:
            logger.error(
                "_stop_branded_campaign: delete failed for %s: %s",
                campaign_id, type(exc).__name__,
            )
    except Exception as exc:
        logger.error(
            "_stop_branded_campaign: delete failed for %s: %s",
            campaign_id, type(exc).__name__,
        )

    try:
        _expire_branded_queue_items(campaign_id)
    except Exception as exc:
        logger.error(
            "_stop_branded_campaign: expire queue failed for %s: %s",
            campaign_id, type(exc).__name__,
        )


def _write_branded_run_summary(plan_id: str, run_id: str, cs: dict) -> None:
    """Update VipBrandedRunSummary with completion metrics after a branded campaign ends.

    Uses update_item to preserve the START record's settings fields written by
    _write_branded_run_start. Must NEVER raise — failures are logged and swallowed.
    No phone numbers written — this table contains no PHI.
    """
    if not _BRANDED_RUN_SUMMARY_TABLE:
        return
    campaign_id = cs.get("campaignId", "")
    branded_id = cs.get("brandedCampaignId", campaign_id)
    if not campaign_id:
        return
    try:
        pending, dialed = get_branded_queue_counts(branded_id) if branded_id else (0, 0)
    except Exception:
        pending, dialed = (0, 0)
    started_at = cs.get("startedAt", "")
    completed_at = cs.get("completedAt", _now_iso())
    try:
        duration = int(
            (datetime.fromisoformat(completed_at.replace("Z", "+00:00")) -
             datetime.fromisoformat(started_at.replace("Z", "+00:00"))).total_seconds()
        ) if started_at else 0
    except Exception:
        duration = 0
    exit_reason = cs.get("exitReason", "")
    if exit_reason == "queue_drained":
        final_status = "COMPLETED"
    elif exit_reason in ("aborted", "manually_stopped", "poll_failure", REASON_EXPIRED):
        final_status = "ABORTED"
    else:
        final_status = "ERROR"
    try:
        _get_ddb_client().update_item(
            TableName=_BRANDED_RUN_SUMMARY_TABLE,
            Key={
                "planId": {"S": plan_id},
                "sk":     {"S": f"{run_id}#{campaign_id}"},
            },
            UpdateExpression=(
                "SET #st = :s, totalSeeded = :ts, totalDialed = :td, "
                "exitReason = :er, completedAt = :ca, durationSeconds = :ds, "
                "runId = if_not_exists(runId, :rid), campaignId = if_not_exists(campaignId, :cid), "
                "brandedCampaignId = if_not_exists(brandedCampaignId, :bid), "
                "startedAt = if_not_exists(startedAt, :sa)"
            ),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":s":   {"S": final_status},
                ":ts":  {"N": str(pending + dialed)},
                ":td":  {"N": str(dialed)},
                ":er":  {"S": exit_reason},
                ":ca":  {"S": completed_at},
                ":ds":  {"N": str(duration)},
                ":rid": {"S": run_id},
                ":cid": {"S": campaign_id},
                ":bid": {"S": branded_id},
                ":sa":  {"S": started_at},
            },
        )
    except Exception as exc:
        logger.error(
            "_write_branded_run_summary: failed for %s/%s campaign %s: %s",
            plan_id, run_id, campaign_id, type(exc).__name__,
        )


def _write_branded_run_start(
    plan_id: str, run: dict, cs: dict, cfg: dict, seg_name: str, seg_arn: str, seeded: int
) -> None:
    """Write a START record to VipBrandedRunSummary when a branded campaign begins.

    Captures all settings for audit trail. The CP segment is deleted later by
    _stop_branded_campaign, so this is the only window to snapshot its identity.
    PHI-safe: only last 4 digits of source phone are stored.
    Must NEVER raise — callers must not be blocked by an audit write failure.
    """
    if not _BRANDED_RUN_SUMMARY_TABLE:
        return
    campaign_id = cs.get("campaignId", "")
    if not campaign_id:
        return
    branded_id = cs.get("brandedCampaignId", campaign_id)
    run_id = run.get("runId", "")
    source_phone = cfg.get("sourcePhone") or cfg.get("sourcePhoneNumber", "")
    source_phone_last4 = source_phone[-4:] if len(source_phone) >= 4 else source_phone

    # Snapshot non-PHI campaign config as segment definition
    _PHI_FIELDS = {"sourcePhone", "sourcePhoneNumber"}
    segment_def = {k: v for k, v in cfg.items() if k not in _PHI_FIELDS}
    segment_def["_segmentName"] = seg_name
    segment_def["_segmentArn"] = seg_arn or ""

    plan_snapshot = run.get("planSnapshot", {})
    try:
        _get_ddb_client().put_item(
            TableName=_BRANDED_RUN_SUMMARY_TABLE,
            Item={
                "planId":               {"S": plan_id},
                "sk":                   {"S": f"{run_id}#{campaign_id}"},
                "runId":                {"S": run_id},
                "campaignId":           {"S": campaign_id},
                "brandedCampaignId":    {"S": branded_id},
                "planName":             {"S": plan_snapshot.get("name", "")},
                "segmentArn":           {"S": seg_arn or ""},
                "segmentName":          {"S": seg_name or ""},
                "segmentDefinitionJson": {"S": json.dumps(segment_def, default=str)},
                "segmentSize":          {"N": str(seeded)},
                "contactFlowId":        {"S": cfg.get("contactFlowId", "")},
                "queueArn":             {"S": cfg.get("queueArn", "")},
                "sourcePhoneLast4":     {"S": source_phone_last4},
                "bucketIndex":          {"N": str(cs.get("bucketIndex", 0))},
                "priority":             {"N": str(cs.get("priority", 0))},
                "status":               {"S": "RUNNING"},
                "startedAt":            {"S": cs.get("startedAt", _now_iso())},
            },
            ConditionExpression="attribute_not_exists(sk)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            logger.error(
                "_write_branded_run_start: failed for %s/%s campaign %s: %s",
                plan_id, run_id, campaign_id, type(exc).__name__,
            )
    except Exception as exc:
        logger.error(
            "_write_branded_run_start: failed for %s/%s campaign %s: %s",
            plan_id, run_id, campaign_id, type(exc).__name__,
        )


def _invoke_seeder(
    campaign_id: str, segment_name: str, contact_flow_id: str, source_phone: str
) -> int:
    """Invoke the progressive dialer seeder Lambda directly.

    Returns number of contacts seeded. PHI rule: source_phone must never appear
    in log lines — only counts and exception type names are logged.
    Raises RuntimeError if the Lambda invocation itself succeeded (HTTP 200) but
    the handler threw — boto3 does NOT raise in that case; callers must check
    FunctionError explicitly.
    """
    import json as _json

    payload = {
        "campaignId": campaign_id,
        "segmentName": segment_name,
        "contactFlowId": contact_flow_id,
        "sourcePhone": source_phone,
    }
    response = _get_lambda_client().invoke(
        FunctionName=_PROGRESSIVE_DIALER_SEEDER_ARN,
        InvocationType="RequestResponse",
        Payload=_json.dumps(payload).encode(),
    )
    if response.get("FunctionError"):
        # Do NOT log the payload — stack frames may contain PHI.
        raise RuntimeError(f"seeder invocation failed: {response['FunctionError']}")
    result = _json.loads(response["Payload"].read())
    return int(result.get("seeded", 0))


_CONNECT_TERMINAL: Final[dict[str, str]] = {
    "Completed": REASON_COMPLETED,
    "Stopped": REASON_STOPPED,
    "Failed": REASON_ERROR,
    "Deleted": "connect_deleted",
}

_CAMPAIGN_TERMINAL_STATUSES: Final = frozenset(
    {"completed", "cancelled", "error", "expired"}
)

_CAMPAIGN_CANCEL_STATUSES: Final = frozenset({"cancelled", "error", "expired"})


# ── Public API ────────────────────────────────────────────────────────────────


def start_run(
    plan_id: str, triggered_by: str = "manual", start_bucket_index: int | None = None
) -> dict:
    plan = get_plan(plan_id)
    if not plan:
        raise ValueError(f"Plan {plan_id} not found")
    if plan.get("isTemplate") or plan.get("is_template"):
        raise ValueError(f"Plan {plan_id} is a template and cannot be run directly")
    if not plan.get("buckets"):
        raise ValueError("Plan has no buckets")

    first_bucket = start_bucket_index or 0

    # Optimistic check before the atomic lock (avoids unnecessary lock contention)
    latest = get_latest_run(plan_id)
    if latest and latest.get("status") == "running":
        raise ValueError(
            f"Plan {plan_id} already has an active run ({latest['runId']})"
        )

    # Generate run_id locally so we can lock BEFORE creating the DynamoDB run record.
    # If lock fails, no orphan run is created in the table.
    _run_id = f"{int(time.time() * 1000)}-{str(uuid.uuid4())[:8]}"

    # Atomic lock — rejects concurrent triggers that slipped past the optimistic check
    lock_plan_run(
        plan_id, _run_id
    )  # raises ValueError if already locked — no orphan created

    run = create_run(plan_id, plan, triggered_by=triggered_by, run_id=_run_id)

    # Mark skipped buckets so run history is accurate
    for i in range(first_bucket):
        bs = run["bucketStates"][i]
        bs["status"] = "cancelled"
        bs["exitReason"] = "skipped"
        for cs in bs.get("campaignStates", []):
            cs["status"] = "cancelled"
            cs["exitReason"] = "skipped"

    # Consume pre-warmed campaigns stored by _prestart_plan (cross-plan warmup)
    pending = plan.get("pendingWarmup")
    # Staleness guard: discard pendingWarmup older than 2 hours. This handles plans that
    # were pre-warmed on a non-working day (e.g. Sunday pre-warm for a MON-SAT plan) or
    # any other scenario where the scheduled run was skipped after pre-warming.
    # Connect campaign schedules embed the creation date, so a day-old warmup would
    # have an already-expired startTime/endTime and complete instantly with 0 dials.
    _WARMUP_MAX_AGE_SECONDS = 7200  # 2 hours
    if pending:
        created_at = pending.get("createdAt")
        if created_at:
            try:
                age = (_now_utc() - datetime.fromisoformat(created_at)).total_seconds()
                if age > _WARMUP_MAX_AGE_SECONDS:
                    _slog.warn(
                        "start_run_warmup_stale_discarded",
                        plan_id=plan_id,
                        warmup_age_hours=round(age / 3600, 1),
                        created_at=created_at,
                    )
                    update_plan_pending_warmup(plan_id, None)
                    pending = None
            except Exception:
                pass  # malformed createdAt — ignore and use the warmup as-is
    if pending and first_bucket == 0:
        for cs in run["bucketStates"][0]["campaignStates"]:
            match = next(
                (
                    p
                    for p in pending.get("campaigns", [])
                    if p.get("campaignId") == cs.get("campaignId")
                ),
                None,
            )
            if match and match.get("connectCampaignId"):
                cs["connectCampaignId"] = match["connectCampaignId"]
                cs["segmentArn"] = match.get("segmentArn")
                cs["segmentName"] = match.get("segmentName")
                cs["leadCount"] = match.get("leadCount")
                cs["warmupStarted"] = match.get("warmupStarted", False)
                cs["status"] = "warming"
        save_run(run)
        update_plan_pending_warmup(plan_id, None)  # clear so next run doesn't re-use
        _activate_warming_bucket(run, plan, 0)
    else:
        _start_bucket(run, first_bucket)

    return run


def start_run_chained(upstream_plan_id: str) -> None:
    """Fire plans whose trigger is on_plan_complete for upstream_plan_id (whole-plan variant).

    Skips plans that have afterBucket set — those are fired per-bucket in _fire_bucket_chains.
    Respects repeat=False: after firing, the trigger is reset to manual.
    """
    chained = find_plans_by_trigger_planid(upstream_plan_id)
    for plan in chained:
        trigger = plan.get("trigger", {})
        if (
            trigger.get("afterBucket") is not None
            or trigger.get("afterCampaign") is not None
        ):
            continue  # handled by _fire_bucket_chains / _fire_campaign_chains
        if not _within_working_hours(plan):
            logger.info(
                "start_run_chained: plan %s outside working hours, skipping chain",
                plan["planId"],
            )
            if plan.get("pendingWarmup"):
                update_plan_pending_warmup(plan["planId"], None)
                _slog.info("start_run_chained_warmup_cleared", plan_id=plan["planId"])
            continue
        try:
            start_run(plan["planId"], triggered_by="chained")
            if not trigger.get("repeat", True):
                update_plan_trigger(plan["planId"], {"type": "manual"})
                logger.info(
                    "start_run_chained: repeat=False, trigger reset to manual for plan %s",
                    plan["planId"],
                )
        except Exception as exc:
            logger.error(
                "start_run_chained: failed to start plan %s: %s", plan["planId"], exc
            )


def _fire_bucket_chains(upstream_plan_id: str, completed_bucket_index: int) -> None:
    """Fire plans whose trigger is on_plan_complete with afterBucket == completed_bucket_index."""
    chained = find_plans_by_trigger_planid(upstream_plan_id)
    for plan in chained:
        if plan.get("isTemplate") or plan.get("is_template"):
            continue
        if not _within_working_hours(plan):
            logger.info(
                "_fire_bucket_chains: plan %s outside working hours, skipping",
                plan["planId"],
            )
            if plan.get("pendingWarmup"):
                update_plan_pending_warmup(plan["planId"], None)
                _slog.info("fire_bucket_chains_warmup_cleared", plan_id=plan["planId"])
            continue
        trigger = plan.get("trigger", {})
        raw_ab = trigger.get("afterBucket")
        if raw_ab is None or int(raw_ab) != completed_bucket_index:
            continue
        if trigger.get("afterCampaign") is not None:
            continue  # handled by _fire_campaign_chains
        try:
            start_run(plan["planId"], triggered_by="chained")
            if not trigger.get("repeat", True):
                update_plan_trigger(plan["planId"], {"type": "manual"})
        except Exception as exc:
            logger.error(
                "_fire_bucket_chains: failed to start plan %s: %s", plan["planId"], exc
            )


def scheduled_run(plan_id: str) -> dict:
    latest = get_latest_run(plan_id)
    if latest and latest.get("status") == "running":
        _slog.info(
            "scheduled_run_already_running", plan_id=plan_id, run_id=latest["runId"]
        )
        return {"ok": True, "reason": "already_running"}
    plan = get_plan(plan_id)
    if not plan:
        _slog.error("scheduled_run_plan_not_found", plan_id=plan_id)
        return {"ok": False, "reason": "plan_not_found"}
    if plan.get("isTemplate") or plan.get("is_template"):
        _slog.info("scheduled_run_skipped_template", plan_id=plan_id)
        return {"ok": True, "reason": "is_template"}
    if not _within_working_hours(plan):
        _slog.info("scheduled_run_outside_hours", plan_id=plan_id)
        return {"ok": True, "reason": "outside_working_hours"}
    run = start_run(plan_id, triggered_by="scheduled")
    _slog.info("scheduled_run_started", plan_id=plan_id, run_id=run["runId"])
    return {"ok": True, "runId": run["runId"]}


def tick(plan_id: str, run_id: str, bucket_index: int) -> dict:
    run = get_run(plan_id, run_id)
    if not run:
        logger.error("tick: run %s/%s not found", plan_id, run_id)
        return {"ok": False, "reason": "run_not_found"}

    if run["status"] != "running":
        logger.info(
            "tick: run %s/%s terminal (status=%s)", plan_id, run_id, run["status"]
        )
        _delete_bucket_schedule_safe(run, bucket_index)
        return {"ok": True, "reason": "already_terminal"}

    bucket_state_check = (
        run["bucketStates"][bucket_index]
        if bucket_index < len(run["bucketStates"])
        else None
    )
    if not bucket_state_check or bucket_state_check["status"] not in (
        "running",
        "warming",
    ):
        logger.info(
            "tick: bucket %d not active (status=%s), skipping",
            bucket_index,
            bucket_state_check["status"] if bucket_state_check else "missing",
        )
        _delete_bucket_schedule_safe(run, bucket_index)
        return {"ok": True, "reason": "stale_tick"}

    plan = run.get("planSnapshot") or get_plan(plan_id)
    if not plan:
        logger.error("tick: plan snapshot missing for run %s", run_id)
        return {"ok": False, "reason": "no_plan"}

    # ── 0. End-time cutoffs: working hours then loop, then daily fallback ────────
    _now_hhmm = _now_cot_hhmm()

    _wh = plan.get("workingHours") or {}
    _wh_end = _wh.get("endTime")
    if _wh_end:
        _end_h, _end_m = (int(x) for x in _wh_end.split(":"))
        if _now_hhmm >= _end_h * 60 + _end_m:
            logger.info(
                "tick: working hours end-time %s COT reached, force-finishing run %s",
                _wh_end,
                run_id,
            )
            _record_plan_event(run, "window_closed", {"reason": "working_hours_cutoff"})
            _force_finish_internal(run, plan)
            return {"ok": True, "reason": "working_hours_cutoff"}

    _loop_cfg = plan.get("loop") or {}
    _loop_end = _loop_cfg.get("endTime")
    if _loop_end:
        _end_h, _end_m = (int(x) for x in _loop_end.split(":"))
        if _now_hhmm >= _end_h * 60 + _end_m:
            logger.info(
                "tick: loop end-time %s COT reached, force-finishing run %s",
                _loop_end,
                run_id,
            )
            _record_plan_event(run, "window_closed", {"reason": "loop_cutoff"})
            _force_finish_internal(run, plan)
            return {"ok": True, "reason": "loop_cutoff"}

    # Fallback hard-stop for non-looping plans stuck past midnight
    if _past_daily_cutoff(_now_utc()):
        logger.info("tick: past daily cutoff, force-finishing run %s", run_id)
        _record_plan_event(run, "window_closed", {"reason": "daily_cutoff"})
        _force_finish_internal(run, plan)
        return {"ok": True, "reason": "daily_cutoff"}

    bucket = plan["buckets"][bucket_index]
    bucket_state = run["bucketStates"][bucket_index]
    run_mode = bucket.get("run_mode") or bucket.get("type", "status_based")
    is_time_based = run_mode in ("time_based", "time-based")

    # ── 1. Poll running campaigns ─────────────────────────────────────────────
    # Snapshot completed campaign IDs before polling (to detect newly-completed)
    prev_completed = {
        cs["campaignId"]
        for cs in bucket_state["campaignStates"]
        if cs["status"] == "completed"
    }

    for cs in bucket_state["campaignStates"]:
        if cs["status"] == "running" and cs.get("connectCampaignId"):
            _poll_campaign_state(cs)
            if cs["status"] == "running":
                _campaign_def = next(
                    (
                        c
                        for c in bucket.get("campaigns", [])
                        if c["id"] == cs["campaignId"]
                    ),
                    {},
                )
                _dur = int(
                    _campaign_def.get("run_duration_minutes")
                    or _campaign_def.get("duration_minutes")
                    or 0
                )
                _cs_started = cs.get("startedAt") or bucket_state.get("startedAt")
                if _dur > 0 and _cs_started:
                    _elapsed = (
                        _now_utc() - datetime.fromisoformat(_cs_started)
                    ).total_seconds() / 60
                    # Pre-warm afterCampaign-triggered plans when this campaign is 5 min from ending
                    if _elapsed >= _dur - _PRESTART_MINUTES and not cs.get(
                        "afterCampaignPrewarmed"
                    ):
                        try:
                            _prestart_after_campaign(plan_id, cs["campaignId"])
                            cs["afterCampaignPrewarmed"] = True
                        except Exception as _exc:
                            logger.error(
                                "tick: _prestart_after_campaign %s failed: %s",
                                cs["campaignId"],
                                _exc,
                            )
                            _emit_prewarm_failure(plan_id)
                    # Force-stop if Connect hasn't transitioned after duration elapsed
                    if _elapsed > _dur + 2:
                        logger.warning(
                            "tick: campaign %s still Running after %.1f min (limit=%d) — force stopping",
                            cs["connectCampaignId"],
                            _elapsed,
                            _dur,
                        )
                        _safe_stop_campaign(cs["connectCampaignId"])

        elif cs.get("brandedCampaignId") and cs["status"] == "running":
            # Poll VipProgressiveCampaignQueue instead of Connect. Poll BEFORE
            # evaluating run_duration_minutes below (root-caused 2026-08-27,
            # adversarial code review) — checking elapsed time first could mark
            # a campaign whose queue already drained this same tick as
            # expired/ABORTED instead of completed, even though it genuinely
            # finished. The telephony branch above has the same ordering:
            # poll first, only evaluate duration if still running afterward.
            try:
                count = _count_branded_queue(cs["brandedCampaignId"])
            except Exception as _poll_exc:
                logger.warning(
                    "tick: branded queue poll failed for %s: %s",
                    cs["brandedCampaignId"], type(_poll_exc).__name__,
                )
                _emit_branded_metric("BrandedTickError")
                _branded_poll_failures[cs["brandedCampaignId"]] = (
                    _branded_poll_failures.get(cs["brandedCampaignId"], 0) + 1
                )
                if _branded_poll_failures[cs["brandedCampaignId"]] >= _BRANDED_POLL_FAILURE_LIMIT:
                    logger.error(
                        "tick: branded poll failed %d consecutive times for campaign %s "
                        "— transitioning to error",
                        _BRANDED_POLL_FAILURE_LIMIT,
                        cs["brandedCampaignId"],
                    )
                    cs["status"] = "error"
                    cs["exitReason"] = "poll_failure"
                    cs["completedAt"] = _now_iso()
                    _write_branded_run_summary(plan_id, run_id, cs)
                    _stop_branded_campaign(cs)
                    _branded_poll_failures.pop(cs["brandedCampaignId"], None)
                continue  # don't transition on poll error

            # Successful poll — reset failure counter
            _branded_poll_failures.pop(cs.get("brandedCampaignId"), None)

            if count == 0:
                logger.info(
                    "tick: branded campaign %s queue drained — completing",
                    cs["brandedCampaignId"],
                )
                cs["status"] = "completed"
                cs["exitReason"] = "queue_drained"
                cs["completedAt"] = _now_iso()
                _write_branded_run_summary(plan_id, run_id, cs)
                _stop_branded_campaign(cs)
                _emit_branded_metric("BrandedCampaignCompleted")
                continue

            # Branded campaign: same run_duration_minutes force-stop as the telephony
            # branch above — branded never gets a connectCampaignId, so without this
            # check it ran indefinitely (root-caused 2026-08-27: a 45-min branded
            # campaign ran 107 min until a human force-finished it, abandoning 994 of
            # 1085 seeded contacts as EXPIRED). Only reached when count > 0 (still
            # has pending work) — a naturally-drained campaign is handled above.
            _campaign_def = next(
                (c for c in bucket.get("campaigns", []) if c["id"] == cs["campaignId"]),
                {},
            )
            _dur = int(
                _campaign_def.get("run_duration_minutes")
                or _campaign_def.get("duration_minutes")
                or 0
            )
            _cs_started = cs.get("startedAt") or bucket_state.get("startedAt")
            if _dur > 0 and _cs_started:
                _elapsed = (
                    _now_utc() - datetime.fromisoformat(_cs_started)
                ).total_seconds() / 60
                if _elapsed > _dur + 2:
                    logger.warning(
                        "tick: branded campaign %s still running after %.1f min (limit=%d) — force stopping",
                        cs["brandedCampaignId"],
                        _elapsed,
                        _dur,
                    )
                    cs["status"] = "expired"
                    cs["exitReason"] = REASON_EXPIRED
                    cs["completedAt"] = _now_iso()
                    _write_branded_run_summary(plan_id, run_id, cs)
                    _stop_branded_campaign(cs)
                    _emit_branded_metric("BrandedCampaignExpired")

        elif cs.get("smsCampaignId") and cs["status"] == "running":
            # SMS campaign: poll VipSmsCampaignQueue PENDING count
            try:
                pending = _count_sms_queue(cs["smsCampaignId"])
            except Exception as _poll_exc:
                logger.warning(
                    "tick: SMS queue poll failed for %s: %s",
                    cs["smsCampaignId"], type(_poll_exc).__name__,
                )
                continue  # don't transition on poll error

            if pending == 0:
                logger.info(
                    "tick: SMS campaign %s queue drained — completing",
                    cs["smsCampaignId"],
                )
                cs["status"] = "completed"
                cs["exitReason"] = "queue_drained"
                cs["completedAt"] = _now_iso()
                _complete_sms_campaign(cs)

    # Fire plans triggered by a specific campaign completing
    newly_completed = {
        cs["campaignId"]
        for cs in bucket_state["campaignStates"]
        if cs["status"] == "completed"
    } - prev_completed
    if newly_completed:
        try:
            _fire_campaign_chains(plan_id, bucket_index, newly_completed)
        except Exception as exc:
            logger.error("tick: _fire_campaign_chains failed: %s", exc)

    # ── 2. Time-based: pre-start + expiry ────────────────────────────────────
    if is_time_based:
        duration_min = int(
            bucket.get("duration_minutes") or bucket.get("durationMinutes", 30)
        )
        started_iso = bucket_state.get("startedAt") or run.get("startedAt")
        elapsed_min = (
            _now_utc() - datetime.fromisoformat(started_iso)
        ).total_seconds() / 60

        # Pre-start within-plan: warm next bucket
        if (
            bucket.get("prestart_next", True)
            and elapsed_min >= duration_min - _PRESTART_MINUTES
            and not _next_bucket_warming(run, bucket_index)
        ):
            _prestart_next_bucket(run, plan, bucket_index)
            save_run(run)

        # Pre-start cross-plan: warm chained/looping plans' first bucket (last bucket only)
        if (
            elapsed_min >= duration_min - _PRESTART_MINUTES
            and bucket_index == len(plan["buckets"]) - 1
        ):
            try:
                _prestart_chained_runs(run, plan, bucket_index)
            except Exception as exc:
                logger.error("tick: _prestart_chained_runs failed: %s", exc)

        # Expiry
        if elapsed_min >= duration_min:
            logger.info(
                "tick: bucket %d expired (elapsed=%.1f/%.0f min)",
                bucket_index,
                elapsed_min,
                duration_min,
            )
            _expire_bucket(run, plan, bucket_index)
            return {"ok": True, "reason": "expired"}

    # ── 2b. Status-based last bucket: cross-plan pre-warm via campaign durations ─
    # Within-plan pre-warm for status_based intermediate buckets is intentionally
    # omitted here: the save_run after _prestart_next_bucket can lose a concurrent
    # write race (two parallel bucket ticks), leaving DynamoDB in "queued" state and
    # causing a new campaign to be created on every subsequent tick.
    # Cross-plan is safe because _prestart_plan has its own idempotent pendingWarmup guard.
    if not is_time_based and bucket_index == len(plan["buckets"]) - 1:
        _campaigns = bucket.get("campaigns", [])
        _effective_duration = max(
            (
                int(
                    c.get("run_duration_minutes")
                    or c.get("duration_minutes")
                    or c.get("durationMinutes")
                    or 0
                )
                for c in _campaigns
            ),
            default=0,
        )
        if _effective_duration > 0:
            _started_iso = bucket_state.get("startedAt") or run.get("startedAt")
            _elapsed_min = (
                _now_utc() - datetime.fromisoformat(_started_iso)
            ).total_seconds() / 60
            if _elapsed_min >= _effective_duration - _PRESTART_MINUTES:
                try:
                    _prestart_chained_runs(run, plan, bucket_index)
                except Exception as exc:
                    logger.error(
                        "tick: _prestart_chained_runs (status_based) failed: %s", exc
                    )

    # ── 3. Dispatch newly-unblocked campaigns (fixed-point until stable) ──────
    changed = True
    _stalled: set[int] = set()
    while changed:
        changed = _dispatch_ready_campaigns(run, plan, bucket_index, _stalled)

    # ── 3b. Eagerly start cross-bucket campaigns whose deps are now satisfied ─
    if _dispatch_cross_bucket_ready(run, plan, bucket_index):
        save_run(run)

    # ── 4. Advance if all campaigns terminal ──────────────────────────────────
    if _all_campaigns_terminal(run, bucket_index):
        _cs_list = run["bucketStates"][bucket_index]["campaignStates"]
        _n_completed = sum(1 for cs in _cs_list if cs["status"] == "completed")
        _n_deleted = sum(
            1 for cs in _cs_list if cs.get("exitReason") == "connect_deleted"
        )
        if _n_deleted > 0 and _n_completed == 0:
            # All campaigns were externally deleted from Connect before any completed.
            # Advancing would falsely trigger downstream chain plans — abort instead.
            logger.error(
                "tick[%s/%s]: bucket %d — %d campaign(s) externally deleted, 0 completed; "
                "aborting run to prevent false chain trigger",
                plan_id,
                run_id,
                bucket_index,
                _n_deleted,
            )
            run["status"] = "aborted"
            run["completedAt"] = _now_iso()
            run["abortReason"] = "external_campaign_deletion"
            save_run(run)
            unlock_plan_run(plan_id)
            _notify_sns(
                subject=f"[VIP Plans] Run ABORTED — campaigns deleted externally (plan={plan_id[:8]})",
                detail=(
                    f"Plan {plan_id} / Run {run_id}: bucket {bucket_index} had {_n_deleted} "
                    f"campaign(s) deleted from Connect before completing. "
                    f"Run was aborted to prevent a false chain trigger."
                ),
                attributes={
                    "alertType": "run_aborted",
                    "planId": plan_id,
                    "runId": run_id,
                },
            )
            return {"ok": False, "reason": "aborted_external_deletion"}
        _advance_bucket(run, plan, bucket_index, reason="all_campaigns_done")
        return {"ok": True, "reason": "advanced"}

    save_run(run)
    return {"ok": True}


def abort_run(plan_id: str, run_id: str) -> dict:
    """Abort a running run, stopping all active campaigns.

    Retries up to 3 times on ConcurrentWriteError. unlock_plan_run is called exactly
    once: on success, on the last ConcurrentWriteError retry, or on any other exception
    from save_run — matching the original finally semantics but without unlocking on
    validation errors or on intermediate retries.
    """
    _MAX_RETRIES = 3
    for attempt in range(_MAX_RETRIES):
        run = get_run(plan_id, run_id)
        if not run:
            raise ValueError(f"Run {run_id} not found in plan {plan_id}")
        if run["status"] != "running":
            if run["status"] == "aborted":
                return run
            raise ValueError(f"Run {run_id} is not running (status={run['status']})")

        now = _now_iso()

        # Stop/cancel ALL buckets — parallel buckets may have multiple active simultaneously
        for bi, bs in enumerate(run["bucketStates"]):
            if bs["status"] not in ("running", "warming", "queued"):
                continue
            for cs in bs["campaignStates"]:
                if cs["status"] == "running" and cs.get("connectCampaignId"):
                    _safe_stop_campaign(cs["connectCampaignId"])
                if cs["status"] == "warming" and cs.get("connectCampaignId"):
                    _safe_stop_campaign(cs["connectCampaignId"])
                    _safe_delete_campaign(cs["connectCampaignId"])
                    if cs.get("segmentName"):
                        _safe_delete_segment(cs["segmentName"])
                # ── Branded cleanup ──
                if cs["status"] in ("running", "creating") and cs.get("brandedCampaignId"):
                    cs["exitReason"] = REASON_ABORTED
                    cs["completedAt"] = now
                    _write_branded_run_summary(plan_id, run_id, cs)
                    _stop_branded_campaign(cs)
                # ── SMS cleanup ──────
                elif cs["status"] == "running" and cs.get("smsCampaignId"):
                    cs["exitReason"] = REASON_ABORTED
                    cs["completedAt"] = now
                    _stop_sms_campaign(cs)
                # ────────────────────
                if cs["status"] in ("running", "warming", "queued", "creating"):
                    cs["status"] = "cancelled"
                    cs["exitReason"] = REASON_ABORTED
                    cs["completedAt"] = now
            _delete_bucket_schedule_safe(run, bi)
            bs["status"] = "completed"
            bs["completedAt"] = now

        run["status"] = "aborted"
        run["completedAt"] = now
        try:
            save_run(run)
            update_plan_pending_warmup(run["planId"], None)
        except ConcurrentWriteError:
            if attempt == _MAX_RETRIES - 1:
                unlock_plan_run(plan_id)
                raise
            logger.info(
                "abort_run: concurrent write on attempt %d — retrying", attempt + 1
            )
            continue
        except Exception:
            unlock_plan_run(plan_id)
            raise
        unlock_plan_run(plan_id)
        return run

    raise ConcurrentWriteError(
        f"abort_run exhausted {_MAX_RETRIES} retries"
    )  # unreachable


def _force_finish_internal(run: dict, plan: dict) -> None:
    """Stop all active campaigns and mark run completed. Shared by force_finish_run and daily cutoff."""
    now = _now_iso()
    for bi, bs in enumerate(run["bucketStates"]):
        if bs["status"] not in ("running", "warming", "queued"):
            continue
        for cs in bs["campaignStates"]:
            if cs["status"] == "running" and cs.get("connectCampaignId"):
                _safe_stop_campaign(cs["connectCampaignId"])
            if cs["status"] == "warming" and cs.get("connectCampaignId"):
                _safe_stop_campaign(cs["connectCampaignId"])
                _safe_delete_campaign(cs["connectCampaignId"])
                if cs.get("segmentName"):
                    _safe_delete_segment(cs["segmentName"])
            # ── Branded cleanup ──
            if cs["status"] in ("running", "creating") and cs.get("brandedCampaignId"):
                _stop_branded_campaign(cs)
            # ── SMS cleanup ──────
            elif cs["status"] == "running" and cs.get("smsCampaignId"):
                _stop_sms_campaign(cs)
            # ────────────────────
            if cs["status"] in ("running", "warming", "queued", "creating"):
                cs["status"] = "completed"
                cs["exitReason"] = "force_finished"
                cs["completedAt"] = now
                if cs.get("brandedCampaignId"):
                    _write_branded_run_summary(run["planId"], run["runId"], cs)
        _delete_bucket_schedule_safe(run, bi)
        if bucket_def := (plan.get("buckets") or [])[bi : bi + 1]:
            if bucket_def[0].get("cleanup", bucket_def[0].get("deleteAfter", True)):
                for cs in bs["campaignStates"]:
                    if cs.get("connectCampaignId"):
                        _safe_delete_campaign(cs["connectCampaignId"])
                    if cs.get("segmentName"):
                        _safe_delete_segment(cs["segmentName"])
        bs["status"] = "completed"
        bs["completedAt"] = now

    run["status"] = "completed"
    run["completedAt"] = now
    try:
        save_run(run)
        update_plan_pending_warmup(run["planId"], None)
    finally:
        unlock_plan_run(run["planId"])
    # Deliberately NOT calling _maybe_loop or start_run_chained:
    # forced completion (daily cutoff or operator action) means stop, not restart.


def force_finish_run(plan_id: str, run_id: str) -> dict:
    """Operator-initiated force-finish: stops campaigns and marks run completed.

    Retries up to 3 times on ConcurrentWriteError. _force_finish_internal unlocks the
    plan in its own finally, so unlock is guaranteed on each attempt.
    """
    _MAX_RETRIES = 3
    for attempt in range(_MAX_RETRIES):
        run = get_run(plan_id, run_id)
        if not run:
            raise ValueError(f"Run {run_id} not found in plan {plan_id}")
        if run["status"] != "running":
            if run["status"] in ("completed", "aborted"):
                return run
            raise ValueError(f"Run {run_id} is not running (status={run['status']})")
        plan = run.get("planSnapshot") or get_plan(plan_id) or {}
        try:
            _force_finish_internal(run, plan)
            return run
        except ConcurrentWriteError:
            if attempt == _MAX_RETRIES - 1:
                raise
            logger.info(
                "force_finish_run: concurrent write on attempt %d — retrying",
                attempt + 1,
            )

    raise ConcurrentWriteError(
        f"force_finish_run exhausted {_MAX_RETRIES} retries"
    )  # unreachable


def force_start_bucket(plan_id: str, run_id: str, bucket_index: int) -> dict:
    """Manually start a queued/warming bucket, bypassing dependency and timing checks."""
    run = get_run(plan_id, run_id)
    if not run:
        raise ValueError(f"Run {run_id} not found")
    if run["status"] != "running":
        raise ValueError(f"Run {run_id} is not running")
    if bucket_index >= len(run["bucketStates"]):
        raise ValueError(f"Bucket index {bucket_index} out of range")
    bs = run["bucketStates"][bucket_index]
    if bs["status"] not in ("queued", "warming"):
        raise ValueError(
            f"Bucket {bucket_index} is {bs['status']} — cannot force-start"
        )

    # Reset warming campaigns to queued so _start_bucket creates them fresh
    if bs["status"] == "warming":
        for cs in bs["campaignStates"]:
            if cs["status"] == "warming" and cs.get("connectCampaignId"):
                _safe_stop_campaign(cs["connectCampaignId"])
                _safe_delete_campaign(cs["connectCampaignId"])
            if cs["status"] in ("warming", "queued"):
                cs.update(
                    {
                        "status": "queued",
                        "connectCampaignId": None,
                        "segmentName": None,
                        "segmentArn": None,
                    }
                )
        bs["status"] = "queued"

    _start_bucket(run, bucket_index)
    return run


def force_stop_bucket(plan_id: str, run_id: str, bucket_index: int) -> dict:
    """Manually stop a running bucket and advance to the next one (or complete the run)."""
    run = get_run(plan_id, run_id)
    if not run:
        raise ValueError(f"Run {run_id} not found")
    if run["status"] != "running":
        raise ValueError(f"Run {run_id} is not running")
    if bucket_index >= len(run["bucketStates"]):
        raise ValueError(f"Bucket index {bucket_index} out of range")
    bs = run["bucketStates"][bucket_index]
    if bs["status"] not in ("running", "warming"):
        raise ValueError(f"Bucket {bucket_index} is not active (status={bs['status']})")
    plan = run.get("planSnapshot") or get_plan(plan_id) or {}
    _expire_bucket(run, plan, bucket_index)
    return run


def force_start_campaign(
    plan_id: str, run_id: str, bucket_index: int, campaign_index: int
) -> dict:
    """Manually start a single queued, cancelled, or error campaign, bypassing dependency checks.

    When the campaign is parent_cancelled and its bucket already completed (siblings all done),
    the bucket is reactivated and cascade-cancelled descendants are reset to queued so the
    dispatcher can resume the chain automatically after this campaign completes.
    """
    run = get_run(plan_id, run_id)
    if not run:
        raise ValueError(f"Run {run_id} not found")
    if run["status"] != "running":
        raise ValueError(f"Run {run_id} is not running")
    if bucket_index >= len(run["bucketStates"]):
        raise ValueError(f"Bucket index {bucket_index} out of range")
    bs = run["bucketStates"][bucket_index]
    if campaign_index >= len(bs["campaignStates"]):
        raise ValueError(f"Campaign index {campaign_index} out of range")
    cs = bs["campaignStates"][campaign_index]
    if cs["status"] not in ("queued", "cancelled", "error"):
        raise ValueError(
            f"Campaign is {cs['status']} — can only force-start queued, cancelled, or error campaigns"
        )

    # Tracks a schedule created by THIS call so a lost Phase-1 save race (below) can
    # roll it back — otherwise its name is never persisted and it orphans forever,
    # same failure shape as the _schedule_tick atomicity bug (BD-013 follow-up).
    _new_schedule_name: str | None = None

    # Completed bucket: allowed for cancelled or queued campaigns.
    # "queued" in a completed bucket is an inconsistent state that occurs when a
    # cascade-cancel save_run lost a ConcurrentWriteError race — the campaign stayed
    # "queued" in DDB while the bucket advanced. Treat it identically to "cancelled".
    if bs["status"] == "completed":
        if cs["status"] not in ("cancelled", "queued"):
            raise ValueError(
                f"Bucket {bucket_index} is already completed — "
                f"force-start only allowed for cancelled or queued campaigns in a completed bucket"
            )
        if cs["status"] == "queued":
            logger.warning(
                "force_start_campaign: campaign %d/%d is 'queued' in completed bucket %d "
                "— inconsistent state, proceeding with force-start",
                bucket_index,
                campaign_index,
                bucket_index,
            )
        # Reactivate bucket so tick will poll the restarted campaign.
        # Reset startedAt so elapsed_min starts fresh — time-based buckets would otherwise
        # expire immediately if startedAt is hours in the past.
        bs["status"] = "running"
        bs["completedAt"] = None
        bs["startedAt"] = _now_iso()
        try:
            sched = _schedule_tick(
                plan_id=plan_id, run_id=run_id, bucket_index=bucket_index
            )
            bs["scheduleName"] = sched
            _new_schedule_name = sched
        except Exception as exc:
            logger.error(
                "force_start_campaign: failed to schedule tick for reactivated bucket %d: %s",
                bucket_index,
                exc,
            )
    elif bs["status"] not in ("running", "warming", "queued"):
        raise ValueError(
            f"Bucket {bucket_index} is '{bs['status']}' — "
            f"force-start requires an active or completed bucket"
        )

    plan = run.get("planSnapshot") or get_plan(plan_id) or {}

    # Ensure bucket is active so the campaign can run
    if bs["status"] in ("queued", "warming"):
        now_iso = _now_iso()
        if bs["status"] == "warming":
            # Clean up any warming campaigns in this bucket
            for other_cs in bs["campaignStates"]:
                if other_cs["status"] == "warming" and other_cs.get(
                    "connectCampaignId"
                ):
                    _safe_stop_campaign(other_cs["connectCampaignId"])
                    _safe_delete_campaign(other_cs["connectCampaignId"])
                if other_cs["status"] in ("warming", "queued"):
                    other_cs.update(
                        {
                            "status": "queued",
                            "connectCampaignId": None,
                            "segmentName": None,
                            "segmentArn": None,
                            # See BD-020 — reconcileRetries is sticky and must
                            # not survive an unrelated queued revert (this
                            # sibling-cleanup path was missed by BD-020;
                            # root-caused 2026-08-27, second adversarial
                            # review round).
                            "reconcileRetries": 0,
                        }
                    )
        bs["status"] = "running"
        bs["startedAt"] = now_iso
        try:
            sched = _schedule_tick(
                plan_id=plan_id, run_id=run_id, bucket_index=bucket_index
            )
            bs["scheduleName"] = sched
            _new_schedule_name = sched
        except Exception as exc:
            logger.error(
                "force_start_campaign: failed to schedule tick for bucket %d: %s",
                bucket_index,
                exc,
            )

    # Reset cascade-cancelled descendants so they can auto-start after this campaign completes
    _reset_cascade_cancelled_children(run, plan, cs["campaignId"])

    # Phase 1: claim. Clear connectCampaignId in this save so that if Phase 3 creates a Connect
    # campaign but the final save_run crashes, tick recovery sees null and safely resets to queued
    # (no stale ID pointing at a deleted campaign, which would cause a spurious error state).
    old_connect_id = cs.get("connectCampaignId")
    old_branded_id = cs.get("brandedCampaignId")
    old_queue_arn = cs.get("queueArn")
    cs["status"] = "creating"
    cs["creatingAt"] = _now_iso()
    cs["exitReason"] = None
    cs["errorDetail"] = None
    cs["completedAt"] = None
    cs["startedAt"] = None
    cs["connectCampaignId"] = None
    cs["segmentArn"] = None
    cs["segmentName"] = None
    cs["brandedCampaignId"] = None
    cs["queueArn"] = None
    cs["reconcileRetries"] = 0
    try:
        save_run(run)
    except ConcurrentWriteError:
        # Another tick already updated the run first. The claim never persisted, so
        # any schedule created above for this call would never have its name recorded
        # anywhere — roll it back before propagating, or it orphans forever.
        if _new_schedule_name:
            logger.warning(
                "force_start_campaign: rolling back schedule %s after lost Phase-1 "
                "save race for bucket %d",
                _new_schedule_name,
                bucket_index,
            )
            _delete_schedule_safe(_new_schedule_name)
        raise

    # Phase 2: clean up stale Connect campaign AFTER claim is persisted.
    if old_connect_id:
        _safe_stop_campaign(old_connect_id)
        _safe_delete_campaign(old_connect_id)
    if old_branded_id:
        _stop_branded_campaign({"brandedCampaignId": old_branded_id, "queueArn": old_queue_arn})

    # Phase 3: create new Connect campaign
    cs["status"] = "queued"  # _start_one_campaign expects "queued"
    _start_one_campaign(run, plan, bucket_index, campaign_index)

    # Snapshot the outcome _start_one_campaign wrote so we can re-apply it if the
    # final save races with a concurrent tick and we must re-read run from DDB.
    _snap = {
        k: cs.get(k)
        for k in (
            "status",
            "connectCampaignId",
            "segmentName",
            "segmentArn",
            "startedAt",
            "exitReason",
            "errorDetail",
            "completedAt",
            "reconcileRetries",
            "brandedCampaignId",
            "queueArn",
        )
    }

    _FINAL_SAVE_RETRIES = 3
    for _attempt in range(_FINAL_SAVE_RETRIES):
        try:
            save_run(run)
            return run
        except ConcurrentWriteError:
            if _attempt == _FINAL_SAVE_RETRIES - 1:
                raise
            logger.info(
                "force_start_campaign: concurrent write on final save attempt %d — retrying",
                _attempt + 1,
            )
            run = get_run(plan_id, run_id)
            if not run:
                raise ValueError(
                    f"Run {run_id} not found after force_start_campaign retry"
                )
            bs = run["bucketStates"][bucket_index]
            cs = bs["campaignStates"][campaign_index]
            if (
                cs["status"] == "running"
                and cs.get("connectCampaignId") == _snap["connectCampaignId"]
            ):
                return run  # concurrent tick already adopted this Connect campaign
            cs.update(_snap)
            if _snap["status"] != "creating":
                cs.pop("creatingAt", None)

    raise ConcurrentWriteError(
        "force_start_campaign exhausted retries on final save"
    )  # unreachable


def _reset_cascade_cancelled_children(run: dict, plan: dict, campaign_id: str) -> None:
    """Reset campaigns that were cascade-cancelled because campaign_id was cancelled, recursively.

    After a force-start, children that were parent_cancelled due to this campaign being
    cancelled are reset to queued. The tick's _dispatch_ready_campaigns will then start
    them automatically once this campaign completes.
    """
    for bucket in plan.get("buckets", []):
        for campaign in bucket.get("campaigns", []):
            if campaign_id not in campaign.get("dependsOn", []):
                continue
            cs = _find_campaign_state(run, campaign["id"])
            if (
                cs
                and cs["status"] == "cancelled"
                and cs.get("exitReason") == REASON_PARENT_CANCELLED
            ):
                cs["status"] = "queued"
                cs["exitReason"] = None
                cs["completedAt"] = None
                # reconcileRetries is a sticky signal meaning "mid empty-segment
                # retry" (see _bucket_has_only_legitimate_waits) — clear it here
                # since this queued state has nothing to do with that retry cycle
                # (root-caused 2026-08-27, adversarial code review: a stale value
                # left over from before cascade-cancel could mask a genuinely
                # stuck campaign as "legitimately waiting").
                cs["reconcileRetries"] = 0
                _reset_cascade_cancelled_children(run, plan, campaign["id"])


def skip_campaign(
    plan_id: str, run_id: str, bucket_index: int, campaign_index: int
) -> dict:
    """Mark a campaign as skipped — transparent to cascade-cancel, does not block children.

    Unlike force_stop_campaign (exitReason=stopped/manually_stopped), skip sets
    exitReason='skipped' so downstream dependsOn campaigns are NOT cascade-cancelled.
    If the campaign is actively running in Connect it is stopped first.
    Only works on non-terminal campaigns (queued, running, warming).

    Retries up to 3 times on ConcurrentWriteError (tick racing with this HTTP call).
    On retry the run is re-read; if the tick already advanced the campaign to a terminal
    state the skip is treated as a no-op success rather than raising ValueError.
    """
    _MAX_RETRIES = 3
    for attempt in range(_MAX_RETRIES):
        run = get_run(plan_id, run_id)
        if not run:
            raise ValueError(f"Run {run_id} not found")
        if run["status"] != "running":
            raise ValueError(f"Run {run_id} is not running")
        if bucket_index >= len(run["bucketStates"]):
            raise ValueError(f"Bucket index {bucket_index} out of range")
        bs = run["bucketStates"][bucket_index]
        if campaign_index >= len(bs["campaignStates"]):
            raise ValueError(f"Campaign index {campaign_index} out of range")
        cs = bs["campaignStates"][campaign_index]

        if cs["status"] in _CAMPAIGN_TERMINAL_STATUSES:
            if attempt > 0:
                # A concurrent tick advanced this campaign between attempts — treat as success.
                return run
            raise ValueError(
                f"Campaign is already terminal ({cs['status']}) — cannot skip. "
                "Use force-start to restart it instead."
            )

        if cs.get("connectCampaignId") and cs["status"] == "running":
            _safe_stop_campaign(cs["connectCampaignId"])
        if cs.get("brandedCampaignId") and cs["status"] == "running":
            _stop_branded_campaign(cs)
        elif cs.get("smsCampaignId") and cs["status"] == "running":
            _stop_sms_campaign(cs)

        cs["status"] = "cancelled"
        cs["exitReason"] = "skipped"
        cs["completedAt"] = _now_iso()

        plan = run.get("planSnapshot") or get_plan(plan_id) or {}
        try:
            changed = True
            _stalled: set[int] = set()
            while changed:
                changed = _dispatch_ready_campaigns(run, plan, bucket_index, _stalled)

            if _all_campaigns_terminal(run, bucket_index):
                _advance_bucket(run, plan, bucket_index, reason="all_campaigns_done")
            else:
                save_run(run)
            return run
        except ConcurrentWriteError:
            if attempt == _MAX_RETRIES - 1:
                raise
            logger.info(
                "skip_campaign: concurrent write on attempt %d — retrying", attempt + 1
            )

    raise ConcurrentWriteError(
        f"skip_campaign exhausted {_MAX_RETRIES} retries"
    )  # unreachable


def force_stop_campaign(
    plan_id: str, run_id: str, bucket_index: int, campaign_index: int
) -> dict:
    """Manually stop a running campaign and mark it expired.

    Retries up to 3 times on ConcurrentWriteError (tick racing with this HTTP call).
    On retry the run is re-read; if the tick already stopped the campaign the stop is
    treated as a no-op success.
    """
    _MAX_RETRIES = 3
    for attempt in range(_MAX_RETRIES):
        run = get_run(plan_id, run_id)
        if not run:
            raise ValueError(f"Run {run_id} not found")
        if run["status"] != "running":
            raise ValueError(f"Run {run_id} is not running")
        if bucket_index >= len(run["bucketStates"]):
            raise ValueError(f"Bucket index {bucket_index} out of range")
        bs = run["bucketStates"][bucket_index]
        if campaign_index >= len(bs["campaignStates"]):
            raise ValueError(f"Campaign index {campaign_index} out of range")
        cs = bs["campaignStates"][campaign_index]
        if cs["status"] != "running":
            if attempt > 0 and cs["status"] in _CAMPAIGN_TERMINAL_STATUSES:
                return run
            raise ValueError(
                f"Campaign is {cs['status']} — can only stop running campaigns"
            )

        if cs.get("connectCampaignId"):
            _safe_stop_campaign(cs["connectCampaignId"])
        if cs.get("brandedCampaignId"):
            cs["exitReason"] = "manually_stopped"
            cs["completedAt"] = _now_iso()
            _write_branded_run_summary(plan_id, run_id, cs)
            _stop_branded_campaign(cs)
        elif cs.get("smsCampaignId"):
            cs["exitReason"] = "manually_stopped"
            cs["completedAt"] = _now_iso()
            _stop_sms_campaign(cs)
        cs["status"] = "expired"
        cs["exitReason"] = "manually_stopped"
        cs["completedAt"] = _now_iso()

        plan = run.get("planSnapshot") or get_plan(plan_id) or {}
        try:
            if _all_campaigns_terminal(run, bucket_index):
                _advance_bucket(run, plan, bucket_index, reason="all_campaigns_done")
            else:
                save_run(run)
            return run
        except ConcurrentWriteError:
            if attempt == _MAX_RETRIES - 1:
                raise
            logger.info(
                "force_stop_campaign: concurrent write on attempt %d — retrying",
                attempt + 1,
            )

    raise ConcurrentWriteError(
        f"force_stop_campaign exhausted {_MAX_RETRIES} retries"
    )  # unreachable


# ── Bucket lifecycle ──────────────────────────────────────────────────────────


def _start_bucket(run: dict, index: int) -> None:
    plan = run.get("planSnapshot") or {}
    now = _now_utc()
    now_iso = now.isoformat()

    bucket_state = run["bucketStates"][index]
    bucket_state["status"] = "running"
    bucket_state["startedAt"] = now_iso
    _record_plan_event(
        run,
        "bucket_started",
        {"bucketIndex": index, "bucketName": plan["buckets"][index].get("name")},
    )

    # Schedule FIRST — if this fails, don't create Connect campaigns without a poller
    try:
        schedule_name = _schedule_tick(
            plan_id=run["planId"],
            run_id=run["runId"],
            bucket_index=index,
        )
        bucket_state["scheduleName"] = schedule_name
    except Exception as exc:
        logger.error("_start_bucket[%d]: scheduler failed: %s", index, exc)
        raise

    # Persist bucket=running + scheduleName before Connect calls
    try:
        save_run(run)
    except ConcurrentWriteError:
        _delete_schedule_safe(schedule_name)
        raise

    # Dispatch stage-1 campaigns — _dispatch_ready_campaigns saves internally per wave (B1-A)
    changed = True
    _stalled: set[int] = set()
    while changed:
        changed = _dispatch_ready_campaigns(run, plan, index, _stalled)

    # Chain-start next bucket immediately if it is marked parallel
    next_index = index + 1
    if next_index < len(plan["buckets"]):
        next_bucket = plan["buckets"][next_index]
        next_bucket_state = run["bucketStates"][next_index]
        if (
            next_bucket.get("parallel", False)
            and next_bucket_state["status"] == "queued"
        ):
            logger.info("_start_bucket: chain-starting parallel bucket %d", next_index)
            _start_bucket(run, next_index)


def _activate_warming_bucket(run: dict, plan: dict, bucket_index: int) -> None:
    """Transition a pre-warmed bucket from warming → running, starting all warming campaigns."""
    bucket_state = run["bucketStates"][bucket_index]
    now_iso = _now_iso()

    # Recovery: campaigns that failed to pre-warm (error, no connectCampaignId) → reset to queued
    # so _dispatch_ready_campaigns picks them up as cold starts.
    for cs in bucket_state["campaignStates"]:
        if cs["status"] == "error" and not cs.get("connectCampaignId"):
            cs["status"] = "queued"
            cs.pop("exitReason", None)
            cs.pop("errorDetail", None)
            cs.pop("completedAt", None)
            # See _reset_cascade_cancelled_children — reconcileRetries is sticky
            # and must not survive an unrelated queued revert (root-caused
            # 2026-08-27, adversarial code review).
            cs["reconcileRetries"] = 0

    bucket_state["status"] = "running"
    bucket_state["startedAt"] = now_iso
    _record_plan_event(
        run,
        "bucket_started",
        {
            "bucketIndex": bucket_index,
            "bucketName": plan["buckets"][bucket_index].get("name"),
        },
    )

    # Schedule FIRST — if this fails, don't start Connect campaigns without a poller
    try:
        schedule_name = _schedule_tick(
            plan_id=run["planId"],
            run_id=run["runId"],
            bucket_index=bucket_index,
        )
        bucket_state["scheduleName"] = schedule_name
    except Exception as exc:
        logger.error(
            "_activate_warming_bucket[%d]: scheduler failed: %s", bucket_index, exc
        )
        raise

    # Start all warming campaigns
    bucket = plan["buckets"][bucket_index]
    for ci, cs in enumerate(bucket_state["campaignStates"]):
        if cs["status"] == "warming" and cs.get("connectCampaignId"):
            seg_name = cs.get("segmentName", "?")
            if cs.get("warmupStarted"):
                # StartCampaign was already called during warmup — campaign is Running in Connect,
                # waiting to dial at the pre-scheduled startTime. Just sync our state.
                cs["status"] = "running"
                cs["startedAt"] = now_iso
                cs.pop("warmupStarted", None)
                _slog.info(
                    "activate_warming_campaign_prewarmed",
                    plan_id=run["planId"],
                    run_id=run["runId"],
                    bucket_index=bucket_index,
                    campaign_index=ci,
                    segment_name=seg_name,
                    connect_campaign_id=cs["connectCampaignId"],
                )
            else:
                # StartCampaign was not called during warmup (failed or cross-plan warmup without start).
                # Refresh the schedule to avoid "start time has already passed", then start.
                try:
                    from vip_shared.infrastructure.persistence.outbound_campaigns_client import (
                        build as build_oc,
                    )

                    oc = build_oc()
                    now_activate = _now_utc()
                    campaign_def = (
                        bucket.get("campaigns", [])[ci]
                        if ci < len(bucket.get("campaigns", []))
                        else {}
                    )
                    run_type = campaign_def.get("run_type", "full")
                    new_end_time = _campaign_end_time(
                        now_activate, campaign_def, run_type
                    )
                    new_start_time = (now_activate + timedelta(seconds=60)).isoformat()
                    oc.update_campaign_schedule(
                        cs["connectCampaignId"],
                        {
                            "startTime": new_start_time,
                            "endTime": new_end_time,
                        },
                    )
                    oc.start_campaign(cs["connectCampaignId"])
                    cs["status"] = "running"
                    cs["startedAt"] = now_iso
                    _slog.info(
                        "activate_warming_campaign_cold_started",
                        plan_id=run["planId"],
                        run_id=run["runId"],
                        bucket_index=bucket_index,
                        campaign_index=ci,
                        segment_name=seg_name,
                        connect_campaign_id=cs["connectCampaignId"],
                        new_start_time=new_start_time,
                    )
                except Exception as exc:
                    _slog.error(
                        "activate_warming_campaign_failed",
                        plan_id=run["planId"],
                        run_id=run["runId"],
                        bucket_index=bucket_index,
                        campaign_index=ci,
                        segment_name=seg_name,
                        connect_campaign_id=cs.get("connectCampaignId"),
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                    cs["status"] = "error"
                    cs["exitReason"] = REASON_CREATION_FAILED
                    cs["errorDetail"] = str(exc)
                    cs["completedAt"] = now_iso
                    _record_plan_event(
                        run,
                        "creation_failed",
                        {
                            "bucketIndex": bucket_index,
                            "campaignIndex": ci,
                            "error": str(exc),
                        },
                    )

    # Dispatch any newly unblocked campaigns — saves internally per wave (B1-A)
    changed = True
    _stalled: set[int] = set()
    while changed:
        changed = _dispatch_ready_campaigns(run, plan, bucket_index, _stalled)

    # Final save — persists warming activations + schedule.
    # B1-A's dispatch saves already wrote the run if any campaigns were dispatched; this
    # save covers the case where only warming activations happened and nothing was dispatched.
    # CWE: delete the orphaned schedule and re-raise; DDB reverts to "warming" — recoverable.
    try:
        save_run(run)
    except ConcurrentWriteError:
        _delete_schedule_safe(schedule_name)
        raise

    # Chain-start next bucket if parallel
    next_index = bucket_index + 1
    if next_index < len(plan["buckets"]):
        next_bucket = plan["buckets"][next_index]
        next_bs = run["bucketStates"][next_index]
        if next_bucket.get("parallel", False) and next_bs["status"] == "queued":
            logger.info(
                "_activate_warming_bucket: chain-starting parallel bucket %d",
                next_index,
            )
            _start_bucket(run, next_index)


def _prestart_next_bucket(run: dict, plan: dict, current_index: int) -> None:
    """Create (but do not start) Connect campaigns for next bucket's stage-1 entries."""
    next_index = current_index + 1
    if next_index >= len(plan["buckets"]):
        return

    next_bucket_state = run["bucketStates"][next_index]
    if next_bucket_state["status"] != "queued":
        return  # Already warming or running

    next_bucket_state["status"] = "warming"
    _slog.info(
        "prestart_next_bucket_start",
        plan_id=run["planId"],
        run_id=run["runId"],
        current_bucket=current_index,
        next_bucket=next_index,
    )

    # Claim save: persist "warming" before creating any Connect campaigns.
    # Without this, a crash between _create_campaign_only and the caller's save_run
    # would leave the bucket as "queued" in DDB, causing a duplicate campaign next tick.
    try:
        save_run(run)
    except Exception as _claim_exc:
        next_bucket_state["status"] = "queued"
        _slog.error(
            "prestart_next_bucket_claim_failed",
            plan_id=run["planId"],
            run_id=run["runId"],
            next_bucket=next_index,
            error=str(_claim_exc),
        )
        return

    next_bucket = plan["buckets"][next_index]
    for ci, campaign in enumerate(next_bucket.get("campaigns", [])):
        if _is_branded(campaign) or _is_sms(campaign):
            continue  # branded/SMS campaigns have no warmup phase — start directly in _start_one_campaign
        if not campaign.get("dependsOn"):
            cs = next_bucket_state["campaignStates"][ci]
            camp_name = campaign.get("name") or campaign.get("id", "?")
            if cs["status"] == "queued":
                try:
                    connect_id, seg_name, seg_arn, warmup_started = (
                        _create_campaign_only(next_bucket, campaign, run)
                    )
                    cs["status"] = "warming"
                    cs["connectCampaignId"] = connect_id
                    cs["segmentName"] = seg_name
                    cs["segmentArn"] = seg_arn
                    cs["warmupStarted"] = warmup_started
                    _slog.info(
                        "prestart_next_bucket_campaign_ok",
                        plan_id=run["planId"],
                        run_id=run["runId"],
                        bucket_index=next_index,
                        campaign_index=ci,
                        campaign=camp_name,
                        connect_campaign_id=connect_id,
                        segment_name=seg_name,
                        warmup_started=warmup_started,
                    )
                    # Mid-flight save: persist connectCampaignId so that if a crash occurs
                    # before the outer save_run, the campaign can be recovered next tick.
                    try:
                        save_run(run)
                    except Exception as _mid_exc:
                        logger.warning(
                            "_prestart_next_bucket campaign %d: mid-flight save failed: %s",
                            ci,
                            _mid_exc,
                        )
                except _RedisRebuildingError as exc:
                    # Transient — leave queued so the next tick retries.
                    _slog.warn(
                        "prestart_next_bucket_redis_rebuilding",
                        plan_id=run["planId"],
                        run_id=run["runId"],
                        bucket_index=next_index,
                        campaign_index=ci,
                        campaign=camp_name,
                        error=str(exc),
                    )
                except _EmptySegmentError as exc:
                    empty_retries = cs.get("reconcileRetries") or 0
                    retry_limit = int(next_bucket.get("reconcileRetryLimit", 5))
                    if empty_retries < retry_limit:
                        cs["reconcileRetries"] = empty_retries + 1
                        _slog.warn(
                            "prestart_next_bucket_empty_segment_retry",
                            plan_id=run["planId"],
                            run_id=run["runId"],
                            bucket_index=next_index,
                            campaign_index=ci,
                            campaign=camp_name,
                            retry=empty_retries + 1,
                            retry_limit=retry_limit,
                            error=str(exc),
                        )
                    else:
                        # Final check: same rebuild-detection fallback as _start_one_campaign.
                        if not _check_redis_ready():
                            cs["reconcileRetries"] = 0
                            _slog.warn(
                                "prestart_next_bucket_redis_not_ready_reset",
                                plan_id=run["planId"],
                                run_id=run["runId"],
                                bucket_index=next_index,
                                campaign_index=ci,
                                campaign=camp_name,
                                retries_exhausted=empty_retries,
                            )
                        else:
                            _slog.warn(
                                "prestart_next_bucket_empty_segment_cancelled",
                                plan_id=run["planId"],
                                run_id=run["runId"],
                                bucket_index=next_index,
                                campaign_index=ci,
                                campaign=camp_name,
                                retries_exhausted=empty_retries,
                                error=str(exc),
                            )
                            cs["status"] = "cancelled"
                            cs["exitReason"] = REASON_SKIPPED_EMPTY
                            cs["completedAt"] = _now_iso()
                except _CutoffTooCloseError as exc:
                    # Warmup attempted too close to daily cutoff — leave queued so that
                    # _start_one_campaign handles it as expired when the bucket activates.
                    _slog.warn(
                        "prestart_next_bucket_cutoff_too_close",
                        plan_id=run["planId"],
                        run_id=run["runId"],
                        bucket_index=next_index,
                        campaign_index=ci,
                        campaign=camp_name,
                        error=str(exc),
                    )
                except Exception as exc:
                    _slog.error(
                        "prestart_next_bucket_campaign_failed",
                        plan_id=run["planId"],
                        run_id=run["runId"],
                        bucket_index=next_index,
                        campaign_index=ci,
                        campaign=camp_name,
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                    cs["status"] = "error"
                    cs["exitReason"] = REASON_CREATION_FAILED
                    cs["errorDetail"] = str(exc)
                    cs["completedAt"] = _now_iso()
                    _record_plan_event(
                        run,
                        "creation_failed",
                        {
                            "bucketIndex": next_index,
                            "campaignIndex": ci,
                            "error": str(exc),
                        },
                    )


def _expire_bucket(run: dict, plan: dict, bucket_index: int) -> None:
    now = _now_iso()
    bucket_state = run["bucketStates"][bucket_index]

    for cs in bucket_state["campaignStates"]:
        if cs["status"] == "running":
            if cs.get("connectCampaignId"):
                _safe_stop_campaign(cs["connectCampaignId"])
            if cs.get("brandedCampaignId"):
                _stop_branded_campaign(cs)
            cs["status"] = "expired"
            cs["exitReason"] = REASON_EXPIRED
            cs["completedAt"] = now
        elif cs["status"] in ("queued", "warming"):
            cs["status"] = "cancelled"
            cs["exitReason"] = REASON_BUCKET_EXPIRED
            cs["completedAt"] = now

    _advance_bucket(run, plan, bucket_index, reason="time_expired")


def _advance_bucket(run: dict, plan: dict, bucket_index: int, reason: str) -> None:
    now = _now_iso()
    bucket = plan["buckets"][bucket_index]
    bucket_state = run["bucketStates"][bucket_index]

    _delete_bucket_schedule_safe(run, bucket_index)

    try:
        # Persist terminal campaign states BEFORE deleting resources from Connect.
        # bucket_state["status"] stays "running" so that a ConcurrentWriteError here
        # leaves the bucket re-tryable (stale_tick guard skips only "completed"/"cancelled"
        # buckets). Without this save, a failed final save_run lets the next tick
        # re-poll Connect, find campaigns "Deleted", and overwrite the correct terminal
        # statuses with connect_deleted — which the S-11-A guard then misreads as an
        # external deletion and aborts a run that legitimately completed.
        save_run(run)

        # Cleanup Connect campaigns + CP segments if requested
        if bucket.get("cleanup", bucket.get("deleteAfter", True)):
            for cs in bucket_state["campaignStates"]:
                if cs.get("connectCampaignId"):
                    _safe_stop_campaign(cs["connectCampaignId"])
                    _safe_delete_campaign(cs["connectCampaignId"])
                if cs.get("segmentName"):
                    _safe_delete_segment(cs["segmentName"])

        bucket_state["status"] = "completed"
        bucket_state["completedAt"] = now

        # Duplicate audit row possible if this function re-executes after a
        # ConcurrentWriteError below (see comment above on stale_tick re-tryability)
        # — accepted, a cosmetic duplicate in the activity feed, not worth a new lock.
        _record_plan_event(
            run, "bucket_completed", {"bucketIndex": bucket_index, "bucketName": bucket.get("name"), "reason": reason}
        )

        # Fire any plans waiting for this specific bucket to complete
        try:
            _fire_bucket_chains(run["planId"], bucket_index)
        except Exception as exc:
            logger.error("_advance_bucket: _fire_bucket_chains failed: %s", exc)

        # Start next SEQUENTIAL bucket if it was waiting for this one to finish
        next_index = bucket_index + 1
        if next_index < len(plan["buckets"]):
            next_bucket = plan["buckets"][next_index]
            next_bucket_state = run["bucketStates"][next_index]
            if not next_bucket.get("parallel", False):
                if next_bucket_state["status"] in ("queued", "warming"):
                    save_run(run)
                    if next_bucket_state["status"] == "warming":
                        _activate_warming_bucket(run, plan, next_index)
                    else:
                        _start_bucket(run, next_index)
                    return  # save_run already called inside start/activate
                elif next_bucket_state["status"] == "running":
                    # Partially activated by cross-bucket eager dispatch; dispatch remaining stage-1 campaigns.
                    # Rescue the tick if _dispatch_cross_bucket_ready failed to schedule it (e.g. on exception).
                    if not next_bucket_state.get("scheduleName"):
                        try:
                            sched = _schedule_tick(
                                plan_id=run["planId"],
                                run_id=run["runId"],
                                bucket_index=next_index,
                            )
                            next_bucket_state["scheduleName"] = sched
                        except Exception as exc:
                            logger.error(
                                "_advance_bucket: rescue tick for bucket %d failed: %s",
                                next_index,
                                exc,
                            )
                    changed = True
                    _stalled: set[int] = set()
                    while changed:
                        changed = _dispatch_ready_campaigns(run, plan, next_index, _stalled)
                    save_run(run)
                    return

        # Check if ALL buckets are now terminal (handles parallel runs completing out of order,
        # and runs started mid-plan where earlier buckets are "cancelled"/skipped)
        _TERMINAL_BUCKET = {"completed", "cancelled"}
        if all(bs["status"] in _TERMINAL_BUCKET for bs in run["bucketStates"]):
            run["status"] = "completed"
            run["completedAt"] = now
            save_run(run)
            unlock_plan_run(run["planId"])
            _maybe_loop(run["planId"])
            # Notify if any campaign ended with error or connect_deleted
            _error_campaigns = [
                cs
                for bs in run["bucketStates"]
                for cs in bs.get("campaignStates", [])
                if cs.get("status") in ("error",)
                or cs.get("exitReason") == "connect_deleted"
            ]
            if _error_campaigns:
                _names = ", ".join(
                    cs.get("name", cs["campaignId"]) for cs in _error_campaigns[:5]
                )
                _notify_sns(
                    subject=f"[VIP Plans] Run completed with errors (plan={run['planId'][:8]})",
                    detail=(
                        f"Plan {run['planId']} / Run {run['runId']} completed but "
                        f"{len(_error_campaigns)} campaign(s) ended with errors:\n{_names}"
                    ),
                    attributes={
                        "alertType": "run_completed_with_errors",
                        "planId": run["planId"],
                    },
                )
            try:
                start_run_chained(run["planId"])
            except Exception as exc:
                logger.error("_advance_bucket: start_run_chained failed: %s", exc)
        else:
            save_run(run)

    except ConcurrentWriteError:
        # The EventBridge rule was already deleted above. Reschedule it so the bucket
        # is not stranded forever with no tick to drive it forward.
        try:
            sched = _schedule_tick(
                plan_id=run["planId"], run_id=run["runId"], bucket_index=bucket_index
            )
            # save_run() above just failed its version check, so the new
            # schedule name can't ride along on that write — persist it
            # directly (bypassing the version lock) so it can still be found
            # and deleted once this bucket completes. Skipping this step is
            # exactly how orphaned vip-plan-* rules/permissions accumulate
            # until they hit the Lambda policy's hard size limit (BD-013).
            try:
                record_bucket_schedule_name(
                    run["planId"], run["runId"], bucket_index, sched
                )
            except ClientError as _sched_exc:
                logger.error(
                    "_advance_bucket[%d]: failed to persist rescheduled tick name %s: %s",
                    bucket_index,
                    sched,
                    _sched_exc,
                )
            logger.info(
                "_advance_bucket[%d]: ConcurrentWriteError — rescheduled tick for retry",
                bucket_index,
            )
        except Exception as _exc:
            logger.error(
                "_advance_bucket[%d]: ConcurrentWriteError and reschedule failed: %s",
                bucket_index,
                _exc,
            )
        raise


# ── Cross-plan warmup ────────────────────────────────────────────────────────


def _emit_prewarm_failure(plan_id: str, count: int = 1) -> None:
    """Emit PrewarmFailure so a missed/failed pre-warm has real visibility.

    RUNBOOKS.md/INTEGRATION_CONTRACTS.md documented this as an existing,
    alarmable metric for months while every pre-warm failure path only ever
    logged an ERROR line and moved on — confirmed live 2026-08-21 (zero data
    in namespace VipConnect/Plans, zero code references to "PrewarmFailure"
    anywhere in the repo). Same emission shape as CampaignDispatchStalled/
    ScheduledRunFallback/NoActiveCampaign: per-plan dimension for drill-down,
    plus a no-dimension aggregate so a single CLI alarm can watch it.
    """
    try:
        boto3.client("cloudwatch").put_metric_data(
            Namespace="VIPPlans",
            MetricData=[
                {
                    "MetricName": "PrewarmFailure",
                    "Value": count,
                    "Unit": "Count",
                    "Dimensions": [{"Name": "PlanId", "Value": plan_id}],
                },
                {
                    "MetricName": "PrewarmFailure",
                    "Value": count,
                    "Unit": "Count",
                    "Dimensions": [],
                },
            ],
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "_emit_prewarm_failure: metric emission failed for %s: %s", plan_id, exc
        )


def _prestart_plan(target_plan_id: str) -> None:
    """Pre-create bucket-0 stage-1 Connect campaigns for a downstream plan.

    Results are stored on the plan META item as `pendingWarmup`.
    When `start_run` fires, it consumes this data and skips campaign creation.

    Retry-aware: if a previous call only partially warmed the bucket (some campaigns
    failed), subsequent calls from prestart_check will retry the missing ones and merge
    the results — so all stage-1 campaigns get a chance to warm within the 4–6 min window.
    """
    target_plan = get_plan(target_plan_id)
    if not target_plan:
        return
    if target_plan.get("isTemplate") or target_plan.get("is_template"):
        return

    # Skip if plan is already running
    latest = get_latest_run(target_plan_id)
    if latest and latest.get("status") == "running":
        return

    buckets = target_plan.get("buckets", [])
    if not buckets:
        return

    bucket = buckets[0]
    stage1_campaigns = [
        c for c in bucket.get("campaigns", []) if not c.get("dependsOn")
    ]

    # Merge with any existing pendingWarmup so retries only attempt missing campaigns.
    existing_warmup = target_plan.get("pendingWarmup") or {}
    already_warmed: dict[str, dict] = {
        c["campaignId"]: c for c in existing_warmup.get("campaigns", [])
    }

    # All stage-1 campaigns already covered — nothing to do.
    if already_warmed and all(
        (c.get("id") or c.get("campaignId")) in already_warmed for c in stage1_campaigns
    ):
        return

    warmed: list[dict] = list(
        already_warmed.values()
    )  # carry over successes from prior calls
    attempted = 0
    for campaign in stage1_campaigns:
        if _is_branded(campaign) or _is_sms(campaign):
            continue  # branded/SMS campaigns have no warmup phase — start directly in _start_one_campaign
        camp_id = campaign.get("id") or campaign.get("campaignId")
        if camp_id in already_warmed:
            continue  # already warmed in a previous prestart_check tick — skip
        attempted += 1
        camp_name = campaign.get("name") or camp_id or "?"
        try:
            connect_id, seg_name, seg_arn, warmup_started = _create_campaign_only(
                bucket, campaign, {}
            )
            warmed.append(
                {
                    "campaignId": camp_id,
                    "connectCampaignId": connect_id,
                    "segmentName": seg_name,
                    "segmentArn": seg_arn,
                    "warmupStarted": warmup_started,
                }
            )
            _slog.info(
                "prestart_plan_campaign_ok",
                plan_id=target_plan_id,
                campaign=camp_name,
                connect_campaign_id=connect_id,
                segment_name=seg_name,
                warmup_started=warmup_started,
            )
        except Exception as exc:
            _slog.error(
                "prestart_plan_campaign_failed",
                plan_id=target_plan_id,
                campaign=camp_name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            # Will be retried on the next prestart_check tick (runs every minute)

    newly_warmed = len(warmed) - len(already_warmed)
    failed = attempted - newly_warmed
    _slog.info(
        "prestart_plan_summary",
        plan_id=target_plan_id,
        attempted=attempted,
        newly_warmed=newly_warmed,
        total_warmed=len(warmed),
        total_stage1=len(stage1_campaigns),
        failed=failed,
        all_covered=len(warmed) == len(stage1_campaigns),
    )
    if failed > 0:
        _emit_prewarm_failure(target_plan_id, failed)
    if warmed:
        update_plan_pending_warmup(
            target_plan_id,
            {"campaigns": warmed, "createdAt": _now_iso()},
        )


def _prestart_chained_runs(run: dict, plan: dict, bucket_index: int) -> None:
    """Pre-warm first-bucket campaigns for downstream plans that will start when this run ends.

    Called from tick when the last time-based bucket enters its pre-start window.
    Covers two cases:
    - on_plan_complete chains: downstream plans triggered when this plan finishes
    - loop: the same plan restarting if the loop window will still be open
    """
    plan_id = run["planId"]

    # 1. on_plan_complete chains
    for downstream in find_plans_by_trigger_planid(plan_id):
        try:
            _prestart_plan(downstream["planId"])
        except Exception as exc:
            logger.error(
                "_prestart_chained_runs: pre-warm %s failed: %s",
                downstream["planId"],
                exc,
            )
            _emit_prewarm_failure(downstream["planId"])

    # 2. Loop: same plan restarts if the loop window will still be open
    loop = plan.get("loop") or {}
    end_time_str = loop.get("endTime")
    if end_time_str:
        _COT = timezone(timedelta(hours=-5))
        now_cot = datetime.now(_COT)
        now_hhmm = now_cot.hour * 60 + now_cot.minute
        end_h, end_m = (int(x) for x in end_time_str.split(":"))
        if now_hhmm < end_h * 60 + end_m:
            try:
                _prestart_plan(plan_id)
            except Exception as exc:
                logger.error(
                    "_prestart_chained_runs: loop self-pre-warm for %s failed: %s",
                    plan_id,
                    exc,
                )
                _emit_prewarm_failure(plan_id)


def _prestart_after_campaign(upstream_plan_id: str, campaign_id: str) -> None:
    """Pre-warm plans whose trigger is afterCampaign == campaign_id on the given upstream plan."""
    for downstream in find_plans_by_trigger_planid(upstream_plan_id):
        trigger = downstream.get("trigger") or {}
        if trigger.get("afterCampaign") != campaign_id:
            continue
        try:
            _prestart_plan(downstream["planId"])
            logger.info(
                "_prestart_after_campaign: pre-warmed %s (afterCampaign=%s)",
                downstream["planId"],
                campaign_id,
            )
        except Exception as exc:
            logger.error(
                "_prestart_after_campaign: failed to pre-warm %s: %s",
                downstream["planId"],
                exc,
            )
            _emit_prewarm_failure(downstream["planId"])


def _ensure_scheduled_run_permission(plan_id: str) -> None:
    """Verify the EventBridge scheduled_run rule exists AND has permission to invoke this Lambda.

    Guards two failure modes detected 4-6 min before trigger:
    1. Missing rule: EventBridge rule was deleted (e.g., console accident). Recreates it
       from the plan's trigger config via upsert_schedule.
    2. Missing Lambda permission: a CDK deploy that recreates the function wipes custom
       add_permission statements. Re-adds the statement so the cron can fire today.
    """
    import json as _json

    from scheduler_manager import _rule_name, LAMBDA_FUNCTION_ARN, _account_id, upsert_schedule

    rule_name = _rule_name(plan_id)

    # ── 1. Verify the EventBridge rule itself exists ─────────────────────────
    events = boto3.client("events")
    rule_exists = True
    try:
        events.describe_rule(Name=rule_name)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceNotFoundException":
            rule_exists = False
        # Any other error: assume rule exists, fall through to permission check

    if not rule_exists:
        plan = get_plan(plan_id)
        trigger = (plan or {}).get("trigger") if plan else None
        if not trigger or trigger.get("type") != "time":
            _slog.error(
                "scheduled_run_rule_missing_no_trigger",
                plan_id=plan_id,
                rule_name=rule_name,
            )
            return
        try:
            upsert_schedule(plan_id, trigger)
            _slog.warn(
                "scheduled_run_rule_recreated",
                plan_id=plan_id,
                rule_name=rule_name,
                reason="rule_was_missing_before_trigger",
            )
        except Exception as exc:
            _slog.error(
                "scheduled_run_rule_recreate_failed",
                plan_id=plan_id,
                rule_name=rule_name,
                error=str(exc),
            )
        # upsert_schedule adds the Lambda permission — no need to re-add below
        return

    # ── 2. Rule exists — verify Lambda invoke permission ────────────────────
    lam = boto3.client("lambda")
    try:
        policy = _json.loads(lam.get_policy(FunctionName=LAMBDA_FUNCTION_ARN)["Policy"])
        existing_sids = {s.get("Sid", "") for s in policy.get("Statement", [])}
        if rule_name in existing_sids:
            return  # already present — nothing to do
    except ClientError:
        return  # can't read policy; skip silently

    # Permission is missing — re-add it before the cron fires
    rule_arn = f"arn:aws:events:us-east-1:{_account_id()}:rule/{rule_name}"
    try:
        lam.add_permission(
            FunctionName=LAMBDA_FUNCTION_ARN,
            StatementId=rule_name,
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=rule_arn,
        )
        _slog.warn(
            "scheduled_run_permission_restored",
            plan_id=plan_id,
            rule_name=rule_name,
            reason="permission_was_missing_before_trigger",
        )
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code != "ResourceConflictException":
            _slog.error(
                "scheduled_run_permission_restore_failed",
                plan_id=plan_id,
                rule_name=rule_name,
                error=str(exc),
            )


def prestart_check() -> dict:
    """Scan all plans and pre-warm any with a time-based trigger starting within 5 minutes.

    Called by handler.py for `action == "prestart_check"` (EventBridge rate(1 min)).
    Also serves as a fallback trigger for plans whose scheduled_run EventBridge cron
    invocation failed — detected at delta=-1 (one minute after trigger time).
    """
    _COT = timezone(timedelta(hours=-5))
    now_cot = datetime.now(_COT)
    now_hhmm = now_cot.hour * 60 + now_cot.minute
    warmed: list[str] = []
    fallback_triggered: list[str] = []

    # Shared across both the prestart and stuck-run loops below.
    all_plans = list_plans()
    cw = boto3.client("cloudwatch")

    for plan in all_plans:
        # Templates never run on a cron (same invariant as update_plan's own
        # vip-sched-* cleanup) — skip both branches below entirely. Without this,
        # _ensure_scheduled_run_permission recreated a template's vip-sched-* rule
        # every day at its old trigger.time regardless of isTemplate, undoing the
        # janitor's cleanup daily (confirmed live 2026-08-24: plan
        # c63d695c-b99e-4885-808a-8eca91d08e8e's rule kept coming back).
        if plan.get("isTemplate") or plan.get("is_template"):
            continue
        trigger = plan.get("trigger") or {}
        if trigger.get("type") != "time":
            continue
        time_str = trigger.get("time", "")
        if not time_str:
            continue
        try:
            t_h, t_m = (int(x) for x in time_str.split(":"))
            trigger_hhmm = t_h * 60 + t_m
            delta = trigger_hhmm - now_hhmm
            if 4 <= delta <= 6:
                if not _is_working_day(plan):
                    continue  # plan doesn't run today; skip pre-warm to avoid wasted Connect warmup
                # Pre-warm: trigger is 4–6 minutes away (5 ± 1 tolerance).
                # Also verify EventBridge has Lambda invoke permission — a CDK deploy
                # that recreates the function wipes custom add_permission statements,
                # silently preventing the scheduled_run cron from invoking the Lambda.
                _ensure_scheduled_run_permission(plan["planId"])
                _slog.info(
                    "prestart_check_warming_plan",
                    plan_id=plan["planId"],
                    plan_name=plan.get("name"),
                    trigger_time=time_str,
                    delta_minutes=delta,
                )
                _prestart_plan(plan["planId"])
                warmed.append(plan["planId"])
            elif delta == -1:
                # Fallback trigger: the EventBridge cron should have fired ~80 seconds ago.
                # If no run was started in the last 3 minutes, the cron invocation failed.
                if not _is_working_day(plan):
                    continue  # plan doesn't run today; absence of a run is expected, not a missed cron
                latest = get_latest_run(plan["planId"])
                already_started = False
                if latest and latest.get("status") == "running":
                    already_started = True
                elif latest and latest.get("startedAt"):
                    try:
                        started_at = datetime.fromisoformat(
                            latest["startedAt"].replace("Z", "+00:00")
                        )
                        if (_now_utc() - started_at).total_seconds() < 180:
                            already_started = True
                    except Exception:
                        pass
                if already_started:
                    continue
                _slog.warn(
                    "prestart_fallback_triggered",
                    plan_id=plan["planId"],
                    plan_name=plan.get("name"),
                    trigger_time=time_str,
                    reason="no_run_found_80s_after_scheduled_trigger",
                )
                try:
                    # Emit twice: with PlanId (for per-plan drill-down) and without
                    # (for the aggregate CLI alarm that can't use SEARCH expressions).
                    cw.put_metric_data(
                        Namespace="VIPPlans",
                        MetricData=[
                            {
                                "MetricName": "ScheduledRunFallback",
                                "Value": 1,
                                "Unit": "Count",
                                "Dimensions": [{"Name": "PlanId", "Value": plan["planId"]}],
                            },
                            {
                                "MetricName": "ScheduledRunFallback",
                                "Value": 1,
                                "Unit": "Count",
                                "Dimensions": [],
                            },
                        ],
                    )
                except Exception as cw_exc:
                    _slog.error("prestart_fallback_metric_failed", error=str(cw_exc))
                scheduled_run(plan["planId"])
                fallback_triggered.append(plan["planId"])
        except Exception as exc:
            _slog.error(
                "prestart_check_plan_failed",
                plan_id=plan.get("planId"),
                plan_name=plan.get("name"),
                error=str(exc),
                error_type=type(exc).__name__,
            )

    _slog.info(
        "prestart_check_done",
        warmed_count=len(warmed),
        warmed_plan_ids=warmed,
        fallback_count=len(fallback_triggered),
        fallback_plan_ids=fallback_triggered,
    )

    # ── Stuck run detection ───────────────────────────────────────────────────
    # Emit a CloudWatch metric for any run that has been "running" longer than
    # _STUCK_RUN_HOURS without completing. A CW alarm on this metric pages oncall.
    stuck: list[str] = []
    for plan in all_plans:
        plan_id = plan.get("planId")
        if not plan_id:
            continue
        try:
            latest = get_latest_run(plan_id)
        except Exception:
            continue
        if not latest or latest.get("status") != "running":
            continue
        started = latest.get("startedAt")
        if not started:
            continue
        hours = (_now_utc() - datetime.fromisoformat(started)).total_seconds() / 3600
        if hours >= _STUCK_RUN_HOURS:
            run_id = latest.get("runId", "unknown")
            logger.warning(
                "prestart_check: stuck run detected plan=%s run=%s hours=%.1f",
                plan_id,
                run_id,
                hours,
            )
            stuck.append(plan_id)
            try:
                cw.put_metric_data(
                    Namespace="VIPPlans",
                    MetricData=[
                        {
                            "MetricName": "StuckRun",
                            "Value": 1,
                            "Unit": "Count",
                            "Dimensions": [{"Name": "PlanId", "Value": plan_id}],
                        },
                        # Aggregate (no dimensions) so a single CLI alarm can
                        # watch "any run stuck" directly — same convention as
                        # ScheduledRunFallback/CampaignDispatchStalled/
                        # NoActiveCampaign. Previously PlanId-only, so no CW
                        # alarm could cheaply watch it — this metric had been
                        # firing every minute for 22+ hours during BD-013 with
                        # no alarm able to catch it.
                        {
                            "MetricName": "StuckRun",
                            "Value": 1,
                            "Unit": "Count",
                            "Dimensions": [],
                        },
                    ],
                )
            except Exception as exc:
                logger.error(
                    "prestart_check: CloudWatch metric failed for %s: %s", plan_id, exc
                )

    if stuck:
        logger.warning("prestart_check: %d stuck run(s): %s", len(stuck), stuck)

    # ── No-active-campaign detection ─────────────────────────────────────────
    # A run can look healthy (status="running", error=None) while its current
    # bucket has sat for minutes with zero campaigns actively progressing —
    # exactly what happened in BD-013: the tick that should have created the
    # bucket's campaigns crashed silently, and nothing else ever surfaced it.
    # StuckRun (above) only catches this after _STUCK_RUN_HOURS (4h); this
    # catches the same failure mode within minutes.
    no_active: list[str] = []
    for plan in all_plans:
        plan_id = plan.get("planId")
        if not plan_id:
            continue
        try:
            latest = get_latest_run(plan_id)
        except Exception:
            continue
        if not latest or latest.get("status") != "running":
            continue
        for bucket_state in latest.get("bucketStates", []):
            if bucket_state.get("status") != "running":
                continue
            started = bucket_state.get("startedAt")
            if not started:
                continue
            minutes = (_now_utc() - datetime.fromisoformat(started)).total_seconds() / 60
            if minutes < _NO_ACTIVE_CAMPAIGN_MINUTES:
                continue
            campaign_states = bucket_state.get("campaignStates", [])
            if any(cs.get("status") in _ACTIVE_CAMPAIGN_STATUSES for cs in campaign_states):
                continue
            if _bucket_has_only_legitimate_waits(latest, plan, bucket_state):
                continue
            run_id = latest.get("runId", "unknown")
            logger.warning(
                "prestart_check: no active campaign for %.0f min plan=%s run=%s bucket=%s",
                minutes,
                plan_id,
                run_id,
                bucket_state.get("bucketId", "?"),
            )
            no_active.append(plan_id)
            try:
                cw.put_metric_data(
                    Namespace="VIPPlans",
                    MetricData=[
                        {
                            "MetricName": "NoActiveCampaign",
                            "Value": 1,
                            "Unit": "Count",
                            "Dimensions": [{"Name": "PlanId", "Value": plan_id}],
                        },
                        {
                            "MetricName": "NoActiveCampaign",
                            "Value": 1,
                            "Unit": "Count",
                            "Dimensions": [],
                        },
                    ],
                )
            except Exception as exc:
                logger.error(
                    "prestart_check: NoActiveCampaign metric failed for %s: %s",
                    plan_id,
                    exc,
                )
            break  # one alert per plan per tick — remaining buckets can't also be "running"

    if no_active:
        logger.warning(
            "prestart_check: %d plan(s) with no active campaign: %s",
            len(no_active),
            no_active,
        )

    return {
        "warmed": warmed,
        "stuck": stuck,
        "fallback_triggered": fallback_triggered,
        "no_active_campaign": no_active,
    }


# ── Campaign chain triggers ────────────────────────────────────────────────────


def _fire_campaign_chains(
    upstream_plan_id: str,
    bucket_index: int,
    completed_campaign_ids: set[str],
) -> None:
    """Fire plans triggered by a specific campaign completing (afterCampaign trigger).

    Called from tick after the poll loop whenever campaigns newly reach 'completed'.
    """
    chained = find_plans_by_trigger_planid(upstream_plan_id)
    for plan in chained:
        if plan.get("isTemplate") or plan.get("is_template"):
            continue
        if not _within_working_hours(plan):
            logger.info(
                "_fire_campaign_chains: plan %s outside working hours, skipping",
                plan["planId"],
            )
            if plan.get("pendingWarmup"):
                update_plan_pending_warmup(plan["planId"], None)
                _slog.info(
                    "fire_campaign_chains_warmup_cleared", plan_id=plan["planId"]
                )
            continue
        trigger = plan.get("trigger", {})
        if trigger.get("type") != "on_plan_complete":
            continue
        after_campaign = trigger.get("afterCampaign")
        if not after_campaign:
            continue  # no afterCampaign → handled by _fire_bucket_chains or start_run_chained
        if after_campaign not in completed_campaign_ids:
            continue
        # Optional bucket guard — afterCampaign plans may also specify afterBucket for display
        after_bucket = trigger.get("afterBucket")
        if after_bucket is not None and int(after_bucket) != bucket_index:
            continue
        try:
            start_run(plan["planId"], triggered_by="chained")
            if not trigger.get("repeat", True):
                update_plan_trigger(plan["planId"], {"type": "manual"})
        except Exception as exc:
            logger.error(
                "_fire_campaign_chains: failed to start plan %s: %s",
                plan["planId"],
                exc,
            )


# ── Loop helper ───────────────────────────────────────────────────────────────


def _maybe_loop(plan_id: str) -> None:
    """Restart the plan if the current COT time falls within the loop window.

    loop.endTime   — HH:MM COT — do not restart on or after this time
    loop.startTime — HH:MM COT — do not restart before this time (optional, default 00:00)

    Reads the *live* plan (not the run snapshot) so the operator can change
    the window without waiting for the current run to end.
    """
    _COT = timezone(timedelta(hours=-5))  # Colombia Time, no DST

    live_plan = get_plan(plan_id)
    if not live_plan:
        return
    loop = live_plan.get("loop") or {}
    end_time_str = loop.get("endTime")
    if not end_time_str:
        return

    # Guard: don't restart if a run is already active (prevents double-start on concurrent calls)
    latest = get_latest_run(plan_id)
    if latest and latest.get("status") == "running":
        logger.info("_maybe_loop[%s]: plan already running, skipping", plan_id)
        return

    try:
        now_cot = datetime.now(_COT)
        now_hhmm = now_cot.hour * 60 + now_cot.minute

        end_h, end_m = (int(x) for x in end_time_str.split(":"))
        end_minutes = end_h * 60 + end_m

        start_time_str = loop.get("startTime", "00:00")
        start_h, start_m = (int(x) for x in start_time_str.split(":"))
        start_minutes = start_h * 60 + start_m

        if start_minutes <= now_hhmm < end_minutes:
            start_run(plan_id, triggered_by="loop")
    except Exception as exc:
        logger.error("_maybe_loop[%s]: %s", plan_id, exc)


# ── Campaign dispatch ─────────────────────────────────────────────────────────


def _dispatch_cross_bucket_ready(
    run: dict, plan: dict, current_bucket_index: int
) -> bool:
    """Eagerly start campaigns in future buckets whose cross-bucket dependsOn are all satisfied.

    Only acts on campaigns with explicit dependsOn. Stage-1 (no deps) campaigns still wait
    for their bucket to activate via _advance_bucket → _start_bucket.

    Two-phase execution to prevent duplicate Connect campaign creation under concurrent ticks:
      Phase 1 — claim: mark eligible campaigns "creating" and save to DynamoDB atomically.
                ConcurrentWriteError here means another tick already won — propagate upward
                so the handler exits cleanly; the winning tick proceeds uninterrupted.
      Phase 2 — execute: create Connect campaigns. Only reached by the tick that won Phase 1.

    Recovery: if the Lambda times out between Phase 1 and Phase 2, campaigns remain in
    "creating" state in DynamoDB. _dispatch_ready_campaigns resets them to "queued" on
    the next tick for that bucket.
    """
    changed = False
    now_iso = _now_iso()
    claimed: list[tuple[int, int]] = []

    for bi in range(current_bucket_index + 1, len(run["bucketStates"])):
        bs = run["bucketStates"][bi]
        if bs["status"] != "queued":
            continue  # warming/running already handled elsewhere
        bucket = plan["buckets"][bi]
        for ci, campaign in enumerate(bucket.get("campaigns", [])):
            cs = bs["campaignStates"][ci]
            if cs["status"] != "queued":
                continue
            depends_on = campaign.get("dependsOn", [])
            if not depends_on:
                continue  # stage-1: waits for bucket to activate normally

            parent_states = [_find_campaign_state(run, cid) for cid in depends_on]

            # Deliberately stricter than _dispatch_ready_campaigns' terminal-status
            # check (BD-014 investigation, 2026-08-27): this function activates an
            # entire future bucket ahead of its natural turn (new schedule, running
            # status) on the strength of the parent alone. Only jump the gun on a
            # clean finish. A cancelled/errored/expired parent still unblocks the
            # dependent — just via the normal path once the bucket's turn comes,
            # where _dispatch_ready_campaigns' permissive check applies. No deadlock,
            # only timing — see docs/BUGLOG.md.
            if all(
                s and (s["status"] == "completed" or s.get("exitReason") == "skipped")
                for s in parent_states
            ):
                if bs["status"] == "queued":
                    # Create schedule BEFORE claiming the bucket as "running".
                    # If schedule creation fails, skip this bucket entirely so it stays
                    # "queued" — no orphaned "running" bucket with no EventBridge rule.
                    try:
                        sched = _schedule_tick(
                            plan_id=run["planId"],
                            run_id=run["runId"],
                            bucket_index=bi,
                        )
                    except Exception as exc:
                        logger.error(
                            "_dispatch_cross_bucket_ready[%d]: schedule failed, skipping: %s",
                            bi,
                            exc,
                        )
                        continue
                    bs["status"] = "running"
                    bs["startedAt"] = now_iso
                    bs["scheduleName"] = sched
                cs["status"] = "creating"
                cs["creatingAt"] = now_iso
                claimed.append((bi, ci))
                changed = True

    if not changed:
        return False

    # Persist the claim before Connect touches anything.
    # Raises ConcurrentWriteError if another tick already saved — caller handles it.
    save_run(run)

    # Campaigns left in "creating" are picked up by _dispatch_ready_campaigns on the
    # next tick for the newly-activated bucket (resets "creating" → "queued" on entry).

    return True


def _dispatch_ready_campaigns(
    run: dict, plan: dict, bucket_index: int, stalled: set[int] | None = None
) -> bool:
    """Start any campaigns whose dependencies are now satisfied.

    Returns True if any state change occurred (used for fixed-point loop).

    stalled: campaign indices that reverted to "queued" without real progress in a
    PRIOR call within the same fixed-point loop (same tick, same Lambda invocation).
    Excluded from Phase 2 so a transient failure (e.g. Redis mid-rebuild) isn't
    retried immediately on every loop iteration — only on the next external tick.
    Callers own this set across their `while changed:` loop; pass the same set
    instance each call so it accumulates. Indices left in "queued" after Phase 4
    are added to it here.

    Two-phase claim prevents duplicate Connect campaigns when save_run races:
      Phase 1 — Recovery: reset any "creating" back to "queued" (prior tick failed mid-flight)
      Phase 2 — Identify ready campaigns and cascade-cancels
      Phase 3 — Claim: set newly_ready to "creating" and save BEFORE calling Connect
      Phase 4 — Start: Connect API calls for each claimed campaign
      Phase 5 — Confirm: save final statuses (connectCampaignId etc.)
    """
    if stalled is None:
        stalled = set()
    bucket = plan["buckets"][bucket_index]
    bucket_state = run["bucketStates"][bucket_index]

    # Phase 1 — Recovery
    for cs in bucket_state["campaignStates"]:
        if cs["status"] == "creating":
            existing_id = cs.get("connectCampaignId")
            if existing_id:
                # Mid-flight marker: a Connect campaign was created but Lambda crashed
                # before the confirm save. Poll Connect instead of blindly resetting to
                # queued — which would cause a duplicate campaign to be created.
                try:
                    state = _get_campaign_state(existing_id)
                    if state in _CONNECT_TERMINAL:
                        cs["connectCampaignId"] = None
                        cs["segmentArn"] = None
                        cs["segmentName"] = None
                        cs["status"] = "queued"
                        cs["reconcileRetries"] = 0
                    else:
                        cs["status"] = "running"
                        logger.info(
                            "_dispatch_ready_campaigns: recovered %s from creating → running (Connect state: %s)",
                            existing_id,
                            state,
                        )
                except Exception as exc:
                    logger.warning(
                        "_dispatch_ready_campaigns: cannot check Connect state for %s: %s — resetting to queued",
                        existing_id,
                        exc,
                    )
                    cs["connectCampaignId"] = None
                    cs["segmentArn"] = None
                    cs["segmentName"] = None
                    cs["status"] = "queued"
                    cs["reconcileRetries"] = 0
            else:
                # No conn_id: the claim was made but Connect was never called.
                # Only reset if the claim is stale (> 5 min) — a fresh claim means a
                # concurrent force_start_campaign Lambda is still in progress and owns
                # this slot. Resetting it early would cause a duplicate Connect campaign.
                _creating_at = cs.get("creatingAt")
                _age_seconds = (
                    (_now_utc() - datetime.fromisoformat(_creating_at)).total_seconds()
                    if _creating_at
                    else 999
                )
                if _age_seconds > 300:
                    cs["status"] = "queued"
                    cs["reconcileRetries"] = 0
                else:
                    logger.info(
                        "_dispatch_ready_campaigns: skipping fresh creating claim for %s "
                        "(age=%.0fs) — concurrent force_start likely active",
                        cs.get("campaignId"),
                        _age_seconds,
                    )

    newly_ready: list[int] = []
    # Phase 2 — Identify
    for ci, campaign in enumerate(bucket.get("campaigns", [])):
        if ci in stalled:
            continue
        cs = bucket_state["campaignStates"][ci]
        if cs["status"] != "queued":
            continue

        depends_on = campaign.get("dependsOn", [])

        if not depends_on:
            is_parallel = bucket.get("parallel", False)
            if (
                bucket_index == 0
                or is_parallel
                or _bucket_completed(run, bucket_index - 1)
            ):
                newly_ready.append(ci)
        else:
            parent_states = [_find_campaign_state(run, cid) for cid in depends_on]

            # Start when all parents are terminal — any outcome (completed, cancelled,
            # error, expired) unblocks the dependent. Dependents always attempt to run;
            # they will skip/error on their own merits if needed.
            if all(
                s and s["status"] in _CAMPAIGN_TERMINAL_STATUSES for s in parent_states
            ):
                newly_ready.append(ci)

    if not newly_ready:
        return False

    # Phase 3 — Claim: save BEFORE Connect API so a failed save leaves no orphan campaigns
    _claim_ts = _now_iso()
    for ci in newly_ready:
        bucket_state["campaignStates"][ci]["status"] = "creating"
        bucket_state["campaignStates"][ci]["creatingAt"] = _claim_ts
    save_run(run)  # raises ConcurrentWriteError on conflict → Connect never called

    # Phase 4 — Start
    for ci in newly_ready:
        cs = bucket_state["campaignStates"][ci]
        cs["status"] = "queued"  # _start_one_campaign expects "queued" as entry state
        _start_one_campaign(run, plan, bucket_index, ci)

    # Phase 5 — Confirm: persist connectCampaignId and final statuses from Phase 4
    if newly_ready:
        save_run(run)

    # A campaign left in "queued" after Phase 4 didn't actually advance — e.g.
    # _start_one_campaign hit _RedisRebuildingError and reverted it to retry on
    # the NEXT external tick. Mark it stalled so THIS call's Phase 2 (on the next
    # `while changed:` iteration) skips it — otherwise a genuinely-progressing
    # dependency chain elsewhere in the bucket keeps changed=True, and the stalled
    # campaign gets re-identified as newly_ready and retried against Redis on every
    # iteration for as long as the chain keeps the loop alive, with no backoff.
    # Reporting changed=True unconditionally here (the original bug) busy-looped
    # even a single stalled campaign with no chain involved: ~90 retries / 19s in
    # one invocation, exhausting reserved concurrency.
    made_progress = False
    for ci in newly_ready:
        cs = bucket_state["campaignStates"][ci]
        if cs["status"] == "queued":
            stalled.add(ci)
            # _start_one_campaign already logs why (Redis rebuilding / empty segment
            # retry) — this metric is for alerting: those paths only warn today, so
            # a long Redis outage would otherwise go unnoticed until an operator goes
            # looking. One CW alarm on this metric covers all revert-to-queued causes.
            _emit_dispatch_stalled_metric(cs.get("campaignId", "unknown"))
        else:
            made_progress = True
    return made_progress


def _find_campaign_state(run: dict, campaign_id: str) -> dict | None:
    """Scan all bucket states to find the campaign state for a given campaign id."""
    for bs in run["bucketStates"]:
        for cs in bs.get("campaignStates", []):
            if cs.get("campaignId") == campaign_id:
                return cs
    return None


def _bucket_has_only_legitimate_waits(
    run: dict, plan: dict, bucket_state: dict
) -> bool:
    """True if every non-terminal campaign in this bucket is "queued" and blocked
    on a dependency (same-bucket or cross-bucket) that has not reached a terminal
    state yet — i.e. correctly waiting, not stalled by a crashed tick.

    Used by prestart_check's no-active-campaign detector (audit follow-up,
    2026-08-21) to avoid alarming on plan 6203a0b5's exact shape: a bucket whose
    only campaign depends on another bucket's still-actively-dialing campaign.
    Without this, a bucket that's correctly waiting on a legitimately long-running
    upstream campaign pages every single minute for as long as that wait lasts.

    Also covers a second legitimate-wait shape (root-caused 2026-08-27, from
    vip-plans-no-active-campaign-sustained false positives on plan
    1a29f025's frequent re-triggers): a campaign mid-retry in
    _start_one_campaign's _EmptySegmentError handler, which reverts status to
    "queued" and defers to a later tick (see reconcileRetries in
    _reconcile_bucket). That's normal segment-not-populated-yet retry, not a
    crashed tick — the two are indistinguishable by status alone, so this uses
    reconcileRetries > 0 as the signal. Bounded by reconcile_retry_limit
    (default 5) before the campaign is cancelled to a terminal status, and by
    StuckRun (4h) as the ultimate backstop either way.

    reconcileRetries is otherwise sticky (only the empty-segment retry cycle
    itself increments or clears it on success/exhaustion) — every OTHER path
    that reverts a campaign to "queued"/"error" for unrelated reasons
    explicitly resets it to 0 as part of that revert, specifically so a stale
    value from a PRIOR, already-finished retry cycle can never masquerade as
    "currently mid-retry" here (root-caused 2026-08-27, adversarial code
    review — this field's presence used to be treated as sufficient on its
    own, which it is not unless every revert-to-queued path is disciplined
    about clearing it). As of the second adversarial review round
    (2026-08-27) the known reset sites are: _activate_warming_bucket's
    pre-warm-failure recovery; force_start_campaign's own claim (Phase 1) and
    its warming-bucket sibling cleanup; _reset_cascade_cancelled_children;
    _start_one_campaign's ClientError throttle/quota-exceeded revert; and all
    3 reset statements across _dispatch_ready_campaigns' Phase-1 "creating"
    recovery (Connect-terminal-state, exception-while-polling, and
    stale->300s claim). Treat this list as best-effort, not a guarantee — the
    first round of this same fix already missed 2 of these sites, so a
    revert-to-queued path found elsewhere in the future should be assumed
    unreset until verified.
    """
    plan = run.get("planSnapshot") or plan
    bucket_def = next(
        (b for b in plan.get("buckets", []) if b.get("id") == bucket_state.get("bucketId")),
        None,
    )
    campaigns_by_id = {c["id"]: c for c in (bucket_def or {}).get("campaigns", [])}

    for cs in bucket_state.get("campaignStates", []):
        if cs.get("status") in _CAMPAIGN_TERMINAL_STATUSES:
            continue
        if cs.get("status") != "queued":
            return False  # creating/warming/running would already be "active"; anything
            # else here (e.g. an unexpected status) isn't a recognized legitimate wait
        if cs.get("reconcileRetries"):
            continue  # mid-retry on an empty/not-yet-populated segment — legitimate
        campaign_def = campaigns_by_id.get(cs.get("campaignId"))
        depends_on = (campaign_def or {}).get("dependsOn") or []
        if not depends_on:
            return False  # nothing to wait on — should already have been dispatched
        parent_states = [_find_campaign_state(run, cid) for cid in depends_on]
        if all(s and s["status"] in _CAMPAIGN_TERMINAL_STATUSES for s in parent_states):
            return False  # every dependency is done — this campaign should be running by now
    return True


def _all_campaigns_terminal(run: dict, bucket_index: int) -> bool:
    bucket_state = run["bucketStates"][bucket_index]
    return all(
        cs["status"] in _CAMPAIGN_TERMINAL_STATUSES
        for cs in bucket_state["campaignStates"]
    )


def _bucket_completed(run: dict, bucket_index: int) -> bool:
    if bucket_index < 0 or bucket_index >= len(run["bucketStates"]):
        return True
    return run["bucketStates"][bucket_index]["status"] == "completed"


def _next_bucket_warming(run: dict, current_index: int) -> bool:
    next_index = current_index + 1
    if next_index >= len(run["bucketStates"]):
        return False
    return run["bucketStates"][next_index]["status"] in ("warming", "running")


# ── Campaign start ────────────────────────────────────────────────────────────


def _start_one_campaign(
    run: dict, plan: dict, bucket_index: int, campaign_index: int
) -> None:
    """Start a single campaign (creating segment + Connect campaign if not already warmed)."""
    bucket = plan["buckets"][bucket_index]
    campaigns = bucket.get("campaigns", [])
    campaign = campaigns[campaign_index]
    cs = run["bucketStates"][bucket_index]["campaignStates"][campaign_index]
    now = _now_utc()
    now_iso = now.isoformat()

    cs["startedAt"] = now_iso

    # ── Branded path — bypass Connect V2 entirely ─────────────────────────────
    if _is_branded(campaign):
        cfg = campaign.get("campaignConfig", {})
        queue_arn = cfg.get("queueArn", "")
        # Deterministic per-run UUID — unique per run so queue + metrics never
        # mix across runs or concurrent plans. Deterministic so re-invocations
        # within the same run don't seed duplicate queue partitions.
        campaign_id = str(
            uuid.uuid5(
                uuid.UUID("c5a6d9e3-3456-7890-1234-f7a8b9c0d1e2"),
                f"{run['planId']}#{run['runId']}#{bucket_index}#{campaign_index}",
            )
        )
        # Set these early so abort/stop paths can find them even if seeder fails mid-flight
        cs["brandedCampaignId"] = campaign_id
        cs["queueArn"] = queue_arn

        # Guard: fail fast before claiming the distributed lock
        if not _ACTIVE_BRANDED_CAMPAIGNS_TABLE or not _CAMPAIGN_QUEUE_TABLE_BRANDED:
            logger.error(
                "_start_one_campaign[%d/%d]: branded env vars not configured",
                bucket_index, campaign_index,
            )
            cs["status"] = "error"
            cs["exitReason"] = REASON_ERROR
            cs["errorDetail"] = "missing_env_vars"
            cs["completedAt"] = now_iso
            return

        # ── Step 1: Claim the slot via atomic put_item — distributed lock ────────
        # This MUST happen before _invoke_seeder. A ConditionalCheckFailedException
        # here means a concurrent invocation already registered this campaign_id and
        # is in the process of seeding. We return immediately to avoid a duplicate
        # seed — which would double the queue and double-dial every contact.
        bucket_end_epoch = int((now + timedelta(hours=24)).timestamp())
        try:
            _get_ddb_client().put_item(
                TableName=_ACTIVE_BRANDED_CAMPAIGNS_TABLE,
                Item={
                    "pk":            {"S": f"QUEUE#{queue_arn}"},
                    "sk":            {"S": f"CAMPAIGN#{campaign_id}"},
                    "queueArn":      {"S": queue_arn},
                    "campaignId":    {"S": campaign_id},
                    "planId":        {"S": run["planId"]},
                    "runId":         {"S": run["runId"]},
                    "contactFlowId": {"S": cfg["contactFlowId"]},
                    "sourcePhone":   {"S": cfg.get("sourcePhone") or cfg.get("sourcePhoneNumber", "")},
                    "priority":      {"N": str(campaign_index)},
                    "createdAt":     {"S": now_iso},
                    "ttl":           {"N": str(bucket_end_epoch + 1800)},
                },
                ConditionExpression="attribute_not_exists(pk)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                # Concurrent invocation already won the lock and is seeding.
                # Mark our state as running (the winner is handling it) and bail.
                logger.warning(
                    "_start_one_campaign[%d/%d]: campaign %s already registered — "
                    "concurrent start detected, skipping duplicate seed",
                    bucket_index, campaign_index, campaign_id,
                )
                cs["status"] = "running"
                return
            logger.error(
                "_start_one_campaign[%d/%d]: branded DDB lock failed: %s",
                bucket_index, campaign_index, type(exc).__name__,
            )
            _emit_branded_metric("BrandedStartError")
            cs["status"] = "error"
            cs["exitReason"] = REASON_ERROR
            cs["errorDetail"] = type(exc).__name__
            cs["completedAt"] = now_iso
            return
        except Exception as exc:
            logger.error(
                "_start_one_campaign[%d/%d]: branded DDB lock failed: %s",
                bucket_index, campaign_index, type(exc).__name__,
            )
            _emit_branded_metric("BrandedStartError")
            cs["status"] = "error"
            cs["exitReason"] = REASON_ERROR
            cs["errorDetail"] = type(exc).__name__
            cs["completedAt"] = now_iso
            return

        # ── Step 2: We hold the lock — seed the queue ────────────────────────────
        # VipActiveBrandedCampaigns record now exists. On any failure below,
        # _stop_branded_campaign cleans up both the record and the queue.
        try:
            pinned_arn = campaign.get("pinnedSegmentArn")
            if pinned_arn:
                seg_name = pinned_arn.rsplit("/", 1)[-1]
            else:
                seg_name, _ = _create_segment(bucket, campaign)
            seeded = _invoke_seeder(
                campaign_id=campaign_id,
                segment_name=seg_name,
                contact_flow_id=cfg["contactFlowId"],
                source_phone=cfg.get("sourcePhone") or cfg.get("sourcePhoneNumber", ""),
            )
        except Exception as exc:
            logger.error(
                "_start_one_campaign[%d/%d]: branded seeder failed: %s",
                bucket_index, campaign_index, type(exc).__name__,
            )
            _emit_branded_metric("BrandedSeederError")
            # VipActiveBrandedCampaigns record exists (put_item succeeded above),
            # so use _stop_branded_campaign to clean up both record and queue.
            _stop_branded_campaign(cs)
            cs["status"] = "error"
            cs["exitReason"] = REASON_ERROR
            cs["errorDetail"] = type(exc).__name__
            cs["completedAt"] = now_iso
            return

        if seeded == 0:
            # Empty segment — release the lock and mark complete
            _stop_branded_campaign(cs)
            cs["status"] = "completed"
            cs["exitReason"] = "empty_segment"
            cs["completedAt"] = now_iso
            return

        _emit_branded_metric("BrandedCampaignStarted")
        _write_branded_run_start(
            plan_id=run["planId"],
            run=run,
            cs=cs,
            cfg=cfg,
            seg_name=seg_name,
            seg_arn=pinned_arn or "",
            seeded=seeded,
        )
        # brandedCampaignId and queueArn already set early (before try/except)
        cs["connectCampaignId"] = None
        cs["status"] = "running"
        return
    # ── End branded path ──────────────────────────────────────────────────────

    # ── SMS path — EUM SMS bulk delivery via Lambda + SQS ────────────────────
    if _is_sms(campaign):
        cfg = campaign.get("campaignConfig", {})
        # Deterministic smsCampaignId — same key on re-invocation prevents duplicate
        # sender Lambda calls from generating orphaned VipSmsCampaignQueue items.
        sms_campaign_id = str(
            uuid.uuid5(
                uuid.UUID("a3e4b7c1-1234-5678-9012-d5e6f7a8b9c0"),
                f"{run['planId']}#{run['runId']}#{bucket_index}#{campaign_index}",
            )
        )
        cs["smsCampaignId"] = sms_campaign_id
        # Store run key in cs so _stop/_complete_sms_campaign can update DDB without a scan
        cs["_smsRunsPlanId"] = run["planId"]
        cs["_smsRunsSk"] = f"{run['runId']}#{sms_campaign_id}"
        cs["status"] = "running"
        cs["startedAt"] = now_iso
        try:
            pinned_arn = campaign.get("pinnedSegmentArn")
            if pinned_arn:
                seg_name = pinned_arn.rsplit("/", 1)[-1]
                seg_arn = pinned_arn
            else:
                seg_name, seg_arn = _create_segment(bucket, campaign)
            cs["segmentName"] = seg_name
            cs["segmentArn"] = seg_arn
            _invoke_sms_sender(
                campaignId=sms_campaign_id,
                planId=run["planId"],
                runId=run["runId"],
                planName=plan.get("name", ""),
                segmentArn=seg_arn,
                segmentName=seg_name,
                messageTemplate=cfg.get("smsMessageTemplate", ""),
                originationNumberArn=cfg.get("smsOriginationNumberArn", ""),
                originationNumber=cfg.get("smsOriginationNumber", ""),
            )
        except Exception as exc:
            logger.error(
                "_start_one_campaign[%d/%d]: SMS sender failed: %s",
                bucket_index,
                campaign_index,
                type(exc).__name__,
            )
            cs["status"] = "error"
            cs["exitReason"] = REASON_ERROR
            cs["errorDetail"] = type(exc).__name__
            cs["completedAt"] = now_iso
        return
    # ── End SMS path ──────────────────────────────────────────────────────────

    _check_native_queue_collision(bucket, campaign, run["planId"], run["runId"])

    if cs.get("connectCampaignId"):
        if cs.get("warmupStarted"):
            # StartCampaign already called during warmup — campaign is Running in Connect.
            cs["status"] = "running"
            cs.pop("warmupStarted", None)
            return
        # Pre-warmed but not yet started — refresh schedule to avoid "start time has already passed", then start.
        try:
            from vip_shared.infrastructure.persistence.outbound_campaigns_client import (
                build as build_oc,
            )

            oc = build_oc()
            run_type = campaign.get("run_type", "full")
            new_end_time = _campaign_end_time(now, campaign, run_type)
            new_start_time = (now + timedelta(seconds=60)).isoformat()
            oc.update_campaign_schedule(
                cs["connectCampaignId"],
                {
                    "startTime": new_start_time,
                    "endTime": new_end_time,
                },
            )
            oc.start_campaign(cs["connectCampaignId"])
            cs["status"] = "running"
            return
        except Exception as exc:
            if (
                "start time has already passed" in str(exc).lower()
                or "already passed" in str(exc).lower()
            ):
                # The pre-warmed campaign's start_time window expired before the plan ran.
                # Delete the stale campaign and fall through to fresh start — the existing
                # CP segment will be reused via the "already exists" recovery in _create_segment.
                logger.warning(
                    "_start_one_campaign[%d/%d]: pre-warmed campaign %s start time passed — recreating",
                    bucket_index,
                    campaign_index,
                    cs["connectCampaignId"],
                )
                _safe_stop_campaign(cs["connectCampaignId"])
                _safe_delete_campaign(cs["connectCampaignId"])
                cs["connectCampaignId"] = None
                # Fall through to fresh start below
            else:
                logger.error(
                    "_start_one_campaign: start warmed campaign failed: %s", exc
                )
                cs["status"] = "error"
                cs["exitReason"] = REASON_CREATION_FAILED
                cs["errorDetail"] = str(exc)
                cs["completedAt"] = now_iso
                _record_plan_event(
                    run,
                    "creation_failed",
                    {
                        "bucketIndex": bucket_index,
                        "campaignIndex": campaign_index,
                        "error": str(exc),
                    },
                )
                return

    # Fresh start: create segment → create Connect campaign → start
    pinned_segment_arn = campaign.get("pinnedSegmentArn")

    if pinned_segment_arn:
        # Operator-pinned segment: skip Redis lookup and segment auto-creation entirely.
        # Used for testing with hand-crafted CP segments.
        segment_arn = pinned_segment_arn
        segment_name = pinned_segment_arn.rsplit("/", 1)[-1]
        logger.info(
            "_start_one_campaign[%d/%d]: using pinned segment %s",
            bucket_index,
            campaign_index,
            segment_name,
        )
    else:
        reconcile_retry_limit = int(bucket.get("reconcileRetryLimit", 5))
        on_exhausted = bucket.get("onReconcileExhausted", "continue")

        segment_name = segment_arn = None
        last_exc: Exception | None = None
        for attempt in range(reconcile_retry_limit + 1):
            try:
                segment_name, segment_arn = _create_segment(bucket, campaign)
                last_exc = None
                break
            except _RedisRebuildingError as exc:
                # Redis is mid-rebuild — transient. Revert to queued so the next tick retries.
                cs["status"] = "queued"
                _slog.warn(
                    "start_one_campaign_redis_rebuilding",
                    plan_id=run["planId"],
                    run_id=run["runId"],
                    bucket_index=bucket_index,
                    campaign_index=campaign_index,
                    error=str(exc),
                )
                return
            except _EmptySegmentError as exc:
                # Redis may still be partially rebuilding: other-state leads arrive first,
                # making is_ready() pass while the target state hasn't loaded yet.
                # Retry across ticks (not within the same tick) to avoid false permanence.
                empty_retries = cs.get("reconcileRetries") or 0
                if empty_retries < reconcile_retry_limit:
                    cs["status"] = "queued"
                    cs["reconcileRetries"] = empty_retries + 1
                    _record_plan_event(
                        run,
                        "reconcile_retry",
                        {
                            "bucketIndex": bucket_index,
                            "campaignIndex": campaign_index,
                            "retry": empty_retries + 1,
                            "retryLimit": reconcile_retry_limit,
                        },
                    )
                    _slog.warn(
                        "start_one_campaign_empty_segment_retry",
                        plan_id=run["planId"],
                        run_id=run["runId"],
                        bucket_index=bucket_index,
                        campaign_index=campaign_index,
                        retry=empty_retries + 1,
                        retry_limit=reconcile_retry_limit,
                        error=str(exc),
                    )
                    return
                last_exc = exc
                _slog.info(
                    "start_one_campaign_empty_segment_cancelled",
                    plan_id=run["planId"],
                    run_id=run["runId"],
                    bucket_index=bucket_index,
                    campaign_index=campaign_index,
                    retries_exhausted=empty_retries,
                    error=str(exc),
                )
                break
            except Exception as exc:
                last_exc = exc
                if attempt < reconcile_retry_limit:
                    _slog.warn(
                        "start_one_campaign_segment_attempt_failed",
                        plan_id=run["planId"],
                        run_id=run["runId"],
                        bucket_index=bucket_index,
                        campaign_index=campaign_index,
                        attempt=attempt + 1,
                        max_attempts=reconcile_retry_limit + 1,
                        error=str(exc),
                    )

        if last_exc is not None:
            if isinstance(last_exc, _EmptySegmentError):
                # Final check: if Redis just dropped to LLEN=0 between our last retry
                # and now, we were hitting a partial-rebuild window, not genuine emptiness.
                # Reset retries so the next tick gives the rebuild a fresh window.
                if not _check_redis_ready():
                    cs["status"] = "queued"
                    cs["reconcileRetries"] = 0
                    _slog.warn(
                        "start_one_campaign_redis_not_ready_reset",
                        plan_id=run["planId"],
                        run_id=run["runId"],
                        bucket_index=bucket_index,
                        campaign_index=campaign_index,
                        retries_exhausted=reconcile_retry_limit,
                    )
                    return
                cs["status"] = "cancelled"
                cs["exitReason"] = REASON_SKIPPED_EMPTY
                cs["errorDetail"] = str(last_exc)
                cs["completedAt"] = now_iso
                return
            reason = (
                REASON_RECONCILE_FAILED
                if on_exhausted == "fail"
                else REASON_CREATION_FAILED
            )
            cs["status"] = "error"
            cs["exitReason"] = reason
            cs["errorDetail"] = (
                f"Segment creation failed after {reconcile_retry_limit + 1} attempts: {last_exc}"
            )
            cs["completedAt"] = now_iso
            return

    cs["segmentName"] = segment_name
    cs["segmentArn"] = segment_arn

    # Phase 2: create + start Connect campaign
    try:
        connect_id, campaign_name = _create_and_start_campaign(
            bucket, campaign, segment_arn, segment_name, now
        )
        cs["connectCampaignId"] = connect_id
        # Mid-flight save: persist {creating, connectCampaignId} so Phase-1 recovery in
        # _dispatch_ready_campaigns can poll Connect and restore to "running" — no duplicate
        # campaign is ever created even if Lambda crashes before Phase-5 confirms.
        # Retry up to 3 times on ConcurrentWriteError: the typical cause is a concurrent
        # tick for a different bucket — bumping the version resolves it without losing any
        # in-memory Phase-4 state.  If all retries fail, Phase-5 outer save is the last line
        # of defence; if that also fails, {creating, null} in DDB triggers a new campaign
        # after 5 min (S10-D threshold) — this is the S10-F residual risk.
        cs["status"] = "creating"
        for _mf_attempt in range(3):
            try:
                save_run(run)
                break
            except ConcurrentWriteError:
                if _mf_attempt == 2:
                    logger.warning(
                        "_start_one_campaign[%d/%d]: mid-flight save failed after 3 attempts — "
                        "outer Phase-5 will confirm; orphan possible if Phase-5 also fails (campaign=%s)",
                        bucket_index,
                        campaign_index,
                        connect_id,
                    )
                    break
                logger.info(
                    "_start_one_campaign[%d/%d]: mid-flight save attempt %d — refreshing version",
                    bucket_index,
                    campaign_index,
                    _mf_attempt + 1,
                )
                fresh = get_run(run["planId"], run["runId"])
                if not fresh:
                    break
                run["_version"] = fresh["_version"]
            except Exception as _mid_exc:
                logger.warning(
                    "_start_one_campaign[%d/%d]: mid-flight save failed — outer save will confirm: %s",
                    bucket_index,
                    campaign_index,
                    _mid_exc,
                )
                break
        cs["status"] = "running"
    except _EmptySegmentError:
        logger.info(
            "_start_one_campaign[%d/%d]: segment empty after creation",
            bucket_index,
            campaign_index,
        )
        if not pinned_segment_arn:
            _safe_delete_segment(segment_name)
        cs["status"] = "cancelled"
        cs["exitReason"] = REASON_SKIPPED_EMPTY
        cs["completedAt"] = now_iso
    except _CutoffTooCloseError as exc:
        logger.info(
            "_start_one_campaign[%d/%d]: skipping — %s",
            bucket_index,
            campaign_index,
            exc,
        )
        if not pinned_segment_arn and segment_name:
            _safe_delete_segment(segment_name)
        cs["status"] = "expired"
        cs["exitReason"] = "cutoff_too_close"
        cs["completedAt"] = now_iso
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in ("ThrottlingException", "ServiceQuotaExceededException"):
            # Transient — Connect API is throttled or at capacity. Revert to queued
            # so the next tick retries automatically. Keep the segment (it was already
            # created) so the retry can reuse it via _create_segment's "already exists"
            # handling rather than re-creating it from scratch.
            logger.warning(
                "_start_one_campaign[%d/%d]: transient Connect error (%s) — reverting to queued: %s",
                bucket_index,
                campaign_index,
                code,
                exc,
            )
            cs["status"] = "queued"
            cs.pop("connectCampaignId", None)
            # See BD-020 — reconcileRetries is sticky and must not survive an
            # unrelated queued revert (this throttle/quota path was missed by
            # BD-020; root-caused 2026-08-27, second adversarial review round).
            cs["reconcileRetries"] = 0
            _notify_sns(
                subject=f"[VIP Plans] Campaign throttled/quota exceeded: {cs.get('name', cs['campaignId'])}",
                detail=(
                    f"Campaign '{cs.get('name', '')}' hit a transient Connect error ({code}).\n"
                    f"It will be retried on the next tick automatically.\n"
                    f"campaignId={cs['campaignId']}, bucket={bucket_index}"
                ),
                attributes={
                    "alertType": "throttle_or_quota",
                    "campaignId": cs["campaignId"],
                },
            )
        else:
            logger.error(
                "_start_one_campaign[%d/%d]: campaign creation failed (%s): %s",
                bucket_index,
                campaign_index,
                code,
                exc,
            )
            if not pinned_segment_arn and segment_name:
                _safe_delete_segment(segment_name)
            cs["status"] = "error"
            cs["exitReason"] = REASON_CREATION_FAILED
            cs["errorDetail"] = f"Campaign creation failed: {exc}"
            cs["completedAt"] = now_iso
            _record_plan_event(
                run,
                "creation_failed",
                {
                    "bucketIndex": bucket_index,
                    "campaignIndex": campaign_index,
                    "error": str(exc),
                },
            )
            _notify_sns(
                subject=f"[VIP Plans] Campaign creation FAILED: {cs.get('name', cs['campaignId'])}",
                detail=(
                    f"Campaign '{cs.get('name', '')}' failed to start (Connect error {code}).\n"
                    f"campaignId={cs['campaignId']}, bucket={bucket_index}\nError: {exc}"
                ),
                attributes={
                    "alertType": "campaign_creation_failed",
                    "campaignId": cs["campaignId"],
                },
            )
    except Exception as exc:
        logger.error(
            "_start_one_campaign[%d/%d]: campaign creation failed: %s",
            bucket_index,
            campaign_index,
            exc,
        )
        if not pinned_segment_arn and segment_name:
            _safe_delete_segment(segment_name)
        cs["status"] = "error"
        cs["exitReason"] = REASON_CREATION_FAILED
        cs["errorDetail"] = f"Campaign creation failed: {exc}"
        cs["completedAt"] = now_iso
        _record_plan_event(
            run,
            "creation_failed",
            {
                "bucketIndex": bucket_index,
                "campaignIndex": campaign_index,
                "error": str(exc),
            },
        )
        _notify_sns(
            subject=f"[VIP Plans] Campaign creation FAILED: {cs.get('name', cs['campaignId'])}",
            detail=(
                f"Campaign '{cs.get('name', '')}' failed to start.\n"
                f"campaignId={cs['campaignId']}, bucket={bucket_index}\nError: {exc}"
            ),
            attributes={
                "alertType": "campaign_creation_failed",
                "campaignId": cs["campaignId"],
            },
        )


def _create_campaign_only(
    bucket: dict,
    campaign: dict,
    run: dict,
) -> tuple[str, str, str]:
    """Create Connect campaign without starting it (for pre-start warming).

    Returns (connectCampaignId, segmentName, segmentArn).
    """
    from vip_shared.infrastructure.persistence.outbound_campaigns_client import (
        build as build_oc,
    )

    now = _now_utc()
    pinned_arn = campaign.get("pinnedSegmentArn")
    if pinned_arn:
        segment_arn = pinned_arn
        segment_name = pinned_arn.rsplit("/", 1)[-1]
    else:
        segment_name, segment_arn = _create_segment(bucket, campaign)

    start_dt = now + timedelta(minutes=6)
    start_time = start_dt.isoformat()
    run_type = campaign.get("run_type", "full")
    end_time = _campaign_end_time(start_dt, campaign, run_type)

    # Guard: Connect rejects campaigns where startTime >= endTime.
    # This can happen in the last few minutes before the daily cutoff when the 6-min
    # warmup window pushes startTime past the end-time cap.
    end_dt = datetime.fromisoformat(end_time)
    if start_dt >= end_dt:
        raise _CutoffTooCloseError(
            f"Campaign start {start_time} >= end {end_time} — too close to daily cutoff, skipping"
        )

    campaign_name = segment_name if pinned_arn else build_segment_name(bucket, campaign)

    instance_arn = (
        f"arn:aws:connect:us-east-1:{_account_id()}:instance/{CONNECT_INSTANCE_ID}"
    )
    profiles_domain_arn = (
        f"arn:aws:profile:us-east-1:{_account_id()}:domains/{PROFILES_DOMAIN_NAME}"
    )

    delivery_type = campaign.get("deliveryType", "campaign")
    state_codes = campaign.get("states") or bucket.get("segmentFilters", {}).get(
        "state", []
    )

    if delivery_type == "journey":
        live_flow_arn = resolve_journey_flow_arn(CONNECT_INSTANCE_ID)
    else:
        live_flow_arn = resolve_campaign_flow_arn(state_codes, CONNECT_INSTANCE_ID)

    # Build a merged bucket view with the campaign's segment filters
    merged_bucket = dict(bucket)
    if "segmentFilters" not in merged_bucket or not merged_bucket.get("segmentFilters"):
        merged_bucket["segmentFilters"] = campaign_to_segment_filters(campaign)

    params = build_campaign_params(
        merged_bucket,
        segment_arn=segment_arn,
        connect_instance_id=CONNECT_INSTANCE_ID,
        profiles_domain_arn=profiles_domain_arn,
        instance_arn=instance_arn,
        start_time=start_time,
        end_time=end_time,
        campaign_name=campaign_name,
        campaign_flow_arn_override=live_flow_arn,
        campaign=campaign,
        delivery_type=delivery_type,
    )

    if "connectCampaignFlowArn" not in params:
        if delivery_type == "journey":
            raise ValueError(
                f"Journey flow '{_JOURNEY_FLOW_NAME}' not found in Connect instance — "
                "create a CAMPAIGN-type flow with that exact name"
            )
        raise ValueError(
            f"No campaign flow ARN available for states {state_codes!r} — "
            "add a CAMPAIGN-type flow named 'campaign-<STATE>' in Connect, "
            "or set campaignFlowArn in the campaign's campaignConfig"
        )

    oc = build_oc()
    created = oc.create_campaign(**params)
    campaign_id = created["id"]

    # Start immediately so Connect begins dialing at startTime without any delay at bucket activation.
    # warmup_started=False means _activate_warming_bucket will fall back to UpdateCampaignSchedule+Start.
    warmup_started = False
    try:
        oc.start_campaign(campaign_id)
        warmup_started = True
        logger.info(
            "_create_campaign_only: started campaign %s (startTime=%s)",
            campaign_id,
            start_time,
        )
    except Exception as exc:
        logger.warning(
            "_create_campaign_only: start_campaign failed for %s — will retry at activation: %s",
            campaign_id,
            exc,
        )

    return campaign_id, segment_name, segment_arn, warmup_started


def _poll_campaign_state(cs: dict) -> None:
    """Update a running campaign state from Connect."""
    state = _get_campaign_state(cs["connectCampaignId"])
    if state in _CONNECT_TERMINAL:
        exit_reason = _CONNECT_TERMINAL[state]
        if exit_reason == REASON_COMPLETED:
            cs["status"] = "completed"
        elif exit_reason == REASON_ERROR:
            cs["status"] = "error"
            cs["errorDetail"] = f"Connect campaign failed (state: {state})"
        else:
            cs["status"] = "cancelled"
        cs["exitReason"] = exit_reason
        cs["completedAt"] = _now_iso()
        if exit_reason == "connect_deleted":
            _notify_sns(
                subject=f"[VIP Plans] Campaign deleted externally: {cs.get('name', cs['campaignId'])}",
                detail=(
                    f"Campaign '{cs.get('name', '')}' (connectId={cs['connectCampaignId']}) "
                    f"was not found in Connect — it may have been deleted manually.\n"
                    f"campaignId={cs['campaignId']}"
                ),
                attributes={
                    "alertType": "connect_deleted",
                    "campaignId": cs["campaignId"],
                },
            )


# ── AWS operations ────────────────────────────────────────────────────────────


class _EmptySegmentError(Exception):
    pass


def _check_redis_ready() -> bool:
    """Return True if the Redis lead list has data (LLEN > 0), False if mid-rebuild.

    Called as a final pre-cancel check after _EmptySegmentError retries are
    exhausted.  If LLEN just dropped to 0 between our last retry and now, the
    campaign was failing due to a partial-rebuild window, not genuine emptiness —
    reset retries and requeue instead of cancelling.  Falls back to True on any
    exception so a connection error does not suppress a legitimate cancel.
    """
    try:
        from vip_shared.infrastructure.persistence.redis_lead_source import (
            build_from_env as build_redis,
        )

        return build_redis().is_ready()
    except Exception:
        return True  # assume ready → don't suppress cancel on unexpected errors


class _RedisRebuildingError(Exception):
    """Redis lead list is empty due to in-progress rebuild — transient, retry."""

    pass


class _CutoffTooCloseError(Exception):
    """start_time >= end_time because campaign creation is within 6 min of daily cutoff."""

    pass


_MAX_SEGMENT_MEMBERS = 3000  # Derived from AWS CP limits: 60 max attributes × 50 max values/attribute = 3,000


def _normalize_phone_e164(raw: str) -> str | None:
    """Normalize a raw CRM phone number to E.164 (+1XXXXXXXXXX for US numbers).

    Returns None for numbers that can't be normalized (missing, too short,
    invalid NANP area code, etc). Handles the common CRM patterns:
      10 digits              → +1XXXXXXXXXX
      11 digits starting w/1 → +1XXXXXXXXXX  (avoids double-prefix bug in sub Lambda)
      Already E.164 (+1...)  → unchanged
    A NANP area code (NPA) can never start with 0 or 1 — a 10-digit CRM value
    that does is truncated/malformed data (a digit was lost upstream), not a
    normalizable number. Blindly prepending '+1' to it produces an undialable
    number that bypasses segment_phones_excluded (which only fires on None).
    """
    if not raw:
        return None
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) == 10 and digits[0] not in "01":
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1") and digits[1] not in "01":
        return "+" + digits
    if raw.strip().startswith("+") and len(digits) >= 10:
        if len(digits) == 11 and digits.startswith("1") and digits[1] in "01":
            return None
        return raw.strip()
    return None


def _max_age_cutoff(max_age_minutes: Any, now: datetime | None = None) -> str | None:
    """Build the createdAt >= cutoff for the maxLeadAgeMinutes filter.

    Casts to int: campaigns read from run["planSnapshot"] come back from
    DynamoDB as decimal.Decimal (boto3 Table resource deserializes all Number
    attributes that way; only the plan-read path normalizes Decimal->int, the
    run-read path does not), and timedelta() raises TypeError on Decimal.
    A non-positive value (e.g. a negative minutes value slipping past upstream
    validation) is treated as "no filter" rather than producing a cutoff in
    the future, which would silently match zero leads.
    """
    if not max_age_minutes:
        return None
    minutes = int(max_age_minutes)
    if minutes <= 0:
        return None
    now = now or datetime.now(timezone.utc)
    return (
        (now - timedelta(minutes=minutes))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _create_segment(bucket: dict, campaign: dict | None = None) -> tuple[str, str]:
    from vip_shared.domain.entities.filter_rule import FilterOperator, FilterRule
    from vip_shared.domain.services.segment_groups_translator import (
        SegmentGroupsTranslator,
        matches_group,
    )
    from vip_shared.infrastructure.persistence.customer_profiles_client import (
        build_from_env as build_cp,
    )
    from vip_shared.infrastructure.persistence.redis_lead_source import (
        build_from_env as build_redis,
    )

    # Resolve effective segment filters
    if campaign is not None and not campaign.get("_legacyBucket"):
        filters = campaign_to_segment_filters(campaign)
    else:
        filters = bucket.get("segmentFilters", {})

    state_codes = filters.get("state", [])
    locations = locations_for_state_codes(state_codes)

    rules: list = []
    if locations:
        rules.append(
            FilterRule(
                field="location", operator=FilterOperator.IN, values=tuple(locations)
            )
        )

    available = filters.get("available") or "True"
    if available in ("True", "False"):
        rules.append(
            FilterRule(
                field="available", operator=FilterOperator.IN, values=(available,)
            )
        )

    all_groups = filters.get("groups", []) + filters.get("attempts", [])
    if all_groups:
        rules.append(
            FilterRule(
                field="groups", operator=FilterOperator.IN, values=tuple(all_groups)
            )
        )

    # Only include leads created within the last N minutes (10-35, step 5;
    # None = no age filter). createdAt is an ISO-8601 UTC string with
    # millisecond precision (e.g. "2026-08-28T14:30:00.123Z") straight from
    # the Redis producer — plain string comparison sorts identically to
    # chronological order as long as both sides use this exact format.
    cutoff = _max_age_cutoff(filters.get("maxLeadAgeMinutes"))
    if cutoff:
        rules.append(
            FilterRule(field="createdAt", operator=FilterOperator.GTE, values=(cutoff,))
        )

    redis_source = build_redis()
    if not redis_source.is_ready():
        raise _RedisRebuildingError(
            f"Redis lead list '{redis_source._list_key}' is empty — likely mid-rebuild"
        )

    # Preserve Redis iteration order (list + seen-set for dedup) so that when
    # the segment is truncated the most recently-added leads take priority.
    # Collect (id, normalized_phone, raw_phone) triples in one pass — raw_phone
    # retained only for excluded-lead logging; not used downstream.
    # Also scan every record's location field against the global known-locations
    # set; any unknown value triggers a CloudWatch metric so ops can add it to
    # VipLocationMapping before leads get silently dropped from segments.
    # Fault-tolerant: if the DynamoDB lookup fails, skip detection rather than
    # aborting segment creation (detection is non-critical telemetry).
    try:
        known_locs: frozenset[str] = all_known_locations()
    except Exception as _exc:
        logger.warning("all_known_locations fetch failed, skipping detection: %s", _exc)
        known_locs = frozenset()
    unknown_locs: set[str] = set()
    seen: set[str] = set()
    entries: list[tuple[str, str | None, str]] = []
    for record in redis_source.iter_records():
        loc_val = str(record.get("location", "")).strip()
        if loc_val and loc_val not in known_locs:
            unknown_locs.add(loc_val)
        if not rules or matches_group(record, rules, "ALL"):
            cid = str(record.get("customerid") or record.get("ID") or "").strip()
            if cid and cid not in seen:
                seen.add(cid)
                raw_phone = str(record.get("phone", "")).strip()
                entries.append((cid, _normalize_phone_e164(raw_phone), raw_phone))

    if unknown_locs:
        _slog.warn("unknown_locations_detected", locations=sorted(unknown_locs))
        try:
            loc_list = sorted(unknown_locs)
            cw = boto3.client("cloudwatch")
            # Per-location dimensional metrics (for CloudWatch console drill-down).
            for i in range(0, len(loc_list), 20):
                cw.put_metric_data(
                    Namespace="VipConnect/ProgressiveDialer",
                    MetricData=[
                        {
                            "MetricName": "UnknownLocation",
                            "Value": 1,
                            "Unit": "Count",
                            "Dimensions": [{"Name": "Location", "Value": loc}],
                        }
                        for loc in loc_list[i : i + 20]
                    ],
                )
            # Dimensionless total — this is what the CloudWatch alarm watches.
            # Dimensional and dimensionless metrics are separate time series in CW.
            cw.put_metric_data(
                Namespace="VipConnect/ProgressiveDialer",
                MetricData=[{
                    "MetricName": "UnknownLocation",
                    "Value": len(loc_list),
                    "Unit": "Count",
                }],
            )
        except Exception as exc:
            logger.warning("unknown_location metric emit failed: %s", type(exc).__name__)

    if not entries:
        raise _EmptySegmentError("No Redis records match campaign filters")

    total_matched = len(entries)
    if total_matched > _MAX_SEGMENT_MEMBERS:
        # Truncate to the AWS CP hard limit (60 attributes × 50 values = 3,000).
        # Takes the first _MAX_SEGMENT_MEMBERS records from Redis — which are the
        # most recently added leads since the pipeline prepends via LPUSH.
        _slog.warn(
            "segment_truncated",
            total_matched=total_matched,
            limit=_MAX_SEGMENT_MEMBERS,
            dropped=total_matched - _MAX_SEGMENT_MEMBERS,
        )
        entries = entries[:_MAX_SEGMENT_MEMBERS]

    # Log leads whose phone couldn't be normalized — they will not be dialed.
    # Log last-4 digits only; full phone number is PHI and must not appear in logs.
    excluded = [(cid, raw) for cid, phone, raw in entries if phone is None]
    if excluded:
        _slog.warn(
            "segment_phones_excluded",
            count=len(excluded),
            excluded=[
                {
                    "cid": cid,
                    "phone_last4": raw[-4:] if len(raw) >= 4 else "",
                    "reason": "empty" if not raw else "bad_format",
                }
                for cid, raw in excluded[:50]
            ],
        )

    # Build segment using phones instead of Lead IDs.
    # The seeder's _extract_phones_from_filter reads phones directly from the
    # segment definition — no BatchGetProfile needed. Using Lead IDs here caused
    # _EmptySegmentError because Lead UUIDs ≠ CP internal ProfileIds.
    phones_e164 = [phone for _, phone, _ in entries if phone]
    if not phones_e164:
        raise _EmptySegmentError("No Redis records with valid phone numbers match campaign filters")

    cp = build_cp()
    segment_name = build_segment_name(bucket, campaign)
    segment_groups = SegmentGroupsTranslator().phones_to_segment_groups(phones_e164)

    state_str = "/".join(state_codes) if state_codes else "all"
    display_name = (
        f"{campaign.get('name') or bucket.get('name', 'campaign')} — {state_str}"
    )

    try:
        resp = cp.create_segment_definition(
            name=segment_name,
            display_name=display_name[:255],
            segment_groups=segment_groups,
            description="Auto-created by Daily Plans",
            tags={"VipPlanBucket": "true", "VipSyncMode": "manual"},
        )
        return segment_name, resp["SegmentDefinitionArn"]
    except ClientError as exc:
        if "already exists" in str(exc):
            # Segment was created by a previous Lambda invocation that crashed before
            # persisting connectCampaignId. Reuse the existing definition — filters are
            # identical (same name encodes same state/attempt/minute/campaign id).
            logger.warning("_create_segment: %s already exists — reusing", segment_name)
            existing = cp.get_segment_definition(segment_name)
            return segment_name, existing["SegmentDefinitionArn"]
        raise


def _create_and_start_campaign(
    bucket: dict,
    campaign: dict | None,
    segment_arn: str,
    segment_name: str,
    now: datetime,
) -> tuple[str, str]:
    from vip_shared.infrastructure.persistence.outbound_campaigns_client import (
        build as build_oc,
    )

    oc = build_oc()
    start_dt = now + timedelta(minutes=6)
    start_time = start_dt.isoformat()

    # run_type → end_time override (full = daily cutoff; time_N = N min from now)
    run_type = (campaign or {}).get("run_type", "full")
    end_time = _campaign_end_time(
        now, campaign if isinstance(campaign, dict) else {}, run_type
    )

    # Guard: Connect rejects campaigns where startTime >= endTime.
    end_dt = datetime.fromisoformat(end_time)
    if start_dt >= end_dt:
        raise _CutoffTooCloseError(
            f"Campaign start {start_time} >= end {end_time} — too close to daily cutoff, skipping"
        )

    instance_arn = (
        f"arn:aws:connect:us-east-1:{_account_id()}:instance/{CONNECT_INSTANCE_ID}"
    )
    profiles_domain_arn = (
        f"arn:aws:profile:us-east-1:{_account_id()}:domains/{PROFILES_DOMAIN_NAME}"
    )

    delivery_type = (campaign or {}).get("deliveryType", "campaign")
    state_codes = (
        campaign.get("states", [])
        if campaign and not campaign.get("_legacyBucket")
        else bucket.get("segmentFilters", {}).get("state", [])
    )

    if delivery_type == "journey":
        live_flow_arn = resolve_journey_flow_arn(CONNECT_INSTANCE_ID)
        if live_flow_arn:
            logger.info("resolved journey flow %s", live_flow_arn)
        else:
            logger.warning(
                "journey flow '%s' not found in Connect", "Test-Journey-Flow"
            )
    else:
        live_flow_arn = resolve_campaign_flow_arn(state_codes, CONNECT_INSTANCE_ID)
        if live_flow_arn:
            logger.info(
                "resolved campaign flow %s for states %s", live_flow_arn, state_codes
            )
        else:
            logger.warning(
                "no campaign flow found for states %s, falling back to stored ARN",
                state_codes,
            )

    # Merge campaign filters into bucket for build_campaign_params
    merged_bucket = dict(bucket)
    if campaign and not campaign.get("_legacyBucket"):
        merged_bucket["segmentFilters"] = campaign_to_segment_filters(campaign)

    params = build_campaign_params(
        merged_bucket,
        segment_arn=segment_arn,
        connect_instance_id=CONNECT_INSTANCE_ID,
        profiles_domain_arn=profiles_domain_arn,
        instance_arn=instance_arn,
        start_time=start_time,
        end_time=end_time,
        campaign_name=segment_name,
        campaign_flow_arn_override=live_flow_arn,
        campaign=campaign,
        delivery_type=delivery_type,
    )

    if "connectCampaignFlowArn" not in params:
        if delivery_type == "journey":
            raise ValueError(
                f"Journey flow '{_JOURNEY_FLOW_NAME}' not found in Connect instance — "
                "create a CAMPAIGN-type flow with that exact name"
            )
        raise ValueError(
            f"No campaign flow ARN available for states {state_codes!r} — "
            "add a CAMPAIGN-type flow named 'campaign-<STATE>' in Connect, "
            "or set campaignFlowArn in the campaign's campaignConfig"
        )

    created = oc.create_campaign(**params)
    campaign_id = created["id"]
    oc.start_campaign(campaign_id)
    return campaign_id, segment_name


_PLAN_PERMISSION_STATEMENT_LIMIT = 12  # proactive cleanup threshold


def _cleanup_orphan_plan_permissions() -> None:
    """Remove vip-plan-* Lambda policy statements whose EventBridge rule no longer exists.

    Called proactively when the statement count nears the Lambda 20 KB policy limit.
    Prevents PolicyLengthExceededException from stale statements that accumulate
    when runs end without cleaning up their bucket-chain schedule.
    """
    import json as _json

    lam = boto3.client("lambda")
    ev = boto3.client("events")
    try:
        policy = _json.loads(lam.get_policy(FunctionName=LAMBDA_FUNCTION_ARN)["Policy"])
    except ClientError:
        return

    orphans = []
    for stmt in policy.get("Statement", []):
        sid = stmt.get("Sid", "")
        if not sid.startswith("vip-plan-"):
            continue
        try:
            ev.describe_rule(Name=sid)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                orphans.append(sid)

    for sid in orphans:
        try:
            lam.remove_permission(FunctionName=LAMBDA_FUNCTION_ARN, StatementId=sid)
            logger.info("Removed orphan Lambda permission: %s", sid)
        except ClientError as exc:
            logger.warning("Could not remove orphan permission %s: %s", sid, exc)


def _sweep_orphan_rules(
    events, prefix: str, protected: set[str]
) -> tuple[list[str], list[str]]:
    """Delete every EventBridge rule under `prefix` not in `protected`."""
    deleted: list[str] = []
    failed: list[str] = []
    kwargs: dict = {"NamePrefix": prefix}
    while True:
        resp = events.list_rules(**kwargs)
        for rule in resp.get("Rules", []):
            name = rule["Name"]
            if name in protected:
                continue
            try:
                _delete_schedule_safe(name)
                deleted.append(name)
            except Exception as exc:
                failed.append(name)
                logger.error("janitor: failed to delete orphan rule %s: %s", name, exc)
        token = resp.get("NextToken")
        if not token:
            break
        kwargs["NextToken"] = token
    return deleted, failed


def janitor_cleanup_orphan_schedules() -> dict:
    """Daily sweep: delete every vip-plan-*/vip-sched-* EventBridge rule (and its
    Lambda permission) that nothing currently references anymore.

    Fixes _cleanup_orphan_plan_permissions' blind spot (BD-013): that function
    only catches a permission whose rule is ALREADY gone — it does nothing for
    a rule that's still alive but nothing tracks anymore, which is exactly
    what an incomplete ConcurrentWriteError recovery can produce. This is the
    "daily janitor" this repo's own runbook (RUNBOOKS.md RB-001, "Prevention")
    documented as the fix but that was never actually built.

    vip-plan-* safety: does NOT decompose the truncated 8-char plan/run IDs
    embedded in a rule's name (vip-plan-{planId[:8]}-run-{runId[:8]}-b{bucketIndex})
    — those truncations can collide and are not safe to reverse uniquely. It
    instead collects the exact, full scheduleName strings currently recorded
    on every plan's LATEST run (only a run that's actually "running" needs
    its schedule to survive — a completed/aborted run's schedule is stale by
    definition, whether or not the janitor ever "sees" it), and deletes any
    vip-plan-* rule whose full name isn't in that set. Exact-string
    membership, no ambiguity.

    vip-sched-* (audit follow-up, 2026-08-21): update_plan's own cleanup only
    ran when a PATCH resent "trigger" in the request body — a PATCH that only
    set isTemplate=true left the rule alive forever with nothing else that
    ever revisits it (confirmed live: plan c63d695c-b99e-4885-808a-8eca91d08e8e).
    That call site is now fixed too, but this sweep is the backstop for
    anything that orphaned before the fix, or any future gap of the same
    shape. A vip-sched-* rule is protected if its owning plan is a
    non-template with a "time" trigger — computed via the exact same
    scheduler_manager._rule_name() the rule was created with, not by parsing
    the rule's name back into a plan_id.
    """
    from scheduler_manager import _rule_name

    protected_plan: set[str] = set()
    protected_sched: set[str] = set()
    for plan in list_plans():
        plan_id = plan.get("planId")
        if not plan_id:
            continue
        if not plan.get("isTemplate") and plan.get("trigger", {}).get("type") == "time":
            protected_sched.add(_rule_name(plan_id))
        try:
            latest = get_latest_run(plan_id)
        except Exception:
            continue
        if not latest or latest.get("status") != "running":
            continue
        for bucket_state in latest.get("bucketStates", []):
            sched = bucket_state.get("scheduleName")
            if sched:
                protected_plan.add(sched)

    events = boto3.client("events")
    deleted_plan, failed_plan = _sweep_orphan_rules(events, "vip-plan-", protected_plan)
    deleted_sched, failed_sched = _sweep_orphan_rules(
        events, "vip-sched-", protected_sched
    )
    deleted = deleted_plan + deleted_sched
    failed = failed_plan + failed_sched

    if deleted:
        logger.warning("janitor: deleted %d orphan rule(s): %s", len(deleted), deleted)
        _notify_sns(
            subject=f"[VIP Plans] Janitor cleaned up {len(deleted)} orphaned schedule(s)",
            detail=(
                f"Deleted {len(deleted)} EventBridge rule(s) (and their Lambda "
                f"permissions) that nothing currently references anymore — "
                f"{len(deleted_plan)} vip-plan-* (tick), {len(deleted_sched)} "
                f"vip-sched-* (time trigger):\n{deleted}\n\n"
                f"This is routine cleanup, not necessarily a problem — but if this "
                f"count is consistently large or growing, something is still "
                f"creating untracked schedules (see BD-013)."
            ),
            attributes={"alertType": "janitor_cleanup", "deletedCount": len(deleted)},
        )
    if failed:
        logger.error("janitor: failed to delete %d rule(s): %s", len(failed), failed)

    return {"deleted": deleted, "failed": failed}


def _schedule_tick(*, plan_id: str, run_id: str, bucket_index: int) -> str:
    import json

    rule_name = f"vip-plan-{plan_id[:8]}-run-{run_id[:8]}-b{bucket_index}"
    input_payload = json.dumps(
        {
            "action": "tick",
            "planId": plan_id,
            "runId": run_id,
            "bucketIndex": bucket_index,
        }
    )

    events = boto3.client("events")
    events.put_rule(
        Name=rule_name,
        ScheduleExpression="rate(1 minute)",
        State="ENABLED",
    )
    events.put_targets(
        Rule=rule_name,
        Targets=[{"Id": "lambda", "Arn": LAMBDA_FUNCTION_ARN, "Input": input_payload}],
    )

    rule_arn = f"arn:aws:events:us-east-1:{_account_id()}:rule/{rule_name}"
    lam = boto3.client("lambda")

    # Proactively clean up orphan vip-plan-* statements before adding a new one.
    # Prevents PolicyLengthExceededException when stale statements accumulate
    # (happens when runs complete without scheduleName saved in DDB).
    try:
        import json as _json
        policy = _json.loads(lam.get_policy(FunctionName=LAMBDA_FUNCTION_ARN)["Policy"])
        plan_stmt_count = sum(
            1 for s in policy.get("Statement", []) if s.get("Sid", "").startswith("vip-plan-")
        )
        if plan_stmt_count >= _PLAN_PERMISSION_STATEMENT_LIMIT:
            _cleanup_orphan_plan_permissions()
    except ClientError:
        pass

    try:
        lam.add_permission(
            FunctionName=LAMBDA_FUNCTION_ARN,
            StatementId=rule_name,
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=rule_arn,
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceConflictException":
            # put_rule/put_targets above already succeeded — a live rule now
            # exists. Every caller of this function used to swallow this
            # exact exception (log-and-continue) without ever deleting the
            # rule it just created, since the caller has no way to know its
            # name on the exception path. Roll it back HERE instead, so
            # _schedule_tick is atomic: it either fully succeeds, or leaves
            # nothing behind. Fixes the orphan mechanism found at 4 call
            # sites (force_start_campaign x2, _advance_bucket's rescue path,
            # _dispatch_cross_bucket_ready) without touching each of them.
            _delete_schedule_safe(rule_name)
            raise
    return rule_name


def _delete_bucket_schedule_safe(run: dict, bucket_index: int) -> None:
    """Delete the EventBridge rule for a specific bucket."""
    bucket_state = (
        run["bucketStates"][bucket_index]
        if bucket_index < len(run["bucketStates"])
        else {}
    )
    schedule_name = bucket_state.get("scheduleName") or run.get("scheduleName")
    _delete_schedule_safe(schedule_name)
    if bucket_state:
        bucket_state["scheduleName"] = None


def _delete_schedule_safe(schedule_name: str | None) -> None:
    if not schedule_name:
        return
    events = boto3.client("events")
    lambda_client = boto3.client("lambda")
    for call in (
        lambda: events.remove_targets(Rule=schedule_name, Ids=["lambda"]),
        lambda: events.delete_rule(Name=schedule_name),
        lambda: lambda_client.remove_permission(
            FunctionName=LAMBDA_FUNCTION_ARN, StatementId=schedule_name
        ),
    ):
        try:
            call()
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in ("ResourceNotFoundException", "NoSuchEntity"):
                logger.warning("_delete_schedule_safe %s: %s", schedule_name, exc)


def _get_campaign_state(campaign_id: str) -> str:
    from vip_shared.infrastructure.persistence.outbound_campaigns_client import (
        build as build_oc,
    )

    try:
        resp = build_oc().get_campaign_state(campaign_id)
        return resp.get("state", "Unknown")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("ResourceNotFoundException", "404"):
            return "Deleted"
        return "Unknown"


def _safe_stop_campaign(campaign_id: str) -> None:
    from vip_shared.infrastructure.persistence.outbound_campaigns_client import (
        build as build_oc,
    )

    try:
        state = _get_campaign_state(campaign_id)
        if state in ("Running", "Paused"):
            build_oc().stop_campaign(campaign_id)
    except Exception as exc:
        logger.warning("_safe_stop_campaign %s: %s", campaign_id, exc)


def _safe_delete_campaign(campaign_id: str) -> None:
    from vip_shared.infrastructure.persistence.outbound_campaigns_client import (
        build as build_oc,
    )

    try:
        state = _get_campaign_state(campaign_id)
        if state not in ("Stopped", "Failed", "Created", "Completed"):
            build_oc().stop_campaign(campaign_id)
        build_oc().delete_campaign(campaign_id)
    except Exception as exc:
        logger.warning("_safe_delete_campaign %s: %s", campaign_id, exc)


def _safe_delete_segment(segment_name: str) -> None:
    from vip_shared.infrastructure.persistence.customer_profiles_client import (
        build_from_env as build_cp,
    )

    try:
        build_cp().delete_segment_definition(segment_name)
    except Exception as exc:
        logger.warning("_safe_delete_segment %s: %s", segment_name, exc)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


def _check_native_queue_collision(
    bucket: dict, campaign: dict | None, plan_id: str, run_id: str
) -> None:
    """Alert-only: a native campaign starting on a queue where a branded campaign
    is already active competes for the same agent capacity — the two dialing
    engines have no awareness of each other. Never blocks or alters the start.

    One-directional by design: cheap here because VipActiveBrandedCampaigns is
    keyed by queue ARN. The reverse (branded checking for an already-active
    native campaign) has no equivalent index — native campaign state isn't
    tracked by queue anywhere — and would require an expensive full-table scan
    or a new tracking table. Out of scope for this alert (root-caused 2026-08-27).
    """
    if not _ACTIVE_BRANDED_CAMPAIGNS_TABLE or not CONNECT_INSTANCE_ID:
        return
    cfg = dict(bucket.get("campaignConfig", {}))
    if campaign:
        cfg.update(campaign.get("campaignConfig", {}))
    queue_id = cfg.get("queueId", "")
    if not queue_id:
        return
    queue_arn = (
        f"arn:aws:connect:us-east-1:{_account_id()}:instance/"
        f"{CONNECT_INSTANCE_ID}/queue/{queue_id}"
    )
    try:
        resp = _get_ddb_client().query(
            TableName=_ACTIVE_BRANDED_CAMPAIGNS_TABLE,
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": {"S": f"QUEUE#{queue_arn}"}},
            Limit=1,
        )
    except Exception as exc:
        logger.warning(
            "_check_native_queue_collision: query failed queue=%s error=%s",
            queue_id, type(exc).__name__,
        )
        return
    items = resp.get("Items", [])
    if not items:
        return
    branded_campaign_id = items[0].get("campaignId", {}).get("S", "")
    logger.warning(
        "_check_native_queue_collision: native campaign starting on queue=%s "
        "while branded campaign %s is active — dial-rate contention risk",
        queue_id, branded_campaign_id,
    )
    _notify_sns(
        subject="Branded+Native queue collision detected",
        detail=(
            f"Native campaign starting for plan={plan_id} run={run_id} on "
            f"queue={queue_id} while branded campaign {branded_campaign_id} is "
            "already active on the same queue. Both dialing engines compete for "
            "the same agent capacity — no automatic action taken."
        ),
        attributes={
            "PlanId": plan_id,
            "RunId": run_id,
            "QueueId": queue_id,
            "BrandedCampaignId": branded_campaign_id,
        },
    )
    _emit_queue_collision_metric(queue_id)


def _emit_queue_collision_metric(queue_id: str) -> None:
    """Emit BrandedNativeQueueCollision to VIPPlans. Fire-and-forget, never raises."""
    try:
        boto3.client("cloudwatch").put_metric_data(
            Namespace="VIPPlans",
            MetricData=[
                {
                    "MetricName": "BrandedNativeQueueCollision",
                    "Value": 1,
                    "Unit": "Count",
                    "Dimensions": [{"Name": "QueueId", "Value": queue_id}],
                },
                {
                    "MetricName": "BrandedNativeQueueCollision",
                    "Value": 1,
                    "Unit": "Count",
                    "Dimensions": [],
                },
            ],
        )
    except Exception as exc:
        logger.warning(
            "_emit_queue_collision_metric failed queue=%s error=%s",
            queue_id, type(exc).__name__,
        )


def _record_plan_event(run: dict, action: str, extra: dict | None = None) -> None:
    """Best-effort write to the day-activity-feed audit trail (AdminAuditLog).

    Telemetry only — a write failure here must never abort plan execution,
    so every exception is caught and logged, not raised.
    """
    try:
        build_audit().record(
            entity_type="plan_run",
            entity_id=f"{run['planId']}/{run['runId']}",
            action=action,
            actor_sub="system",
            actor_email="system@api-plans-executor",
            extra=extra,
        )
    except Exception as exc:
        logger.warning(
            "_record_plan_event(%s) failed: %s", action, type(exc).__name__
        )


def _notify_sns(subject: str, detail: str, attributes: dict | None = None) -> None:
    """Fire-and-forget SNS publish. Never raises — alerting must not affect plan execution."""
    if not SNS_ALERTS_TOPIC_ARN:
        return
    try:
        msg_attrs: dict = {}
        if attributes:
            for k, v in attributes.items():
                msg_attrs[k] = {"DataType": "String", "StringValue": str(v)}
        boto3.client("sns").publish(
            TopicArn=SNS_ALERTS_TOPIC_ARN,
            Subject=subject[:100],
            Message=detail,
            MessageAttributes=msg_attrs,
        )
    except Exception as exc:
        logger.warning(
            "_notify_sns: publish failed (topic=%s): %s", SNS_ALERTS_TOPIC_ARN, exc
        )


def _future_iso(base: datetime, minutes: int) -> str:
    return (base + timedelta(minutes=minutes)).isoformat()


_LEGACY_RUN_TYPE_MINUTES: dict[str, int] = {
    "time_30": 30,
    "time_45": 45,
    "time_60": 60,
    "time_90": 90,
    "time_120": 120,
}


def _campaign_end_time(now: datetime, campaign: dict, run_type: str) -> str:
    """Compute campaign end_time from run_type + optional run_duration_minutes."""
    if run_type == "custom":
        mins = campaign.get("run_duration_minutes")
        if mins and int(mins) > 0:
            return _future_iso(now, int(mins))
    elif run_type in _LEGACY_RUN_TYPE_MINUTES:
        return _future_iso(now, _LEGACY_RUN_TYPE_MINUTES[run_type])
    return _daily_cutoff_iso(now)


def _daily_cutoff_iso(now: datetime) -> str:
    """Return the next 7 PM COT (UTC-5) as UTC ISO string (campaign end-time cap).

    Uses COT consistently with all other time guards in tick() (workingHours, loop.endTime).
    7 PM COT = 00:00 UTC, giving campaigns the full window before midnight UTC.
    """
    _COT = timezone(timedelta(hours=-5))
    cot_now = now.astimezone(_COT)
    end_cot = cot_now.replace(
        hour=_DAILY_CUTOFF_HOUR, minute=0, second=0, microsecond=0
    )
    if end_cot <= cot_now:
        end_cot += timedelta(days=1)
    return end_cot.astimezone(timezone.utc).isoformat()


def _past_daily_cutoff(now: datetime) -> bool:
    """True if current COT time (UTC-5) is at or past the daily cutoff (7 PM).

    Uses COT consistently with all other time guards in tick().
    """
    _COT = timezone(timedelta(hours=-5))
    cot_now = now.astimezone(_COT)
    return cot_now.hour >= _DAILY_CUTOFF_HOUR


_COT_TZ = timezone(timedelta(hours=-5))
_DAY_ABBR = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]  # weekday() → 0=MON


def _now_cot_hhmm() -> int:
    """Current COT time as minutes-since-midnight. Extracted for testability."""
    now = datetime.now(_COT_TZ)
    return now.hour * 60 + now.minute


def _is_working_day(plan: dict) -> bool:
    """Return True if today (COT) is an allowed day for the plan.

    If workingHours or workingHours.days is not configured, always returns True (no restriction).
    """
    wh = plan.get("workingHours")
    if not wh:
        return True
    allowed_days = wh.get("days") or []
    if not allowed_days:
        return True
    day_abbr = _DAY_ABBR[datetime.now(_COT_TZ).weekday()]
    return day_abbr in allowed_days


def _within_working_hours(plan: dict) -> bool:
    """Return True if current COT time/day is within plan's workingHours window.

    If workingHours is not configured, always returns True (no restriction).
    """
    wh = plan.get("workingHours")
    if not wh:
        return True
    if not _is_working_day(plan):
        return False
    now_cot = datetime.now(_COT_TZ)
    start = wh.get("startTime", "00:00")
    end = wh.get(
        "endTime", "24:00"
    )  # default runs through midnight; 24:00 = 1440 min > 23:59

    def hhmm(s: str) -> int:
        h, m = (int(x) for x in s.split(":"))
        return h * 60 + m

    now_min = now_cot.hour * 60 + now_cot.minute
    return hhmm(start) <= now_min < hhmm(end)


_cached_account_id: str | None = None


def _account_id() -> str:
    global _cached_account_id
    if not _cached_account_id:
        _cached_account_id = boto3.client("sts").get_caller_identity()["Account"]
    return _cached_account_id
