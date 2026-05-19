"""Run lifecycle handlers: trigger, status, abort."""
from __future__ import annotations

from vip_shared.application.http import (
    extract_caller,
    json_response,
    parse_body,
)
from vip_shared.infrastructure.persistence.audit import build_from_env as build_audit

import executor
import store


def trigger_run(event: dict, path_params: dict) -> dict:
    plan_id = path_params["id"]
    caller = extract_caller(event)
    body = parse_body(event) or {}

    start_bucket_index = body.get("startBucketIndex")
    if start_bucket_index is not None:
        try:
            start_bucket_index = int(start_bucket_index)
        except (TypeError, ValueError):
            return json_response(400, {"error": {"code": "INVALID_INPUT", "message": "startBucketIndex must be an integer"}})

    plan = store.get_plan(plan_id)
    if not plan:
        return json_response(404, {"error": {"code": "NOT_FOUND", "message": f"Plan {plan_id} not found"}})

    if start_bucket_index is not None and start_bucket_index >= len(plan.get("buckets", [])):
        return json_response(400, {"error": {"code": "INVALID_INPUT", "message": f"startBucketIndex {start_bucket_index} out of range"}})

    # Reject if a run is already active
    latest = store.get_latest_run(plan_id)
    if latest and latest.get("status") == "running":
        raise ValueError(f"Plan {plan_id} already has an active run ({latest['runId']}). Abort it first.")

    run = executor.start_run(plan_id, start_bucket_index=start_bucket_index)
    build_audit().record(
        entity_type="plan_run", entity_id=f"{plan_id}/{run['runId']}", action="start",
        actor_sub=caller.sub, actor_email=caller.email,
        ip_address=caller.ip_address, user_agent=caller.user_agent,
        after={"planId": plan_id, "runId": run["runId"], "startBucketIndex": start_bucket_index},
    )
    return json_response(201, run)


def list_runs(event: dict, path_params: dict) -> dict:
    plan_id = path_params["id"]
    runs = store.list_runs(plan_id)
    return json_response(200, {"runs": runs})


def get_run(event: dict, path_params: dict) -> dict:
    plan_id = path_params["id"]
    run_id = path_params["runId"]
    run = store.get_run(plan_id, run_id)
    if not run:
        return json_response(404, {"error": {"code": "NOT_FOUND", "message": f"Run {run_id} not found"}})
    return json_response(200, run)


def abort_run(event: dict, path_params: dict) -> dict:
    plan_id = path_params["id"]
    run_id = path_params["runId"]
    caller = extract_caller(event)

    run = executor.abort_run(plan_id, run_id)
    build_audit().record(
        entity_type="plan_run", entity_id=f"{plan_id}/{run_id}", action="abort",
        actor_sub=caller.sub, actor_email=caller.email,
        ip_address=caller.ip_address, user_agent=caller.user_agent,
        after={"status": "aborted"},
    )
    return json_response(200, run)


def force_finish_run(event: dict, path_params: dict) -> dict:
    plan_id = path_params["id"]
    run_id = path_params["runId"]
    caller = extract_caller(event)

    run = executor.force_finish_run(plan_id, run_id)
    build_audit().record(
        entity_type="plan_run", entity_id=f"{plan_id}/{run_id}", action="force_finish",
        actor_sub=caller.sub, actor_email=caller.email,
        ip_address=caller.ip_address, user_agent=caller.user_agent,
        after={"status": "completed"},
    )
    return json_response(200, run)


def force_start_bucket(event: dict, path_params: dict) -> dict:
    plan_id = path_params["id"]
    run_id = path_params["runId"]
    bucket_index = int(path_params["bucketIndex"])
    caller = extract_caller(event)

    run = executor.force_start_bucket(plan_id, run_id, bucket_index)
    build_audit().record(
        entity_type="plan_run", entity_id=f"{plan_id}/{run_id}", action="force_start_bucket",
        actor_sub=caller.sub, actor_email=caller.email,
        ip_address=caller.ip_address, user_agent=caller.user_agent,
        after={"bucketIndex": bucket_index},
    )
    return json_response(200, run)


def force_stop_bucket(event: dict, path_params: dict) -> dict:
    plan_id = path_params["id"]
    run_id = path_params["runId"]
    bucket_index = int(path_params["bucketIndex"])
    caller = extract_caller(event)

    run = executor.force_stop_bucket(plan_id, run_id, bucket_index)
    build_audit().record(
        entity_type="plan_run", entity_id=f"{plan_id}/{run_id}", action="force_stop_bucket",
        actor_sub=caller.sub, actor_email=caller.email,
        ip_address=caller.ip_address, user_agent=caller.user_agent,
        after={"bucketIndex": bucket_index},
    )
    return json_response(200, run)


def force_start_campaign(event: dict, path_params: dict) -> dict:
    plan_id = path_params["id"]
    run_id = path_params["runId"]
    bucket_index = int(path_params["bucketIndex"])
    campaign_index = int(path_params["campaignIndex"])
    caller = extract_caller(event)

    run = executor.force_start_campaign(plan_id, run_id, bucket_index, campaign_index)
    build_audit().record(
        entity_type="plan_run", entity_id=f"{plan_id}/{run_id}", action="force_start_campaign",
        actor_sub=caller.sub, actor_email=caller.email,
        ip_address=caller.ip_address, user_agent=caller.user_agent,
        after={"bucketIndex": bucket_index, "campaignIndex": campaign_index},
    )
    return json_response(200, run)


def force_stop_campaign(event: dict, path_params: dict) -> dict:
    plan_id = path_params["id"]
    run_id = path_params["runId"]
    bucket_index = int(path_params["bucketIndex"])
    campaign_index = int(path_params["campaignIndex"])
    caller = extract_caller(event)

    run = executor.force_stop_campaign(plan_id, run_id, bucket_index, campaign_index)
    build_audit().record(
        entity_type="plan_run", entity_id=f"{plan_id}/{run_id}", action="force_stop_campaign",
        actor_sub=caller.sub, actor_email=caller.email,
        ip_address=caller.ip_address, user_agent=caller.user_agent,
        after={"bucketIndex": bucket_index, "campaignIndex": campaign_index},
    )
    return json_response(200, run)


def skip_campaign(event: dict, path_params: dict) -> dict:
    plan_id = path_params["id"]
    run_id = path_params["runId"]
    bucket_index = int(path_params["bucketIndex"])
    campaign_index = int(path_params["campaignIndex"])
    caller = extract_caller(event)

    run = executor.skip_campaign(plan_id, run_id, bucket_index, campaign_index)
    build_audit().record(
        entity_type="plan_run", entity_id=f"{plan_id}/{run_id}", action="skip_campaign",
        actor_sub=caller.sub, actor_email=caller.email,
        ip_address=caller.ip_address, user_agent=caller.user_agent,
        after={"bucketIndex": bucket_index, "campaignIndex": campaign_index},
    )
    return json_response(200, run)
