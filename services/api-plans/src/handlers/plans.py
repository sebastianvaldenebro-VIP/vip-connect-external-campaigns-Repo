"""CRUD handlers for plan definitions."""

from __future__ import annotations

import uuid

from vip_shared.application.http import (
    extract_caller,
    json_response,
    parse_body,
)
from vip_shared.infrastructure.persistence.audit import build_from_env as build_audit

import builders
import executor
import scheduler_manager
import store


def get_location_mapping(event: dict, _path_params: dict) -> dict:
    """Return all state/location groups from VipLocationMapping DynamoDB table."""
    groups = builders.get_all_location_groups()
    return json_response(200, {"groups": groups})


def resolve_campaign_flow(event: dict, _path_params: dict) -> dict:
    """Resolve (and auto-create if missing) the canonical campaign flow ARN
    for the given states. Thin wrapper over builders.resolve_campaign_flow_arn,
    which already has connect:CreateContactFlow — this just exposes it over HTTP
    so the frontend can stop guessing flow names client-side."""
    body = parse_body(event)
    caller = extract_caller(event)
    states = body.get("states")
    if (
        not isinstance(states, list)
        or not states
        or not all(isinstance(s, str) for s in states)
    ):
        return json_response(
            400,
            {
                "error": {
                    "code": "BAD_REQUEST",
                    "message": "states must be a non-empty list of strings",
                }
            },
        )

    arn = builders.resolve_campaign_flow_arn(states, executor.CONNECT_INSTANCE_ID)

    build_audit().record(
        entity_type="campaign_flow",
        entity_id=arn or ",".join(states),
        action="resolve",
        actor_sub=caller.sub,
        actor_email=caller.email,
        ip_address=caller.ip_address,
        user_agent=caller.user_agent,
        after={"states": states, "arn": arn},
    )
    return json_response(200, {"arn": arn})


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
    return json_response(
        200,
        {
            "plans": sorted(
                templates, key=lambda p: p.get("updatedAt") or "", reverse=True
            )
        },
    )


def get_plan(event: dict, path_params: dict) -> dict:
    plan_id = path_params["id"]
    plan = store.get_plan(plan_id)
    if not plan:
        return json_response(
            404,
            {"error": {"code": "NOT_FOUND", "message": f"Plan {plan_id} not found"}},
        )
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
            new_campaigns.append(
                {
                    **campaign,
                    "id": new_id,
                    "dependsOn": [
                        old_to_new.get(d, d) for d in campaign.get("dependsOn", [])
                    ],
                }
            )
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
            return json_response(
                404,
                {
                    "error": {
                        "code": "NOT_FOUND",
                        "message": f"Source plan {duplicate_from_id} not found",
                    }
                },
            )
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

    branded_errors = _validate_plan_body(body)
    if branded_errors:
        return json_response(
            400,
            {"error": {"code": "VALIDATION_ERROR", "messages": branded_errors}},
        )

    _validate_dag(body.get("buckets", []))

    plan = store.put_plan(body)

    if trigger.get("type") == "time":
        scheduler_manager.upsert_schedule(plan["planId"], trigger)
    elif plan.get("schedule") and plan["schedule"].get("enabled"):
        scheduler_manager.upsert_schedule(plan["planId"], plan["schedule"])

    build_audit().record(
        entity_type="plan",
        entity_id=plan["planId"],
        action="create",
        actor_sub=caller.sub,
        actor_email=caller.email,
        ip_address=caller.ip_address,
        user_agent=caller.user_agent,
        after={"name": plan["name"], "bucketCount": len(plan["buckets"])},
    )
    return json_response(201, plan)


