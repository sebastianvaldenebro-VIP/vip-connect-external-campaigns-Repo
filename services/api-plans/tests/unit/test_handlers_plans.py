"""Tests for handlers/plans.py — POST /plans/resolve-campaign-flow.

No existing test file is a generic catch-all for handlers/plans.py — the
existing ones (test_location_mapping.py, test_branded_validation.py,
test_plan_validation_sms.py) are each scoped to one specific handler
behaviour. resolve_campaign_flow is a new, distinct handler, so it gets its
own file here rather than being bolted onto an unrelated one.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

_stub_modules = {
    "store": MagicMock(),
    "scheduler_manager": MagicMock(),
    "vip_shared": MagicMock(),
    "vip_shared.application": MagicMock(),
    "vip_shared.application.http": MagicMock(),
    "vip_shared.infrastructure": MagicMock(),
    "vip_shared.infrastructure.persistence": MagicMock(),
    "vip_shared.infrastructure.persistence.audit": MagicMock(),
}
with patch.dict(sys.modules, _stub_modules):
    with patch("boto3.client"), patch("boto3.resource"):
        import handlers.plans as plans_handler  # noqa: E402

# parse_body/json_response were stubbed as MagicMock (vip_shared lives in a
# Lambda layer unavailable in the test env) — replace with real
# implementations so handler assertions can parse real request/response bodies.


def _parse_body(event: dict) -> dict:
    raw = event.get("body") or "{}"
    return json.loads(raw) if isinstance(raw, str) else raw


def _json_response(status: int, body: object) -> dict:
    return {"statusCode": status, "body": json.dumps(body)}


plans_handler.parse_body = _parse_body  # type: ignore[attr-defined]
plans_handler.json_response = _json_response  # type: ignore[attr-defined]


def test_resolve_campaign_flow_returns_arn(monkeypatch):
    monkeypatch.setattr(
        plans_handler.builders,
        "resolve_campaign_flow_arn",
        lambda states, instance_id: "arn:aws:connect:us-east-1:165505826690:instance/x/contact-flow/y",
    )
    event = {"body": '{"states": ["PA"]}'}
    resp = plans_handler.resolve_campaign_flow(event, {})
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["arn"] == "arn:aws:connect:us-east-1:165505826690:instance/x/contact-flow/y"


def test_resolve_campaign_flow_returns_null_when_not_found(monkeypatch):
    monkeypatch.setattr(
        plans_handler.builders,
        "resolve_campaign_flow_arn",
        lambda states, instance_id: None,
    )
    event = {"body": '{"states": ["ZZ"]}'}
    resp = plans_handler.resolve_campaign_flow(event, {})
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["arn"] is None


def test_resolve_campaign_flow_requires_states(monkeypatch):
    event = {"body": '{"states": []}'}
    resp = plans_handler.resolve_campaign_flow(event, {})
    assert resp["statusCode"] == 400


def test_resolve_campaign_flow_rejects_bare_string_states(monkeypatch):
    """A bare string is truthy and iterable in Python — states="PA" must be
    rejected, not silently accepted and later truncated to state_codes[0]
    == "P" inside builders.resolve_campaign_flow_arn.
    """
    event = {"body": '{"states": "PA"}'}
    resp = plans_handler.resolve_campaign_flow(event, {})
    assert resp["statusCode"] == 400


def test_resolve_campaign_flow_rejects_non_string_elements(monkeypatch):
    event = {"body": '{"states": ["PA", 123]}'}
    resp = plans_handler.resolve_campaign_flow(event, {})
    assert resp["statusCode"] == 400


def test_resolve_campaign_flow_records_audit_entry(monkeypatch):
    monkeypatch.setattr(
        plans_handler.builders,
        "resolve_campaign_flow_arn",
        lambda states, instance_id: "arn:aws:connect:us-east-1:165505826690:instance/x/contact-flow/y",
    )
    fake_audit = MagicMock()
    monkeypatch.setattr(plans_handler, "build_audit", lambda: fake_audit)

    event = {"body": '{"states": ["PA"]}'}
    resp = plans_handler.resolve_campaign_flow(event, {})

    assert resp["statusCode"] == 200
    fake_audit.record.assert_called_once()
    call_kwargs = fake_audit.record.call_args.kwargs
    assert call_kwargs["entity_type"] == "campaign_flow"
    assert call_kwargs["action"] == "resolve"
    assert call_kwargs["after"]["states"] == ["PA"]


# ── update_plan vip-sched-* cleanup (audit follow-up, 2026-08-21) ─────────────
# Previously the whole is_template/had_time_trigger cleanup block only ran when
# the PATCH body resent "trigger" — a PATCH that only sent {"isTemplate": true}
# left an existing vip-sched-* rule alive forever. Confirmed live: plan
# c63d695c-b99e-4885-808a-8eca91d08e8e.


def _existing_plan(*, is_template: bool, trigger: dict) -> dict:
    return {
        "planId": "plan-1",
        "name": "Test Plan",
        "isTemplate": is_template,
        "trigger": trigger,
        "buckets": [],
    }


def test_update_plan_deletes_schedule_when_template_set_without_trigger_resend(
    monkeypatch,
):
    fake_sched = MagicMock()
    monkeypatch.setattr(plans_handler, "scheduler_manager", fake_sched)
    monkeypatch.setattr(
        plans_handler.store,
        "get_plan",
        lambda plan_id: _existing_plan(
            is_template=False, trigger={"type": "time", "time": "08:40"}
        ),
    )
    monkeypatch.setattr(plans_handler.store, "put_plan", lambda p: p)
    monkeypatch.setattr(plans_handler, "build_audit", lambda: MagicMock())

    event = {"body": json.dumps({"isTemplate": True})}
    resp = plans_handler.update_plan(event, {"id": "plan-1"})

    assert resp["statusCode"] == 200
    fake_sched.delete_schedule.assert_called_once_with("plan-1")
    fake_sched.upsert_schedule.assert_not_called()


def test_update_plan_keeps_schedule_when_unrelated_field_updated(monkeypatch):
    """An update that touches neither isTemplate nor trigger must not disturb
    an existing, still-valid time-triggered schedule."""
    fake_sched = MagicMock()
    monkeypatch.setattr(plans_handler, "scheduler_manager", fake_sched)
    monkeypatch.setattr(
        plans_handler.store,
        "get_plan",
        lambda plan_id: _existing_plan(
            is_template=False, trigger={"type": "time", "time": "08:40"}
        ),
    )
    monkeypatch.setattr(plans_handler.store, "put_plan", lambda p: p)
    monkeypatch.setattr(plans_handler, "build_audit", lambda: MagicMock())

    event = {"body": json.dumps({"name": "renamed"})}
    resp = plans_handler.update_plan(event, {"id": "plan-1"})

    assert resp["statusCode"] == 200
    fake_sched.delete_schedule.assert_not_called()
    fake_sched.upsert_schedule.assert_not_called()


def test_update_plan_upserts_when_trigger_explicitly_set_to_time(monkeypatch):
    fake_sched = MagicMock()
    monkeypatch.setattr(plans_handler, "scheduler_manager", fake_sched)
    monkeypatch.setattr(
        plans_handler.store,
        "get_plan",
        lambda plan_id: _existing_plan(is_template=False, trigger={"type": "manual"}),
    )
    monkeypatch.setattr(plans_handler.store, "put_plan", lambda p: p)
    monkeypatch.setattr(plans_handler, "build_audit", lambda: MagicMock())

    new_trigger = {"type": "time", "time": "09:00"}
    event = {"body": json.dumps({"trigger": new_trigger})}
    resp = plans_handler.update_plan(event, {"id": "plan-1"})

    assert resp["statusCode"] == 200
    fake_sched.upsert_schedule.assert_called_once_with("plan-1", new_trigger)
    fake_sched.delete_schedule.assert_not_called()


def test_update_plan_deletes_schedule_when_trigger_changes_away_from_time(monkeypatch):
    fake_sched = MagicMock()
    monkeypatch.setattr(plans_handler, "scheduler_manager", fake_sched)
    monkeypatch.setattr(
        plans_handler.store,
        "get_plan",
        lambda plan_id: _existing_plan(
            is_template=False, trigger={"type": "time", "time": "08:40"}
        ),
    )
    monkeypatch.setattr(plans_handler.store, "put_plan", lambda p: p)
    monkeypatch.setattr(plans_handler, "build_audit", lambda: MagicMock())

    event = {"body": json.dumps({"trigger": {"type": "manual"}})}
    resp = plans_handler.update_plan(event, {"id": "plan-1"})

    assert resp["statusCode"] == 200
    fake_sched.delete_schedule.assert_called_once_with("plan-1")
    fake_sched.upsert_schedule.assert_not_called()


def test_update_plan_template_takes_precedence_over_resent_time_trigger(monkeypatch):
    """isTemplate=true must win even if the SAME PATCH also resends a "time"
    trigger — templates never run on a cron, regardless of what else is sent."""
    fake_sched = MagicMock()
    monkeypatch.setattr(plans_handler, "scheduler_manager", fake_sched)
    monkeypatch.setattr(
        plans_handler.store,
        "get_plan",
        lambda plan_id: _existing_plan(
            is_template=False, trigger={"type": "time", "time": "08:40"}
        ),
    )
    monkeypatch.setattr(plans_handler.store, "put_plan", lambda p: p)
    monkeypatch.setattr(plans_handler, "build_audit", lambda: MagicMock())

    event = {
        "body": json.dumps(
            {"isTemplate": True, "trigger": {"type": "time", "time": "09:00"}}
        )
    }
    resp = plans_handler.update_plan(event, {"id": "plan-1"})

    assert resp["statusCode"] == 200
    fake_sched.delete_schedule.assert_called_once_with("plan-1")
    fake_sched.upsert_schedule.assert_not_called()
