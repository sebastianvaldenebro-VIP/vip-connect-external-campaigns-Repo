"""Tests for branded campaign field validation in plan save boundary."""
from __future__ import annotations

import json as _json
import sys
import os
from unittest.mock import MagicMock, patch

import pytest

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
    from handlers.plans import _validate_dag, _validate_plan_body  # noqa: E402


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


# ── maxLeadAgeMinutes validation (2026-08-27 adversarial review) ──────────────
#
# The frontend <select> only emits None/10/15/20/25/30/35, but that's not a
# trust boundary — a direct API call or a stale/duplicated plan record could
# carry any value. Applies to every deliveryType, not just branded.


class TestMaxLeadAgeMinutesValidation:
    def _plan_with(self, campaign_extra: dict):
        campaign = {"id": "c-1", "name": "New Lead", "deliveryType": "campaign", **campaign_extra}
        bucket = {"name": "b1", "campaigns": [campaign]}
        return {"name": "Test Plan", "timezone": "America/New_York", "buckets": [bucket]}

    def test_omitted_field_passes(self):
        errors = _validate_plan(self._plan_with({}))
        assert errors == [] or errors is None

    def test_null_passes(self):
        errors = _validate_plan(self._plan_with({"maxLeadAgeMinutes": None}))
        assert errors == [] or errors is None

    def test_each_allowed_value_passes(self):
        for minutes in (10, 15, 20, 25, 30, 35):
            errors = _validate_plan(self._plan_with({"maxLeadAgeMinutes": minutes}))
            assert errors == [] or errors is None, f"{minutes} should be valid"

    def test_negative_value_fails(self):
        errors = _validate_plan(self._plan_with({"maxLeadAgeMinutes": -5}))
        assert any("maxLeadAgeMinutes" in str(e) for e in (errors or []))

    def test_zero_fails(self):
        errors = _validate_plan(self._plan_with({"maxLeadAgeMinutes": 0}))
        assert any("maxLeadAgeMinutes" in str(e) for e in (errors or []))

    def test_non_step_value_fails(self):
        errors = _validate_plan(self._plan_with({"maxLeadAgeMinutes": 12}))
        assert any("maxLeadAgeMinutes" in str(e) for e in (errors or []))

    def test_out_of_range_value_fails(self):
        errors = _validate_plan(self._plan_with({"maxLeadAgeMinutes": 1000}))
        assert any("maxLeadAgeMinutes" in str(e) for e in (errors or []))

    def test_string_value_fails(self):
        errors = _validate_plan(self._plan_with({"maxLeadAgeMinutes": "15"}))
        assert any("maxLeadAgeMinutes" in str(e) for e in (errors or []))

    def test_list_value_fails_without_raising(self):
        """Regression: a JSON array is unhashable — 'in <set>' must not raise TypeError."""
        errors = _validate_plan(self._plan_with({"maxLeadAgeMinutes": [15]}))
        assert any("maxLeadAgeMinutes" in str(e) for e in (errors or []))

    def test_dict_value_fails_without_raising(self):
        """Regression: a JSON object is unhashable — 'in <set>' must not raise TypeError."""
        errors = _validate_plan(self._plan_with({"maxLeadAgeMinutes": {}}))
        assert any("maxLeadAgeMinutes" in str(e) for e in (errors or []))

    def test_applies_regardless_of_delivery_type(self):
        campaign = {
            "id": "bc-1",
            "name": "Branded",
            "deliveryType": "branded",
            "maxLeadAgeMinutes": -5,
            "campaignConfig": {
                "queueArn": "arn:aws:connect:us-east-1:123:instance/i/queue/q",
                "contactFlowId": "flow-abc",
                "sourcePhone": "+12125550199",
            },
        }
        bucket = {"name": "b1", "campaigns": [campaign]}
        plan = {"name": "Test Plan", "buckets": [bucket]}
        errors = _validate_plan(plan)
        assert any("maxLeadAgeMinutes" in str(e) for e in (errors or []))

    def test_legacy_bucket_flag_cannot_bypass_validation(self):
        """Regression: executor._create_segment reads maxLeadAgeMinutes from
        bucket.segmentFilters (not the campaign dict) when a campaign is
        marked _legacyBucket. Validation must read from the same source, or a
        client-supplied "_legacyBucket": true smuggles an unvalidated value in.
        """
        campaign = {"id": "c-1", "name": "Legacy", "_legacyBucket": True}
        bucket = {
            "name": "b1",
            "campaigns": [campaign],
            "segmentFilters": {"state": ["NY"], "groups": [], "maxLeadAgeMinutes": "not-a-number"},
        }
        plan = {"name": "Test Plan", "buckets": [bucket]}
        errors = _validate_plan(plan)
        assert any("maxLeadAgeMinutes" in str(e) for e in (errors or []))

    def test_legacy_bucket_valid_value_passes(self):
        campaign = {"id": "c-1", "name": "Legacy", "_legacyBucket": True}
        bucket = {
            "name": "b1",
            "campaigns": [campaign],
            "segmentFilters": {"state": ["NY"], "groups": [], "maxLeadAgeMinutes": 15},
        }
        plan = {"name": "Test Plan", "buckets": [bucket]}
        errors = _validate_plan(plan)
        assert errors == [] or errors is None

    def test_legacy_bucket_ignores_campaign_level_value_even_if_valid(self):
        """Locks in precedence: when _legacyBucket is set, the campaign-level
        maxLeadAgeMinutes must be ignored outright — never merely
        deprioritized — in favor of bucket.segmentFilters.maxLeadAgeMinutes,
        which is what executor._create_segment actually consumes. Guards
        against a future "prefer campaign value if present" edit silently
        reopening the validation-bypass gap.
        """
        campaign = {
            "id": "c-1", "name": "Legacy", "_legacyBucket": True,
            "maxLeadAgeMinutes": 15,  # in-range, must still be ignored
        }
        bucket = {
            "name": "b1",
            "campaigns": [campaign],
            "segmentFilters": {"state": ["NY"], "groups": [], "maxLeadAgeMinutes": -5},
        }
        plan = {"name": "Test Plan", "buckets": [bucket]}
        errors = _validate_plan(plan)
        assert any("maxLeadAgeMinutes" in str(e) for e in (errors or []))


