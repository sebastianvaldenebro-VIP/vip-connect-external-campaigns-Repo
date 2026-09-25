"""Tests for sms_sender_handler.py."""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

_ENV = {
    "SMS_CAMPAIGN_QUEUE_TABLE": "VipSmsCampaignQueue",
    "SMS_CAMPAIGN_RUNS_TABLE": "VipSmsCampaignRuns",
    "SMS_SQS_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123/vip-sms-campaign-queue",
    "PROFILES_DOMAIN_NAME": "amazon-connect-test",
    "OPT_OUT_TABLE": "VipConnectOptOutList",
}


def _load_handler():
    with patch.dict(os.environ, _ENV):
        with patch("boto3.client"), patch("boto3.resource"):
            import importlib
            import sms_sender_handler

            importlib.reload(sms_sender_handler)
            return sms_sender_handler


def _base_event(
    message_template: str | None = None, clinic_name: str | None = None
) -> dict:
    event = {
        "campaignId": "cmp-test-1",
        "planId": "plan-1",
        "runId": "run-1",
        "planName": "Test Plan",
        "segmentArn": "arn:aws:profile:us-east-1:123:domains/test/segment-definitions/seg-1",
        "segmentName": "seg-1",
        "messageTemplate": message_template
        or "Your appointment is confirmed. Reply STOP to opt out.",
        "originationNumberArn": "arn:aws:sms-voice:us-east-1:123:phone-number/p-1",
        "originationNumber": "+15125551111",
    }
    if clinic_name is not None:
        event["clinicName"] = clinic_name
    return event


def _make_recipient_reader(phones: list[str] | None = None):
    """Mock the complete-audience reader, whose AWS contract has its own tests."""
    if phones is None:
        phones = ["+15125559999"]
    return MagicMock(
        return_value=[{"phone": phone, "FirstName": ""} for phone in phones]
    )


def _make_recipient_reader_profiles(profiles: list[dict]):
    return MagicMock(
        return_value=[
            {
                "phone": profile.get("PhoneNumber")
                or profile.get("MobilePhoneNumber")
                or "",
                "FirstName": profile.get("FirstName") or "",
            }
            for profile in profiles
        ]
    )


def _mock_ddb():
    """A DDB mock whose .Table(name) returns one consistent MagicMock per name,
    so callers can inspect e.g. put_item calls on a specific table by name."""
    tables: dict[str, MagicMock] = {}

    def _table(name):
        return tables.setdefault(name, MagicMock())

    mock_ddb = MagicMock()
    mock_ddb.Table.side_effect = _table
    return mock_ddb


def test_sender_enqueues_valid_e164_phones():
    handler = _load_handler()

    mock_ddb = MagicMock()
    mock_runs_table = MagicMock()
    mock_queue_table = MagicMock()
    mock_ddb.Table.side_effect = lambda name: (
        mock_runs_table if "Runs" in name else mock_queue_table
    )

    mock_sqs = MagicMock()
    reader = _make_recipient_reader(phones=["+15125559999"])

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_get_segment_recipients", reader),
        patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda *_: False)),
        patch.object(handler, "_is_within_quiet_hours", lambda *_a, **_k: True),
    ):
        result = handler.lambda_handler(_base_event(), None)

    assert result["enqueued"] == 1
    mock_sqs.send_message_batch.assert_called()


def test_sender_skips_phone_on_opt_out_list_and_counts_opted_out():
    handler = _load_handler()

    mock_ddb = MagicMock()
    mock_runs_table = MagicMock()
    mock_queue_table = MagicMock()
    mock_ddb.Table.side_effect = lambda name: (
        mock_runs_table if "Runs" in name else mock_queue_table
    )

    mock_sqs = MagicMock()
    reader = _make_recipient_reader(phones=["+15125559999", "+15125558888"])

    mock_opt_out = MagicMock()
    mock_opt_out.is_blocked.side_effect = lambda p: p == "+15125559999"

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_get_segment_recipients", reader),
        patch.object(handler, "_opt_out", mock_opt_out),
        patch.object(handler, "_is_within_quiet_hours", lambda *_a, **_k: True),
    ):
        result = handler.lambda_handler(_base_event(), None)

    assert result["enqueued"] == 1
    mock_opt_out.is_blocked.assert_any_call("+15125559999")
    mock_opt_out.is_blocked.assert_any_call("+15125558888")
    runs_update = mock_runs_table.update_item.call_args.kwargs
    assert runs_update["ExpressionAttributeValues"][":o"] == 1
    # :o must be bound to totalSkippedOptOut (contacts never enqueued at all), not
    # totalOptedOut (a different counter owned by sms_processor_handler.py for
    # contacts EUM's own suppression list rejected after enqueue).
    assert "totalSkippedOptOut" in runs_update["UpdateExpression"]
    assert "totalOptedOut" not in runs_update["UpdateExpression"]


