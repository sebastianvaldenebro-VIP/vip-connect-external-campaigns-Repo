"""Internal Plans scheduling authority survives sender/retry persistence.

scheduleSource is an IAM-privileged caller contract, not caller authentication.
These tests never invoke AWS and never treat a bare planId as that contract.
"""
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
from tests.unit.test_sms_profile import EVENT, PHONE, Queue, Runs, profile
from tests.unit.test_sms_sender_lifecycle import _ENV, _EVENT, _conflict


def _condition_matches(expression, record, values):
    """Evaluate the small boolean DDB condition subset used by this fake."""
    expression = expression.strip()
    if not expression:
        return True
    for operator in ("OR", "AND"):
        depth, cuts = 0, []
        for token in re.finditer(r"\(|\)|\bAND\b|\bOR\b", expression):
            if token[0] == "(":
                depth += 1
            elif token[0] == ")":
                depth -= 1
            elif depth == 0 and token[0] == operator:
                cuts.append((token.start(), token.end()))
        if cuts:
            terms, start = [], 0
            for left, right in cuts:
                terms.append(expression[start:left])
                start = right
            terms.append(expression[start:])
            outcomes = (_condition_matches(term, record, values) for term in terms)
            return any(outcomes) if operator == "OR" else all(outcomes)
    if expression.startswith("(") and expression.endswith(")"):
        return _condition_matches(expression[1:-1], record, values)
    match = re.fullmatch(r"attribute_(not_)?exists\((\w+)\)", expression)
    if match:
        exists = match[2] in (record or {})
        return not exists if match[1] else exists
    match = re.fullmatch(r"(\w+)\s*=\s*(:\w+)", expression)
    if match:
        return match[1] in (record or {}) and record[match[1]] == values[match[2]]
    raise AssertionError(f"Unsupported fake condition: {expression}")


class ScheduleRuns(Runs):
    """Durable fake with missing-attribute CAS and injectable competing writes."""

    def __init__(self):
        super().__init__()
        self.before_source_write = None
        self.source_updates = []

    def update_item(self, **kwargs):
        names = kwargs.get("ExpressionAttributeNames", {})
        expression = kwargs["UpdateExpression"]
        for alias, field in names.items():
            expression = expression.replace(alias, field)
        source_update = "scheduleSource" in expression
        if source_update:
            self.source_updates.append(copy.deepcopy(kwargs))
            if self.before_source_write:
                callback, self.before_source_write = self.before_source_write, None
                callback(self)
        condition = kwargs.get("ConditionExpression", "")
        for alias, field in names.items():
            condition = condition.replace(alias, field)
        if not _condition_matches(condition, self.record, kwargs["ExpressionAttributeValues"]):
            raise _conflict()
        old = copy.deepcopy(self.record)
        if source_update:
            # Honor only the submitted CAS, rather than assuming every
            # conditional update implicitly checks status == RUNNING.
            for field, value in re.findall(r"([\w]+)\s*=\s*(:\w+)", expression):
                self.record[field] = copy.deepcopy(kwargs["ExpressionAttributeValues"][value])
            result = {}
        else:
            result = super().update_item(**kwargs)
        if kwargs.get("ReturnValues") == "ALL_NEW":
            return {"Attributes": copy.deepcopy(self.record)}
        if kwargs.get("ReturnValues") == "ALL_OLD":
            return {"Attributes": old}
        return result


@pytest.fixture
def env():
    with patch.dict(os.environ, _ENV), patch("boto3.client"), patch("boto3.resource"):
        import sms_processor_handler
        import sms_sender_handler

        sender = importlib.reload(sms_sender_handler)
        processor = importlib.reload(sms_processor_handler)
    queue, runs, sqs, sms = Queue(), ScheduleRuns(), MagicMock(), MagicMock()
    ddb = MagicMock()
    ddb.Table.side_effect = lambda name: queue if name == "queue" else runs
    ddb.meta.client.exceptions.ConditionalCheckFailedException = ClientError
    sms.exceptions.ValidationException = type("ValidationException", (Exception,), {})
    sms.send_text_message.return_value = {"MessageId": "synthetic-provider-accepted"}
    sqs.send_message_batch.return_value = {"Failed": []}
    reader = MagicMock(return_value=[profile()])
    with (
        patch.object(sender, "_ddb", ddb), patch.object(processor, "_ddb", ddb),
        patch.object(sender, "_sqs", sqs), patch.object(processor, "_sms", sms),
        patch.object(sender, "_load_segment_recipients", reader),
        patch.object(sender, "_opt_out", MagicMock(is_blocked=lambda _: False)),
    ):
        yield sender, processor, queue, runs, sqs, sms, reader


def _event(mode, **extra):
    return {**copy.deepcopy(EVENT if mode == "profile" else _EVENT), **extra}


