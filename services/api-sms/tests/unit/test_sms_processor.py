"""Tests for sms_processor_handler.py."""

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
    "SMS_CONFIG_SET_NAME": "vip-sms-config-set",
    "SMS_OPT_OUT_LIST_NAME": "vip-sms-opt-out",
}


def _load_handler():
    with patch.dict(os.environ, _ENV):
        with patch("boto3.client"), patch("boto3.resource"):
            import importlib
            import sms_processor_handler
            importlib.reload(sms_processor_handler)
            return sms_processor_handler


def _make_sqs_event(
    campaign_id: str = "cmp-1",
    sk: str = "2026-01-01T00:00:00+00:00#abc123",
    phone: str = "+15125551234",
    message_template: str = "Your appointment is confirmed.",
    origination_arn: str = "arn:aws:sms-voice:us-east-1:123:phone-number/p-1",
    plan_id: str = "plan-1",
    run_id: str = "run-1",
) -> dict:
    return {
        "Records": [
            {
                "body": json.dumps(
                    {
                        "campaignId": campaign_id,
                        "sk": sk,
                        "phone": phone,
                        "messageTemplate": message_template,
                        "originationNumberArn": origination_arn,
                        "planId": plan_id,
                        "runId": run_id,
                    }
                )
            }
        ]
    }


def _make_ddb_mock():
    """Return mock_ddb with queue/runs table split and no ConditionalCheckFailed by default."""
    mock_ddb = MagicMock()
    mock_queue_table = MagicMock()
    mock_runs_table = MagicMock()
    mock_ddb.Table.side_effect = lambda name: (
        mock_runs_table if "Runs" in name else mock_queue_table
    )
    # Default: ConditionalCheckFailedException is never raised (claim succeeds)
    mock_ddb.meta.client.exceptions.ConditionalCheckFailedException = type(
        "ConditionalCheckFailedException", (Exception,), {}
    )
    return mock_ddb, mock_queue_table, mock_runs_table


# ── SENT path ────────────────────────────────────────────────────────────────


def test_sent_path_updates_queue_and_increments_counter():
    handler = _load_handler()

    mock_sms = MagicMock()
    mock_sms.send_text_message.return_value = {"MessageId": "msg-1234"}
    mock_sms.exceptions.ValidationException = Exception

    mock_ddb, mock_queue_table, mock_runs_table = _make_ddb_mock()

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_sms", mock_sms),
        patch.object(handler, "_ddb", mock_ddb),
    ):
        handler.lambda_handler(_make_sqs_event(), None)

    # Queue item: first call = PENDING→SENDING claim, last call = SENT update
    assert mock_queue_table.update_item.call_count == 2
    last_queue_update = mock_queue_table.update_item.call_args
    assert "SENT" in str(last_queue_update)

    # Runs counter incremented with totalSent
    runs_update = mock_runs_table.update_item.call_args
    assert "totalSent" in str(runs_update)


def test_sent_path_includes_message_id_in_queue_update():
    handler = _load_handler()

    mock_sms = MagicMock()
    mock_sms.send_text_message.return_value = {"MessageId": "msg-xyz-9999"}
    mock_sms.exceptions.ValidationException = Exception

    mock_ddb, mock_queue_table, mock_runs_table = _make_ddb_mock()

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_sms", mock_sms),
        patch.object(handler, "_ddb", mock_ddb),
    ):
        handler.lambda_handler(_make_sqs_event(), None)

    # Last queue update contains the messageId
    update_kwargs = mock_queue_table.update_item.call_args.kwargs
    assert "msg-xyz-9999" in str(update_kwargs)


def test_sent_path_does_not_reraise():
    handler = _load_handler()

    mock_sms = MagicMock()
    mock_sms.send_text_message.return_value = {"MessageId": "msg-ok"}
    mock_sms.exceptions.ValidationException = Exception

    mock_ddb, mock_queue_table, mock_runs_table = _make_ddb_mock()

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_sms", mock_sms),
        patch.object(handler, "_ddb", mock_ddb),
    ):
        # Should not raise
        handler.lambda_handler(_make_sqs_event(), None)


