"""Profile-mode lifecycle tests with durable fake DDB and inline SQS consumption."""
from __future__ import annotations

import copy
import importlib
import json
import os
import re
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from segment_recipients import SegmentRecipientsPending
from vip_shared.domain.services.precall_sms import CATALOG_VERSION
from tests.unit.test_sms_sender_lifecycle import _Queue, _Runs, _ENV, _EVENT, _conflict

POLICY = {"mode": "profile", "catalogVersion": CATALOG_VERSION}
EVENT = {**_EVENT, "precallPolicy": POLICY}
PHONE = "+12125551111"


def profile(phone=PHONE, name="José", specialty="Vein", clinic="Approved Clinic"):
    return {"phone": phone, "ProfileId": "synthetic-profile", "FirstName": name,
            "Attributes": {"campaign": specialty, "location": "TEST", "clinic_name": clinic}}


class Runs(_Runs):
    def update_item(self, *, UpdateExpression, ExpressionAttributeValues, **kwargs):
        values = ExpressionAttributeValues
        if ":snapshot" in values:
            return super().update_item(UpdateExpression=UpdateExpression,
                                       ExpressionAttributeValues=values, **kwargs)
        if kwargs.get("ConditionExpression") and (not self.record or self.record.get("status") != "RUNNING"):
            raise _conflict()
        if ":initializing" in values and self.record.get("initializationComplete") is not values[":initializing"]:
            raise _conflict()
        if ":no_batches" in values and self.record.get("activeEnqueueBatches", 0) != values[":no_batches"]:
            raise _conflict()
        if ":revision" in values and self.record.get("enqueueRevision", 0) != values[":revision"]:
            raise _conflict()
        for action, expression in re.findall(r"(SET|ADD) (.*?)(?= SET | ADD |$)", UpdateExpression):
            for term in expression.split(","):
                field, placeholder = term.replace("=", " ").split()
                field = kwargs.get("ExpressionAttributeNames", {}).get(field, field)
                value = copy.deepcopy(values[placeholder])
                self.record[field] = value if action == "SET" else self.record.get(field, 0) + value


class Queue(_Queue):
    def update_item(self, *, Key, UpdateExpression, ExpressionAttributeValues, **kwargs):
        values = ExpressionAttributeValues
        row = self.items.get(Key["sk"])
        old = copy.deepcopy(row)
        if kwargs.get("ConditionExpression") and (not row or not (
            row.get("status") == "PENDING" or
            (row.get("status") == "SENDING" and row["updatedAt"] < values[":stale_before"])
        )):
            raise _conflict()
        for field, value in re.findall(r"([#\w]+) = (:\w+)", UpdateExpression):
            field = kwargs.get("ExpressionAttributeNames", {}).get(field, field)
            row[field] = values[value]
        return {"Attributes": old} if kwargs.get("ReturnValues") == "ALL_OLD" else {}


@pytest.fixture
def env():
    with patch.dict(os.environ, _ENV), patch("boto3.client"), patch("boto3.resource"):
        import sms_sender_handler
        import sms_processor_handler
        sender = importlib.reload(sms_sender_handler)
        processor = importlib.reload(sms_processor_handler)
    queue, runs, sqs, sms = Queue(), Runs(), MagicMock(), MagicMock()
    ddb = MagicMock()
    ddb.Table.side_effect = lambda name: queue if name == "queue" else runs
    ddb.meta.client.exceptions.ConditionalCheckFailedException = ClientError
    sms.exceptions.ValidationException = type("ValidationException", (Exception,), {})
    sms.send_text_message.return_value = {"MessageId": "provider-accepted"}
    sqs.send_message_batch.return_value = {"Failed": []}
    reader = MagicMock(return_value=[profile()])
    with patch.object(sender, "_ddb", ddb), patch.object(processor, "_ddb", ddb), \
         patch.object(sender, "_sqs", sqs), patch.object(processor, "_sms", sms), \
         patch.object(sender, "_load_segment_recipients", reader), \
         patch.object(sender, "_is_within_quiet_hours", lambda p: True), \
         patch.object(sender, "_opt_out", MagicMock(is_blocked=lambda p: False)):
        yield sender, processor, queue, runs, sqs, sms, reader


