"""DynamoDB read/write for plans and plan runs.

Single-table design: ``VipAdminPlans``
  Plan: PK=``PLAN#<planId>`` SK=``META``
  Run:  PK=``PLAN#<planId>`` SK=``RUN#<epoch_ms>#<uuid8>``
        SK prefix preserves chronological ordering via lexicographic sort.

Run schema (v2):
  - planSnapshot: immutable copy of the plan at trigger time
  - bucketStates[].campaignStates[]: per-campaign state tracking
  - bucketStates[].scheduleName: per-bucket EventBridge rule name (was run-level)

Backward compat:
  - Old runs with a single run-level scheduleName are read back correctly.
  - Old plan buckets without a ``campaigns`` array are silently migrated to a
    single-campaign array so the executor can handle both schemas uniformly.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

TABLE_NAME = os.environ.get("PLANS_TABLE_NAME", "VipAdminPlans")


class ConcurrentWriteError(Exception):
    """Raised when save_run loses an optimistic-locking race.

    Two Lambda ticks ran simultaneously for the same run. The first writer
    incremented _version; the second's conditional check failed. The losing
    tick should exit cleanly — the winning tick already applied the update.
    """


def _table():
    return boto3.resource("dynamodb").Table(TABLE_NAME)


# ── Plans ─────────────────────────────────────────────────────────────────────


def list_plans() -> list[dict]:
    from boto3.dynamodb.conditions import Attr

    items: list[dict] = []
    kwargs: dict = {"FilterExpression": Attr("sk").eq("META")}
    while True:
        result = _table().scan(**kwargs)
        items.extend(result.get("Items", []))
        last = result.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return [_plan_from_item(i) for i in items]


def get_plan(plan_id: str) -> dict | None:
    result = _table().get_item(Key={"pk": f"PLAN#{plan_id}", "sk": "META"})
    item = result.get("Item")
    return _plan_from_item(item) if item else None


def put_plan(plan: dict) -> dict:
    now = _now_iso()
    plan_id = plan.get("planId") or str(uuid.uuid4())
    item: dict[str, Any] = {
        "pk": f"PLAN#{plan_id}",
        "sk": "META",
        "planId": plan_id,
        "name": plan["name"],
        "description": plan.get("description", ""),
        "trigger": plan.get("trigger", {"type": "manual"}),
        "loop": plan.get("loop") or None,
        "workingHours": plan.get("workingHours") or None,
        "buckets": _normalize(plan.get("buckets", [])),
        "isTemplate": plan.get("isTemplate", plan.get("is_template", False)),
        "isDefault": plan.get("isDefault", plan.get("is_default", False)),
        "createdAt": plan.get("createdAt", now),
        "updatedAt": now,
    }
    _table().put_item(Item=item)
    return _plan_from_item(item)


def delete_plan(plan_id: str) -> None:
    _table().delete_item(Key={"pk": f"PLAN#{plan_id}", "sk": "META"})


def update_plan_trigger(plan_id: str, trigger: dict) -> None:
    """Partial update — replace only the trigger field (used for repeat=False one-shot disable)."""
    _table().update_item(
        Key={"pk": f"PLAN#{plan_id}", "sk": "META"},
        UpdateExpression="SET #t = :trigger, updatedAt = :now",
        ExpressionAttributeNames={"#t": "trigger"},
        ExpressionAttributeValues={":trigger": trigger, ":now": _now_iso()},
    )


def update_plan_pending_warmup(plan_id: str, warmup_data: dict | None) -> None:
    """Store or clear the pendingWarmup payload on the plan META item.

    warmup_data = {"campaigns": [{campaignId, connectCampaignId, segmentName, segmentArn}]}
    Pass None to clear after start_run consumes it.
    """
    if warmup_data is None:
        _table().update_item(
            Key={"pk": f"PLAN#{plan_id}", "sk": "META"},
            UpdateExpression="REMOVE pendingWarmup",
        )
    else:
        _table().update_item(
            Key={"pk": f"PLAN#{plan_id}", "sk": "META"},
            UpdateExpression="SET pendingWarmup = :w",
            ExpressionAttributeValues={":w": warmup_data},
        )


def lock_plan_run(plan_id: str, run_id: str) -> None:
    """Atomically set runLock on the plan META item.

    Raises ValueError if the plan is already locked (another run just started).
    Uses attribute_not_exists so only one concurrent caller wins.
    """
    from botocore.exceptions import ClientError

    try:
        _table().update_item(
            Key={"pk": f"PLAN#{plan_id}", "sk": "META"},
            UpdateExpression="SET runLock = :run_id",
            ConditionExpression="attribute_not_exists(runLock)",
            ExpressionAttributeValues={":run_id": run_id},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ValueError(
                f"Plan {plan_id} already has an active run (concurrent trigger rejected)"
            )
        raise


def unlock_plan_run(plan_id: str) -> None:
    """Remove runLock from the plan META item when a run reaches a terminal state."""
    _table().update_item(
        Key={"pk": f"PLAN#{plan_id}", "sk": "META"},
        UpdateExpression="REMOVE runLock",
    )


def find_plans_by_trigger_planid(upstream_plan_id: str) -> list[dict]:
    """Return all plans whose trigger is on_plan_complete for upstream_plan_id.

    Uses a full scan since the plan table is small (< 100 items).
    """
    from boto3.dynamodb.conditions import Attr

    items: list[dict] = []
    kwargs: dict = {"FilterExpression": Attr("sk").eq("META")}
    while True:
        result = _table().scan(**kwargs)
        items.extend(result.get("Items", []))
        last = result.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    out = []
    for item in items:
        trigger = item.get("trigger") or {}
        if (
            trigger.get("type") == "on_plan_complete"
            and trigger.get("planId") == upstream_plan_id
        ):
            out.append(_plan_from_item(item))
    return out


# ── Runs ──────────────────────────────────────────────────────────────────────


def create_run(
    plan_id: str, plan: dict, triggered_by: str = "manual", run_id: str | None = None
) -> dict:
    """Create a new run record with full plan snapshot and nested campaign states."""
    now = _now_iso()
    if run_id is None:
        epoch_ms = int(time.time() * 1000)
        suffix = str(uuid.uuid4())[:8]
        run_id = f"{epoch_ms}-{suffix}"

    buckets = plan.get("buckets", [])
    bucket_states = [_initial_bucket_state(b, i) for i, b in enumerate(buckets)]

    item: dict[str, Any] = {
        "pk": f"PLAN#{plan_id}",
        "sk": f"RUN#{run_id}",
        "planId": plan_id,
        "runId": run_id,
        "status": "running",
        "planSnapshot": plan,
        "currentBucketIndex": 0,
        "bucketStates": bucket_states,
        "startedAt": now,
        "completedAt": None,
        "triggeredBy": triggered_by,
        "error": None,
        "_version": 0,
    }
    _table().put_item(Item=item)
    return _run_from_item(item)


def get_run(plan_id: str, run_id: str) -> dict | None:
    result = _table().get_item(Key={"pk": f"PLAN#{plan_id}", "sk": f"RUN#{run_id}"})
    item = result.get("Item")
    return _run_from_item(item) if item else None


def get_latest_run(plan_id: str) -> dict | None:
    result = _table().query(
        KeyConditionExpression=Key("pk").eq(f"PLAN#{plan_id}")
        & Key("sk").begins_with("RUN#"),
        ScanIndexForward=False,
        Limit=1,
    )
    items = result.get("Items", [])
    return _run_from_item(items[0]) if items else None


def list_runs(plan_id: str, limit: int = 20) -> list[dict]:
    result = _table().query(
        KeyConditionExpression=Key("pk").eq(f"PLAN#{plan_id}")
        & Key("sk").begins_with("RUN#"),
        ScanIndexForward=False,
        Limit=limit,
    )
    return [_run_from_item(i) for i in result.get("Items", [])]


def save_run(run: dict) -> None:
    current_version = run.get("_version", 0)
    run["_version"] = current_version + 1
    try:
        _table().put_item(
            Item={
                "pk": f"PLAN#{run['planId']}",
                "sk": f"RUN#{run['runId']}",
                **{k: v for k, v in run.items() if k not in ("pk", "sk")},
            },
            ConditionExpression="attribute_not_exists(#v) OR #v = :current_v",
            ExpressionAttributeNames={"#v": "_version"},
            ExpressionAttributeValues={":current_v": current_version},
        )
    except ClientError as exc:
        run["_version"] = current_version  # revert so in-memory state stays consistent
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ConcurrentWriteError(
                f"Run {run.get('runId')} version conflict (expected {current_version})"
            ) from exc
        raise


# ── Internal helpers ──────────────────────────────────────────────────────────


def _initial_bucket_state(bucket: dict, index: int) -> dict:
    bucket_id = bucket.get("id") or bucket.get("bucketId", str(index))
    campaigns = _ensure_campaigns(bucket)
    return {
        "bucketId": bucket_id,
        "name": bucket.get("name", f"Bucket {index + 1}"),
        "status": "queued",
        "scheduleName": None,
        "startedAt": None,
        "completedAt": None,
        "campaignStates": [_initial_campaign_state(c) for c in campaigns],
    }


def _initial_campaign_state(campaign: dict) -> dict:
    return {
        "campaignId": campaign.get("id")
        or campaign.get("campaignId")
        or str(uuid.uuid4()),
        "name": campaign.get("name", ""),
        "status": "queued",  # queued | warming | running | completed | cancelled | error | expired
        "connectCampaignId": None,
        "segmentName": None,
        "segmentArn": None,
        "leadCount": None,
        "startedAt": None,
        "completedAt": None,
        "exitReason": None,
        "errorDetail": None,
    }


def _ensure_campaigns(bucket: dict) -> list[dict]:
    """Return the campaigns list, synthesizing a single-campaign entry for old-schema buckets."""
    if "campaigns" in bucket and bucket["campaigns"]:
        return bucket["campaigns"]
    # Legacy schema: bucket has segmentFilters + campaignConfig directly
    return [
        {
            "id": bucket.get("bucketId", str(uuid.uuid4())),
            "name": bucket.get("name", "Campaign"),
            "states": bucket.get("segmentFilters", {}).get("state", []),
            "group": "",
            "attempts": [],
            "run_type": "full",
            "dependsOn": [],
            "_legacyBucket": True,
        }
    ]


def _run_from_item(item: dict) -> dict:
    bucket_states = item.get("bucketStates", [])

    # Back-compat: old runs had a single run-level scheduleName and flat bucketStates
    # without campaignStates. Migrate transparently on read.
    run_schedule = item.get("scheduleName")
    migrated_states = []
    for bs in bucket_states:
        if "campaignStates" not in bs:
            # Old schema: synthesize a single campaign state from flat bucket state
            bs = dict(bs)
            bs["campaignStates"] = [_migrate_old_bucket_state(bs)]
            bs["scheduleName"] = bs.get("scheduleName", run_schedule)
        migrated_states.append(bs)

    return {
        "planId": item["planId"],
        "runId": item["runId"],
        "status": item["status"],
        "planSnapshot": item.get("planSnapshot"),
        "currentBucketIndex": int(item.get("currentBucketIndex", 0)),
        "bucketStates": migrated_states,
        "startedAt": item.get("startedAt"),
        "completedAt": item.get("completedAt"),
        "triggeredBy": item.get("triggeredBy", "manual"),
        "error": item.get("error"),
        # Keep old-schema field for backward compat with any callers that still read it
        "scheduleName": item.get("scheduleName"),
        "_version": int(item.get("_version", 0)),
    }


def _migrate_old_bucket_state(bs: dict) -> dict:
    """Convert an old flat bucket state into a single campaign state entry."""
    return {
        "campaignId": bs.get("bucketId", "legacy"),
        "name": bs.get("campaignName") or bs.get("bucketId", ""),
        "status": _map_old_bucket_status(
            bs.get("status", "queued"), bs.get("exitReason")
        ),
        "connectCampaignId": bs.get("campaignId"),
        "segmentName": bs.get("segmentName"),
        "segmentArn": bs.get("segmentArn"),
        "leadCount": None,
        "startedAt": bs.get("startedAt"),
        "completedAt": bs.get("completedAt"),
        "exitReason": bs.get("exitReason"),
        "errorDetail": bs.get("errorDetail"),
    }


def _map_old_bucket_status(status: str, exit_reason: str | None) -> str:
    mapping = {
        "pending": "queued",
        "running": "running",
        "completed": "completed",
        "failed": "error",
        "aborted": "cancelled",
        "cancelled": "cancelled",
    }
    return mapping.get(status, status)


def _plan_from_item(item: dict) -> dict:
    out = {
        "planId": item["planId"],
        "name": item["name"],
        "description": item.get("description", ""),
        "trigger": item.get("trigger", {"type": "manual"}),
        "loop": item.get("loop"),
        "workingHours": item.get("workingHours"),
        "buckets": _normalize(item.get("buckets", [])),
        "isTemplate": item.get("isTemplate", False),
        "isDefault": item.get("isDefault", False),
        "createdAt": item.get("createdAt"),
        "updatedAt": item.get("updatedAt"),
        "pendingWarmup": item.get("pendingWarmup"),
    }
    # Back-compat aliases
    out["is_template"] = out["isTemplate"]
    out["is_default"] = out["isDefault"]
    return out


def apply_plan_to_run(plan_id: str, run_id: str, live_plan: dict) -> dict:
    """Merge live plan definition into an active run's planSnapshot.

    Only queued (not-yet-started) buckets are updated — running/completed
    buckets keep their original snapshot config. Plan-level workingHours and
    loop are always updated since they control when future buckets can run.
    """
    run = get_run(plan_id, run_id)
    if not run:
        raise ValueError(f"Run {run_id} not found")
    if run.get("status") != "running":
        raise ValueError(f"Run {run_id} is not running (status={run.get('status')})")

    snapshot = run.get("planSnapshot") or {}
    old_buckets = list(snapshot.get("buckets", []))
    new_buckets = live_plan.get("buckets", [])

    merged_buckets = list(old_buckets)
    for bi, bs in enumerate(run.get("bucketStates", [])):
        if bs.get("status") == "queued" and bi < len(new_buckets):
            merged_buckets[bi] = _normalize(new_buckets[bi])

    run["planSnapshot"] = {
        **snapshot,
        "buckets": merged_buckets,
        "workingHours": live_plan.get("workingHours"),
        "loop": live_plan.get("loop"),
    }
    save_run(run)
    return run


def _normalize(obj: Any) -> Any:
    """Recursively convert Decimal → int/float so json.dumps never serializes numbers as strings.

    boto3's DynamoDB resource returns all numeric attributes as Decimal.
    vip_shared.json_response uses default=str, which would turn Decimal('30') into "30".
    Normalizing on read and write prevents numeric fields from round-tripping as strings.
    """
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, dict):
        return {k: _normalize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize(v) for v in obj]
    return obj


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
