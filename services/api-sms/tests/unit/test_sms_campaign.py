"""SMS-only v1 uses real sender/processor boundaries with durable local fakes."""
import copy
import json
from unittest.mock import MagicMock, patch

import pytest
from segment_recipients import SegmentRecipientsPending
from tests.unit.test_sms_plan_schedule_source import env as _schedule_environment
from tests.unit.test_sms_profile import POLICY, consume_inline, profile
from tests.unit.test_sms_sender_lifecycle import _EVENT
from vip_shared.domain.services.sms_campaign import (
    DEFAULT_TEMPLATE,
    TEMPLATE_VERSION,
    render,
)

EVENT = {
    **_EVENT,
    "originationNumberArn": "arn:aws:sms-voice:us-east-1:123456789012:phone-number/phone-11111111111111111111111111111111",
    "smsTemplateVersion": TEMPLATE_VERSION,
    "messageTemplate": DEFAULT_TEMPLATE,
    "scheduleSource": "plans",
}


@pytest.fixture(name="env")
def campaign_environment():
    for environment in _schedule_environment.__wrapped__():
        origins = MagicMock()
        origins.describe_phone_numbers.side_effect = lambda **kwargs: {"PhoneNumbers": [{
            "PhoneNumberArn": kwargs["PhoneNumberIds"][0],
            "Status": "ACTIVE", "NumberCapabilities": ["SMS"], "MessageType": "PROMOTIONAL",
        }]}
        with patch.object(environment[0], "_origin_client", origins):
            yield environment


def _bodies(sqs):
    return [json.loads(entry["MessageBody"])
            for call in sqs.send_message_batch.call_args_list
            for entry in call.kwargs["Entries"]]


def test_default_reaches_provider_as_promotional_without_precall_requirements(env):
    sender, _, queue, runs, sqs, sms, reader = env
    recipient = {"phone": "+12125551111", "FirstName": "Jose\u0301"}
    reader.return_value = [recipient]
    consume_inline(env)
    with patch.object(sender, "_is_within_quiet_hours", side_effect=AssertionError("Plans owns hours")):
        result = sender.lambda_handler(EVENT, None)
    assert result["initializationComplete"] is True and result["pending"] is False
    assert result["totalSent"] == result["totalEnqueued"] == 1
    assert result["totalSkippedPersonalization"] == 0
    body = _bodies(sqs)[0]
    assert body["smsTemplateVersion"] == runs.record["smsTemplateVersion"] == TEMPLATE_VERSION
    assert "precallPolicy" not in body and "precallPolicy" not in runs.record
    assert body["messageTemplate"] == render(DEFAULT_TEMPLATE, recipient=recipient)
    request = sms.send_text_message.call_args.kwargs
    assert request["MessageBody"] == body["messageTemplate"]
    assert request["MessageType"] == "PROMOTIONAL" and "TimeToLive" not in request
    assert request["OriginationIdentity"] == EVENT["originationNumberArn"]
    durable = json.dumps([runs.record, list(queue.items.values())], ensure_ascii=False)
    assert "José" not in durable and "Jose\u0301" not in durable


def test_initialization_and_provider_acceptance_are_reported_separately(env):
    sender, processor, _, _, sqs, sms, _ = env
    result = sender.lambda_handler(EVENT, None)
    assert result["initializationComplete"] is True and result["pending"] is True
    assert result["totalEnqueued"] == 1 and result["totalSent"] == 0
    processor.lambda_handler({"Records": [{"body": json.dumps(_bodies(sqs)[0])}]}, None)
    assert sender.lambda_handler(EVENT, None)["pending"] is False
    assert sqs.send_message_batch.call_count == sms.send_text_message.call_count == 1