def consume_inline(env, *, abort=False):
    sender, processor, queue, runs, sqs, sms, reader = env
    def send(**kwargs):
        assert runs.record["totalEnqueued"] >= len(kwargs["Entries"])
        if abort:
            runs.record["status"] = "ABORTED"
        for entry in kwargs["Entries"]:
            body = json.loads(entry["MessageBody"])
            assert queue.items[body["sk"]]["status"] == "PENDING"
            processor.lambda_handler({"Records": [{"body": entry["MessageBody"]}]}, None)
        return {"Failed": []}
    sqs.send_message_batch.side_effect = send


def test_mixed_profiles_render_complete_exact_copy_and_wait_for_acceptance(env):
    sender, processor, queue, runs, sqs, sms, reader = env
    reader.return_value = [profile(), profile("+12125552222", "Zoë", "Pain", "Second Clinic")]
    event = {**EVENT, "precallPolicy": {**POLICY, "clinicName": " Campaign Clinic "}}
    first = sender.lambda_handler(event, None)
    assert first["initializationComplete"] is True and first["pending"] is True
    assert first["totalEnqueued"] == 2 and first["totalSent"] == 0
    bodies = [json.loads(e["MessageBody"]) for e in sqs.send_message_batch.call_args.kwargs["Entries"]]
    assert bodies[0]["messageTemplate"] == "Hi José! This is Campaign Clinic. We’re about to give you a quick call regarding your vein consultation request. Look out for a call!"
    assert bodies[1]["messageTemplate"] == "Hi Zoë! Campaign Clinic here. We’re calling you in just a moment to discuss your pain management request. Talk soon!"
    for body in bodies:
        processor.lambda_handler({"Records": [{"body": json.dumps(body)}]}, None)
    second = sender.lambda_handler(EVENT, None)
    assert second["pending"] is False and second["totalSent"] == 2
    assert sqs.send_message_batch.call_count == 1
    assert all(call.get("ConsistentRead") for call in runs.reads)
    durable = json.dumps([runs.record, list(queue.items.values())])
    assert "José" not in durable and "Approved Clinic" not in durable and "consultation" not in durable
    assert runs.record["precallPolicy"] == {**POLICY, "clinicName": "Campaign Clinic"}
    assert all(c.kwargs["TimeToLive"] == 300 for c in sms.send_text_message.call_args_list)
    assert [c.kwargs["MessageBody"] for c in sms.send_text_message.call_args_list] == [b["messageTemplate"] for b in bodies]


def test_inline_processor_finds_pending_and_sender_never_overwrites_sent(env):
    sender, processor, queue, runs, sqs, sms, reader = env
    consume_inline(env)
    result = sender.lambda_handler(EVENT, None)
    assert result["initializationComplete"] is True and result["pending"] is False
    assert result["totalEnqueued"] == result["totalSent"] == 1
    assert [v["status"] for v in queue.items.values() if "status" in v] == ["SENT"]


@pytest.mark.parametrize("clinic_config", [{}, {"clinicName": ""}, {"clinicName": " \n\t\u00a0 "}])
def test_omitted_campaign_clinic_reaches_provider_without_any_clinic_phrase(env, clinic_config):
    sender, processor, queue, runs, sqs, sms, reader = env
    reader.return_value = [profile(), profile("+12125552222", "Ana", "Pain", "Ignored Clinic")]
    consume_inline(env)
    result = sender.lambda_handler({**EVENT, "precallPolicy": {**POLICY, **clinic_config}}, None)
    assert result["totalSent"] == 2 and result["totalSkippedPersonalization"] == 0
    assert runs.record["precallPolicy"] == POLICY
    assert [c.kwargs["MessageBody"] for c in sms.send_text_message.call_args_list] == [
        "Hi José! We’re about to give you a quick call regarding your vein consultation request. Look out for a call!",
        "Hi Ana! We’re calling you in just a moment to discuss your pain management request. Talk soon!",
    ]


@pytest.mark.parametrize("profile_attributes", [{"campaign": "Vein"}, {"campaign": "Vein", "clinic_name": "Different Clinic"},
                                               {"campaign": "Vein", "clinic_name": ["invalid"]}])