def test_runs_counter_uses_plan_id_and_run_id_from_sqs_body():
    """Counter increment targets the run record directly (no scan)."""
    handler = _load_handler()

    mock_sms = MagicMock()
    mock_sms.send_text_message.return_value = {"MessageId": "msg-ok"}
    mock_sms.exceptions.ValidationException = Exception

    mock_ddb, mock_queue_table, mock_runs_table = _make_ddb_mock()

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_sms", mock_sms),
        patch.object(handler, "_ddb", mock_ddb),
    ):
        handler.lambda_handler(
            _make_sqs_event(campaign_id="cmp-42", plan_id="plan-99", run_id="run-77"),
            None,
        )

    runs_update_kwargs = mock_runs_table.update_item.call_args.kwargs
    # Key must use planId and runId#campaignId sk — no scan
    assert runs_update_kwargs["Key"]["planId"] == "plan-99"
    assert runs_update_kwargs["Key"]["sk"] == "run-77#cmp-42"


# ── H-A1: Idempotency — PENDING → SENDING claim ──────────────────────────────


def test_duplicate_message_skipped_when_not_pending(capsys):
    """If ConditionalCheckFailedException is raised, processing is skipped entirely."""
    handler = _load_handler()

    mock_sms = MagicMock()
    mock_sms.exceptions.ValidationException = Exception

    mock_ddb, mock_queue_table, mock_runs_table = _make_ddb_mock()

    # Simulate: item is already SENT (claim fails)
    ConditionalCheckFailed = mock_ddb.meta.client.exceptions.ConditionalCheckFailedException
    mock_queue_table.update_item.side_effect = ConditionalCheckFailed("already claimed")

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_sms", mock_sms),
        patch.object(handler, "_ddb", mock_ddb),
    ):
        handler.lambda_handler(_make_sqs_event(), None)

    # SMS must NOT be sent
    mock_sms.send_text_message.assert_not_called()
    # Log confirms the skip
    captured = capsys.readouterr()
    assert "skipped" in captured.out


def test_pending_to_sending_claim_is_first_queue_update():
    """The conditional PENDING→SENDING write is the first update on the queue table."""
    handler = _load_handler()

    mock_sms = MagicMock()
    mock_sms.send_text_message.return_value = {"MessageId": "msg-ok"}
    mock_sms.exceptions.ValidationException = Exception

    mock_ddb, mock_queue_table, mock_runs_table = _make_ddb_mock()

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_sms", mock_sms),
        patch.object(handler, "_ddb", mock_ddb),
    ):
        handler.lambda_handler(_make_sqs_event(), None)

    # First call: claim (contains "SENDING" and ConditionExpression)
    first_call_kwargs = mock_queue_table.update_item.call_args_list[0].kwargs
    assert "SENDING" in str(first_call_kwargs)
    assert "ConditionExpression" in first_call_kwargs


# ── OPTED_OUT path ────────────────────────────────────────────────────────────


def test_opted_out_path_does_not_reraise():
    handler = _load_handler()

    class FakeValidationException(Exception):
        pass

    mock_sms = MagicMock()
    mock_sms.exceptions.ValidationException = FakeValidationException
    mock_sms.send_text_message.side_effect = FakeValidationException("OptedOut number")

    mock_ddb, mock_queue_table, mock_runs_table = _make_ddb_mock()

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_sms", mock_sms),
        patch.object(handler, "_ddb", mock_ddb),
    ):
        # Should NOT raise
        handler.lambda_handler(_make_sqs_event(), None)

    last_queue_update = mock_queue_table.update_item.call_args.kwargs
    assert "OPTED_OUT" in str(last_queue_update)

    runs_update = mock_runs_table.update_item.call_args
    assert "totalOptedOut" in str(runs_update)


# ── FAILED path (generic exception) ──────────────────────────────────────────


def test_generic_exception_marks_failed_and_reraises():
    handler = _load_handler()

    class FakeValidationException(Exception):
        pass

    mock_sms = MagicMock()
    mock_sms.exceptions.ValidationException = FakeValidationException
    mock_sms.send_text_message.side_effect = RuntimeError("unexpected")

    mock_ddb, mock_queue_table, mock_runs_table = _make_ddb_mock()

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_sms", mock_sms),
        patch.object(handler, "_ddb", mock_ddb),
    ):
        with pytest.raises(RuntimeError):
            handler.lambda_handler(_make_sqs_event(), None)

    last_queue_update = mock_queue_table.update_item.call_args.kwargs
    assert "FAILED" in str(last_queue_update)
    runs_update = mock_runs_table.update_item.call_args
    assert "totalFailed" in str(runs_update)


def test_validation_exception_non_opted_out_marks_failed_and_reraises():
    handler = _load_handler()

    class FakeValidationException(Exception):
        pass

    mock_sms = MagicMock()
    mock_sms.exceptions.ValidationException = FakeValidationException
    mock_sms.send_text_message.side_effect = FakeValidationException("Invalid phone number")

    mock_ddb, mock_queue_table, mock_runs_table = _make_ddb_mock()

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_sms", mock_sms),
        patch.object(handler, "_ddb", mock_ddb),
    ):
        # B3: original exception is replaced with RuntimeError to strip PHI
        with pytest.raises(RuntimeError):
            handler.lambda_handler(_make_sqs_event(), None)

    last_queue_update = mock_queue_table.update_item.call_args.kwargs
    assert "FAILED" in str(last_queue_update)


