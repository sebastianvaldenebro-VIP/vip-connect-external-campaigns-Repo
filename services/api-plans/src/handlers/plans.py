"""CRUD handlers for plan definitions."""
from __future__ import annotations

import uuid

from vip_shared.application.http import (
    extract_caller,
    json_response,
    parse_body,
)
from vip_shared.infrastructure.persistence.audit import build_from_env as build_audit

import scheduler_manager
import store


def list_plans(event: dict, _path_params: dict) -> dict:
    plans = store.list_plans()
    result = []
    for plan in sorted(plans, key=lambda p: p.get("updatedAt") or "", reverse=True):
        latest = store.get_latest_run(plan["planId"])
        result.append({**plan, "latestRun": latest})
    return json_response(200, {"plans": result})


def list_templates(event: dict, _path_params: dict) -> dict:
    plans = store.list_plans()
    templates = [p for p in plans if p.get("isTemplate") or p.get("is_template")]
    return json_response(200, {"plans": sorted(templates, key=lambda p: p.get("updatedAt") or "", reverse=True)})


def get_plan(event: dict, path_params: dict) -> dict:
    plan_id = path_params["id"]
    plan = store.get_plan(plan_id)
    if not plan:
        return json_response(404, {"error": {"code": "NOT_FOUND", "message": f"Plan {plan_id} not found"}})
    latest = store.get_latest_run(plan_id)
    return json_response(200, {"plan": plan, "latestRun": latest})


def _regenerate_bucket_ids(buckets: list) -> list:
    """Deep-copy buckets with fresh UUIDs for all bucket and campaign IDs.

    Updates cross-bucket dependsOn references so the DAG remains intact.
    """
    old_to_new: dict[str, str] = {}
    for bucket in buckets:
        for campaign in bucket.get("campaigns", []):
            old_id = campaign.get("id", "")
            if old_id:
                old_to_new[old_id] = str(uuid.uuid4())

    result = []
    for bucket in buckets:
        bucket_copy = {**bucket, "id": str(uuid.uuid4())}
        new_campaigns = []
        for campaign in bucket.get("campaigns", []):
            new_id = old_to_new.get(campaign.get("id", ""), str(uuid.uuid4()))
            new_campaigns.append({
                **campaign,
                "id": new_id,
                "dependsOn": [old_to_new.get(d, d) for d in campaign.get("dependsOn", [])],
            })
        bucket_copy["campaigns"] = new_campaigns
        result.append(bucket_copy)
    return result


def create_plan(event: dict, _path_params: dict) -> dict:
    body = parse_body(event)
    caller = extract_caller(event)

    # Duplicate shorthand: POST /plans with {_duplicateFromId, name}
    duplicate_from_id = body.get("_duplicateFromId")
    if duplicate_from_id:
        source = store.get_plan(duplicate_from_id)
        if not source:
            return json_response(404, {"error": {"code": "NOT_FOUND", "message": f"Source plan {duplicate_from_id} not found"}})
        body = {
            "name": body.get("name") or f"{source['name']} (copy)",
            "description": source.get("description", ""),
            "trigger": {"type": "manual"},
            "isTemplate": False,
            "isDefault": False,
            "buckets": _regenerate_bucket_ids(source.get("buckets", [])),
        }

    _require(body, ("name",))

    if not body.get("buckets"):
        raise ValueError("Plan must have at least one bucket")

    trigger = body.get("trigger", {"type": "manual"})
    if trigger.get("type") == "on_plan_complete":
        _validate_trigger_no_cycle(None, trigger, store.list_plans())

    _validate_dag(body.get("buckets", []))

    plan = store.put_plan(body)

    if trigger.get("type") == "time":
        scheduler_manager.upsert_schedule(plan["planId"], trigger)
    elif plan.get("schedule") and plan["schedule"].get("enabled"):
        scheduler_manager.upsert_schedule(plan["planId"], plan["schedule"])

    build_audit().record(
        entity_type="plan", entity_id=plan["planId"], action="create",
        actor_sub=caller.sub, actor_email=caller.email,
        ip_address=caller.ip_address, user_agent=caller.user_agent,
        after={"name": plan["name"], "bucketCount": len(plan["buckets"])},
    )
    return json_response(201, plan)