def update_plan(event: dict, path_params: dict) -> dict:
    plan_id = path_params["id"]
    body = parse_body(event)
    caller = extract_caller(event)

    existing = store.get_plan(plan_id)
    if not existing:
        return json_response(
            404,
            {"error": {"code": "NOT_FOUND", "message": f"Plan {plan_id} not found"}},
        )

    trigger = body.get("trigger") or existing.get("trigger", {"type": "manual"})
    if trigger.get("type") == "on_plan_complete":
        all_plans = store.list_plans()
        _validate_trigger_no_cycle(plan_id, trigger, all_plans)

    if "buckets" in body:
        branded_errors = _validate_plan_body(body)
        if branded_errors:
            return json_response(
                400,
                {"error": {"code": "VALIDATION_ERROR", "messages": branded_errors}},
            )
        _validate_dag(body["buckets"])

    allowed = (
        "name",
        "description",
        "buckets",
        "trigger",
        "loop",
        "workingHours",
        "isTemplate",
        "isDefault",
        "schedule",
    )
    updated = {**existing, **{k: v for k, v in body.items() if k in allowed}}
    plan = store.put_plan(updated)

    is_template = updated.get("isTemplate") or updated.get("is_template")
    had_time_trigger = existing.get("trigger", {}).get("type") == "time"

    new_trigger = body.get("trigger")
    if is_template:
        # Templates never run on a cron — remove the rule if one exists. Checked
        # unconditionally (not gated on new_trigger being resent): isTemplate alone
        # is enough to make the plan a template, and a PATCH that only sends
        # {"isTemplate": true} without "trigger" must still clean up an existing
        # vip-sched-* rule, or it orphans forever (audit follow-up, 2026-08-21 —
        # confirmed live: plan c63d695c-b99e-4885-808a-8eca91d08e8e).
        if had_time_trigger:
            scheduler_manager.delete_schedule(plan_id)
    elif new_trigger is not None:
        if new_trigger.get("type") == "time":
            scheduler_manager.upsert_schedule(plan_id, new_trigger)
        elif had_time_trigger:
            # Trigger changed away from "time" — remove the EventBridge rule
            scheduler_manager.delete_schedule(plan_id)

    new_schedule = body.get("schedule")
    if new_schedule is not None:
        if new_schedule.get("enabled") and not is_template:
            scheduler_manager.upsert_schedule(plan_id, new_schedule)
        else:
            scheduler_manager.delete_schedule(plan_id)

    build_audit().record(
        entity_type="plan",
        entity_id=plan_id,
        action="update",
        actor_sub=caller.sub,
        actor_email=caller.email,
        ip_address=caller.ip_address,
        user_agent=caller.user_agent,
        after={"name": plan["name"]},
    )
    return json_response(200, plan)


def delete_plan(event: dict, path_params: dict) -> dict:
    plan_id = path_params["id"]
    caller = extract_caller(event)

    existing = store.get_plan(plan_id)
    if not existing:
        return json_response(
            404,
            {"error": {"code": "NOT_FOUND", "message": f"Plan {plan_id} not found"}},
        )

    store.delete_plan(plan_id)
    scheduler_manager.delete_schedule(plan_id)

    # Reset dangling on_plan_complete triggers that point at this deleted plan
    for downstream in store.find_plans_by_trigger_planid(plan_id):
        store.update_plan_trigger(downstream["planId"], {"type": "manual"})

    build_audit().record(
        entity_type="plan",
        entity_id=plan_id,
        action="delete",
        actor_sub=caller.sub,
        actor_email=caller.email,
        ip_address=caller.ip_address,
        user_agent=caller.user_agent,
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
        return json_response(
            404,
            {"error": {"code": "NOT_FOUND", "message": f"Template {tid} not found"}},
        )
    if not (template.get("isTemplate") or template.get("is_template")):
        return json_response(
            400,
            {
                "error": {
                    "code": "NOT_A_TEMPLATE",
                    "message": f"Plan {tid} is not a template",
                }
            },
        )

    new_plan = {
        "name": body.get("name") or f"{template['name']} (copy)",
        "description": body.get("description") or template.get("description", ""),
        "trigger": {"type": "manual"},
        "buckets": _regenerate_bucket_ids(template.get("buckets", [])),
        "isTemplate": False,
        "isDefault": False,
    }

    branded_errors = _validate_plan_body(new_plan)
    if branded_errors:
        return json_response(
            400,
            {"error": {"code": "VALIDATION_ERROR", "messages": branded_errors}},
        )
    _validate_dag(new_plan["buckets"])

    plan = store.put_plan(new_plan)

    build_audit().record(
        entity_type="plan",
        entity_id=plan["planId"],
        action="clone_template",
        actor_sub=caller.sub,
        actor_email=caller.email,
        ip_address=caller.ip_address,
        user_agent=caller.user_agent,
        after={"name": plan["name"], "sourceTemplateId": tid},
    )
    return json_response(201, plan)


# ── Validation helpers ────────────────────────────────────────────────────────


def _require(body: dict, fields: tuple[str, ...]) -> None:
    missing = [f for f in fields if f not in body or body[f] is None]
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(missing)}")