def test_shared_phone_differences_in_unused_profile_clinic_do_not_conflict(env, profile_attributes):
    sender, processor, queue, runs, sqs, sms, reader = env
    reader.return_value = [profile(), {**profile(), "Attributes": profile_attributes}]
    consume_inline(env)
    result = sender.lambda_handler({**EVENT, "precallPolicy": {**POLICY, "clinicName": "Campaign Clinic"}}, None)
    assert result["totalSent"] == 1 and result["totalSkippedPersonalization"] == 0
    assert "This is Campaign Clinic." in sms.send_text_message.call_args.kwargs["MessageBody"]


@pytest.mark.parametrize("initial_clinic,expected_clinic", [("  Clinica Jose\u0301  ", "Clinica José"), (" \t ", None)])
@pytest.mark.parametrize("recovery", ["sender_without_policy", "sender_changed_policy", "retry_changed_policy"])
def test_pending_run_freezes_normalized_clinic_across_recovery(env, initial_clinic, expected_clinic, recovery):
    sender, processor, queue, runs, sqs, sms, reader = env
    event = {**EVENT, "precallPolicy": {**POLICY, "clinicName": initial_clinic}}
    reader.side_effect = SegmentRecipientsPending("pending")
    assert sender.lambda_handler(event, None)["pending"] is True
    frozen = {**POLICY, **({"clinicName": expected_clinic} if expected_clinic else {})}
    assert runs.record["precallPolicy"] == frozen
    reader.side_effect = None
    reader.return_value = [profile(clinic="Another Profile Clinic")]
    event["precallPolicy"]["clinicName"] = "New Campaign Clinic"
    consume_inline(env)
    if recovery == "sender_without_policy":
        result = sender.lambda_handler(_EVENT, None)
    elif recovery == "sender_changed_policy":
        result = sender.lambda_handler(event, None)
    else:
        result = sender.retry_quiet_hours_skipped(event, None)
    assert result["pending"] is False and result["totalSent"] == 1
    body = json.loads(sqs.send_message_batch.call_args.kwargs["Entries"][0]["MessageBody"])
    assert body["precallPolicy"] == runs.record["precallPolicy"] == frozen
    assert "New Campaign Clinic" not in body["messageTemplate"] and "Another Profile Clinic" not in body["messageTemplate"]
    if expected_clinic:
        assert f"This is {expected_clinic}." in body["messageTemplate"]
    else:
        assert body["messageTemplate"].startswith("Hi José! We’re")
    assert sms.send_text_message.call_args.kwargs["MessageBody"] == body["messageTemplate"]
    sender.lambda_handler(event, None)
    sender.retry_quiet_hours_skipped(event, None)
    processor.lambda_handler({"Records": [{"body": json.dumps(body)}]}, None)
    assert sqs.send_message_batch.call_count == sms.send_text_message.call_count == 1
    assert runs.record["precallPolicy"] == frozen


def test_processor_cancels_if_queue_clinic_differs_from_frozen_policy(env):
    sender, processor, queue, runs, sqs, sms, reader = env
    sender.lambda_handler({**EVENT, "precallPolicy": {**POLICY, "clinicName": "Campaign Clinic"}}, None)
    body = json.loads(sqs.send_message_batch.call_args.kwargs["Entries"][0]["MessageBody"])
    body["precallPolicy"]["clinicName"] = "Changed Clinic"
    processor.lambda_handler({"Records": [{"body": json.dumps(body)}]}, None)
    sms.send_text_message.assert_not_called()
    assert runs.record["totalCancelled"] == 1


@pytest.mark.parametrize("change", [{"FirstName": "Bob"}, {"Attributes": {"campaign": "Pain", "clinic_name": "Approved Clinic"}}])
def test_shared_phone_conflict_suppresses_all_profiles(env, change):
    sender, processor, queue, runs, sqs, sms, reader = env
    reader.return_value = [profile(), {**profile(), **change}]
    result = sender.lambda_handler(EVENT, None)
    assert result["initializationComplete"] is True and result["pending"] is False
    assert result["totalSkippedPersonalization"] == 1 and result["totalEnqueued"] == 0
    assert runs.record["personalizationSkipReasons"] == {"conflicting_profile": 1}
    sqs.send_message_batch.assert_not_called()