def _entry(sender, name):
    return sender.lambda_handler if name == "sender" else sender.retry_quiet_hours_skipped


def _pending(env, event):
    sender, _, _, runs, _, _, reader = env
    reader.side_effect = SegmentRecipientsPending("synthetic pending snapshot")
    assert sender.lambda_handler(event, None)["pending"] is True
    assert runs.record["status"] == "RUNNING"
    reader.side_effect = None
    reader.reset_mock()


def _consume_accepted_batches(env):
    # Legacy rows are written after SQS accepts the batch. Consume only after
    # the sender has returned, unlike the profile-only inline fixture.
    _, processor, _, _, sqs, _, _ = env
    for call in sqs.send_message_batch.call_args_list:
        for entry in call.kwargs["Entries"]:
            processor.lambda_handler({"Records": [{"body": entry["MessageBody"]}]}, None)


@pytest.mark.parametrize("mode", ["manual", "profile"])
def test_plan_source_skips_phone_gate_on_initial_send_and_reaches_provider(env, mode):
    sender, _, _, runs, sqs, sms, _ = env
    gate = MagicMock(side_effect=AssertionError("Plans owns this scheduling decision"))
    with patch.object(sender, "_is_within_quiet_hours", gate):
        sender.lambda_handler(_event(mode, scheduleSource="plans"), None)
    _consume_accepted_batches(env)
    gate.assert_not_called()
    assert runs.record["scheduleSource"] == "plans"
    assert runs.record["totalEnqueued"] == runs.record["totalSent"] == 1
    assert runs.record["totalSkippedQuietHours"] == 0
    body = json.loads(sqs.send_message_batch.call_args.kwargs["Entries"][0]["MessageBody"])
    assert sms.send_text_message.call_args.kwargs["MessageBody"] == body["messageTemplate"]


@pytest.mark.parametrize("mode", ["manual", "profile"])
@pytest.mark.parametrize("source", [None, "recipient", "unknown", "PLANS", " plans ", True, {}, ["plans"]])
def test_nonexact_source_and_bare_plan_id_retain_phone_gate(env, mode, source):
    sender, _, _, runs, sqs, _, _ = env
    event = _event(mode)
    if source is not None:
        event["scheduleSource"] = source
    gate = MagicMock(return_value=False)
    with patch.object(sender, "_is_within_quiet_hours", gate):
        sender.lambda_handler(event, None)
    gate.assert_called_once_with(PHONE)
    assert runs.record.get("scheduleSource") != "plans"
    assert runs.record["totalSkippedQuietHours"] == 1
    assert runs.record["totalEnqueued"] == 0
    sqs.send_message_batch.assert_not_called()


@pytest.mark.parametrize("mode", ["manual", "profile"])
@pytest.mark.parametrize("entry", ["sender", "retry"])
@pytest.mark.parametrize("replacement", [{}, {"scheduleSource": "recipient"}])
def test_pending_snapshot_freezes_plan_source_across_replay_and_retry(env, mode, entry, replacement):
    sender, _, _, runs, _, sms, reader = env
    _pending(env, _event(mode, scheduleSource="plans"))
    assert runs.record["scheduleSource"] == "plans"
    gate = MagicMock(side_effect=AssertionError("A replay cannot change the frozen source"))
    replay = _event(mode, messageTemplate="changed replay text", clinicName="Changed Clinic", **replacement)
    with patch.object(sender, "_is_within_quiet_hours", gate):
        _entry(sender, entry)(replay, None)
    _consume_accepted_batches(env)
    gate.assert_not_called()
    assert runs.record["scheduleSource"] == "plans"
    assert runs.record["messageTemplate"] != "changed replay text"
    assert sms.send_text_message.call_count == 1
    assert "Changed Clinic" not in sms.send_text_message.call_args.kwargs["MessageBody"]
    reader.assert_called_once()


@pytest.mark.parametrize("mode", ["manual", "profile"])
@pytest.mark.parametrize("entry", ["sender", "retry"])
def test_active_legacy_row_without_source_adopts_plans_once(env, mode, entry):
    sender, _, _, runs, _, sms, _ = env
    _pending(env, _event(mode))
    runs.record.pop("scheduleSource", None)  # Existing deployed row predates this field.
    frozen_copy = (runs.record["messageTemplate"], runs.record.get("precallPolicy"))
    with patch.object(sender, "_is_within_quiet_hours", side_effect=AssertionError("adopted Plans source")):
        _entry(sender, entry)(_event(mode, scheduleSource="plans"), None)
        _entry(sender, entry)(_event(mode, scheduleSource="recipient"), None)
    _consume_accepted_batches(env)
    assert runs.record["scheduleSource"] == "plans"
    assert len(runs.source_updates) == 1
    assert runs.record["status"] == "RUNNING"
    assert (runs.record["messageTemplate"], runs.record.get("precallPolicy")) == frozen_copy
    assert sms.send_text_message.call_count == 1
    assert all(read.get("ConsistentRead") for read in runs.reads)


