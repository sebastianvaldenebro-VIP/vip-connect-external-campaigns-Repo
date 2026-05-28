"""EventBridge Rules management for plan daily schedules.

Rule naming: vip-sched-{planId_no_dashes[:20]}
Each rule fires at the configured local time (converted to UTC) on selected days.
Target: this Lambda with {"action": "scheduled_run", "planId": "..."}.

DST note: EventBridge Rules only support UTC cron. We convert local->UTC at save
time. When DST transitions, schedules drift 1 hour — operator re-saves to correct.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

LAMBDA_FUNCTION_ARN: str = os.environ.get("LAMBDA_FUNCTION_ARN", "")
_cached_account_id: str | None = None


def _account_id() -> str:
    global _cached_account_id
    if not _cached_account_id:
        _cached_account_id = boto3.client("sts").get_caller_identity()["Account"]
    return _cached_account_id


def _rule_name(plan_id: str) -> str:
    return f"vip-sched-{plan_id.replace('-', '')[:20]}"


DEFAULT_TIMEZONE = "America/Bogota"  # COT = UTC-5, no DST


def _build_cron(hour: int, minute: int, timezone: str, days: list[str]) -> str:
    """Convert local hour/minute + days to EventBridge UTC cron expression."""
    try:
        tz = ZoneInfo(timezone)
    except Exception:
        tz = ZoneInfo(DEFAULT_TIMEZONE)

    now = datetime.now(tz)
    local_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    utc_dt = local_dt.astimezone(ZoneInfo("UTC"))

    days_str = ",".join(days) if days else "MON-SUN"
    return f"cron({utc_dt.minute} {utc_dt.hour} ? * {days_str} *)"


def _parse_trigger(trigger: dict) -> tuple[int, int, str, list[str]]:
    """Extract (hour, minute, timezone, days) from either trigger or legacy schedule dict."""
    if trigger.get("type") == "time":
        # New format: {"type": "time", "time": "HH:MM"}
        hhmm = trigger.get("time", "08:00")
        hour, minute = (int(x) for x in hhmm.split(":")[:2])
        return hour, minute, DEFAULT_TIMEZONE, ["MON-SUN"]
    # Legacy format: {"hour": N, "minute": N, "timezone": "...", "days": [...]}
    return (
        int(trigger["hour"]),
        int(trigger["minute"]),
        trigger.get("timezone", DEFAULT_TIMEZONE),
        trigger.get("days", ["MON", "TUE", "WED", "THU", "FRI"]),
    )


def upsert_schedule(plan_id: str, trigger: dict) -> None:
    """Create or update the EventBridge Rule for a plan's daily schedule."""
    rule_name = _rule_name(plan_id)
    hour, minute, timezone, days = _parse_trigger(trigger)
    cron_expr = _build_cron(hour=hour, minute=minute, timezone=timezone, days=days)
    input_payload = json.dumps({"action": "scheduled_run", "planId": plan_id})

    events = boto3.client("events")
    events.put_rule(
        Name=rule_name,
        ScheduleExpression=cron_expr,
        State="ENABLED",
        Description=f"Daily auto-run for plan {plan_id}",
    )
    events.put_targets(
        Rule=rule_name,
        Targets=[{"Id": "lambda", "Arn": LAMBDA_FUNCTION_ARN, "Input": input_payload}],
    )

    rule_arn = f"arn:aws:events:us-east-1:{_account_id()}:rule/{rule_name}"
    try:
        boto3.client("lambda").add_permission(
            FunctionName=LAMBDA_FUNCTION_ARN,
            StatementId=rule_name,
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=rule_arn,
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceConflictException":
            raise

    logger.info("upsert_schedule: plan %s → %s (%s)", plan_id, cron_expr, rule_name)


def delete_schedule(plan_id: str) -> None:
    """Remove the EventBridge Rule for a plan schedule (idempotent)."""
    rule_name = _rule_name(plan_id)
    events = boto3.client("events")
    lambda_client = boto3.client("lambda")

    for call in (
        lambda: events.remove_targets(Rule=rule_name, Ids=["lambda"]),
        lambda: events.delete_rule(Name=rule_name),
        lambda: lambda_client.remove_permission(
            FunctionName=LAMBDA_FUNCTION_ARN, StatementId=rule_name
        ),
    ):
        try:
            call()
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in ("ResourceNotFoundException", "NoSuchEntity"):
                logger.warning("delete_schedule %s: %s", rule_name, exc)

    logger.info("delete_schedule: plan %s rule %s removed", plan_id, rule_name)