def test_sender_skips_invalid_phone_formats():
    handler = _load_handler()

    mock_ddb = MagicMock()
    mock_ddb.Table.return_value = MagicMock()
    mock_sqs = MagicMock()
    # "555-1234" = 7 digits (not 10/11), "invalid" = no digits, "abc-123" = 3 digits
    # — none can be normalized to E.164. "+15125559999" is the only valid one.
    reader = _make_recipient_reader(
        phones=["555-1234", "invalid", "+15125559999", "abc-123"]
    )

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_get_segment_recipients", reader),
        patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda *_: False)),
        patch.object(handler, "_is_within_quiet_hours", lambda *_a, **_k: True),
    ):
        result = handler.lambda_handler(_base_event(), None)

    assert result["enqueued"] == 1


def test_sender_empty_segment_returns_zero():
    handler = _load_handler()

    mock_ddb = MagicMock()
    mock_ddb.Table.return_value = MagicMock()
    mock_sqs = MagicMock()
    reader = _make_recipient_reader(phones=[])

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_get_segment_recipients", reader),
        patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda *_: False)),
        patch.object(handler, "_is_within_quiet_hours", lambda *_a, **_k: True),
    ):
        result = handler.lambda_handler(_base_event(), None)

    assert result["enqueued"] == 0
    mock_sqs.send_message_batch.assert_not_called()


def test_sender_sqs_flushes_every_10():
    """SQS send_message_batch is called after every 10 messages."""
    handler = _load_handler()

    phones = [f"+1512555{str(i).zfill(4)}" for i in range(25)]
    mock_ddb = MagicMock()
    mock_ddb.Table.return_value = MagicMock()
    mock_sqs = MagicMock()
    reader = _make_recipient_reader(phones=phones)

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_get_segment_recipients", reader),
        patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda *_: False)),
        patch.object(handler, "_is_within_quiet_hours", lambda *_a, **_k: True),
    ):
        result = handler.lambda_handler(_base_event(), None)

    assert result["enqueued"] == 25
    # 2 batches of 10 + 1 batch of 5 = 3 total SQS send calls
    assert mock_sqs.send_message_batch.call_count == 3


# ── SQS send_message_batch partial failure (audit follow-up, 2026-08-21) ─────
# send_message_batch does not raise on a partial failure — some entries in the
# batch can fail while the call itself returns 200. Previously the response was
# discarded entirely, so a failed entry's DDB row was still written as PENDING
# with no message ever in the queue: permanently stuck, invisible, never retried.


def test_sender_partial_sqs_failure_marks_item_failed_not_pending():
    handler = _load_handler()

    mock_runs_table = MagicMock()
    mock_queue_table = MagicMock()
    mock_ddb = MagicMock()
    mock_ddb.Table.side_effect = lambda name: (
        mock_runs_table if "Runs" in name else mock_queue_table
    )

    mock_sqs = MagicMock()
    reader = _make_recipient_reader(phones=["+15125559999"])

    # Capture the entry Id assigned to the single phone so the failure response
    # can reference it back.
    captured_entries = {}

    def _send_batch(QueueUrl, Entries):
        captured_entries["id"] = Entries[0]["Id"]
        return {"Failed": [{"Id": Entries[0]["Id"], "Code": "ThrottlingException"}]}

    mock_sqs.send_message_batch.side_effect = _send_batch

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_get_segment_recipients", reader),
        patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda *_: False)),
        patch.object(handler, "_is_within_quiet_hours", lambda *_a, **_k: True),
    ):
        result = handler.lambda_handler(_base_event(), None)

    assert result["enqueued"] == 0
    assert result["failed"] == 1

    put_calls = [
        c.kwargs["Item"]
        for c in mock_queue_table.batch_writer.return_value.__enter__.return_value.put_item.call_args_list
    ]
    assert len(put_calls) == 1
    assert put_calls[0]["status"] == "SQS_SEND_FAILED"


