"""Durable recovery for profile voice starts, independent of mutable run snapshots.

An immutable ownership record fences reuse of a Connect ID. A separate active
index lets the periodic reaper find old/deleted runs without scanning history or
retired tombstones. No patient attributes or automatic TTLs belong in these rows.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import ClientError

_INTENT_PK = "PROFILE_VOICE_INTENT#v1"
_ACTIVE_PK = "PROFILE_VOICE_CLEANUP#v1"
_CONTROL_SK = "CONTROL"
_SCHEMA_VERSION = 1
# Plans' Lambda has a five-minute hard timeout; retain an extra minute for an
# invocation that was already in flight. This is not an SMS/hour policy.
WORKER_WINDOW_SECONDS = 360
_HEARTBEAT_MAX_AGE = 180
_REAPER_LEASE_SECONDS = 360
_PAGE_SIZE = 25
_ACTIVE_STATUSES = frozenset({"queued", "creating", "warming", "running"})
_TERMINAL_CONNECT = frozenset({"Stopped", "Failed", "Completed", "Deleted"})
_SDK_CONFIG = Config(connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 2})
_logger = logging.getLogger(__name__)


class CleanupUnavailable(RuntimeError):
    """Starting voice would lack a verified, durable recovery path."""


def _table():
    return boto3.resource("dynamodb", config=_SDK_CONFIG).Table(
        os.environ.get("PLANS_TABLE_NAME", "VipAdminPlans")
    )


def _connect():
    return boto3.client("connectcampaignsv2", config=_SDK_CONFIG)


def _now() -> int:
    return int(time.time())


def _conditional_failure(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _key(partition: str, connect_id: str) -> dict:
    return {"pk": partition, "sk": f"CAMPAIGN#{connect_id}"}


def _ready(table, now: int) -> None:
    if os.environ.get("PROFILE_VOICE_CLEANUP_ENABLED") != "true":
        raise CleanupUnavailable("profile_voice_cleanup_not_configured")
    control = table.get_item(
        Key={"pk": _ACTIVE_PK, "sk": _CONTROL_SK}, ConsistentRead=True,
    ).get("Item") or {}
    if control.get("degraded"):
        raise CleanupUnavailable("profile_voice_cleanup_degraded")
    heartbeat = int(control.get("heartbeatAt") or 0)
    if control.get("schemaVersion") != _SCHEMA_VERSION or not -30 <= now - heartbeat <= _HEARTBEAT_MAX_AGE:
        raise CleanupUnavailable("profile_voice_cleanup_heartbeat_stale")


def register_start(run: dict, cs: dict, *, now: int | None = None) -> dict:
    """Persist both proof and discovery index before the caller can invoke Start.

    Call only after CAS has published this exact ownership in the run. A replay
    may extend its lease, but a different owner, CLEANING or RETIRED ID can never
    be reused. Failure of either write means the caller must not start voice.
    """
    now = _now() if now is None else now
    table = _table()
    _ready(table, now)
    identity = {k: run[k] for k in ("planId", "runId")}
    identity.update(campaignId=cs["campaignId"], connectCampaignId=cs["connectCampaignId"],
                    generation=int(cs.get("precallSmsGeneration") or 0))
    if any(not isinstance(identity[k], str) or not identity[k] for k in
           ("planId", "runId", "campaignId", "connectCampaignId")):
        raise CleanupUnavailable("profile_voice_cleanup_invalid_identity")
    import json

    owner = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    intent = {
        **_key(_INTENT_PK, identity["connectCampaignId"]), **identity,
        "ownerToken": owner, "version": uuid.uuid4().hex, "state": "OWNED",
        "workerExpiresAt": now + WORKER_WINDOW_SECONDS, "updatedAt": now,
    }
    try:
        table.put_item(
            Item=intent,
            ConditionExpression=("attribute_not_exists(pk) OR "
                                 "(#owner = :owner AND #state = :owned AND workerExpiresAt <= :expires)"),
            ExpressionAttributeNames={"#owner": "ownerToken", "#state": "state"},
            ExpressionAttributeValues={":owner": owner, ":owned": "OWNED", ":expires": intent["workerExpiresAt"]},
        )
    except ClientError as exc:
        if _conditional_failure(exc):
            raise CleanupUnavailable("profile_voice_cleanup_identity_fenced") from None
        raise
    # A crash before this write cannot have reached Start. A late write after
    # retirement can only recreate an index that the reaper safely removes.
    table.put_item(Item={**_key(_ACTIVE_PK, identity["connectCampaignId"]), "ownerToken": owner})
    return intent


def check_start_window(intent: dict, *, now: int | None = None) -> None:
    now = _now() if now is None else now
    table = _table()
    _ready(table, now)
    current = table.get_item(
        Key={"pk": intent["pk"], "sk": intent["sk"]}, ConsistentRead=True,
    ).get("Item") or {}
    if (current.get("version") != intent["version"] or current.get("state") != "OWNED"
            or current.get("ownerToken") != intent["ownerToken"]
            or now >= int(current.get("workerExpiresAt") or 0)):
        raise CleanupUnavailable("profile_voice_cleanup_start_window_closed")


def _ownership(table, intent: dict) -> str:
    current = table.get_item(
        Key={"pk": f"PLAN#{intent['planId']}", "sk": f"RUN#{intent['runId']}"},
        ConsistentRead=True,
    ).get("Item")
    if not current:
        return "obsolete"
    if not isinstance(current.get("bucketStates"), list) or not isinstance(current.get("status"), str):
        raise CleanupUnavailable("profile_voice_cleanup_invalid_run")
    matches = []
    for bucket in current["bucketStates"]:
        for candidate in bucket["campaignStates"]:
            if candidate.get("connectCampaignId") == intent["connectCampaignId"]:
                active = (current["status"] == "running" and bucket.get("status") in _ACTIVE_STATUSES
                          and candidate.get("status") in _ACTIVE_STATUSES)
                if active:
                    matches.append(candidate.get("campaignId") == intent["campaignId"]
                                   and int(candidate.get("precallSmsGeneration") or 0) == intent["generation"])
    if False in matches:
        return "conflict"  # Never stop an externally reused ID from a live generation.
    return "active" if matches else "obsolete"


def _connect_state(client, connect_id: str) -> str:
    try:
        return client.get_campaign_state(id=connect_id).get("state", "Unknown")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return "Deleted"
        raise


def _remove_index(table, index: dict) -> None:
    try:
        table.delete_item(
            Key={"pk": index["pk"], "sk": index["sk"]},
            ConditionExpression="ownerToken = :owner",
            ExpressionAttributeValues={":owner": index["ownerToken"]},
        )
    except ClientError as exc:
        if not _conditional_failure(exc):
            raise


def _reconcile(table, client, index: dict, now: int) -> str:
    intent = table.get_item(
        Key={"pk": _INTENT_PK, "sk": index["sk"]}, ConsistentRead=True,
    ).get("Item")
    if not intent or intent.get("ownerToken") != index.get("ownerToken"):
        raise CleanupUnavailable("profile_voice_cleanup_index_mismatch")
    if intent.get("state") == "RETIRED":
        _remove_index(table, index)
        return "retired"
    ownership = _ownership(table, intent)
    if ownership == "active":
        return "active"
    if ownership == "conflict":
        raise CleanupUnavailable("profile_voice_cleanup_owner_conflict")
    # Fence a writer renewal before acting on the ownership observation. An
    # old reaper snapshot cannot stop/delete through a newly renewed intent.
    cleanup_version = uuid.uuid4().hex
    try:
        result = table.update_item(
            Key={"pk": intent["pk"], "sk": intent["sk"]},
            UpdateExpression="SET #state = :cleaning, #version = :next, updatedAt = :now",
            ConditionExpression="#version = :version AND (#state = :owned OR #state = :cleaning)",
            ExpressionAttributeNames={"#state": "state", "#version": "version"},
            ExpressionAttributeValues={":version": intent["version"], ":next": cleanup_version,
                                       ":owned": "OWNED", ":cleaning": "CLEANING", ":now": now},
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if _conditional_failure(exc):
            return "changed"
        raise
    intent = result["Attributes"]
    # Recheck after fencing. A changed current run is never permission to stop
    # a different live owner, even if someone bypassed normal registration.
    if _ownership(table, intent) != "obsolete":
        return "changed"
    state = _connect_state(client, intent["connectCampaignId"])
    if state in {"Running", "Paused"}:
        client.stop_campaign(id=intent["connectCampaignId"])
        state = _connect_state(client, intent["connectCampaignId"])
    if now < int(intent["workerExpiresAt"]):
        return "waiting_worker"
    if state == "Initialized":
        # It never started and its worker can no longer start it. Stop does
        # not accept Initialized; delete only this fenced, obsolete ID.
        client.delete_campaign(id=intent["connectCampaignId"])
        state = _connect_state(client, intent["connectCampaignId"])
    if state not in _TERMINAL_CONNECT:
        return "waiting_state"
    try:
        table.update_item(
            Key={"pk": intent["pk"], "sk": intent["sk"]},
            UpdateExpression="SET #state = :retired, updatedAt = :now",
            ConditionExpression="#version = :version AND #state = :cleaning AND workerExpiresAt <= :now",
            ExpressionAttributeNames={"#state": "state", "#version": "version"},
            ExpressionAttributeValues={":version": cleanup_version, ":cleaning": "CLEANING", ":retired": "RETIRED", ":now": now},
        )
    except ClientError as exc:
        if _conditional_failure(exc):
            return "changed"
        raise
    _remove_index(table, index)
    return "retired"


def reap(context=None) -> dict:
    """Advance one bounded page, retaining a durable cursor across invocations.

    The fixed rule calls this even for terminal/deleted/non-latest runs. Every
    row, including active/failing rows, advances the cursor. Retired ownership
    tombstones live outside this queried partition and cannot starve recovery.
    """
    table = _table()
    token = uuid.uuid4().hex
    now = _now()
    control_key = {"pk": _ACTIVE_PK, "sk": _CONTROL_SK}
    try:
        leased = table.update_item(
            Key=control_key,
            UpdateExpression="SET leaseToken = :token, leaseUntil = :until",
            ConditionExpression="attribute_not_exists(leaseUntil) OR leaseUntil < :now",
            ExpressionAttributeValues={":token": token, ":until": now + _REAPER_LEASE_SECONDS, ":now": now},
            ReturnValues="ALL_NEW",
        )["Attributes"]
    except ClientError as exc:
        if _conditional_failure(exc):
            return {"ok": True, "reason": "cleanup_worker_busy"}
        raise
    try:
        kwargs = {
            "KeyConditionExpression": Key("pk").eq(_ACTIVE_PK) & Key("sk").begins_with("CAMPAIGN#"),
            "ConsistentRead": True, "Limit": _PAGE_SIZE,
        }
        cursor = leased.get("cursor")
        if cursor:
            kwargs["ExclusiveStartKey"] = cursor
        page = table.query(**kwargs)
        counts: dict[str, int] = {}
        client = _connect()
        consumed = 0
        for index in page.get("Items", []):
            if context is not None and context.get_remaining_time_in_millis() < 45000:
                break
            try:
                outcome = _reconcile(table, client, index, _now())
            except Exception as exc:
                outcome = "failed"
                _logger.error("profile_voice_cleanup_failed id=%s error_type=%s", index.get("sk"), type(exc).__name__)
            counts[outcome] = counts.get(outcome, 0) + 1
            cursor = {"pk": index["pk"], "sk": index["sk"]}
            # A crash resumes after the last fully considered item, even if
            # that item is still active or a service failure needs later retry.
            checkpoint = "SET #cursor = :cursor"
            checkpoint_values = {":cursor": cursor, ":token": token}
            if outcome == "failed":
                checkpoint += ", cycleFailed = :failed, degraded = :failed"
                checkpoint_values[":failed"] = True
                leased.update(cycleFailed=True, degraded=True)
            table.update_item(
                Key=control_key, UpdateExpression=checkpoint,
                ConditionExpression="leaseToken = :token",
                ExpressionAttributeNames={"#cursor": "cursor"},
                ExpressionAttributeValues=checkpoint_values,
            )
            consumed += 1
        if page.get("Items") and not consumed:
            # A handler that cannot consider even one pending item has not
            # demonstrated recovery readiness. Preserve the old heartbeat
            # and cursor while allowing the next invocation to try again.
            table.update_item(
                Key=control_key, UpdateExpression="REMOVE leaseUntil, leaseToken",
                ConditionExpression="leaseToken = :token",
                ExpressionAttributeValues={":token": token},
            )
            return {"ok": True, "processed": 0, "reason": "insufficient_budget"}
        if consumed == len(page.get("Items", [])):
            cursor = page.get("LastEvaluatedKey") or {}
        # A healthy later page must not mask a failed earlier page. Once
        # degraded, require a complete error-free traversal before publishing
        # readiness again; keep advancing the cursor so failures cannot starve
        # other cleanup obligations.
        failed = bool(counts.get("failed"))
        cycle_failed = bool(leased.get("cycleFailed")) or failed
        degraded = bool(leased.get("degraded")) or failed
        if not cursor and not cycle_failed:
            degraded = False
        values = {":cursor": cursor or {}, ":token": token,
                  ":cycle_failed": cycle_failed if cursor else False, ":degraded": degraded}
        update = "SET #cursor = :cursor, cycleFailed = :cycle_failed, degraded = :degraded"
        if not degraded:
            update += ", heartbeatAt = :now, schemaVersion = :schema"
            values.update({":now": _now(), ":schema": _SCHEMA_VERSION})
        table.update_item(
            Key=control_key,
            UpdateExpression=update + " REMOVE leaseUntil, leaseToken",
            ConditionExpression="leaseToken = :token",
            ExpressionAttributeNames={"#cursor": "cursor"},
            ExpressionAttributeValues=values,
        )
        return {"ok": not failed, "processed": consumed, "counts": counts}
    except Exception:
        # Do not publish a heartbeat on an uncompleted pass. Release for the
        # next scheduled attempt if possible; hard crashes expire the lease.
        try:
            table.update_item(
                Key=control_key, UpdateExpression="REMOVE leaseUntil, leaseToken",
                ConditionExpression="leaseToken = :token",
                ExpressionAttributeValues={":token": token},
            )
        except Exception:
            pass
        raise