def update_plan(event: dict, path_params: dict) -> dict:
    plan_id = path_params["id"]
    body = parse_body(event)
    caller = extract_caller(event)

    existing = store.get_plan(plan_id)
    if not existing:
        return json_response(404, {"error": {"code": "NOT_FOUND", "message": f"Plan {plan_id} not found"}})

    trigger = body.get("trigger") or existing.get("trigger", {"type": "manual"})
    if trigger.get("type") == "on_plan_complete":
        all_plans = store.list_plans()
        _validate_trigger_no_cycle(plan_id, trigger, all_plans)

    if "buckets" in body:
        _validate_dag(body["buckets"])

    allowed = ("name", "description", "buckets", "trigger", "loop", "workingHours", "isTemplate", "isDefault", "schedule")
    updated = {**existing, **{k: v for k, v in body.items() if k in allowed}}
    plan = store.put_plan(updated)

    new_trigger = body.get("trigger")
    if new_trigger is not None:
        if new_trigger.get("type") == "time":
            scheduler_manager.upsert_schedule(plan_id, new_trigger)
        elif existing.get("trigger", {}).get("type") == "time":
            # Trigger changed away from "time" — remove the EventBridge rule
            scheduler_manager.delete_schedule(plan_id)

    new_schedule = body.get("schedule")
    if new_schedule is not None:
        if new_schedule.get("enabled"):
            scheduler_manager.upsert_schedule(plan_id, new_schedule)
        else:
            scheduler_manager.delete_schedule(plan_id)

    build_audit().record(
        entity_type="plan", entity_id=plan_id, action="update",
        actor_sub=caller.sub, actor_email=caller.email,
        ip_address=caller.ip_address, user_agent=caller.user_agent,
        after={"name": plan["name"]},
    )
    return json_response(200, plan)


def delete_plan(event: dict, path_params: dict) -> dict:
    plan_id = path_params["id"]
    caller = extract_caller(event)

    existing = store.get_plan(plan_id)
    if not existing:
        return json_response(404, {"error": {"code": "NOT_FOUND", "message": f"Plan {plan_id} not found"}})

    store.delete_plan(plan_id)
    scheduler_manager.delete_schedule(plan_id)

    # Reset dangling on_plan_complete triggers that point at this deleted plan
    for downstream in store.find_plans_by_trigger_planid(plan_id):
        store.update_plan_trigger(downstream["planId"], {"type": "manual"})

    build_audit().record(
        entity_type="plan", entity_id=plan_id, action="delete",
        actor_sub=caller.sub, actor_email=caller.email,
        ip_address=caller.ip_address, user_agent=caller.user_agent,
        before={"name": existing["name"]},
    )
    return json_response(204, {})


def clone_from_template(event: dict, path_params: dict) -> dict:
    """POST /plans/from-template/{tid} — clone a template into a new draft plan."""
    tid = path_params["tid"]
    caller = extract_caller(event)
    body = parse_body(event) or {}

    template = store.get_plan(tid)
    if not template:
        return json_response(404, {"error": {"code": "NOT_FOUND", "message": f"Template {tid} not found"}})
    if not (template.get("isTemplate") or template.get("is_template")):
        return json_response(400, {"error": {"code": "NOT_A_TEMPLATE", "message": f"Plan {tid} is not a template"}})

    new_plan = {
        "name": body.get("name") or f"{template['name']} (copy)",
        "description": body.get("description") or template.get("description", ""),
        "trigger": {"type": "manual"},
        "buckets": _regenerate_bucket_ids(template.get("buckets", [])),
        "isTemplate": False,
        "isDefault": False,
    }
    plan = store.put_plan(new_plan)

    build_audit().record(
        entity_type="plan", entity_id=plan["planId"], action="clone_template",
        actor_sub=caller.sub, actor_email=caller.email,
        ip_address=caller.ip_address, user_agent=caller.user_agent,
        after={"name": plan["name"], "sourceTemplateId": tid},
    )
    return json_response(201, plan)