@pytest.mark.parametrize("recovery", ["replay_changed", "replay_omitted", "retry_changed", "retry_omitted"])
def test_snapshot_pending_freezes_version_message_origin_audience_and_schedule(env, recovery):
    sender, _, _, runs, sqs, sms, reader = env
    reader.side_effect = SegmentRecipientsPending("pending")
    assert sender.lambda_handler(EVENT, None)["pending"] is True
    original = copy.deepcopy(runs.record)
    reader.side_effect = None
    consume_inline(env)
    altered = {
        **EVENT, "messageTemplate": "A different message for {{FirstName}}", "segmentName": "changed",
        "originationNumberArn": "arn:changed", "scheduleSource": "recipient",
    }
    if recovery.endswith("omitted"):
        altered.pop("smsTemplateVersion")
        altered.pop("scheduleSource")
    fn = sender.retry_quiet_hours_skipped if recovery.startswith("retry") else sender.lambda_handler
    with patch.object(sender, "_is_within_quiet_hours", side_effect=AssertionError("Frozen Plans ownership")):
        result = fn(altered, None)
    assert result["totalSent"] == 1 and result["pending"] is False
    for key in ("smsTemplateVersion", "messageTemplate", "originationNumberArn", "segmentName", "scheduleSource"):
        assert runs.record[key] == original[key]
    assert _bodies(sqs)[0]["messageTemplate"] == render(DEFAULT_TEMPLATE, recipient=reader.return_value[0])
    assert sms.send_text_message.call_args.kwargs["MessageType"] == "PROMOTIONAL"


def test_legacy_pending_row_cannot_adopt_new_content_version_via_replay(env):
    sender, processor, _, runs, sqs, sms, reader = env
    reader.side_effect = SegmentRecipientsPending("pending")
    assert sender.lambda_handler({**_EVENT, "scheduleSource": "plans"}, None)["pending"] is True
    reader.side_effect = None
    sender.lambda_handler(EVENT, None)
    assert "smsTemplateVersion" not in runs.record
    body = _bodies(sqs)[0]
    assert "smsTemplateVersion" not in body and body["messageTemplate"] == "Hi there from Original Clinic!"
    processor.lambda_handler({"Records": [{"body": json.dumps(body)}]}, None)
    assert sms.send_text_message.call_args.kwargs["MessageType"] == "TRANSACTIONAL"


@pytest.mark.parametrize("version", [None, "", "campaign-v2", [], {}, False, 1])
def test_invalid_version_cannot_create_run_read_audience_or_enqueue(env, version):
    sender, _, _, runs, sqs, _, reader = env
    with pytest.raises(ValueError, match="unsupported_sms_template_version"):
        sender.lambda_handler({**EVENT, "smsTemplateVersion": version}, None)
    assert runs.record is None
    reader.assert_not_called()
    sqs.send_message_batch.assert_not_called()


@pytest.mark.parametrize("template", [None, "", "A" * 1531, "Hey {{ClinicName}}", "https://example.com", "Patient 123-45-6789"])
def test_invalid_template_rejected_before_ledger_or_claim(env, template):
    sender, _, queue, runs, sqs, _, reader = env
    with pytest.raises(ValueError):
        sender.lambda_handler({**EVENT, "messageTemplate": template}, None)
    assert runs.record is None and queue.items == {}
    reader.assert_not_called()
    sqs.send_message_batch.assert_not_called()


def test_precall_and_campaign_version_cannot_be_combined(env):
    sender, _, _, runs, sqs, _, reader = env
    with pytest.raises(ValueError, match="conflicting_sms_modes"):
        sender.lambda_handler({**EVENT, "precallPolicy": POLICY}, None)
    assert runs.record is None
    reader.assert_not_called()
    sqs.send_message_batch.assert_not_called()


def test_shared_phone_conflict_is_suppressed_and_unused_profile_attributes_do_not_conflict(env):
    sender, _, _, _, sqs, sms, reader = env
    reader.return_value = [
        profile(name="Ana", specialty="Unrelated"), profile(name="Bob"),
        profile("+12125552222", name="李", specialty="Pain", clinic="one"),
        profile("+12125552222", name="李", specialty="Vein", clinic="two"),
    ]
    consume_inline(env)
    result = sender.lambda_handler(EVENT, None)
    assert result["totalSkippedPersonalization"] == 1 and result["totalSent"] == 1
    assert len(_bodies(sqs)) == sms.send_text_message.call_count == 1
    assert sms.send_text_message.call_args.kwargs["MessageBody"].startswith("Hey 李!")


def test_optout_deduplication_and_sealed_cohort_survive_replays(env):
    sender, processor, _, runs, sqs, sms, reader = env
    reader.return_value = [profile(), profile(), profile("+12125552222")]
    consume_inline(env)
    with patch.object(sender, "_opt_out", MagicMock(is_blocked=lambda phone: phone == "+12125552222")):
        first = sender.lambda_handler(EVENT, None)
    assert first["totalSent"] == first["totalSkippedOptOut"] == 1
    reader.reset_mock()
    reader.return_value = [profile("+12125553333")]
    sender.lambda_handler(EVENT, None)
    sender.retry_quiet_hours_skipped(EVENT, None)
    processor.lambda_handler({"Records": [{"body": json.dumps(_bodies(sqs)[0])}]}, None)
    reader.assert_not_called()
    assert sqs.send_message_batch.call_count == sms.send_text_message.call_count == 1
    assert runs.record["initializationComplete"] is True