def test_identical_shared_profiles_send_once_missing_individual_does_not_block(env):
    sender, processor, queue, runs, sqs, sms, reader = env
    reader.return_value = [profile(), profile(), profile("+12125552222", clinic=""), profile("+12125553333", specialty="Fibroid")]
    consume_inline(env)
    result = sender.lambda_handler(EVENT, None)
    assert result["totalSent"] == 2 and result["totalSkippedPersonalization"] == 1
    assert result["pending"] is False


def test_sqs_rejection_compensates_once_and_retries_without_waiting_for_claim_expiry(env):
    sender, processor, queue, runs, sqs, sms, reader = env
    sqs.send_message_batch.side_effect = lambda **kw: {"Failed": [{"Id": e["Id"], "Code": "Throttled"} for e in kw["Entries"]]}
    first = sender.lambda_handler(EVENT, None)
    assert first["pending"] is True and first["initializationComplete"] is False
    assert first["totalEnqueued"] == 0 and first["totalSqsSendFailed"] == 1
    assert "CLAIM#" + PHONE not in queue.items
    consume_inline(env)
    second = sender.lambda_handler(EVENT, None)
    third = sender.retry_quiet_hours_skipped(EVENT, None)
    assert second["pending"] is False and third["totalSent"] == third["totalEnqueued"] == 1
    assert third["totalSqsSendFailed"] == 1 and sqs.send_message_batch.call_count == 2


def test_ambiguous_sqs_failure_cannot_report_empty_success_or_automatically_resend(env):
    sender, processor, queue, runs, sqs, sms, reader = env
    sqs.send_message_batch.side_effect = TimeoutError("unknown SQS outcome")
    with pytest.raises(TimeoutError):
        sender.lambda_handler(EVENT, None)
    assert runs.record["totalEnqueued"] == 1 and runs.record["initializationComplete"] is False
    sqs.send_message_batch.side_effect = None
    retry = sender.lambda_handler(EVENT, None)
    assert retry["pending"] is True and retry["totalSent"] == 0
    assert sqs.send_message_batch.call_count == 1
    runs.record["status"] = "ABORTED"
    assert sender.lambda_handler(EVENT, None)["terminal"] is True


def test_crash_after_reservation_before_pending_stays_unfinished_until_abort(env):
    sender, processor, queue, runs, sqs, sms, reader = env
    with patch.object(queue, "batch_writer", side_effect=RuntimeError("DDB unavailable")):
        with pytest.raises(RuntimeError):
            sender.lambda_handler(EVENT, None)
    result = sender.lambda_handler(EVENT, None)
    assert result["totalEnqueued"] == 1 and result["pending"] is True
    assert result["initializationComplete"] is False
    sqs.send_message_batch.assert_not_called()
    runs.record["status"] = "ABORTED"
    assert sender.lambda_handler(EVENT, None)["terminal"] is True


def test_snapshot_pending_and_retry_keep_policy_and_zero_integer_aggregates(env):
    sender, processor, queue, runs, sqs, sms, reader = env
    reader.side_effect = SegmentRecipientsPending("pending")
    result = sender.lambda_handler(EVENT, None)
    assert result["pending"] is True and result["initializationComplete"] is False
    assert result["totalEnqueued"] == result["totalSent"] == 0
    assert type(result["totalEnqueued"]) is int
    reader.side_effect = None
    # Caller may omit the optional policy during recovery; stored policy wins.
    consume_inline(env)
    result = sender.lambda_handler(_EVENT, None)
    assert result["pending"] is False and runs.record["precallPolicy"] == POLICY
    assert json.loads(sqs.send_message_batch.call_args.kwargs["Entries"][0]["MessageBody"])["precallPolicy"] == POLICY


@pytest.mark.parametrize("policy", [None, {"mode": "unknown", "catalogVersion": CATALOG_VERSION}, {"mode": "profile", "catalogVersion": "future"},
                                   {**POLICY, "extra": "invalid"}, *({**POLICY, "clinicName": value} for value in [None, [], {}, False, 0, "{{unsafe}}", "Clinic diagnosis"])])