def test_sender_partial_sqs_failure_updates_run_summary_total_sqs_send_failed():
    handler = _load_handler()

    mock_runs_table = MagicMock()
    mock_queue_table = MagicMock()
    mock_ddb = MagicMock()
    mock_ddb.Table.side_effect = lambda name: (
        mock_runs_table if "Runs" in name else mock_queue_table
    )

    mock_sqs = MagicMock()
    reader = _make_recipient_reader(phones=["+15125559999"])
    mock_sqs.send_message_batch.side_effect = lambda QueueUrl, Entries: {
        "Failed": [{"Id": Entries[0]["Id"], "Code": "ThrottlingException"}]
    }

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_get_segment_recipients", reader),
        patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda *_: False)),
        patch.object(handler, "_is_within_quiet_hours", lambda *_a, **_k: True),
    ):
        handler.lambda_handler(_base_event(), None)

    runs_update_kwargs = mock_runs_table.update_item.call_args.kwargs
    assert runs_update_kwargs["ExpressionAttributeValues"][":f"] == 1
    assert runs_update_kwargs["ExpressionAttributeValues"][":n"] == 0
    # The SQS-rejected count must land on totalSqsSendFailed, never totalFailed —
    # totalFailed is exclusively owned by sms_processor_handler.py's atomic ADD for
    # a different population (enqueued-then-rejected, which IS inside totalEnqueued).
    assert "totalSqsSendFailed :f" in runs_update_kwargs["UpdateExpression"]
    assert "totalFailed" not in runs_update_kwargs["UpdateExpression"]


def test_sender_mixed_success_and_failure_in_same_batch():
    handler = _load_handler()

    mock_runs_table = MagicMock()
    mock_queue_table = MagicMock()
    mock_ddb = MagicMock()
    mock_ddb.Table.side_effect = lambda name: (
        mock_runs_table if "Runs" in name else mock_queue_table
    )

    mock_sqs = MagicMock()
    reader = _make_recipient_reader(
        phones=["+15125550001", "+15125550002"],
    )

    def _send_batch(QueueUrl, Entries):
        # Fail only the second entry.
        return {"Failed": [{"Id": Entries[1]["Id"], "Code": "ThrottlingException"}]}

    mock_sqs.send_message_batch.side_effect = _send_batch

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_get_segment_recipients", reader),
        patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda *_: False)),
        patch.object(handler, "_is_within_quiet_hours", lambda *_a, **_k: True),
    ):
        result = handler.lambda_handler(_base_event(), None)

    assert result["enqueued"] == 1
    assert result["failed"] == 1

    put_calls = [
        c.kwargs["Item"]
        for c in mock_queue_table.batch_writer.return_value.__enter__.return_value.put_item.call_args_list
    ]
    statuses = sorted(item["status"] for item in put_calls)
    assert statuses == ["PENDING", "SQS_SEND_FAILED"]


def test_sender_no_sqs_failures_all_written_pending():
    handler = _load_handler()

    mock_runs_table = MagicMock()
    mock_queue_table = MagicMock()
    mock_ddb = MagicMock()
    mock_ddb.Table.side_effect = lambda name: (
        mock_runs_table if "Runs" in name else mock_queue_table
    )

    mock_sqs = MagicMock()
    mock_sqs.send_message_batch.return_value = {"Failed": []}
    reader = _make_recipient_reader(phones=["+15125559999"])

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_get_segment_recipients", reader),
        patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda *_: False)),
        patch.object(handler, "_is_within_quiet_hours", lambda *_a, **_k: True),
    ):
        result = handler.lambda_handler(_base_event(), None)

    assert result["enqueued"] == 1
    assert result["failed"] == 0


def test_sender_runs_table_condition_expression_set():
    """put_item for runs table uses ConditionExpression to prevent duplicates."""
    handler = _load_handler()

    mock_runs_table = MagicMock()
    mock_queue_table = MagicMock()
    mock_ddb = MagicMock()
    mock_ddb.Table.side_effect = lambda name: (
        mock_runs_table if "Runs" in name else mock_queue_table
    )
    mock_sqs = MagicMock()
    reader = _make_recipient_reader(phones=[])

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_get_segment_recipients", reader),
        patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda *_: False)),
        patch.object(handler, "_is_within_quiet_hours", lambda *_a, **_k: True),
    ):
        handler.lambda_handler(_base_event(), None)

    call_kwargs = mock_runs_table.put_item.call_args.kwargs
    assert "ConditionExpression" in call_kwargs
    assert "attribute_not_exists" in str(call_kwargs["ConditionExpression"])


def test_normalize_phone_10_digit_adds_plus1():
    handler = _load_handler()
    assert handler._normalize_phone("5125551234") == "+15125551234"


def test_normalize_phone_11_digit_adds_plus():
    handler = _load_handler()
    assert handler._normalize_phone("15125551234") == "+15125551234"