def test_abort_while_dispatching_cancels_without_provider_send(env):
    sender, _, queue, runs, _, sms, _ = env
    consume_inline(env, abort=True)
    assert sender.lambda_handler(EVENT, None)["terminal"] is True
    sms.send_text_message.assert_not_called()
    assert runs.record["totalCancelled"] == runs.record["totalEnqueued"] == 1
    assert [row["status"] for row in queue.items.values() if "status" in row] == ["CANCELLED"]


def test_rejected_sqs_reservations_compensate_and_retry_frozen_copy(env):
    sender, _, _, runs, sqs, sms, _ = env
    sqs.send_message_batch.side_effect = lambda **kw: {"Failed": [{"Id": e["Id"], "Code": "Throttled"} for e in kw["Entries"]]}
    result = sender.lambda_handler(EVENT, None)
    assert result["initializationComplete"] is False and result["pending"] is True
    assert result["totalEnqueued"] == 0 and result["totalSqsSendFailed"] == 1
    consume_inline(env)
    retry = sender.retry_quiet_hours_skipped({**EVENT, "messageTemplate": "Changed"}, None)
    assert retry["totalSent"] == retry["totalEnqueued"] == 1
    assert sms.send_text_message.call_args.kwargs["MessageBody"].startswith("Hey José!")
    assert runs.record["activeEnqueueBatches"] == 0


def test_ambiguous_sqs_outcome_keeps_reservation_pending_without_resend(env):
    sender, _, _, runs, sqs, sms, _ = env
    sqs.send_message_batch.side_effect = TimeoutError("unknown outcome")
    with pytest.raises(TimeoutError):
        sender.lambda_handler(EVENT, None)
    sqs.send_message_batch.side_effect = None
    result = sender.lambda_handler(EVENT, None)
    assert result["pending"] is True and result["initializationComplete"] is False
    assert runs.record["activeEnqueueBatches"] == result["totalEnqueued"] == 1
    assert sqs.send_message_batch.call_count == 1
    sms.send_text_message.assert_not_called()


def test_stale_sending_after_possible_provider_acceptance_never_resends(env):
    sender, processor, queue, runs, sqs, sms, _ = env
    sender.lambda_handler(EVENT, None)
    body = _bodies(sqs)[0]
    payload = {"Records": [{"body": json.dumps(body)}]}
    with patch.object(processor, "_update_queue_item", side_effect=SystemExit("worker terminated")):
        with pytest.raises(SystemExit):
            processor.lambda_handler(payload, None)
    queue.items[body["sk"]]["updatedAt"] = "2000-01-01T00:00:00+00:00"
    processor.lambda_handler(payload, None)
    processor.lambda_handler(payload, None)
    assert sms.send_text_message.call_count == 1
    assert runs.record["totalFailed"] == 1
    assert queue.items[body["sk"]]["errorCode"] == "PROVIDER_OUTCOME_UNKNOWN"


@pytest.mark.parametrize("mutation", ["aborted", "version_drift", "precall_drift"])
def test_processor_rechecks_live_run_ownership_before_sending(env, mutation):
    sender, processor, queue, runs, sqs, sms, _ = env
    sender.lambda_handler(EVENT, None)
    body = _bodies(sqs)[0]
    if mutation == "aborted":
        runs.record["status"] = "ABORTED"
    elif mutation == "version_drift":
        runs.record["smsTemplateVersion"] = "campaign-v2"
    else:
        runs.record["precallPolicy"] = POLICY
    processor.lambda_handler({"Records": [{"body": json.dumps(body)}]}, None)
    sms.send_text_message.assert_not_called()
    assert queue.items[body["sk"]]["status"] == "CANCELLED"
    assert runs.record["totalCancelled"] == 1