_MIN_TIME_BASED_DURATION = (
    10  # must be > 2 × PRESTART_MINUTES (5) to avoid instant pre-warm
)


def _validate_dag(buckets: list[dict]) -> None:
    """Topological sort across all campaigns in all buckets to detect dependency cycles.

    Also validates that dependsOn references exist in same or earlier buckets,
    and that time-based bucket durations are long enough for the pre-start window.
    """
    for bi, bucket in enumerate(buckets):
        if bucket.get("run_mode") in ("time_based", "time-based"):
            duration = int(bucket.get("duration_minutes") or 0)
            if duration < _MIN_TIME_BASED_DURATION:
                raise ValueError(
                    f"Bucket {bi} duration_minutes={duration} is too short — "
                    f"must be >= {_MIN_TIME_BASED_DURATION} to allow pre-start warming"
                )
    # Build a flat map of campaign_id → bucket_index. Ids must be unique across
    # the whole plan — build_segment_name uses a campaign's id as a
    # disambiguator to prevent same-bucket campaigns from colliding on an
    # identical segment name (and silently reusing each other's lead set); a
    # duplicate id defeats that guarantee (e.g. via _regenerate_bucket_ids
    # collapsing two source campaigns onto the same new id when cloning).
    campaign_bucket: dict[str, int] = {}
    for bi, bucket in enumerate(buckets):
        for campaign in bucket.get("campaigns", []):
            cid = campaign.get("id")
            if not cid:
                continue
            if cid in campaign_bucket:
                raise ValueError(
                    f"Duplicate campaign id '{cid}' found in bucket "
                    f"{campaign_bucket[cid]} and bucket {bi} — campaign ids "
                    f"must be unique across the whole plan"
                )
            campaign_bucket[cid] = bi

    # Separately: build_segment_name's disambiguator is a LOSSY projection of
    # id/name (sanitized, truncated to 12 chars) — two campaigns with distinct
    # raw ids can still collapse onto the same projection (or, if neither has
    # an id nor a name, onto no disambiguator at all), reopening the exact
    # segment-name collision the duplicate-id check above cannot catch by
    # itself. Validate the same token build_segment_name actually consumes,
    # so the two can never drift apart again.
    seen_tokens: dict[str, tuple[int, str]] = {}
    for bi, bucket in enumerate(buckets):
        for campaign in bucket.get("campaigns", []):
            token = builders.campaign_name_token(campaign)
            label = campaign.get("id") or campaign.get("name") or "(unnamed)"
            if not token:
                raise ValueError(
                    f"Campaign '{label}' in bucket {bi} has neither an id nor "
                    f"a name — at least one is required to build a unique "
                    f"segment name"
                )
            if token in seen_tokens:
                prev_bi, prev_label = seen_tokens[token]
                raise ValueError(
                    f"Campaign '{label}' in bucket {bi} and campaign "
                    f"'{prev_label}' in bucket {prev_bi} produce the same "
                    f"segment-name disambiguator (first 12 sanitized chars of "
                    f"id/name) — campaign ids/names must be distinct within "
                    f"that prefix"
                )
            seen_tokens[token] = (bi, label)

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
                    raise ValueError(f"Campaign '{cid}' cannot depend on itself")
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
        raise ValueError(
            "Plan contains a dependency cycle in campaign dependsOn references"
        )


def _validate_branded_campaign(campaign: dict, bucket_name: str, ci: int) -> list[str]:
    """Return validation errors for a branded campaign's required config fields."""
    errors = []
    cfg = campaign.get("campaignConfig") or {}
    prefix = f"bucket '{bucket_name}' campaign[{ci}]"
    for required_key in ("queueArn", "contactFlowId"):
        if not cfg.get(required_key):
            errors.append(
                f"{prefix}: deliveryType='branded' requires campaignConfig.{required_key}"
            )
    # Accept either sourcePhone or sourcePhoneNumber (frontend sends sourcePhoneNumber)
    if not cfg.get("sourcePhone") and not cfg.get("sourcePhoneNumber"):
        errors.append(
            f"{prefix}: deliveryType='branded' requires campaignConfig.sourcePhone"
        )
    return errors


