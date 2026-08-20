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