def test_normalize_phone_already_plus1_unchanged():
    handler = _load_handler()
    assert handler._normalize_phone("+15125551234") == "+15125551234"


def test_normalize_phone_strips_formatting():
    handler = _load_handler()
    assert handler._normalize_phone("(512) 555-1234") == "+15125551234"


def test_sender_processes_all_recipients_returned_by_complete_audience_reader():
    handler = _load_handler()
    reader = _make_recipient_reader(phones=["+15125551111", "+15125552222"])
    sqs = MagicMock()
    sqs.send_message_batch.return_value = {"Failed": []}
    with (
        patch.object(handler, "_ddb", _mock_ddb()),
        patch.object(handler, "_sqs", sqs),
        patch.object(handler, "_get_segment_recipients", reader),
        patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda p: False)),
        patch.object(handler, "_is_within_quiet_hours", lambda p: True),
    ):
        result = handler.lambda_handler(_base_event(), None)
    assert result["enqueued"] == 2
    reader.assert_called_once()


def test_sender_recipient_read_failure_raises_without_updating_counters():
    """A failed read must remain distinguishable from a genuinely empty audience.

    Reader error sanitization and run recovery have separate lifecycle tests.
    """
    handler = _load_handler()

    mock_ddb = MagicMock()
    mock_ddb.Table.return_value = MagicMock()
    mock_sqs = MagicMock()
    reader = MagicMock(side_effect=RuntimeError("SMS recipient read failed"))

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_get_segment_recipients", reader),
        patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda *_: False)),
        patch.object(handler, "_is_within_quiet_hours", lambda *_a, **_k: True),
    ):
        with pytest.raises(RuntimeError, match="SMS recipient read failed"):
            handler.lambda_handler(_base_event(), None)

    mock_ddb.Table.return_value.update_item.assert_not_called()
    mock_sqs.send_message_batch.assert_not_called()


def test_sender_skips_phone_outside_recipient_local_quiet_hours():
    handler = _load_handler()

    mock_ddb = MagicMock()
    mock_runs_table = MagicMock()
    mock_queue_table = MagicMock()
    mock_ddb.Table.side_effect = lambda name: (
        mock_runs_table if "Runs" in name else mock_queue_table
    )

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_sqs", MagicMock()),
        patch.object(
            handler,
            "_get_segment_recipients",
            _make_recipient_reader(phones=["+12125551234", "+14155551234"]),
        ),
        patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda *_: False)),
        patch.object(
            handler, "_is_within_quiet_hours", lambda p, **_: p == "+12125551234"
        ),
    ):
        result = handler.lambda_handler(_base_event(), None)

    assert result["enqueued"] == 1
    assert (
        mock_runs_table.update_item.call_args.kwargs["ExpressionAttributeValues"][":q"]
        == 1
    )


def test_opt_out_is_checked_before_quiet_hours():
    """Ordering matters: an opted-out contact must count as opted out, not as
    quiet-hours-skipped, or the two suppression reasons blur in reporting."""
    handler = _load_handler()
    calls: list[str] = []

    mock_ddb = MagicMock()
    mock_ddb.Table.return_value = MagicMock()

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_sqs", MagicMock()),
        patch.object(
            handler,
            "_get_segment_recipients",
            _make_recipient_reader(phones=["+12125551234"]),
        ),
        patch.object(
            handler,
            "_opt_out",
            MagicMock(is_blocked=lambda *_: (calls.append("opt_out"), True)[1]),
        ),
        patch.object(
            handler,
            "_is_within_quiet_hours",
            lambda *_a, **_k: (calls.append("quiet_hours"), True)[1],
        ),
    ):
        handler.lambda_handler(_base_event(), None)

    assert calls == ["opt_out"]  # quiet_hours never reached


def test_sender_no_phi_in_print_calls():
    """Phone numbers must NOT appear in any StructuredLogger call (formerly print())."""
    handler = _load_handler()

    mock_ddb = MagicMock()
    mock_ddb.Table.return_value = MagicMock()
    mock_sqs = MagicMock()
    reader = _make_recipient_reader(phones=["+15125559876"])

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_get_segment_recipients", reader),
        patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda *_: False)),
        patch.object(handler, "_is_within_quiet_hours", lambda *_a, **_k: True),
        patch.object(handler, "_logger") as mock_logger,
    ):
        handler.lambda_handler(_base_event(), None)

    all_calls = mock_logger.info.call_args_list + mock_logger.warn.call_args_list
    assert all_calls, "expected at least one log call"
    for logged_call in all_calls:
        assert "+15125559876" not in str(logged_call)