# Ceiling for a *rendered* SMS body (GSM-7, single segment). Named so the
# frontend character counter (Task 6) and the length tests below agree on one
# source of truth instead of a bare 160 scattered across the codebase.
_MAX_SMS_CHARS = 160

# deliveryTypes that actually place a dial — the only ones a pre-call SMS can
# meaningfully precede. 'sms' has no dial to precede.
_VOICE_DELIVERY_TYPES = {"campaign", "branded", "journey"}


def _screen_sms_template_content(
    tmpl: str, prefix: str, field_name: str, render_campaign: dict
) -> list[str]:
    """Screen a present (non-empty) SMS template string: non-allowlisted
    placeholders, rendered-length overflow, and PHI patterns.

    Shared by _validate_sms_campaign (smsMessageTemplate) and
    _validate_precall_sms (precallSms.messageTemplate) so the bulk-SMS and
    pre-call channels can never drift apart in what content they allow.
    Callers own the "is the template present at all" check — this only
    screens content that IS present.

    `render_campaign` is passed straight through to max_rendered_length's
    `campaign=` kwarg (render()'s source for {{ClinicName}}): the bulk-SMS
    campaign passes its own campaignConfig, precall passes the precallSms
    block itself, since that is where its own clinicName lives.
    """
    import re as _re

    from vip_shared.domain.services.sms_template import (
        ALLOWED_FIELDS,
        extract_placeholders,
        max_rendered_length,
        render,
        strip_placeholders,
    )

    errors: list[str] = []

    # Placeholder policy: a NAMED ALLOWLIST, not a blanket ban.
    # The blanket {{...}} ban existed partly because no renderer existed —
    # sms_processor_handler passes the template verbatim to EUM, so a
    # placeholder would reach the patient as literal braces. That renderer now
    # exists (vip_shared.domain.services.sms_template), so the ban narrows to
    # "only these fields". Everything else stays blocked, and ${...} stays
    # banned outright because no renderer supports it.
    rendered_for_screen = None
    unknown = extract_placeholders(tmpl) - ALLOWED_FIELDS
    if unknown:
        errors.append(
            f"{prefix}: {field_name} uses non-allowlisted placeholder(s) "
            f"{sorted(unknown)}. Allowed: {sorted(ALLOWED_FIELDS)}."
        )
    else:
        try:
            rendered_len = max_rendered_length(tmpl, campaign=render_campaign)
            # No recipient data is needed: the renderer uses its neutral name
            # fallback and the actual clinic value, preserving substitution
            # boundaries that can assemble a prohibited value.
            rendered_for_screen = render(tmpl, recipient={}, campaign=render_campaign)
        except ValueError:
            errors.append(
                f"{prefix}: {field_name} contains malformed or unresolved placeholder "
                "syntax. Use only {{FirstName}} and {{ClinicName}}."
            )
        else:
            if rendered_len > _MAX_SMS_CHARS:
                errors.append(
                    f"{prefix}: {field_name} must render to ≤{_MAX_SMS_CHARS} chars "
                    f"(worst case {rendered_len})"
                )

    # Active PHI detection — block templates with identifiable information
    _PHI_PATTERNS = [
        (_re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "SSN-like number"),
        (_re.compile(r"\b\d{3}\s\d{2}\s\d{4}\b"), "SSN-like number"),
        (_re.compile(r"\S+@\S+\.\S+"), "email address"),
        (_re.compile(r"\b\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}\b"), "date with day/month"),
        (_re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), "ISO date (possible DOB)"),
        (_re.compile(r"\b\d{7,}\b"), "long numeric ID (possible MRN/account)"),
        (_re.compile(r"https?://"), "URL"),
        (_re.compile(r"\$\{[^}]+\}"), "template placeholder"),
        (
            _re.compile(r"\b(?:diagnosis|dx|condition|prescribed|medication)\b", _re.IGNORECASE),
            "clinical term",
        ),
    ]
    # Run the remaining PHI patterns against the template with placeholders
    # stripped, so an allowlisted placeholder cannot itself trip a pattern
    # (e.g. the clinical-term regex) while real violations elsewhere in the
    # copy still do.
    #
    # Must use strip_placeholders (== extract_placeholders'/render()'s own
    # \{\{\s*(\w+)\s*\}\} regex), NOT a broader hand-rolled pattern like
    # `\{\{[^}]+\}\}` — that broader pattern would also match and delete
    # something like {{123-45-6789}} or {{jane@example.com}}, which is
    # neither a recognized placeholder (extract_placeholders ignores it, so
    # the unknown-placeholder check above never fires) nor substituted by
    # render() (it's left untouched in the outbound SMS verbatim). Stripping
    # only well-formed tokens here ensures anything shaped like {{...}} but
    # not a valid placeholder stays in `scannable` for the PHI patterns below
    # to catch.
    #
    # Keep scanning the clinic value independently, and also screen the real
    # substitution boundaries: "123-{{ClinicName}}-6789" with clinicName "45"
    # assembles an SSN even though neither input matches the pattern alone.
    clinic_name = str(render_campaign.get("clinicName") or "")
    scannable = [strip_placeholders(tmpl), clinic_name]
    if rendered_for_screen is not None:
        scannable.append(rendered_for_screen)
    violations = [
        label for pattern, label in _PHI_PATTERNS
        if any(pattern.search(text) for text in scannable)
    ]
    if violations:
        errors.append(
            f"{prefix}: {field_name} may contain PHI — detected: "
            f"{', '.join(violations)}. Remove identifying information."
        )

    return errors


