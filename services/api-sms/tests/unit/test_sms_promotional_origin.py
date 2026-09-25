"""Real sender entry points must validate only the durable promotional origin."""
import copy
from unittest.mock import patch

from botocore.exceptions import ClientError
import pytest
from segment_recipients import SegmentRecipientsPending

from tests.unit.test_sms_campaign import EVENT, campaign_environment
from tests.unit.test_sms_profile import POLICY, consume_inline
from tests.unit.test_sms_sender_lifecycle import _EVENT

TX_ORIGIN = "arn:aws:sms-voice:us-east-1:123456789012:phone-number/phone-22222222222222222222222222222222"


@pytest.fixture
def env():
    yield from campaign_environment.__wrapped__()


def number(arn, **changes):
    return {"PhoneNumberArn": arn, "PhoneNumberId": arn.rsplit("/", 1)[-1],
            "Status": "ACTIVE", "NumberCapabilities": ["SMS"], "MessageType": "PROMOTIONAL", **changes}


def pending(env):
    sender, _, _, _, _, _, reader = env
    reader.side_effect = SegmentRecipientsPending("pending synthetic export")
    assert sender.lambda_handler(EVENT, None)["pending"] is True
    reader.side_effect = None
    reader.reset_mock()
    sender._origin_client.reset_mock()


@pytest.mark.parametrize("changes", [
    {"MessageType": "TRANSACTIONAL"}, {"Status": "PENDING"}, {"NumberCapabilities": ["VOICE"]},
])
def test_initial_incompatible_origin_rejected_before_run_export_claims_and_enqueue(env, changes):
    sender, _, queue, runs, sqs, sms, reader = env
    sender._origin_client.describe_phone_numbers.side_effect = None
    sender._origin_client.describe_phone_numbers.return_value = {"PhoneNumbers": [number(EVENT["originationNumberArn"], **changes)]}
    with pytest.raises(ValueError):
        sender.lambda_handler(EVENT, None)
    assert runs.record is None and queue.items == {}
    reader.assert_not_called()
    sqs.send_message_batch.assert_not_called()
    sms.send_text_message.assert_not_called()


def test_initial_lookup_failure_does_not_leave_running_run_or_export(env):
    sender, _, queue, runs, sqs, _, reader = env
    sender._origin_client.describe_phone_numbers.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "sensitive detail must not escape"}}, "DescribePhoneNumbers")
    with pytest.raises(ValueError) as error:
        sender.lambda_handler(EVENT, None)
    assert "sensitive detail" not in str(error.value)
    assert runs.record is None and queue.items == {}
    reader.assert_not_called()
    sqs.send_message_batch.assert_not_called()


def test_authoritative_promotional_origin_validated_before_reader_and_provider_keeps_type(env):
    sender, _, _, _, _, sms, reader = env
    recipients = reader.return_value

    def read(*args, **kwargs):
        sender._origin_client.describe_phone_numbers.assert_called_once_with(PhoneNumberIds=[EVENT["originationNumberArn"]])
        return recipients

    reader.side_effect = read
    consume_inline(env)
    assert sender.lambda_handler(EVENT, None)["totalSent"] == 1
    assert sms.send_text_message.call_args.kwargs["MessageType"] == "PROMOTIONAL"


@pytest.mark.parametrize("entry", ["lambda_handler", "retry_quiet_hours_skipped"])
def test_resume_checks_frozen_origin_even_when_payload_origin_and_version_changed(env, entry):
    sender, _, _, runs, _, sms, _ = env
    pending(env)
    consume_inline(env)
    altered = {**EVENT, "originationNumberArn": TX_ORIGIN}
    altered.pop("smsTemplateVersion")
    assert getattr(sender, entry)(altered, None)["totalSent"] == 1
    sender._origin_client.describe_phone_numbers.assert_called_once_with(PhoneNumberIds=[EVENT["originationNumberArn"]])
    assert runs.record["originationNumberArn"] == EVENT["originationNumberArn"]
    assert sms.send_text_message.call_args.kwargs["OriginationIdentity"] == EVENT["originationNumberArn"]


@pytest.mark.parametrize("entry", ["lambda_handler", "retry_quiet_hours_skipped"])
def test_unfinished_old_transactional_run_cannot_be_upgraded_by_new_payload(env, entry):
    sender, _, queue, runs, sqs, _, reader = env
    pending(env)
    runs.record["originationNumberArn"] = TX_ORIGIN  # Existing pre-guard ledger.
    sender._origin_client.describe_phone_numbers.side_effect = None
    sender._origin_client.describe_phone_numbers.return_value = {"PhoneNumbers": [number(TX_ORIGIN, MessageType="TRANSACTIONAL")]}
    with pytest.raises(ValueError):
        getattr(sender, entry)(EVENT, None)
    sender._origin_client.describe_phone_numbers.assert_called_once_with(PhoneNumberIds=[TX_ORIGIN])
    assert runs.record["originationNumberArn"] == TX_ORIGIN and runs.record["initializationComplete"] is False
    assert runs.record["totalEnqueued"] == 0 and queue.items == {}
    reader.assert_not_called()
    sqs.send_message_batch.assert_not_called()