# ── Task 3: template personalization (renderer wired into the sender) ────────


def test_sender_renders_first_name_per_recipient():
    handler = _load_handler()
    sent_bodies = []
    mock_sqs = MagicMock()
    mock_sqs.send_message_batch.side_effect = lambda **kw: (
        sent_bodies.extend(json.loads(e["MessageBody"]) for e in kw["Entries"]),
        {"Failed": []},
    )[1]

    reader = _make_recipient_reader_profiles(
        [
            {"PhoneNumber": "+12125551234", "FirstName": "Maria"},
            {"PhoneNumber": "+12125555678", "FirstName": "Jose"},
        ]
    )

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", _mock_ddb()),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_get_segment_recipients", reader),
        patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda *_: False)),
        patch.object(handler, "_is_within_quiet_hours", lambda *_a, **_k: True),
    ):
        handler.lambda_handler(
            _base_event(
                message_template="Hi {{FirstName}}! This is {{ClinicName}}.",
                clinic_name="VIP Medical Group",
            ),
            None,
        )

    bodies = sorted(b["messageTemplate"] for b in sent_bodies)
    assert bodies == [
        "Hi Jose! This is VIP Medical Group.",
        "Hi Maria! This is VIP Medical Group.",
    ]


def test_sender_never_writes_a_rendered_body_to_dynamo():
    """The queue item must stay body-free — it is the long-lived record."""
    handler = _load_handler()

    mock_runs_table = MagicMock()
    mock_queue_table = MagicMock()
    mock_ddb = MagicMock()
    mock_ddb.Table.side_effect = lambda name: (
        mock_runs_table if "Runs" in name else mock_queue_table
    )
    mock_sqs = MagicMock()
    reader = _make_recipient_reader_profiles(
        [{"PhoneNumber": "+12125551234", "FirstName": "Maria"}]
    )

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_get_segment_recipients", reader),
        patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda *_: False)),
        patch.object(handler, "_is_within_quiet_hours", lambda *_a, **_k: True),
    ):
        handler.lambda_handler(
            _base_event(
                message_template="Hi {{FirstName}}!", clinic_name="VIP Medical Group"
            ),
            None,
        )

    written = [
        c.kwargs["Item"]
        for c in mock_queue_table.batch_writer.return_value.__enter__.return_value.put_item.call_args_list
    ]
    assert written, "expected at least one DDB write"
    for item in written:
        assert "messageTemplate" not in item and "messageBody" not in item


def test_sender_never_logs_a_name_or_a_rendered_body():
    """Rendered text (which may contain a first name) must never reach the
    structured logger — same PHI rule that already applies to phone numbers."""
    handler = _load_handler()

    mock_ddb = MagicMock()
    mock_ddb.Table.return_value = MagicMock()
    mock_sqs = MagicMock()
    reader = _make_recipient_reader_profiles(
        [{"PhoneNumber": "+12125551234", "FirstName": "Maria"}]
    )

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_get_segment_recipients", reader),
        patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda *_: False)),
        patch.object(handler, "_is_within_quiet_hours", lambda *_a, **_k: True),
        patch.object(handler, "_logger") as mock_logger,
    ):
        handler.lambda_handler(
            _base_event(
                message_template="Hi {{FirstName}}!", clinic_name="VIP Medical Group"
            ),
            None,
        )

    all_calls = mock_logger.info.call_args_list + mock_logger.warn.call_args_list
    assert all_calls, "expected at least one log call"
    for logged_call in all_calls:
        assert "Maria" not in str(logged_call)


def test_sender_skips_campaign_when_render_rejects_unallowlisted_placeholder():
    """Defense in depth: _validate_sms_campaign should already have rejected a
    template like this. If one slips through anyway, render() raises — the
    sender must not send anything rather than deliver literal braces."""
    handler = _load_handler()

    mock_ddb = MagicMock()
    mock_ddb.Table.return_value = MagicMock()
    mock_sqs = MagicMock()
    reader = _make_recipient_reader_profiles(
        [{"PhoneNumber": "+12125551234", "FirstName": "Maria"}]
    )

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_get_segment_recipients", reader),
        patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda *_: False)),
        patch.object(handler, "_is_within_quiet_hours", lambda *_a, **_k: True),
    ):
        result = handler.lambda_handler(
            _base_event(message_template="Your {{Diagnosis}} is ready."), None
        )

    assert result["enqueued"] == 0
    mock_sqs.send_message_batch.assert_not_called()