def test_invalid_policy_cannot_create_run_or_read_audience(env, policy):
    sender, processor, queue, runs, sqs, sms, reader = env
    with pytest.raises(ValueError):
        sender.lambda_handler({**EVENT, "precallPolicy": policy}, None)
    assert runs.record is None
    reader.assert_not_called()
    sqs.send_message_batch.assert_not_called()


def test_abort_during_sqs_dispatch_cancels_before_provider_and_preserves_cancelled_row(env):
    sender, processor, queue, runs, sqs, sms, reader = env
    consume_inline(env, abort=True)
    assert sender.lambda_handler(EVENT, None)["terminal"] is True
    assert runs.record["totalCancelled"] == runs.record["totalEnqueued"] == 1
    assert [v["status"] for v in queue.items.values() if "status" in v] == ["CANCELLED"]
    sms.send_text_message.assert_not_called()


def test_suppression_counters_cannot_settle_unaccepted_enqueued_recipient(env):
    sender, processor, queue, runs, sqs, sms, reader = env
    reader.return_value = [profile(), profile("+12125552222", specialty="Fibroid"), profile("+12125553333")]
    with patch.object(sender, "_opt_out", MagicMock(is_blocked=lambda p: p == "+12125553333")):
        result = sender.lambda_handler(EVENT, None)
    assert result["pending"] is True and result["totalEnqueued"] == 1
    assert result["totalSkippedOptOut"] == result["totalSkippedPersonalization"] == 1
    assert result["totalOptedOut"] == result["totalFailed"] == result["totalCancelled"] == 0


def test_legacy_message_remains_compatible_without_profile_ttl_or_gate(env):
    sender, processor, queue, runs, sqs, sms, reader = env
    sender.lambda_handler(_EVENT, None)
    body = json.loads(sqs.send_message_batch.call_args.kwargs["Entries"][0]["MessageBody"])
    assert "precallPolicy" not in body and body["messageTemplate"] == "Hi there from Original Clinic!"
    # The legacy consumer has no dependency on a profile run status.
    runs.record["status"] = "ABORTED"
    processor.lambda_handler({"Records": [{"body": json.dumps(body)}]}, None)
    assert "TimeToLive" not in sms.send_text_message.call_args.kwargs


def test_quiet_hours_suppression_is_immediately_resolved_and_never_retried_after_initialization(env):
    sender, processor, queue, runs, sqs, sms, reader = env
    with patch.object(sender, "_is_within_quiet_hours", lambda p: False):
        result = sender.lambda_handler(EVENT, None)
    assert result["initializationComplete"] is True and result["pending"] is False
    assert result["totalSkippedQuietHours"] == 1 and result["totalEnqueued"] == 0
    reader.reset_mock()
    # Hours opening or updated recipient fields cannot generate late pre-call SMS.
    reader.return_value = [profile(name="Changed", clinic="Changed Clinic")]
    assert sender.lambda_handler(EVENT, None)["pending"] is False
    assert sender.retry_quiet_hours_skipped(EVENT, None)["retried"] == 0
    reader.assert_not_called()
    sqs.send_message_batch.assert_not_called()


def test_initialization_freezes_missing_profile_decision_and_provider_pending_cohort(env):
    sender, processor, queue, runs, sqs, sms, reader = env
    reader.return_value = [profile(), profile("+12125552222", specialty="Fibroid")]
    first = sender.lambda_handler(EVENT, None)
    assert first["initializationComplete"] is True and first["pending"] is True
    reader.reset_mock()
    reader.return_value = [profile(), profile("+12125552222")]
    sender.lambda_handler(EVENT, None)
    sender.retry_quiet_hours_skipped(EVENT, None)
    reader.assert_not_called()
    assert sqs.send_message_batch.call_count == 1 and runs.record["totalSkippedPersonalization"] == 1