@pytest.mark.parametrize("mode", ["manual", "profile"])
@pytest.mark.parametrize("source", ["recipient", "unknown", None])
def test_existing_explicit_nonplan_source_is_not_overwritten(env, mode, source):
    sender, _, _, runs, sqs, _, _ = env
    _pending(env, _event(mode))
    runs.record["scheduleSource"] = source
    gate = MagicMock(return_value=False)
    with patch.object(sender, "_is_within_quiet_hours", gate):
        sender.retry_quiet_hours_skipped(_event(mode, scheduleSource="plans"), None)
    assert runs.record["scheduleSource"] == source
    assert runs.source_updates == []
    gate.assert_called_once_with(PHONE)
    sqs.send_message_batch.assert_not_called()


@pytest.mark.parametrize("mode", ["manual", "profile"])
@pytest.mark.parametrize("winner", ["plans", "recipient"])
def test_legacy_adoption_conflict_uses_persisted_winner(env, mode, winner):
    sender, _, _, runs, sqs, sms, _ = env
    _pending(env, _event(mode))
    runs.record.pop("scheduleSource", None)
    runs.before_source_write = lambda table: table.record.update(scheduleSource=winner)
    gate = MagicMock(return_value=False)
    with patch.object(sender, "_is_within_quiet_hours", gate):
        sender.retry_quiet_hours_skipped(_event(mode, scheduleSource="plans"), None)
    _consume_accepted_batches(env)
    assert runs.record["scheduleSource"] == winner
    assert len(runs.source_updates) == 1
    if winner == "plans":
        gate.assert_not_called()
        assert sms.send_text_message.call_count == 1
    else:
        gate.assert_called_once_with(PHONE)
        sqs.send_message_batch.assert_not_called()


@pytest.mark.parametrize("entry", ["sender", "retry"])
def test_terminal_legacy_run_never_adopts_or_sends(env, entry):
    sender, _, _, runs, sqs, _, reader = env
    _pending(env, _event("manual"))
    runs.record.pop("scheduleSource", None)
    runs.record["status"] = "ABORTED"
    _entry(sender, entry)(_event("manual", scheduleSource="plans"), None)
    assert "scheduleSource" not in runs.record
    assert not runs.source_updates
    reader.assert_not_called()
    sqs.send_message_batch.assert_not_called()


@pytest.mark.parametrize("entry", ["sender", "retry"])
def test_abort_wins_legacy_adoption_race_without_new_send(env, entry):
    sender, _, _, runs, sqs, _, _ = env
    _pending(env, _event("manual"))
    runs.record.pop("scheduleSource", None)
    runs.before_source_write = lambda table: table.record.update(status="ABORTED")
    with patch.object(sender, "_is_within_quiet_hours", return_value=False):
        _entry(sender, entry)(_event("manual", scheduleSource="plans"), None)
    assert runs.record["status"] == "ABORTED"
    assert "scheduleSource" not in runs.record
    sqs.send_message_batch.assert_not_called()


@pytest.mark.parametrize("entry", ["sender", "retry"])
def test_legacy_adoption_failure_cannot_send_with_unpersisted_authority(env, entry):
    sender, _, _, runs, sqs, _, reader = env
    _pending(env, _event("manual"))
    runs.record.pop("scheduleSource", None)

    def fail(_table):
        raise RuntimeError("synthetic DynamoDB unavailable")

    runs.before_source_write = fail
    with pytest.raises(RuntimeError, match="synthetic DynamoDB unavailable"):
        _entry(sender, entry)(_event("manual", scheduleSource="plans"), None)
    assert "scheduleSource" not in runs.record
    reader.assert_not_called()
    sqs.send_message_batch.assert_not_called()


def test_already_sealed_profile_skip_is_not_reopened_by_new_source(env):
    sender, _, _, runs, sqs, _, reader = env
    with patch.object(sender, "_is_within_quiet_hours", return_value=False):
        original = sender.lambda_handler(_event("profile"), None)
    assert original["initializationComplete"] is True
    assert original["totalSkippedQuietHours"] == 1
    reader.reset_mock()
    sender.lambda_handler(_event("profile", scheduleSource="plans"), None)
    sender.retry_quiet_hours_skipped(_event("profile", scheduleSource="plans"), None)
    assert runs.record["initializationComplete"] is True
    assert runs.record["totalEnqueued"] == 0
    reader.assert_not_called()
    sqs.send_message_batch.assert_not_called()