@pytest.mark.parametrize("entry", ["lambda_handler", "retry_quiet_hours_skipped"])
def test_resume_lookup_failure_blocks_export_and_keeps_existing_identity(env, entry):
    sender, _, queue, runs, sqs, _, reader = env
    pending(env)
    original = copy.deepcopy(runs.record)
    sender._origin_client.describe_phone_numbers.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException"}}, "DescribePhoneNumbers")
    with pytest.raises(ValueError):
        getattr(sender, entry)(EVENT, None)
    assert runs.record == original and queue.items == {}
    reader.assert_not_called()
    sqs.send_message_batch.assert_not_called()


@pytest.mark.parametrize("entry", ["lambda_handler", "retry_quiet_hours_skipped"])
@pytest.mark.parametrize("state", ["sealed_failed", "ABORTED"])
def test_completed_or_terminal_old_run_remains_observable_without_origin_lookup(env, entry, state):
    sender, _, _, runs, sqs, _, reader = env
    pending(env)
    runs.record.update(originationNumberArn=TX_ORIGIN, initializationComplete=True, totalEnqueued=1, totalFailed=1)
    if state == "ABORTED":
        runs.record["status"] = "ABORTED"
    sender._origin_client.describe_phone_numbers.side_effect = AssertionError("No new origin dependency for observation")
    result = getattr(sender, entry)({**EVENT, "originationNumberArn": "invalid-loser-payload"}, None)
    if state == "sealed_failed":
        assert result["initializationComplete"] is True and result["pending"] is False and result["totalFailed"] == 1
    sender._origin_client.describe_phone_numbers.assert_not_called()
    reader.assert_not_called()
    sqs.send_message_batch.assert_not_called()


def test_put_collision_revalidates_winners_origin_before_export(env):
    sender, _, queue, runs, sqs, _, reader = env
    original_put = runs.put_item

    def competing_put(**kwargs):
        runs.record = {**copy.deepcopy(kwargs["Item"]), "originationNumberArn": TX_ORIGIN}
        return original_put(**kwargs)

    def describe(**kwargs):
        arn = kwargs["PhoneNumberIds"][0]
        return {"PhoneNumbers": [number(arn, MessageType="TRANSACTIONAL" if arn == TX_ORIGIN else "PROMOTIONAL")]}

    sender._origin_client.describe_phone_numbers.side_effect = describe
    with patch.object(runs, "put_item", side_effect=competing_put), pytest.raises(ValueError):
        sender.lambda_handler(EVENT, None)
    assert [c.kwargs["PhoneNumberIds"] for c in sender._origin_client.describe_phone_numbers.call_args_list] == [[EVENT["originationNumberArn"]], [TX_ORIGIN]]
    assert runs.record["originationNumberArn"] == TX_ORIGIN and queue.items == {}
    reader.assert_not_called()
    sqs.send_message_batch.assert_not_called()


def test_candidate_lookup_failure_recovers_concurrently_sealed_winner(env):
    sender, _, queue, runs, sqs, _, reader = env

    def winner_finishes_during_lookup(**kwargs):
        runs.record = {
            "planId": EVENT["planId"], "sk": f"{EVENT['runId']}#{EVENT['campaignId']}",
            "smsCampaignId": EVENT["campaignId"], "status": "RUNNING",
            "smsTemplateVersion": EVENT["smsTemplateVersion"], "messageTemplate": EVENT["messageTemplate"],
            "originationNumberArn": EVENT["originationNumberArn"], "segmentName": EVENT["segmentName"],
            "initializationComplete": True, "activeEnqueueBatches": 0,
            "totalEnqueued": 1, "totalSent": 0, "totalFailed": 1,
        }
        return {"PhoneNumbers": [number(TX_ORIGIN, MessageType="TRANSACTIONAL")]}

    sender._origin_client.describe_phone_numbers.side_effect = winner_finishes_during_lookup
    result = sender.lambda_handler({**EVENT, "originationNumberArn": TX_ORIGIN}, None)
    assert result["initializationComplete"] is True and result["pending"] is False and result["totalFailed"] == 1
    sender._origin_client.describe_phone_numbers.assert_called_once_with(PhoneNumberIds=[TX_ORIGIN])
    assert runs.record["originationNumberArn"] == EVENT["originationNumberArn"] and queue.items == {}
    reader.assert_not_called()
    sqs.send_message_batch.assert_not_called()


@pytest.mark.parametrize("mode", ["legacy", "profile"])
def test_legacy_and_precall_keep_existing_origin_contract_without_eum_lookup(env, mode):
    sender, processor, _, _, sqs, sms, _ = env
    sender._origin_client.describe_phone_numbers.side_effect = AssertionError("Legacy and profile must not look up origin")
    event = {**_EVENT, "scheduleSource": "plans", **({"precallPolicy": POLICY} if mode == "profile" else {})}
    sender.lambda_handler(event, None)
    for call in sqs.send_message_batch.call_args_list:
        processor.lambda_handler({"Records": [{"body": item["MessageBody"]} for item in call.kwargs["Entries"]]}, None)
    sender._origin_client.describe_phone_numbers.assert_not_called()
    assert sms.send_text_message.call_args.kwargs["MessageType"] == "TRANSACTIONAL"