def test_failed_sqs_status_write_cannot_remove_reservation_and_report_empty_success(env):
    sender, processor, queue, runs, sqs, sms, reader = env
    sqs.send_message_batch.side_effect = lambda **kw: {"Failed": [{"Id": e["Id"], "Code": "Throttled"} for e in kw["Entries"]]}
    with patch.object(queue, "update_item", side_effect=RuntimeError("DDB unavailable")):
        with pytest.raises(RuntimeError):
            sender.lambda_handler(EVENT, None)
    assert runs.record["totalEnqueued"] == 1
    result = sender.lambda_handler(EVENT, None)
    assert result["pending"] is True and result["totalSent"] == 0
    assert sqs.send_message_batch.call_count == 1


def test_counter_failure_after_sent_never_resends_and_keeps_acceptance_pending(env):
    sender, processor, queue, runs, sqs, sms, reader = env
    consume_inline(env)
    update = runs.update_item
    def fail_sent_counter(**kwargs):
        if "ADD totalSent" in kwargs["UpdateExpression"]:
            raise RuntimeError("DDB unavailable")
        return update(**kwargs)
    with patch.object(runs, "update_item", side_effect=fail_sent_counter):
        first = sender.lambda_handler(EVENT, None)
    assert first["pending"] is True and first["totalEnqueued"] == 1 and first["totalSent"] == 0
    assert sender.lambda_handler(EVENT, None)["pending"] is True
    assert sqs.send_message_batch.call_count == sms.send_text_message.call_count == 1
    assert [v["status"] for v in queue.items.values() if "status" in v] == ["SENT"]


def test_initializer_already_reading_cannot_enqueue_after_another_invocation_seals_cohort(env):
    sender, processor, queue, runs, sqs, sms, reader = env
    consume_inline(env)
    # A has created its row and is inside CP. B completes the same campaign
    # with one phone. A returns a changed profile set containing a new phone.
    def read_while_other_initializer_finishes(**kwargs):
        reader.side_effect = None
        reader.return_value = [profile()]
        completed = sender.lambda_handler(EVENT, None)
        assert completed["pending"] is False and completed["totalSent"] == 1
        return [profile(), profile("+12125552222", name="Bob")]
    reader.side_effect = read_while_other_initializer_finishes
    result = sender.lambda_handler(EVENT, None)
    assert result["pending"] is False and result["totalSent"] == result["totalEnqueued"] == 1
    assert sqs.send_message_batch.call_count == sms.send_text_message.call_count == 1


def test_concurrent_initializer_cannot_seal_before_explicit_sqs_rejection(env):
    sender, processor, queue, runs, sqs, sms, reader = env
    nested = []

    def reject_after_other_initializer_reads_pending(**kwargs):
        nested.append(sender.lambda_handler(EVENT, None))
        return {"Failed": [{"Id": entry["Id"], "Code": "Throttled"}
                           for entry in kwargs["Entries"]]}

    sqs.send_message_batch.side_effect = reject_after_other_initializer_reads_pending
    first = sender.lambda_handler(EVENT, None)
    assert nested[0]["initializationComplete"] is False and nested[0]["pending"] is True
    assert first["initializationComplete"] is False and first["pending"] is True
    assert first["totalEnqueued"] == 0 and first["totalSqsSendFailed"] == 1
    assert runs.record["activeEnqueueBatches"] == 0
    consume_inline(env)
    retry = sender.retry_quiet_hours_skipped(EVENT, None)
    assert retry["pending"] is False and retry["totalSent"] == 1
    assert sqs.send_message_batch.call_count == 2
    assert sms.send_text_message.call_count == 1


def test_initial_history_is_rechecked_after_other_initializer_finishes_rejection(env):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    sender, processor, queue, runs, sqs, sms, reader = env
    ready, release = Event(), Event()

    def paused_rejection(**kwargs):
        ready.set()
        assert release.wait(timeout=5)
        return {"Failed": [{"Id": entry["Id"], "Code": "Throttled"}
                           for entry in kwargs["Entries"]]}

    sqs.send_message_batch.side_effect = paused_rejection
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(sender.lambda_handler, EVENT, None)
        assert ready.wait(timeout=5)

        def finish_first_after_second_read_pending(**kwargs):
            # The replay has already captured its initial already_sent_phones.
            release.set()
            assert first.result(timeout=5)["pending"] is True
            return [profile()]

        reader.side_effect = finish_first_after_second_read_pending
        try:
            second = sender.lambda_handler(EVENT, None)
        finally:
            release.set()
    assert second["initializationComplete"] is False and second["pending"] is True
    assert second["totalEnqueued"] == 0
    reader.side_effect = None
    consume_inline(env)
    assert sender.lambda_handler(EVENT, None)["totalSent"] == 1