# ── _validate_dag: duplicate campaign ids across the plan (round 3) ───────────
#
# build_segment_name uses a campaign's id as a disambiguator so two campaigns
# in the same bucket never collide on an identical segment name. A duplicate
# id (e.g. produced by _regenerate_bucket_ids collapsing two source campaigns
# onto the same freshly-generated id when cloning) would defeat that
# guarantee — so it must be rejected at save time, before any clone/duplicate
# path can ever be fed a plan/template containing one.


class TestDuplicateCampaignIdValidation:
    def test_duplicate_id_in_same_bucket_raises(self):
        c1 = {"id": "dup", "name": "A", "deliveryType": "campaign"}
        c2 = {"id": "dup", "name": "B", "deliveryType": "campaign"}
        buckets = [{"name": "b1", "campaigns": [c1, c2]}]
        with pytest.raises(ValueError, match="Duplicate campaign id"):
            _validate_dag(buckets)

    def test_duplicate_id_across_buckets_raises(self):
        c1 = {"id": "dup", "name": "A", "deliveryType": "campaign"}
        c2 = {"id": "dup", "name": "B", "deliveryType": "campaign"}
        buckets = [
            {"name": "b1", "campaigns": [c1]},
            {"name": "b2", "campaigns": [c2]},
        ]
        with pytest.raises(ValueError, match="Duplicate campaign id"):
            _validate_dag(buckets)

    def test_unique_ids_pass(self):
        c1 = {"id": "c-1", "name": "A", "deliveryType": "campaign"}
        c2 = {"id": "c-2", "name": "B", "deliveryType": "campaign"}
        buckets = [{"name": "b1", "campaigns": [c1, c2]}]
        _validate_dag(buckets)  # must not raise


# ── _validate_dag: segment-name disambiguator token collisions (round 4) ─────
#
# build_segment_name's disambiguator is a LOSSY projection of id/name
# (sanitized, truncated to 12 chars) — the raw-id duplicate check above
# cannot catch two campaigns whose ids/names differ but collapse onto the
# same projection, or that both lack an id and a name entirely.