# ── Validation helpers ────────────────────────────────────────────────────────


def _require(body: dict, fields: tuple[str, ...]) -> None:
    missing = [f for f in fields if f not in body or body[f] is None]
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(missing)}")


_MIN_TIME_BASED_DURATION = 10  # must be > 2 × PRESTART_MINUTES (5) to avoid instant pre-warm


def _validate_dag(buckets: list[dict]) -> None:
    """Topological sort across all campaigns in all buckets to detect dependency cycles.

    Also validates that dependsOn references exist in same or earlier buckets,
    and that time-based bucket durations are long enough for the pre-start window.
    """
    for bi, bucket in enumerate(buckets):
        if bucket.get("run_mode") in ("time_based", "time-based"):
            duration = bucket.get("duration_minutes") or 0
            if duration < _MIN_TIME_BASED_DURATION:
                raise ValueError(
                    f"Bucket {bi} duration_minutes={duration} is too short — "
                    f"must be >= {_MIN_TIME_BASED_DURATION} to allow pre-start warming"
                )
    # Build a flat map of campaign_id → bucket_index
    campaign_bucket: dict[str, int] = {}
    for bi, bucket in enumerate(buckets):
        for campaign in bucket.get("campaigns", []):
            cid = campaign.get("id")
            if cid:
                campaign_bucket[cid] = bi

    all_ids = set(campaign_bucket)

    # Validate dependsOn references and collect edges
    edges: dict[str, list[str]] = {cid: [] for cid in all_ids}
    for bi, bucket in enumerate(buckets):
        for campaign in bucket.get("campaigns", []):
            cid = campaign.get("id")
            if not cid:
                continue
            for parent_id in campaign.get("dependsOn", []):
                if parent_id == cid:
                    raise ValueError(
                        f"Campaign '{cid}' cannot depend on itself"
                    )
                if parent_id not in all_ids:
                    raise ValueError(
                        f"Campaign '{cid}' depends on unknown campaign '{parent_id}'"
                    )
                if campaign_bucket[parent_id] > bi:
                    raise ValueError(
                        f"Campaign '{cid}' in bucket {bi} cannot depend on campaign "
                        f"'{parent_id}' in later bucket {campaign_bucket[parent_id]}"
                    )
                edges[cid].append(parent_id)

    # Kahn's algorithm — detect cycles
    in_degree = {cid: 0 for cid in all_ids}
    for cid, parents in edges.items():
        for p in parents:
            in_degree[cid] += 1

    queue = [cid for cid, deg in in_degree.items() if deg == 0]
    visited = 0
    while queue:
        node = queue.pop()
        visited += 1
        # Find who depends on this node
        for cid, parents in edges.items():
            if node in parents:
                in_degree[cid] -= 1
                if in_degree[cid] == 0:
                    queue.append(cid)

    if visited != len(all_ids):
        raise ValueError("Plan contains a dependency cycle in campaign dependsOn references")


def _validate_trigger_no_cycle(
    current_plan_id: str | None,
    trigger: dict,
    all_plans: list[dict],
) -> None:
    """BFS from the trigger's target planId to detect on_plan_complete cycles."""
    if trigger.get("type") != "on_plan_complete":
        return

    plan_map = {p["planId"]: p for p in all_plans}
    start = trigger["planId"]

    visited: set[str] = set()
    queue = [start]
    while queue:
        pid = queue.pop()
        if pid == current_plan_id:
            raise ValueError(
                f"Trigger cycle detected: plan '{current_plan_id}' would create a "
                f"circular on_plan_complete dependency"
            )
        if pid in visited:
            continue
        visited.add(pid)
        upstream = plan_map.get(pid)
        if upstream:
            up_trigger = upstream.get("trigger", {})
            if up_trigger.get("type") == "on_plan_complete":
                queue.append(up_trigger["planId"])