def _validate_sms_campaign(campaign: dict, bucket_name: str, ci: int) -> list[str]:
    """Return validation errors for an SMS campaign's required config fields + PHI guard."""
    errors = []
    cfg = campaign.get("campaignConfig") or {}
    prefix = f"bucket '{bucket_name}' campaign[{ci}]"

    tmpl = cfg.get("smsMessageTemplate", "")
    if not tmpl:
        errors.append(f"{prefix}: deliveryType='sms' requires campaignConfig.smsMessageTemplate")
    else:
        errors.extend(
            _screen_sms_template_content(tmpl, prefix, "smsMessageTemplate", cfg)
        )
        from vip_shared.domain.services.sms_template import extract_placeholders

        # An unset clinicName renders {{ClinicName}} as an empty string —
        # "This is ." shipped to a patient — so require the value whenever
        # the template actually references the placeholder. Mirrors the
        # identical guard in _validate_precall_sms below.
        if "ClinicName" in extract_placeholders(tmpl) and not str(cfg.get("clinicName") or "").strip():
            errors.append(
                f"{prefix}: smsMessageTemplate uses {{{{ClinicName}}}} but "
                f"campaignConfig.clinicName is not set"
            )

    if not cfg.get("smsOriginationNumberArn"):
        errors.append(
            f"{prefix}: deliveryType='sms' requires campaignConfig.smsOriginationNumberArn"
        )

    if not cfg.get("phiAcknowledged"):
        errors.append(
            f"{prefix}: deliveryType='sms' requires campaignConfig.phiAcknowledged=true"
        )

    return errors