@pytest.mark.parametrize("version", [None, "campaign-v2", False, {}])
def test_processor_unknown_explicit_version_does_not_fall_through_to_transactional(env, version):
    sender, processor, queue, runs, sqs, sms, _ = env
    sender.lambda_handler(EVENT, None)
    body = _bodies(sqs)[0]
    body["smsTemplateVersion"] = version
    processor.lambda_handler({"Records": [{"body": json.dumps(body)}]}, None)
    processor.lambda_handler({"Records": [{"body": json.dumps(body)}]}, None)
    sms.send_text_message.assert_not_called()
    assert queue.items[body["sk"]]["status"] == "FAILED"
    assert queue.items[body["sk"]]["errorCode"] == "INVALID_SMS_CONTRACT"
    assert runs.record["totalFailed"] == 1


@pytest.mark.parametrize("state", ["running", "aborted", "stale_sending"])
def test_durable_queue_version_prevents_omitted_payload_version_downgrade(env, state):
    sender, processor, queue, runs, sqs, sms, _ = env
    sender.lambda_handler(EVENT, None)
    body = _bodies(sqs)[0]
    del body["smsTemplateVersion"]
    row = queue.items[body["sk"]]
    assert row["smsTemplateVersion"] == TEMPLATE_VERSION
    if state == "aborted":
        runs.record["status"] = "ABORTED"
    elif state == "stale_sending":
        row.update(status="SENDING", updatedAt="2000-01-01T00:00:00+00:00")
    payload = {"Records": [{"body": json.dumps(body)}]}
    processor.lambda_handler(payload, None)
    processor.lambda_handler(payload, None)
    sms.send_text_message.assert_not_called()
    assert row["status"] == "FAILED" and row["errorCode"] == "INVALID_SMS_CONTRACT"
    assert runs.record["totalFailed"] == 1


def test_payload_cannot_upgrade_legacy_queue_row_into_campaign_mode(env):
    sender, processor, queue, runs, sqs, sms, _ = env
    sender.lambda_handler({**_EVENT, "scheduleSource": "plans"}, None)
    body = _bodies(sqs)[0]
    body["smsTemplateVersion"] = TEMPLATE_VERSION
    processor.lambda_handler({"Records": [{"body": json.dumps(body)}]}, None)
    sms.send_text_message.assert_not_called()
    assert queue.items[body["sk"]]["status"] == "FAILED"
    assert runs.record["totalFailed"] == 1


@pytest.mark.parametrize("bad_body", ["界" * 631, "https://example.com", "Hi {{FirstName}}", "Patient 123-45-6789"])
def test_processor_enforces_actual_body_guard_before_eum_without_logging_content(env, capsys, bad_body):
    sender, processor, queue, runs, sqs, sms, _ = env
    sender.lambda_handler(EVENT, None)
    body = _bodies(sqs)[0]
    body["messageTemplate"] = bad_body
    with pytest.raises(RuntimeError, match="SmsCampaignError"):
        processor.lambda_handler({"Records": [{"body": json.dumps(body)}]}, None)
    sms.send_text_message.assert_not_called()
    assert runs.record["totalFailed"] == 1 and queue.items[body["sk"]]["status"] == "FAILED"
    assert bad_body not in capsys.readouterr().out


def test_bare_plan_id_does_not_bypass_standalone_recipient_gate(env):
    sender, _, _, _, sqs, _, _ = env
    standalone = {key: value for key, value in EVENT.items() if key != "scheduleSource"}
    with patch.object(sender, "_is_within_quiet_hours", return_value=False) as gate:
        result = sender.lambda_handler(standalone, None)
    gate.assert_called_once()
    assert result["totalSkippedQuietHours"] == 1
    sqs.send_message_batch.assert_not_called()


def test_concurrent_initializer_cannot_seal_before_batch_rejection(env):
    sender, _, _, runs, sqs, _, _ = env
    observed = []

    def reject_after_competing_initializer(**kwargs):
        observed.append(sender.lambda_handler(EVENT, None))
        return {"Failed": [{"Id": entry["Id"], "Code": "Throttled"} for entry in kwargs["Entries"]]}

    sqs.send_message_batch.side_effect = reject_after_competing_initializer
    result = sender.lambda_handler(EVENT, None)
    assert observed[0]["initializationComplete"] is False
    assert result["pending"] is True and result["initializationComplete"] is False
    assert runs.record["totalEnqueued"] == 0
    consume_inline(env)
    assert sender.retry_quiet_hours_skipped(EVENT, None)["totalSent"] == 1
