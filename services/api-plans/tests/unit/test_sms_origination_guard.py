"""Real Plans handlers and execution gates reject incompatible campaign origins."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from vip_shared.domain.services.sms_campaign import DEFAULT_TEMPLATE
import sms_origination
from .test_plan_validation_sms import plans_handler
from .test_executor_sms_pending import executor, model

ARN = "arn:aws:sms-voice:us-east-1:123456789012:phone-number/phone-promo"


def plan():
    return {"planId": "p", "name": "Campaign", "trigger": {"type": "manual"}, "buckets": [{
        "id": "b", "name": "SMS", "run_mode": "status_based", "cleanup": False, "prestart_next": False,
        "campaignConfig": {}, "campaigns": [{"id": "c", "name": "SMS", "deliveryType": "sms",
        "run_type": "full", "dependsOn": [], "states": [], "groups": [], "pinnedSegmentArn": "arn/segment",
        "campaignConfig": {"smsTemplateVersion": "campaign-v1", "smsOriginationNumberArn": ARN,
                           "smsMessageTemplate": DEFAULT_TEMPLATE, "phiAcknowledged": True}}]}]}


@pytest.fixture
def env(monkeypatch):
    client = MagicMock()
    client.describe_phone_numbers.return_value = {"PhoneNumbers": [{
        "PhoneNumberArn": ARN, "Status": "ACTIVE", "MessageType": "PROMOTIONAL", "NumberCapabilities": ["SMS"]}]}
    monkeypatch.setattr(sms_origination, "_origin_client", client)
    storage = MagicMock()
    storage.get_plan.return_value = plan()
    storage.put_plan.side_effect = lambda body: {**body, "planId": "p"}
    monkeypatch.setattr(plans_handler, "store", storage)
    monkeypatch.setattr(plans_handler, "parse_body", lambda event: json.loads(event["body"]))
    monkeypatch.setattr(plans_handler, "json_response", lambda status, body: {"statusCode": status, "body": json.dumps(body)})
    monkeypatch.setattr(plans_handler, "extract_caller", lambda event: SimpleNamespace(
        sub="operator", email="operator@example.test", ip_address="127.0.0.1", user_agent="test"))
    audit, scheduler = MagicMock(), MagicMock()
    monkeypatch.setattr(plans_handler, "build_audit", lambda: audit)
    monkeypatch.setattr(plans_handler, "scheduler_manager", scheduler)
    return SimpleNamespace(client=client, storage=storage, audit=audit, scheduler=scheduler)


def invoke(route):
    body = plan()
    if route == "duplicate":
        body = {"_duplicateFromId": "p", "name": "Copy"}
    elif route == "rename":
        body = {"name": "Renamed"}
    event = {"body": json.dumps(body)}
    if route in {"create", "duplicate"}:
        return plans_handler.create_plan(event, {})
    return plans_handler.update_plan(event, {"id": "p"})


def incompatible(env, reason):
    if reason == "lookup":
        env.client.describe_phone_numbers.side_effect = RuntimeError("provider secret must not escape")
    elif reason == "missing":
        env.client.describe_phone_numbers.return_value = {"PhoneNumbers": []}
    else:
        phone = env.client.describe_phone_numbers.return_value["PhoneNumbers"][0]
        phone.update({"transactional": {"MessageType": "TRANSACTIONAL"}, "voice_only": {"NumberCapabilities": ["VOICE"]},
                      "inactive": {"Status": "PENDING"}, "wrong_arn": {"PhoneNumberArn": ARN + "other"}}[reason])


@pytest.mark.parametrize("route", ["create", "update", "duplicate", "rename"])
@pytest.mark.parametrize("reason", ["transactional", "voice_only", "missing", "lookup", "inactive", "wrong_arn"])
def test_crud_rejects_before_persist_audit_or_schedule(env, route, reason):
    incompatible(env, reason)
    response = invoke(route)
    assert response["statusCode"] == 400
    assert "campaign_sms_origin_" in response["body"]
    assert "provider secret" not in response["body"]
    env.storage.put_plan.assert_not_called()
    assert env.audit.mock_calls == [] and env.scheduler.mock_calls == []


@pytest.mark.parametrize("route", ["create", "update", "duplicate", "rename"])
def test_crud_accepts_authoritative_promotional_origin(env, route):
    response = invoke(route)
    assert response["statusCode"] in {200, 201}
    env.storage.put_plan.assert_called_once()
    env.client.describe_phone_numbers.assert_called_once_with(PhoneNumberIds=[ARN])


@pytest.mark.parametrize("trigger", ["manual", "scheduled", "chained"])
def test_common_start_rejects_before_lock_record_or_bucket_resources(env, monkeypatch, trigger):
    incompatible(env, "transactional")
    monkeypatch.setattr(executor, "get_plan", lambda _: plan())
    lock, create, bucket = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr(executor, "lock_plan_run", lock)
    monkeypatch.setattr(executor, "create_run", create)
    monkeypatch.setattr(executor, "_start_bucket", bucket)
    with pytest.raises(ValueError, match="not_promotional"):
        executor.start_run("p", triggered_by=trigger)
    lock.assert_not_called()
    create.assert_not_called()
    bucket.assert_not_called()


def test_common_start_accepts_promotional_before_creating_run(env, monkeypatch):
    body = plan()
    run = {"planId": "p", "runId": "r", "bucketStates": []}
    monkeypatch.setattr(executor, "get_plan", lambda _: body)
    monkeypatch.setattr(executor, "get_latest_run", lambda _: None)
    lock, create, bucket = MagicMock(), MagicMock(return_value=run), MagicMock()
    monkeypatch.setattr(executor, "lock_plan_run", lock)
    monkeypatch.setattr(executor, "create_run", create)
    monkeypatch.setattr(executor, "_start_bucket", bucket)
    assert executor.start_run("p", triggered_by="scheduled") is run
    lock.assert_called_once()
    create.assert_called_once()
    bucket.assert_called_once_with(run, 0)


def test_delayed_or_force_dispatch_rechecks_before_segment_save_or_invoke(env, monkeypatch):
    incompatible(env, "transactional")
    run, body, state = model("sms")
    body["buckets"][0]["campaigns"][0]["campaignConfig"] = plan()["buckets"][0]["campaigns"][0]["campaignConfig"]
    body["buckets"][0]["campaigns"][0].pop("pinnedSegmentArn", None)
    segment, save, send = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr(executor, "_create_segment", segment)
    monkeypatch.setattr(executor, "save_run", save)
    monkeypatch.setattr(executor, "_invoke_sms_sender", send)
    executor._start_one_campaign(run, body, 0, 0)
    assert state["status"] == "error"
    assert state["errorDetail"] == "campaign_sms_origin_not_promotional"
    assert not state.get("smsCampaignId")
    segment.assert_not_called()
    save.assert_not_called()
    send.assert_not_called()


def test_same_origin_looked_up_once_per_plan_operation_but_rechecked_next_time(env):
    body = plan()
    body["buckets"][0]["campaigns"].append(deepcopy(body["buckets"][0]["campaigns"][0]))
    assert sms_origination.validate_plan_origins(body) == []
    assert env.client.describe_phone_numbers.call_count == 1
    incompatible(env, "transactional")
    assert len(sms_origination.validate_plan_origins(body)) == 2
    assert env.client.describe_phone_numbers.call_count == 2


@pytest.mark.parametrize("delivery", ["sms", "campaign", "branded"])
def test_legacy_and_precall_do_not_require_promotional_origin_or_call_eum(env, delivery):
    body = plan()
    campaign = body["buckets"][0]["campaigns"][0]
    campaign["deliveryType"] = delivery
    campaign["campaignConfig"].pop("smsTemplateVersion")
    campaign["campaignConfig"]["precallSms"] = {"enabled": True, "originationNumberArn": "legacy-arn"}
    assert sms_origination.validate_plan_origins(body) == []
    env.client.describe_phone_numbers.assert_not_called()


@pytest.mark.parametrize("campaign_version", [True, False])
def test_bucket_sms_version_cannot_silently_downgrade_to_legacy(env, campaign_version):
    body = plan()
    body["buckets"][0]["campaignConfig"] = {"smsTemplateVersion": "campaign-v1", "smsOriginationNumberArn": ARN}
    if not campaign_version:
        body["buckets"][0]["campaigns"][0]["campaignConfig"].pop("smsTemplateVersion")
    errors = sms_origination.validate_plan_origins(body)
    assert errors and "must_be_configured_per_campaign" in errors[0]
    env.client.describe_phone_numbers.assert_not_called()