def test_enqueue_revision_rejects_seal_after_batch_starts_and_finishes_during_history_read(env):
    sender, processor, queue, runs, sqs, sms, reader = env
    reader.return_value = []
    query = queue.query

    def another_initializer_during_final_history(**kwargs):
        old_history = query(**kwargs)
        queue.query = query
        reader.return_value = [profile()]
        sqs.send_message_batch.side_effect = lambda **kw: {
            "Failed": [{"Id": entry["Id"], "Code": "Throttled"} for entry in kw["Entries"]]
        }
        assert sender.lambda_handler(EVENT, None)["pending"] is True
        assert runs.record["activeEnqueueBatches"] == 0
        return old_history

    with patch.object(queue, "query", side_effect=another_initializer_during_final_history):
        result = sender.lambda_handler(EVENT, None)
    assert result["initializationComplete"] is False and result["pending"] is True
    consume_inline(env)
    assert sender.lambda_handler(EVENT, None)["totalSent"] == 1


def test_crash_after_sqs_success_before_batch_release_cannot_report_success(env):
    sender, processor, queue, runs, sqs, sms, reader = env
    consume_inline(env)
    update = runs.update_item

    def crash_on_release(**kwargs):
        if kwargs["UpdateExpression"] == "ADD activeEnqueueBatches :closed":
            raise RuntimeError("DDB unavailable")
        return update(**kwargs)

    with patch.object(runs, "update_item", side_effect=crash_on_release):
        with pytest.raises(RuntimeError):
            sender.lambda_handler(EVENT, None)
    assert runs.record["activeEnqueueBatches"] == 1
    replay = sender.lambda_handler(EVENT, None)
    assert replay["pending"] is True and replay["initializationComplete"] is False
    assert replay["totalSent"] == replay["totalEnqueued"] == 1
    assert sqs.send_message_batch.call_count == sms.send_text_message.call_count == 1


def test_profile_stale_sending_after_provider_acceptance_never_resends(env):
    sender, processor, queue, runs, sqs, sms, reader = env
    sender.lambda_handler(EVENT, None)
    payload = sqs.send_message_batch.call_args.kwargs["Entries"][0]["MessageBody"]
    event = {"Records": [{"body": payload}]}
    # EUM already returned MessageId, then the worker terminates before SENT.
    with patch.object(processor, "_update_queue_item", side_effect=SystemExit("hard termination")):
        with pytest.raises(SystemExit):
            processor.lambda_handler(event, None)
    row = queue.items[json.loads(payload)["sk"]]
    row["updatedAt"] = "2000-01-01T00:00:00+00:00"
    processor.lambda_handler(event, None)
    processor.lambda_handler(event, None)
    assert sms.send_text_message.call_count == 1
    assert row["status"] == "FAILED" and row["errorCode"] == "PROVIDER_OUTCOME_UNKNOWN"
    result = sender.lambda_handler(EVENT, None)
    assert result["pending"] is False and result["totalFailed"] == result["totalEnqueued"] == 1
    assert result["totalSent"] == 0


def test_legacy_stale_sending_keeps_existing_retry_behavior(env):
    sender, processor, queue, runs, sqs, sms, reader = env
    sender.lambda_handler(_EVENT, None)
    payload = sqs.send_message_batch.call_args.kwargs["Entries"][0]["MessageBody"]
    event = {"Records": [{"body": payload}]}
    with patch.object(processor, "_update_queue_item", side_effect=SystemExit("hard termination")):
        with pytest.raises(SystemExit):
            processor.lambda_handler(event, None)
    row = queue.items[json.loads(payload)["sk"]]
    row["updatedAt"] = "2000-01-01T00:00:00+00:00"
    processor.lambda_handler(event, None)
    assert sms.send_text_message.call_count == 2
    assert row["status"] == "SENT" and runs.record["totalSent"] == 1