@pytest.mark.parametrize("entry", ["sender", "retry"])
def test_profile_seal_wins_legacy_adoption_race(env, entry):
    sender, _, _, runs, sqs, _, reader = env
    _pending(env, _event("profile"))
    runs.record.pop("scheduleSource", None)
    runs.before_source_write = lambda table: table.record.update(
        initializationComplete=True, totalSkippedQuietHours=1,
    )
    _entry(sender, entry)(_event("profile", scheduleSource="plans"), None)
    assert "scheduleSource" not in runs.record
    assert runs.record["initializationComplete"] is True
    assert runs.record["totalEnqueued"] == 0
    reader.assert_not_called()
    sqs.send_message_batch.assert_not_called()


@pytest.mark.parametrize("entry", ["sender", "retry"])
def test_stale_recipient_hours_worker_cannot_seal_after_plan_source_adoption(env, entry):
    sender, _, _, runs, _, sms, _ = env
    _pending(env, _event("profile"))
    adopted = []

    def concurrent_adoption(_phone):
        # This worker has already captured the old, absent source. A second
        # worker successfully claims Plans scheduling before the old one seals.
        winner = sender._bind_plan_schedule_source(
            copy.deepcopy(runs.record), runs, {"scheduleSource": "plans"},
        )
        adopted.append(winner.get("scheduleSource"))
        return False

    with patch.object(sender, "_is_within_quiet_hours", side_effect=concurrent_adoption):
        first = _entry(sender, entry)(_event("profile"), None)
    assert adopted == ["plans"]
    assert runs.record["scheduleSource"] == "plans"
    assert first["pending"] is True
    assert runs.record["initializationComplete"] is False
    assert runs.record["totalSkippedQuietHours"] == 0
    with patch.object(sender, "_is_within_quiet_hours", side_effect=AssertionError("Plans source won")):
        second = sender.retry_quiet_hours_skipped(_event("profile", scheduleSource="plans"), None)
    assert second["initializationComplete"] is True
    assert runs.record["totalEnqueued"] == 1
    assert runs.record["totalSkippedQuietHours"] == 0
    _consume_accepted_batches(env)
    assert sms.send_text_message.call_count == 1


@pytest.mark.parametrize("mode", ["manual", "profile"])
def test_rejected_sqs_batch_retries_under_frozen_plan_source_once(env, mode):
    sender, _, _, runs, sqs, sms, _ = env
    sqs.send_message_batch.side_effect = lambda **kwargs: {
        "Failed": [{"Id": entry["Id"], "Code": "Throttled"} for entry in kwargs["Entries"]],
    }
    with patch.object(sender, "_is_within_quiet_hours", side_effect=AssertionError("Plans source")):
        sender.lambda_handler(_event(mode, scheduleSource="plans"), None)
        assert runs.record["totalEnqueued"] == 0
        sqs.send_message_batch.side_effect = None
        sender.retry_quiet_hours_skipped(_event(mode), None)
        sender.retry_quiet_hours_skipped(_event(mode, scheduleSource="recipient"), None)
    # Only the accepted batch was delivered; the first batch was rejected.
    processor = env[1]
    accepted = sqs.send_message_batch.call_args_list[-1].kwargs["Entries"]
    for entry in accepted:
        processor.lambda_handler({"Records": [{"body": entry["MessageBody"]}]}, None)
    assert runs.record["scheduleSource"] == "plans"
    assert runs.record["totalEnqueued"] == runs.record["totalSent"] == 1
    assert runs.record["totalSqsSendFailed"] == 1
    assert sqs.send_message_batch.call_count == 2
    assert sms.send_text_message.call_count == 1


@pytest.mark.parametrize("mode", ["manual", "profile"])
def test_plan_source_keeps_optout_deduplication_and_provider_body(env, mode):
    sender, _, _, runs, sqs, sms, reader = env
    blocked = "+12125552222"
    reader.return_value = [profile(), profile(), profile(blocked)]
    with (
        patch.object(sender, "_is_within_quiet_hours", side_effect=AssertionError("Plans source")),
        patch.object(sender._opt_out, "is_blocked", side_effect=lambda phone: phone == blocked),
    ):
        sender.lambda_handler(_event(mode, scheduleSource="plans"), None)
        sender.retry_quiet_hours_skipped(_event(mode), None)
        sender.lambda_handler(_event(mode, scheduleSource="recipient"), None)
    _consume_accepted_batches(env)
    assert runs.record["totalEnqueued"] == runs.record["totalSent"] == 1
    assert runs.record["totalSkippedOptOut"] == 1
    assert runs.record["totalSkippedQuietHours"] == 0
    assert sqs.send_message_batch.call_count == sms.send_text_message.call_count == 1
    assert sms.send_text_message.call_args.kwargs["DestinationPhoneNumber"] == PHONE