def _validate_precall_sms(campaign: dict, bucket_name: str, ci: int) -> list[str]:
    """Return validation errors for campaignConfig.precallSms.

    Only validated when enabled=true — an unset/disabled block is inert
    config, not a save-time error. Content (placeholders, rendered length,
    PHI) is screened by the same _screen_sms_template_content helper
    _validate_sms_campaign uses, so the pre-call and bulk-SMS channels can
    never drift apart in what they allow.

    Two checks are specific to precall and have no bulk-SMS equivalent:

    - deliveryType must be a voice campaign — one of _VOICE_DELIVERY_TYPES
      ('campaign', 'branded', or 'journey') — a pre-call SMS on an 'sms'
      campaign has no dial to precede.
    - dependsOn must be empty — a campaign with dependsOn is never
      pre-warmed (see _fire_precall_sms's docstring in executor.py), so it
      never reaches "warming" with a real segmentArn and the SMS would
      silently never fire. Reject at save time instead of failing quietly
      at run time.
    """
    cfg = campaign.get("campaignConfig") or {}
    precall = cfg.get("precallSms") or {}
    if not precall.get("enabled"):
        return []

    prefix = f"bucket '{bucket_name}' campaign[{ci}]"
    errors: list[str] = []

    delivery_type = campaign.get("deliveryType", "campaign")
    if delivery_type not in _VOICE_DELIVERY_TYPES:
        errors.append(
            f"{prefix}: campaignConfig.precallSms is only valid on a voice "
            f"campaign (deliveryType one of {sorted(_VOICE_DELIVERY_TYPES)}), "
            f"not '{delivery_type}' — there is no dial for it to precede"
        )
        return errors

    if campaign.get("dependsOn"):
        errors.append(
            f"{prefix}: campaignConfig.precallSms cannot be combined with "
            f"dependsOn — a dependent campaign is never pre-warmed, so it has "
            f"no segmentArn at bucket activation and the pre-call SMS would "
            f"silently never fire"
        )

    tmpl = precall.get("messageTemplate", "")
    if not tmpl:
        errors.append(
            f"{prefix}: precallSms.enabled requires precallSms.messageTemplate"
        )
    else:
        from vip_shared.domain.services.sms_template import extract_placeholders

        errors.extend(
            _screen_sms_template_content(
                tmpl, prefix, "precallSms.messageTemplate", precall
            )
        )
        # An unset clinicName renders {{ClinicName}} as an empty string —
        # "This is ." shipped to a patient — so require the value whenever
        # the template actually references the placeholder.
        if "ClinicName" in extract_placeholders(tmpl) and not str(precall.get("clinicName") or "").strip():
            errors.append(
                f"{prefix}: precallSms.messageTemplate uses {{{{ClinicName}}}} "
                f"but precallSms.clinicName is not set"
            )

    if not precall.get("originationNumberArn"):
        errors.append(
            f"{prefix}: precallSms.enabled requires precallSms.originationNumberArn"
        )

    return errors


_ALLOWED_MAX_LEAD_AGE_MINUTES = {None, 10, 15, 20, 25, 30, 35}


def _validate_max_lead_age(campaign: dict, bucket: dict, bucket_name: str, ci: int) -> list[str]:
    """Return validation errors for the maxLeadAgeMinutes lead-age filter.

    Mirrors executor._create_segment's filter-source selection exactly: a
    legacy-bucket campaign (campaign["_legacyBucket"] truthy) reads
    maxLeadAgeMinutes from bucket["segmentFilters"], not from the campaign
    dict — validating only the campaign-level field would let a
    client-supplied "_legacyBucket": true flag bypass this check entirely.

    The frontend <select> only emits None/10/15/20/25/30/35, but that's a
    client-side constraint, not a trust boundary — a direct API call or a
    stale/duplicated plan record could carry any value.
    """
    if campaign.get("_legacyBucket"):
        val = bucket.get("segmentFilters", {}).get("maxLeadAgeMinutes")
    else:
        val = campaign.get("maxLeadAgeMinutes")
    try:
        is_allowed = val in _ALLOWED_MAX_LEAD_AGE_MINUTES
    except TypeError:
        # Unhashable JSON type (list/dict) — parse_body enforces no schema,
        # so this is reachable from a raw request body, not just internal data.
        is_allowed = False
    if not is_allowed:
        prefix = f"bucket '{bucket_name}' campaign[{ci}]"
        msg = f"{prefix}: maxLeadAgeMinutes must be omitted/null or one of 10,15,20,25,30,35 (got {val!r})"
        return [msg]
    return []


def _validate_plan_body(plan_body: dict) -> list[str]:
    """Validate plan body; return a list of error strings (empty means valid)."""
    errors: list[str] = []
    for bi, bucket in enumerate(plan_body.get("buckets", [])):
        bucket_name = bucket.get("name", str(bi))
        for ci, campaign in enumerate(bucket.get("campaigns", [])):
            if campaign.get("deliveryType") == "branded":
                errors.extend(
                    _validate_branded_campaign(campaign, bucket_name, ci)
                )
            elif campaign.get("deliveryType") == "sms":
                errors.extend(
                    _validate_sms_campaign(campaign, bucket_name, ci)
                )
            errors.extend(_validate_precall_sms(campaign, bucket_name, ci))
            errors.extend(_validate_max_lead_age(campaign, bucket, bucket_name, ci))
    return errors


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
