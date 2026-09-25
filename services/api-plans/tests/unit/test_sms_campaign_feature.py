"""Campaign SMS validation and the real Plans-to-SMS Lambda boundary, offline."""
import json
from unittest.mock import MagicMock, patch

import pytest

from vip_shared.domain.services.sms_campaign import DEFAULT_TEMPLATE, TEMPLATE_VERSION
from .test_plan_validation_sms import _campaign, _validate, validate_plan
from .test_executor_sms_pending import environment as _environment, executor, model, response


@pytest.fixture
def environment(monkeypatch):
    yield from _environment.__wrapped__(monkeypatch)


def campaign(template=DEFAULT_TEMPLATE):
    value = _campaign(template)
    value["campaignConfig"]["smsTemplateVersion"] = TEMPLATE_VERSION
    return value


def managed(*, initialized=True, enqueued=1, sent=0):
    return {"enqueued": enqueued, "failed": 0, "pending": not initialized or enqueued > sent,
            "initializationComplete": initialized, "totalEnqueued": enqueued,
            "totalSent": sent, "totalFailed": 0, "totalOptedOut": 0,
            "totalCancelled": 0, "totalSkippedPersonalization": 0}


def test_booking_copy_validates_only_with_explicit_campaign_version():
    assert _validate(campaign(), "SMS", 0) == []
    legacy = _validate(_campaign(DEFAULT_TEMPLATE), "SMS", 0)
    assert any("160" in error for error in legacy)
    assert any("URL" in error for error in legacy)


@pytest.mark.parametrize("version", [None, True, "profile", "campaign-v2", {}, []])
def test_unknown_version_never_uses_legacy_or_relaxed_validation(version):
    value = campaign("Hi")
    value["campaignConfig"]["smsTemplateVersion"] = version
    assert _validate(value, "SMS", 0)


@pytest.mark.parametrize("template", [None, 123, [], "", "{{LastName}}", "{{ClinicName}}",
                                      "https://example.com", "Hi 123-45-6789",
                                      DEFAULT_TEMPLATE + "?patient=Alex", "A" * 1531])
def test_rejects_invalid_campaign_content(template):
    assert _validate(campaign(template), "SMS", 0)


@pytest.mark.parametrize("ack", [False, None, "true", 1])
def test_new_campaign_requires_explicit_boolean_acknowledgment(ack):
    value = campaign()
    value["campaignConfig"]["phiAcknowledged"] = ack
    assert _validate(value, "SMS", 0)


def test_ui_shape_needs_no_voice_settings_and_preserves_chosen_segment():
    value = campaign()
    value.update(id="sms-campaign-unique", name="Appointment invitations", states=[], groups=[],
                 dependsOn=[], run_type="full", pinnedSegmentArn="arn:aws:profile:us-east-1:123:domains/d/segment-definitions/audience")
    body = {"name": "Appointment invitations", "trigger": {"type": "manual"}, "buckets": [{
        "id": "sms-bucket-unique", "name": "SMS", "run_mode": "status_based", "cleanup": False,
        "prestart_next": False, "campaignConfig": {}, "campaigns": [value],
    }]}
    assert validate_plan(body) == []
    assert body["buckets"][0]["cleanup"] is False


def test_lambda_payload_keeps_editable_template_version_and_plans_source(environment):
    client, _ = environment
    run, plan, state = model("sms")
    state["smsCampaignId"] = "sms-id"
    cfg = plan["buckets"][0]["campaigns"][0]["campaignConfig"]
    cfg.update(smsTemplateVersion=TEMPLATE_VERSION, smsMessageTemplate=DEFAULT_TEMPLATE)
    client.invoke.return_value = response(managed())
    executor._continue_bulk_sms_initialization(run, plan, 0, 0)
    payload = json.loads(client.invoke.call_args.kwargs["Payload"])
    assert payload["smsTemplateVersion"] == TEMPLATE_VERSION
    assert payload["messageTemplate"] == DEFAULT_TEMPLATE
    assert payload["scheduleSource"] == "plans"
    assert "precallPolicy" not in payload
    assert state["smsInitializationState"] == "complete"
    assert state["status"] == "running"


def test_snapshot_resume_uses_same_payload_then_finishes_initialization(environment):
    client, _ = environment
    run, plan, state = model("sms")
    state["smsCampaignId"] = "sms-id"
    plan["buckets"][0]["campaigns"][0]["campaignConfig"].update(
        smsTemplateVersion=TEMPLATE_VERSION, smsMessageTemplate=DEFAULT_TEMPLATE)
    client.invoke.side_effect = [response(managed(initialized=False, enqueued=0)), response(managed())]
    executor._continue_bulk_sms_initialization(run, plan, 0, 0)
    assert state["smsInitializationState"] == "pending"
    executor._continue_bulk_sms_initialization(run, plan, 0, 0)
    assert state["smsInitializationState"] == "complete"
    assert client.invoke.call_args_list[0].kwargs["Payload"] == client.invoke.call_args_list[1].kwargs["Payload"]


@pytest.mark.parametrize("result", [{"enqueued": 1}, {**managed(), "initializationComplete": "yes"},
                                   {**managed(), "totalSent": -1}])
def test_campaign_result_cannot_silently_use_legacy_response(environment, result):
    client, _ = environment
    client.invoke.return_value = response(result)
    with pytest.raises(RuntimeError):
        executor._invoke_sms_sender(campaignId="sms-id", smsTemplateVersion=TEMPLATE_VERSION)


def test_queue_completion_uses_current_records_across_empty_pages():
    table = MagicMock()
    continuation = {"campaignId": "sms-id", "sk": "claim"}

    def query(**kwargs):
        # Emulate the stale empty observation a default eventual read permits.
        if not kwargs.get("ConsistentRead"):
            return {"Count": 0}
        if "ExclusiveStartKey" not in kwargs:
            return {"Count": 0, "LastEvaluatedKey": continuation}
        assert kwargs["ExclusiveStartKey"] == continuation
        return {"Count": 1}  # A newly reserved PENDING delivery exists.

    table.query.side_effect = query
    with patch.object(executor.boto3, "resource") as resource:
        resource.return_value.Table.return_value = table
        assert executor._count_sms_queue("sms-id") == 1
    assert table.query.call_count == 2
