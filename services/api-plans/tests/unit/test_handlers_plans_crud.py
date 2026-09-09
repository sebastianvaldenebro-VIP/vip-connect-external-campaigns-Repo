"""Tests for the plan CRUD handlers in handlers/plans.py: list_plans,
list_templates, get_plan, create_plan, update_plan, delete_plan,
clone_from_template, plus the smaller validation helpers (_require,
_validate_dag's dependsOn/cycle checks, _validate_trigger_no_cycle).

Mirrors test_handlers_plans.py's / test_branded_validation.py's convention:
store/scheduler_manager/vip_shared are stubbed inside a scoped
patch.dict(sys.modules, ...) block around the import (builders/executor are
left real — nothing here calls into them). parse_body/json_response/
extract_caller are then replaced with real/controlled implementations so
response bodies and audit calls can be asserted on directly.
"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

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


def _parse_body(event: dict) -> dict:
    raw = event.get("body") or "{}"
    return json.loads(raw) if isinstance(raw, str) else raw


def _json_response(status: int, body: object) -> dict:
    return {"statusCode": status, "body": json.dumps(body, default=str)}


_CALLER = SimpleNamespace(
    sub="user-1", email="user@medwork.io", ip_address="1.2.3.4", user_agent="test-agent"
)

plans_handler.parse_body = _parse_body  # type: ignore[attr-defined]
plans_handler.json_response = _json_response  # type: ignore[attr-defined]
plans_handler.extract_caller = lambda event: _CALLER  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _reset_mocks():
    # reset_mock() alone only clears call history — return_value/side_effect
    # configured by a PRIOR test persist otherwise (side_effect in particular
    # takes precedence over a later test's return_value, silently reusing the
    # wrong fake), so explicitly reset both here too.
    plans_handler.store.reset_mock(return_value=True, side_effect=True)
    plans_handler.scheduler_manager.reset_mock(return_value=True, side_effect=True)
    plans_handler.build_audit.reset_mock(return_value=True, side_effect=True)
    yield


def _event(body=None):
    return {"body": json.dumps(body) if body is not None else None}


def _plan(plan_id="p1", **overrides):
    base = {
        "planId": plan_id,
        "name": "Plan",
        "description": "",
        "trigger": {"type": "manual"},
        "loop": None,
        "workingHours": None,
        "buckets": [],
        "isTemplate": False,
        "isDefault": False,
        "createdAt": "t0",
        "updatedAt": "t0",
        "is_template": False,
        "is_default": False,
    }
    base.update(overrides)
    return base


class TestListPlans:
    def test_returns_plans_sorted_by_updated_at_desc_with_latest_run(self):
        plans_handler.store.list_plans.return_value = [
            _plan("p1", updatedAt="2026-01-01T00:00:00"),
            _plan("p2", updatedAt="2026-02-01T00:00:00"),
        ]
        plans_handler.store.get_latest_run.side_effect = lambda pid: {"runId": f"run-{pid}"}

        response = plans_handler.list_plans({}, {})

        body = json.loads(response["body"])
        assert [p["planId"] for p in body["plans"]] == ["p2", "p1"]
        assert body["plans"][0]["latestRun"] == {"runId": "run-p2"}


class TestListTemplates:
    def test_filters_to_templates_only(self):
        plans_handler.store.list_plans.return_value = [
            _plan("p1", isTemplate=False),
            _plan("p2", isTemplate=True, updatedAt="2026-02-01T00:00:00"),
        ]

        response = plans_handler.list_templates({}, {})

        body = json.loads(response["body"])
        assert [p["planId"] for p in body["plans"]] == ["p2"]


class TestGetPlan:
    def test_returns_404_when_missing(self):
        plans_handler.store.get_plan.return_value = None
        response = plans_handler.get_plan({}, {"id": "missing"})
        assert response["statusCode"] == 404

    def test_returns_plan_with_latest_run(self):
        plans_handler.store.get_plan.return_value = _plan("p1")
        plans_handler.store.get_latest_run.return_value = {"runId": "r1"}

        response = plans_handler.get_plan({}, {"id": "p1"})

        body = json.loads(response["body"])
        assert response["statusCode"] == 200
        assert body["plan"]["planId"] == "p1"
        assert body["latestRun"] == {"runId": "r1"}


class TestCreatePlan:
    def _valid_body(self, **overrides):
        body = {
            "name": "New Plan",
            "buckets": [{"id": "b0", "campaigns": [{"id": "c0", "name": "C0"}]}],
        }
        body.update(overrides)
        return body

    def test_creates_plan_and_records_audit(self):
        plans_handler.store.list_plans.return_value = []
        plans_handler.store.put_plan.return_value = _plan("p1", name="New Plan", buckets=self._valid_body()["buckets"])

        response = plans_handler.create_plan(_event(self._valid_body()), {})

        assert response["statusCode"] == 201
        plans_handler.store.put_plan.assert_called_once()
        plans_handler.build_audit.return_value.record.assert_called_once()
        audit_kwargs = plans_handler.build_audit.return_value.record.call_args.kwargs
        assert audit_kwargs["action"] == "create"
        assert audit_kwargs["actor_sub"] == "user-1"

    def test_requires_name(self):
        with pytest.raises(ValueError, match="Missing required fields"):
            plans_handler.create_plan(_event({"buckets": []}), {})

    def test_requires_at_least_one_bucket(self):
        with pytest.raises(ValueError, match="at least one bucket"):
            plans_handler.create_plan(_event({"name": "X", "buckets": []}), {})

    def test_duplicate_shorthand_clones_source_plan(self):
        source = _plan("src-1", name="Source", buckets=[{"id": "b0", "campaigns": [{"id": "c0", "name": "C0"}]}])
        plans_handler.store.get_plan.return_value = source
        plans_handler.store.list_plans.return_value = []
        plans_handler.store.put_plan.side_effect = lambda body: _plan("new-id", **body)

        response = plans_handler.create_plan(
            _event({"_duplicateFromId": "src-1", "name": "Copy of Source"}), {}
        )

        assert response["statusCode"] == 201
        put_call = plans_handler.store.put_plan.call_args.args[0]
        assert put_call["name"] == "Copy of Source"
        assert put_call["trigger"] == {"type": "manual"}

    def test_duplicate_shorthand_404s_when_source_missing(self):
        plans_handler.store.get_plan.return_value = None
        response = plans_handler.create_plan(
            _event({"_duplicateFromId": "missing", "name": "X"}), {}
        )
        assert response["statusCode"] == 404

    def test_rejects_branded_campaign_missing_required_fields(self):
        body = self._valid_body(
            buckets=[
                {
                    "id": "b0",
                    "campaigns": [
                        {"id": "c0", "name": "C0", "deliveryType": "branded", "campaignConfig": {}}
                    ],
                }
            ]
        )
        response = plans_handler.create_plan(_event(body), {})
        assert response["statusCode"] == 400
        body_resp = json.loads(response["body"])
        assert body_resp["error"]["code"] == "VALIDATION_ERROR"

    def test_time_trigger_upserts_schedule(self):
        plans_handler.store.list_plans.return_value = []
        plans_handler.store.put_plan.return_value = _plan(
            "p1", trigger={"type": "time", "time": "08:00"}
        )

        plans_handler.create_plan(
            _event(self._valid_body(trigger={"type": "time", "time": "08:00"})), {}
        )

        plans_handler.scheduler_manager.upsert_schedule.assert_called_once_with(
            "p1", {"type": "time", "time": "08:00"}
        )

    def test_schedule_field_enabled_upserts_schedule_when_trigger_not_time(self):
        """create_plan's `elif plan.get("schedule") and enabled` branch — a
        plan with a non-"time" trigger can still carry a "schedule" field
        (legacy shape) that must upsert a rule."""
        plans_handler.store.list_plans.return_value = []
        plans_handler.store.put_plan.return_value = _plan(
            "p1", trigger={"type": "manual"}, schedule={"enabled": True, "hour": 8}
        )

        plans_handler.create_plan(_event(self._valid_body()), {})

        plans_handler.scheduler_manager.upsert_schedule.assert_called_once_with(
            "p1", {"enabled": True, "hour": 8}
        )

    def test_on_plan_complete_trigger_validates_no_cycle(self):
        plans_handler.store.list_plans.return_value = [
            _plan("upstream-1", trigger={"type": "on_plan_complete", "planId": "new-plan-would-be"})
        ]
        body = self._valid_body(
            trigger={"type": "on_plan_complete", "planId": "upstream-1"}
        )
        plans_handler.store.put_plan.return_value = _plan("p1", trigger=body["trigger"])
        # No cycle here (current_plan_id is None on create) — should succeed.
        response = plans_handler.create_plan(_event(body), {})
        assert response["statusCode"] == 201


class TestUpdatePlan:
    def test_returns_404_when_plan_missing(self):
        plans_handler.store.get_plan.return_value = None
        response = plans_handler.update_plan(_event({"name": "X"}), {"id": "missing"})
        assert response["statusCode"] == 404

    def test_updates_allowed_fields_only(self):
        existing = _plan("p1", name="Old", description="old desc")
        plans_handler.store.get_plan.return_value = existing
        plans_handler.store.put_plan.side_effect = lambda body: body

        response = plans_handler.update_plan(
            _event({"name": "New Name", "notAllowedField": "x"}), {"id": "p1"}
        )

        assert response["statusCode"] == 200
        put_call = plans_handler.store.put_plan.call_args.args[0]
        assert put_call["name"] == "New Name"
        assert "notAllowedField" not in put_call

    def test_validates_on_plan_complete_trigger_cycle(self):
        existing = _plan("p1", trigger={"type": "manual"})
        plans_handler.store.get_plan.return_value = existing
        plans_handler.store.list_plans.return_value = [_plan("p1")]
        plans_handler.store.put_plan.side_effect = lambda body: body

        # Trigger points at itself -> cycle
        response_body = {"trigger": {"type": "on_plan_complete", "planId": "p1"}}
        with pytest.raises(ValueError, match="cycle"):
            plans_handler.update_plan(_event(response_body), {"id": "p1"})

    def test_validates_buckets_when_present_in_body(self):
        existing = _plan("p1")
        plans_handler.store.get_plan.return_value = existing

        body = {
            "buckets": [
                {
                    "id": "b0",
                    "campaigns": [
                        {"id": "c0", "deliveryType": "branded", "campaignConfig": {}}
                    ],
                }
            ]
        }
        response = plans_handler.update_plan(_event(body), {"id": "p1"})
        assert response["statusCode"] == 400

    def test_valid_bucket_update_passes_dag_validation_and_saves(self):
        """A buckets update that passes _validate_plan_body must actually
        reach and pass _validate_dag (not just short-circuit on validation
        errors) before being persisted."""
        existing = _plan("p1")
        plans_handler.store.get_plan.return_value = existing
        plans_handler.store.put_plan.side_effect = lambda body: body

        body = {
            "buckets": [
                {"id": "b0", "campaigns": [{"id": "c0", "name": "C0"}]},
            ]
        }
        response = plans_handler.update_plan(_event(body), {"id": "p1"})
        assert response["statusCode"] == 200

    def test_template_flag_deletes_existing_time_schedule(self):
        existing = _plan("p1", trigger={"type": "time", "time": "08:00"})
        plans_handler.store.get_plan.return_value = existing
        plans_handler.store.put_plan.side_effect = lambda body: body

        plans_handler.update_plan(_event({"isTemplate": True}), {"id": "p1"})

        plans_handler.scheduler_manager.delete_schedule.assert_called_once_with("p1")
        plans_handler.scheduler_manager.upsert_schedule.assert_not_called()

    def test_new_time_trigger_upserts_schedule(self):
        existing = _plan("p1", trigger={"type": "manual"})
        plans_handler.store.get_plan.return_value = existing
        plans_handler.store.put_plan.side_effect = lambda body: body

        plans_handler.update_plan(
            _event({"trigger": {"type": "time", "time": "09:00"}}), {"id": "p1"}
        )

        plans_handler.scheduler_manager.upsert_schedule.assert_called_once_with(
            "p1", {"type": "time", "time": "09:00"}
        )

    def test_trigger_changed_away_from_time_deletes_schedule(self):
        existing = _plan("p1", trigger={"type": "time", "time": "08:00"})
        plans_handler.store.get_plan.return_value = existing
        plans_handler.store.put_plan.side_effect = lambda body: body

        plans_handler.update_plan(_event({"trigger": {"type": "manual"}}), {"id": "p1"})

        plans_handler.scheduler_manager.delete_schedule.assert_called_once_with("p1")

    def test_schedule_enabled_upserts_when_not_template(self):
        existing = _plan("p1")
        plans_handler.store.get_plan.return_value = existing
        plans_handler.store.put_plan.side_effect = lambda body: body

        plans_handler.update_plan(
            _event({"schedule": {"enabled": True, "hour": 8}}), {"id": "p1"}
        )

        plans_handler.scheduler_manager.upsert_schedule.assert_called_once_with(
            "p1", {"enabled": True, "hour": 8}
        )

    def test_schedule_disabled_deletes_rule(self):
        existing = _plan("p1")
        plans_handler.store.get_plan.return_value = existing
        plans_handler.store.put_plan.side_effect = lambda body: body

        plans_handler.update_plan(
            _event({"schedule": {"enabled": False}}), {"id": "p1"}
        )

        plans_handler.scheduler_manager.delete_schedule.assert_called_once_with("p1")


class TestDeletePlan:
    def test_returns_404_when_missing(self):
        plans_handler.store.get_plan.return_value = None
        response = plans_handler.delete_plan({}, {"id": "missing"})
        assert response["statusCode"] == 404

    def test_deletes_plan_and_schedule_and_resets_downstream_triggers(self):
        existing = _plan("p1", name="To Delete")
        plans_handler.store.get_plan.return_value = existing
        plans_handler.store.find_plans_by_trigger_planid.return_value = [
            _plan("downstream-1")
        ]

        response = plans_handler.delete_plan({}, {"id": "p1"})

        assert response["statusCode"] == 204
        plans_handler.store.delete_plan.assert_called_once_with("p1")
        plans_handler.scheduler_manager.delete_schedule.assert_called_once_with("p1")
        plans_handler.store.update_plan_trigger.assert_called_once_with(
            "downstream-1", {"type": "manual"}
        )
        audit_kwargs = plans_handler.build_audit.return_value.record.call_args.kwargs
        assert audit_kwargs["action"] == "delete"
        assert audit_kwargs["before"]["name"] == "To Delete"


class TestCloneFromTemplate:
    def test_returns_404_when_template_missing(self):
        plans_handler.store.get_plan.return_value = None
        response = plans_handler.clone_from_template({}, {"tid": "missing"})
        assert response["statusCode"] == 404

    def test_returns_400_when_plan_is_not_a_template(self):
        plans_handler.store.get_plan.return_value = _plan("p1", isTemplate=False)
        response = plans_handler.clone_from_template({}, {"tid": "p1"})
        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"]["code"] == "NOT_A_TEMPLATE"


class TestRequire:
    def test_raises_when_field_missing(self):
        with pytest.raises(ValueError, match="Missing required fields: name"):
            plans_handler._require({}, ("name",))

    def test_raises_when_field_is_none(self):
        with pytest.raises(ValueError, match="Missing required fields: name"):
            plans_handler._require({"name": None}, ("name",))

    def test_passes_when_all_fields_present(self):
        plans_handler._require({"name": "x"}, ("name",))  # must not raise


class TestValidateDagAdditionalBranches:
    def test_rejects_time_based_bucket_with_too_short_duration(self):
        buckets = [{"run_mode": "time_based", "duration_minutes": 5, "campaigns": []}]
        with pytest.raises(ValueError, match="too short"):
            plans_handler._validate_dag(buckets)

    def test_accepts_time_based_bucket_with_sufficient_duration(self):
        buckets = [{"run_mode": "time_based", "duration_minutes": 10, "campaigns": []}]
        plans_handler._validate_dag(buckets)  # must not raise

    def test_rejects_self_dependency(self):
        buckets = [
            {
                "campaigns": [
                    {"id": "c0", "name": "C0", "dependsOn": ["c0"]},
                ]
            }
        ]
        with pytest.raises(ValueError, match="cannot depend on itself"):
            plans_handler._validate_dag(buckets)

    def test_rejects_dependency_on_unknown_campaign(self):
        buckets = [
            {"campaigns": [{"id": "c0", "name": "C0", "dependsOn": ["ghost"]}]}
        ]
        with pytest.raises(ValueError, match="unknown campaign"):
            plans_handler._validate_dag(buckets)

    def test_rejects_dependency_on_later_bucket(self):
        buckets = [
            {"campaigns": [{"id": "c0", "name": "C0", "dependsOn": ["c1"]}]},
            {"campaigns": [{"id": "c1", "name": "C1"}]},
        ]
        with pytest.raises(ValueError, match="cannot depend on campaign"):
            plans_handler._validate_dag(buckets)

    def test_rejects_dependency_cycle_across_two_campaigns(self):
        buckets = [
            {
                "campaigns": [
                    {"id": "c0", "name": "C0", "dependsOn": ["c1"]},
                    {"id": "c1", "name": "C1", "dependsOn": ["c0"]},
                ]
            }
        ]
        with pytest.raises(ValueError, match="dependency cycle"):
            plans_handler._validate_dag(buckets)

    def test_accepts_valid_dag_with_dependency(self):
        buckets = [
            {"campaigns": [{"id": "c0", "name": "C0"}]},
            {"campaigns": [{"id": "c1", "name": "C1", "dependsOn": ["c0"]}]},
        ]
        plans_handler._validate_dag(buckets)  # must not raise


class TestValidateTriggerNoCycle:
    def test_returns_early_for_non_on_plan_complete_trigger(self):
        # Must not raise / not even inspect all_plans.
        plans_handler._validate_trigger_no_cycle("p1", {"type": "manual"}, [])

    def test_raises_when_target_plan_is_the_current_plan(self):
        with pytest.raises(ValueError, match="cycle"):
            plans_handler._validate_trigger_no_cycle(
                "p1", {"type": "on_plan_complete", "planId": "p1"}, []
            )

    def test_raises_for_indirect_cycle_through_chain(self):
        all_plans = [
            _plan("p2", trigger={"type": "on_plan_complete", "planId": "p1"}),
        ]
        with pytest.raises(ValueError, match="cycle"):
            plans_handler._validate_trigger_no_cycle(
                "p1", {"type": "on_plan_complete", "planId": "p2"}, all_plans
            )

    def test_passes_for_acyclic_chain(self):
        all_plans = [
            _plan("p2", trigger={"type": "manual"}),
        ]
        plans_handler._validate_trigger_no_cycle(
            "p1", {"type": "on_plan_complete", "planId": "p2"}, all_plans
        )  # must not raise

    def test_visited_guard_prevents_infinite_loop_on_unrelated_upstream_cycle(self):
        """An existing on_plan_complete cycle among OTHER plans (A->B->C->A)
        that does not involve current_plan_id must not infinite-loop and
        must not falsely raise — the visited-set continue is what stops the
        traversal once it comes back around to an already-processed node."""
        all_plans = [
            _plan("plan-a", trigger={"type": "on_plan_complete", "planId": "plan-b"}),
            _plan("plan-b", trigger={"type": "on_plan_complete", "planId": "plan-c"}),
            _plan("plan-c", trigger={"type": "on_plan_complete", "planId": "plan-a"}),
        ]
        plans_handler._validate_trigger_no_cycle(
            "unrelated-plan-x",
            {"type": "on_plan_complete", "planId": "plan-a"},
            all_plans,
        )  # must return normally, not raise, not hang

    def test_handles_upstream_plan_missing_from_all_plans(self):
        """A trigger pointing at a planId not present in all_plans (e.g.
        already deleted) must not crash — treated as a dead end."""
        plans_handler._validate_trigger_no_cycle(
            "p1", {"type": "on_plan_complete", "planId": "ghost-plan"}, []
        )  # must not raise