class TestSegmentNameTokenCollisionValidation:
    def test_distinct_ids_sharing_12_char_prefix_raises(self):
        # "campaign-abc" is exactly 12 chars — both ids share that full prefix
        # and diverge only after it, which the [:12] projection discards.
        c1 = {"id": "campaign-abc" + "x" * 50, "name": "A", "deliveryType": "campaign"}
        c2 = {"id": "campaign-abc" + "y" * 50, "name": "B", "deliveryType": "campaign"}
        buckets = [{"name": "b1", "campaigns": [c1, c2]}]
        with pytest.raises(ValueError, match="segment-name disambiguator"):
            _validate_dag(buckets)

    def test_missing_id_falls_back_to_distinct_names_and_passes(self):
        c1 = {"name": "Morning Wave", "deliveryType": "campaign"}
        c2 = {"name": "Evening Wave", "deliveryType": "campaign"}
        buckets = [{"name": "b1", "campaigns": [c1, c2]}]
        _validate_dag(buckets)  # must not raise — distinct name-derived tokens

    def test_missing_id_with_same_name_raises(self):
        c1 = {"name": "New Lead", "deliveryType": "campaign"}
        c2 = {"name": "New Lead", "deliveryType": "campaign"}
        buckets = [{"name": "b1", "campaigns": [c1, c2]}]
        with pytest.raises(ValueError, match="segment-name disambiguator"):
            _validate_dag(buckets)

    def test_missing_id_and_name_raises(self):
        c1 = {"deliveryType": "campaign"}
        c2 = {"deliveryType": "campaign"}
        buckets = [{"name": "b1", "campaigns": [c1, c2]}]
        with pytest.raises(ValueError, match="neither an id nor a name"):
            _validate_dag(buckets)


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


# ── clone_from_template: must validate before persisting (2026-08-27 round 2) ─
#
# Regression: clone_from_template was the only store.put_plan call site that
# never ran _validate_plan_body, so a template carrying an out-of-set
# maxLeadAgeMinutes (or any other invalid campaign field) was copied verbatim
# into every new plan cloned from it, bypassing the validation added above.

def _json_response(status: int, body: object) -> dict:
    return {"statusCode": status, "body": _json.dumps(body)}


def _make_clone_from_template_fn(template: dict, body: dict):
    """Return (response_or_exception, mock_store) for a clone_from_template call
    with mocked deps. _validate_dag raises ValueError directly (unlike
    _validate_plan_body, which returns an error list) — the router converts
    that to a 400 in production, but calling the handler directly here means
    the exception propagates, so it's caught and returned rather than raised,
    letting callers assert on both outcome and mock_store uniformly.
    """
    import importlib

    mock_store = MagicMock()
    mock_audit = MagicMock()
    mock_audit.record = MagicMock()

    mock_store.get_plan.return_value = template
    mock_store.put_plan.side_effect = lambda p: {**p, "planId": "new-plan-id"}

    stubs = {
        "store": mock_store,
        "scheduler_manager": MagicMock(),
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

    with (
        patch.object(plans_mod, "store", mock_store),
        patch.object(plans_mod, "json_response", _json_response),
        patch.object(plans_mod, "build_audit", return_value=mock_audit),
        patch.object(plans_mod, "parse_body", return_value=body),
        patch.object(
            plans_mod,
            "extract_caller",
            return_value=MagicMock(sub="t", email="t@t.com", ip_address="", user_agent=""),
        ),
    ):
        try:
            response = plans_mod.clone_from_template({}, {"tid": template["planId"]})
        except Exception as exc:  # noqa: BLE001 - deliberately captured for the test
            return exc, mock_store

    return response, mock_store


class TestCloneFromTemplateValidation:
    def _template_with_campaign(self, campaign_extra: dict):
        campaign = {"id": "c-1", "name": "New Lead", "deliveryType": "campaign", **campaign_extra}
        return {
            "planId": "tmpl-1",
            "name": "Template",
            "isTemplate": True,
            "buckets": [{"name": "b1", "campaigns": [campaign]}],
        }

    def test_invalid_max_lead_age_rejected_with_400(self):
        template = self._template_with_campaign({"maxLeadAgeMinutes": 7})
        response, mock_store = _make_clone_from_template_fn(template, {})
        assert response["statusCode"] == 400
        mock_store.put_plan.assert_not_called()

    def test_valid_campaign_clones_successfully(self):
        template = self._template_with_campaign({"maxLeadAgeMinutes": 15})
        response, mock_store = _make_clone_from_template_fn(template, {})
        assert response["statusCode"] == 201
        mock_store.put_plan.assert_called_once()

    def test_dependency_cycle_rejected_before_persisting(self):
        """Regression: clone_from_template must call _validate_dag (not just
        _validate_plan_body) before store.put_plan, mirroring create_plan.
        """
        c1 = {"id": "c-1", "name": "A", "deliveryType": "campaign", "dependsOn": ["c-2"]}
        c2 = {"id": "c-2", "name": "B", "deliveryType": "campaign", "dependsOn": ["c-1"]}
        template = {
            "planId": "tmpl-1",
            "name": "Template",
            "isTemplate": True,
            "buckets": [{"name": "b1", "campaigns": [c1, c2]}],
        }
        outcome, mock_store = _make_clone_from_template_fn(template, {})
        assert isinstance(outcome, ValueError)
        mock_store.put_plan.assert_not_called()
