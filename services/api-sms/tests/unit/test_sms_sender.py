"""Tests for sms_sender_handler.py."""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

_ENV = {
    "SMS_CAMPAIGN_QUEUE_TABLE": "VipSmsCampaignQueue",
    "SMS_CAMPAIGN_RUNS_TABLE": "VipSmsCampaignRuns",
    "SMS_SQS_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123/vip-sms-campaign-queue",
    "PROFILES_DOMAIN_NAME": "amazon-connect-test",
}


def _load_handler():
    with patch.dict(os.environ, _ENV):
        with patch("boto3.client"), patch("boto3.resource"):
            import importlib
            import sms_sender_handler
            importlib.reload(sms_sender_handler)
            return sms_sender_handler


def _base_event() -> dict:
    return {
        "campaignId": "cmp-test-1",
        "planId": "plan-1",
        "runId": "run-1",
        "planName": "Test Plan",
        "segmentArn": "arn:aws:profile:us-east-1:123:domains/test/segment-definitions/seg-1",
        "segmentName": "seg-1",
        "messageTemplate": "Your appointment is confirmed. Reply STOP to opt out.",
        "originationNumberArn": "arn:aws:sms-voice:us-east-1:123:phone-number/p-1",
        "originationNumber": "+15125551111",
    }


def _make_mock_cp(phone_ids: list[str] = None, phones: list[str] = None):
    """Build a mock CP client that returns phone numbers for a segment."""
    phone_ids = phone_ids or ["profile-001"]
    phones = phones or ["+15125559999"]
    cp = MagicMock()
    cp.get_segment_membership.return_value = {
        "Profiles": phone_ids,
    }
    cp.batch_get_profile.return_value = {
        "Profiles": [{"PhoneNumber": p} for p in phones],
    }
    return cp


def test_sender_enqueues_valid_e164_phones():
    handler = _load_handler()

    mock_ddb = MagicMock()
    mock_runs_table = MagicMock()
    mock_queue_table = MagicMock()
    mock_ddb.Table.side_effect = lambda name: (
        mock_runs_table if "Runs" in name else mock_queue_table
    )

    mock_sqs = MagicMock()
    mock_cp = _make_mock_cp(phones=["+15125559999"])

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_cp", mock_cp),
    ):
        result = handler.lambda_handler(_base_event(), None)

    assert result["enqueued"] == 1
    mock_sqs.send_message_batch.assert_called()
    call_args = mock_sqs.send_message_batch.call_args
    entries = call_args.kwargs.get("Entries") or call_args.args[0] if call_args.args else call_args.kwargs.get("Entries")


def test_sender_skips_invalid_phone_formats():
    handler = _load_handler()

    mock_ddb = MagicMock()
    mock_ddb.Table.return_value = MagicMock()
    mock_sqs = MagicMock()
    # "555-1234" = 7 digits (not 10/11), "invalid" = no digits, "abc-123" = 3 digits
    # — none can be normalized to E.164. "+15125559999" is the only valid one.
    mock_cp = _make_mock_cp(phones=["555-1234", "invalid", "+15125559999", "abc-123"])

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_cp", mock_cp),
    ):
        result = handler.lambda_handler(_base_event(), None)

    assert result["enqueued"] == 1


def test_sender_empty_segment_returns_zero():
    handler = _load_handler()

    mock_ddb = MagicMock()
    mock_ddb.Table.return_value = MagicMock()
    mock_sqs = MagicMock()
    cp = MagicMock()
    cp.get_segment_membership.return_value = {"Profiles": []}

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_cp", cp),
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
    mock_cp = _make_mock_cp(
        phone_ids=[f"p-{i}" for i in range(25)],
        phones=phones,
    )
    # H-C4: batch_get_profile is now called with up to 100 IDs at once.
    # The mock must return a phone for each ProfileId passed (not just one).
    def _batch_get(DomainName, ProfileIds):
        result = []
        for pid in ProfileIds:
            idx = int(pid.split("-")[1])  # "p-0" → 0
            result.append({"PhoneNumber": phones[idx]})
        return {"Profiles": result}
    mock_cp.batch_get_profile.side_effect = _batch_get

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_cp", mock_cp),
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
    mock_cp = _make_mock_cp(phones=["+15125559999"])

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
        patch.object(handler, "_cp", mock_cp),
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