def test_reraise_does_not_contain_phone_number(capsys):
    """B3: RuntimeError raised on failure must not include the phone number (PHI)."""
    handler = _load_handler()

    class FakeValidationException(Exception):
        pass

    phone = "+15125551234"
    mock_sms = MagicMock()
    mock_sms.exceptions.ValidationException = FakeValidationException
    # EUM SMS routinely includes DestinationPhoneNumber in ValidationException messages
    mock_sms.send_text_message.side_effect = FakeValidationException(
        f"Number {phone} is invalid for this region"
    )

    mock_ddb, mock_queue_table, mock_runs_table = _make_ddb_mock()

    raised_msg = ""
    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_sms", mock_sms),
        patch.object(handler, "_ddb", mock_ddb),
    ):
        try:
            handler.lambda_handler(_make_sqs_event(phone=phone), None)
        except RuntimeError as exc:
            raised_msg = str(exc)

    assert phone not in raised_msg


# ── Config set and opt-out list are passed ────────────────────────────────────


def test_config_set_included_in_send_when_set():
    handler = _load_handler()

    mock_sms = MagicMock()
    mock_sms.send_text_message.return_value = {"MessageId": "msg-ok"}
    mock_sms.exceptions.ValidationException = Exception

    mock_ddb, mock_queue_table, mock_runs_table = _make_ddb_mock()

    with (
        patch.dict(os.environ, {**_ENV, "SMS_CONFIG_SET_NAME": "vip-sms-config-set"}),
        patch.object(handler, "_sms", mock_sms),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_CONFIG_SET", "vip-sms-config-set"),
    ):
        handler.lambda_handler(_make_sqs_event(), None)

    call_kwargs = mock_sms.send_text_message.call_args.kwargs
    assert call_kwargs.get("ConfigurationSetName") == "vip-sms-config-set"


def test_counter_increment_failure_is_non_fatal():
    handler = _load_handler()

    mock_sms = MagicMock()
    mock_sms.send_text_message.return_value = {"MessageId": "msg-ok"}
    mock_sms.exceptions.ValidationException = Exception

    mock_ddb, mock_queue_table, mock_runs_table = _make_ddb_mock()
    # Counter increment uses update_item directly (no scan) — simulate DDB error
    mock_runs_table.update_item.side_effect = Exception("DDB update failed")

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_sms", mock_sms),
        patch.object(handler, "_ddb", mock_ddb),
    ):
        # Should NOT raise even if counter increment fails
        handler.lambda_handler(_make_sqs_event(), None)


def test_counter_skipped_when_plan_id_missing():
    """If planId is absent from SQS body, counter increment is skipped (no crash)."""
    handler = _load_handler()

    mock_sms = MagicMock()
    mock_sms.send_text_message.return_value = {"MessageId": "msg-ok"}
    mock_sms.exceptions.ValidationException = Exception

    mock_ddb, mock_queue_table, mock_runs_table = _make_ddb_mock()

    # SQS body without planId/runId (legacy message)
    event = {
        "Records": [{
            "body": json.dumps({
                "campaignId": "cmp-1",
                "sk": "sk-1",
                "phone": "+15125551234",
                "messageTemplate": "Msg",
                "originationNumberArn": "arn:1",
            })
        }]
    }

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_sms", mock_sms),
        patch.object(handler, "_ddb", mock_ddb),
    ):
        handler.lambda_handler(event, None)

    # Runs table must NOT be touched (early return from _increment_runs_counter)
    mock_runs_table.update_item.assert_not_called()


# ── No PHI in logs ────────────────────────────────────────────────────────────


def test_no_phi_phone_in_print_output(capsys):
    handler = _load_handler()

    mock_sms = MagicMock()
    mock_sms.send_text_message.return_value = {"MessageId": "msg-ok"}
    mock_sms.exceptions.ValidationException = Exception

    mock_ddb, mock_queue_table, mock_runs_table = _make_ddb_mock()

    phone = "+15125559876"

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_sms", mock_sms),
        patch.object(handler, "_ddb", mock_ddb),
    ):
        handler.lambda_handler(_make_sqs_event(phone=phone), None)

    captured = capsys.readouterr()
    assert phone not in captured.out
    assert phone not in captured.err
