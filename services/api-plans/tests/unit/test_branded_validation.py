"""Tests for branded campaign field validation in plan save boundary."""
from __future__ import annotations

import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

# Import _validate_plan_body while keeping sys.modules clean for other test
# modules.  patch.dict restores the original mapping when the block exits.
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
    from handlers.plans import _validate_plan_body  # noqa: E402


def _validate_plan(plan_body):
    """Thin wrapper so test assertions stay readable."""
    return _validate_plan_body(plan_body)


class TestBrandedCampaignValidation:
    def _valid_branded_campaign(self):
        return {
            "id": "bc-1",
            "name": "Branded",
            "deliveryType": "branded",
            "campaignConfig": {
                "dialerType": "progressive",
                "queueArn": "arn:aws:connect:us-east-1:123:instance/i/queue/q",
                "contactFlowId": "flow-abc",
                "sourcePhone": "+12125550199",
            },
        }

    def test_valid_branded_campaign_passes_validation(self):
        campaign = self._valid_branded_campaign()
        bucket = {
            "name": "b1",
            "campaigns": [campaign],
            "startTime": "09:00",
            "endTime": "17:00",
        }
        plan = {
            "name": "Test Plan",
            "timezone": "America/New_York",
            "buckets": [bucket],
        }
        errors = _validate_plan(plan)
        assert errors == [] or errors is None

    def test_missing_queue_arn_fails_validation(self):
        campaign = self._valid_branded_campaign()
        del campaign["campaignConfig"]["queueArn"]
        bucket = {
            "name": "b1",
            "campaigns": [campaign],
            "startTime": "09:00",
            "endTime": "17:00",
        }
        plan = {
            "name": "Test Plan",
            "timezone": "America/New_York",
            "buckets": [bucket],
        }
        errors = _validate_plan(plan)
        assert any("queueArn" in str(e) for e in (errors or []))

    def test_missing_contact_flow_id_fails_validation(self):
        campaign = self._valid_branded_campaign()
        del campaign["campaignConfig"]["contactFlowId"]
        bucket = {"name": "b1", "campaigns": [campaign]}
        plan = {
            "name": "Test Plan",
            "timezone": "America/New_York",
            "buckets": [bucket],
        }
        errors = _validate_plan(plan)
        assert any("contactFlowId" in str(e) for e in (errors or []))

    def test_missing_source_phone_fails_validation(self):
        campaign = self._valid_branded_campaign()
        del campaign["campaignConfig"]["sourcePhone"]
        bucket = {"name": "b1", "campaigns": [campaign]}
        plan = {
            "name": "Test Plan",
            "timezone": "America/New_York",
            "buckets": [bucket],
        }
        errors = _validate_plan(plan)
        assert any("sourcePhone" in str(e) for e in (errors or []))

    def test_non_branded_campaign_not_affected(self):
        campaign = {
            "id": "c-1",
            "name": "Connect",
            "deliveryType": "connect",
            "campaignConfig": {"dialerType": "progressive", "bandwidthAllocation": 1.0},
        }
        bucket = {"name": "b1", "campaigns": [campaign]}
        plan = {
            "name": "Test Plan",
            "timezone": "America/New_York",
            "buckets": [bucket],
        }
        errors = _validate_plan(plan)
        assert errors == [] or errors is None

    def test_error_message_does_not_echo_source_phone_value(self):
        """sourcePhone is PHI — its value must never appear in error messages."""
        campaign = self._valid_branded_campaign()
        del campaign["campaignConfig"]["sourcePhone"]
        bucket = {"name": "b1", "campaigns": [campaign]}
        plan = {
            "name": "Test Plan",
            "timezone": "America/New_York",
            "buckets": [bucket],
        }
        errors = _validate_plan(plan)
        # The error must name the field but not echo any PHI value
        assert any("sourcePhone" in str(e) for e in (errors or []))
        phone_value = "+12125550199"
        for err in errors or []:
            assert phone_value not in str(err), (
                "PHI value leaked into validation error message"
            )

    def test_all_three_missing_reports_three_errors(self):
        campaign = {
            "id": "bc-2",
            "name": "Branded incomplete",
            "deliveryType": "branded",
            "campaignConfig": {"dialerType": "progressive"},
        }
        bucket = {"name": "bucket-a", "campaigns": [campaign]}
        plan = {"name": "P", "buckets": [bucket]}
        errors = _validate_plan(plan)
        field_names = {"queueArn", "contactFlowId", "sourcePhone"}
        reported = {f for f in field_names if any(f in e for e in errors or [])}
        assert reported == field_names


# ── update_plan: template flag must disable EventBridge rule ──────────────────

def _make_update_plan_fn(existing_plan: dict, body: dict):
    """Return a callable update_plan with all deps mocked, plus the mock_scheduler."""
    import importlib

    mock_store = MagicMock()
    mock_scheduler = MagicMock()
    mock_audit = MagicMock()
    mock_audit.record = MagicMock()

    mock_store.get_plan.return_value = existing_plan
    mock_store.put_plan.return_value = {**existing_plan, **body}

    stubs = {
        "store": mock_store,
        "scheduler_manager": mock_scheduler,
        "vip_shared": MagicMock(),
        "vip_shared.application": MagicMock(),
        "vip_shared.application.http": MagicMock(),
        "vip_shared.infrastructure": MagicMock(),
        "vip_shared.infrastructure.persistence": MagicMock(),
        "vip_shared.infrastructure.persistence.audit": MagicMock(),
    }
    with patch.dict(sys.modules, stubs):
        import handlers.plans as plans_mod
        importlib.reload(plans_mod)

    # patch_body returns the body dict; build_audit returns the audit mock
    with (
        patch.object(plans_mod, "store", mock_store),
        patch.object(plans_mod, "scheduler_manager", mock_scheduler),
        patch.object(plans_mod, "build_audit", return_value=mock_audit),
        patch.object(plans_mod, "parse_body", return_value=body),
        patch.object(plans_mod, "extract_caller", return_value=MagicMock(sub="t", email="t@t.com", ip_address="", user_agent="")),
    ):
        plans_mod.update_plan({}, {"id": existing_plan["planId"]})

    return mock_scheduler


class TestUpdatePlanTemplateSchedule:
    """Regression: marking a plan as isTemplate must remove its EventBridge cron rule."""

    def test_marking_as_template_deletes_eventbridge_rule(self):
        """isTemplate=true on a time-triggered plan must delete the rule, not upsert."""
        existing = {
            "planId": "plan-1",
            "name": "Plan 1",
            "trigger": {"type": "time", "time": "08:40"},
            "isTemplate": False,
            "buckets": [],
        }
        body = {"trigger": {"type": "time", "time": "08:40"}, "isTemplate": True}

        mock_scheduler = _make_update_plan_fn(existing, body)

        mock_scheduler.delete_schedule.assert_called_once_with("plan-1")
        mock_scheduler.upsert_schedule.assert_not_called()

    def test_non_template_time_trigger_upserts_rule(self):
        """isTemplate=false with a time trigger must upsert the rule."""
        existing = {
            "planId": "plan-2",
            "name": "Plan 2",
            "trigger": {"type": "manual"},
            "isTemplate": False,
            "buckets": [],
        }
        body = {"trigger": {"type": "time", "time": "09:00"}, "isTemplate": False}

        mock_scheduler = _make_update_plan_fn(existing, body)

        mock_scheduler.upsert_schedule.assert_called_once()
        mock_scheduler.delete_schedule.assert_not_called()