def test_sender_partial_sqs_failure_updates_run_summary_total_failed():
    handler = _load_handler()

    mock_runs_table = MagicMock()
    mock_queue_table = MagicMock()
    mock_ddb = MagicMock()
    mock_ddb.Table.side_effect = lambda name: (
        mock_runs_table if "Runs" in name else mock_queue_table
    )

    mock_sqs = MagicMock()
    mock_cp = _make_mock_cp(phones=["+15125559999"])
    mock_sqs.send_message_batch.side_effect = lambda QueueUrl, Entries: {
        "Failed": [{"Id": Entries[0]["Id"], "Code": "ThrottlingException"}]
    }

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_cp", mock_cp),
    ):
        handler.lambda_handler(_base_event(), None)

    runs_update_kwargs = mock_runs_table.update_item.call_args.kwargs
    assert runs_update_kwargs["ExpressionAttributeValues"][":f"] == 1
    assert runs_update_kwargs["ExpressionAttributeValues"][":n"] == 0


def test_sender_mixed_success_and_failure_in_same_batch():
    handler = _load_handler()

    mock_runs_table = MagicMock()
    mock_queue_table = MagicMock()
    mock_ddb = MagicMock()
    mock_ddb.Table.side_effect = lambda name: (
        mock_runs_table if "Runs" in name else mock_queue_table
    )

    mock_sqs = MagicMock()
    mock_cp = _make_mock_cp(
        phone_ids=["p-0", "p-1"],
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
        patch.object(handler, "_cp", mock_cp),
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
    mock_cp = _make_mock_cp(phones=["+15125559999"])

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_cp", mock_cp),
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
    mock_cp = _make_mock_cp(phones=[])

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_cp", mock_cp),
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


def test_sender_skips_profile_with_no_id():
    """Empty ProfileId entries are skipped without breaking the loop (line 170)."""
    handler = _load_handler()

    mock_ddb = MagicMock()
    mock_ddb.Table.return_value = MagicMock()
    mock_sqs = MagicMock()
    cp = MagicMock()
    # Return one dict entry with no ProfileId — should be skipped silently
    cp.get_segment_membership.return_value = {"Profiles": [{}]}

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_cp", cp),
    ):
        result = handler.lambda_handler(_base_event(), None)

    assert result["enqueued"] == 0
    cp.batch_get_profile.assert_not_called()


def test_sender_pagination_follows_next_token():
    """NextToken causes a second call to get_segment_membership (line 182)."""
    handler = _load_handler()

    mock_ddb = MagicMock()
    mock_ddb.Table.return_value = MagicMock()
    mock_sqs = MagicMock()

    cp = MagicMock()
    # First page returns a NextToken; second page returns no NextToken
    cp.get_segment_membership.side_effect = [
        {"Profiles": ["profile-001"], "NextToken": "tok-1"},
        {"Profiles": ["profile-002"]},
    ]
    cp.batch_get_profile.side_effect = [
        {"Profiles": [{"PhoneNumber": "+15125550001"}]},
        {"Profiles": [{"PhoneNumber": "+15125550002"}]},
    ]

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_cp", cp),
    ):
        result = handler.lambda_handler(_base_event(), None)

    assert result["enqueued"] == 2
    assert cp.get_segment_membership.call_count == 2


def test_sender_get_segment_phones_exception_returns_zero(capsys):
    """Exception inside _get_segment_phones is caught and returns empty list (lines 183-184)."""
    handler = _load_handler()

    mock_ddb = MagicMock()
    mock_ddb.Table.return_value = MagicMock()
    mock_sqs = MagicMock()
    cp = MagicMock()
    cp.get_segment_membership.side_effect = RuntimeError("CP unavailable")

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_cp", cp),
    ):
        result = handler.lambda_handler(_base_event(), None)

    assert result["enqueued"] == 0
    captured = capsys.readouterr()
    assert "RuntimeError" in captured.out


def test_sender_no_phi_in_print_calls(capsys):
    """Phone numbers must NOT appear in any print() output."""
    handler = _load_handler()

    mock_ddb = MagicMock()
    mock_ddb.Table.return_value = MagicMock()
    mock_sqs = MagicMock()
    mock_cp = _make_mock_cp(phones=["+15125559876"])

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_cp", mock_cp),
    ):
        handler.lambda_handler(_base_event(), None)

    captured = capsys.readouterr()
    assert "+15125559876" not in captured.out
    assert "+15125559876" not in captured.err
