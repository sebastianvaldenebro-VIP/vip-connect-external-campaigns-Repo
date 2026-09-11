"""Tests for the precall-SMS quiet-hours retry mechanism added to
sms_sender_handler.py (2026-09 adversarial-review finding: the pre-call SMS
quiet-hours check ran exactly once, at bucket activation, while Connect
Campaigns V2's own AREA_CODE quiet-hours re-evaluation is continuous for as
long as the voice campaign is running — a recipient outside their window at
activation could get dialed hours later having never received the text).

Covers:
  - _process_recipients: the shared per-recipient loop extracted out of
    lambda_handler, plus its one new behavior (skip a phone already in
    already_sent_phones, no double-send).
  - retry_quiet_hours_skipped: the new invokable entry point (a second Lambda
    Function in CDK, same code asset — see infra/lib/stacks/api-sms-stack.ts).
  - clinicName round-trip: persisted on the VipSmsCampaignRuns start record by
    lambda_handler, read back and used to render by the retry function.

lambda_handler's own existing behavior (unchanged by the _process_recipients
extraction) is covered by test_sms_sender.py, which passes unmodified against
the refactored code.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

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


def _make_mock_cp(phones: list[str] | None = None):
    phones = phones if phones is not None else []
    cp = MagicMock()
    cp.get_segment_membership.return_value = {
        "Profiles": [f"profile-{i}" for i in range(len(phones))]
    }
    cp.batch_get_profile.return_value = {
        "Profiles": [{"PhoneNumber": p} for p in phones]
    }
    return cp


def _base_event(clinic_name: str | None = None) -> dict:
    event = {
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
    if clinic_name is not None:
        event["clinicName"] = clinic_name
    return event


def _mock_ddb_with_runs_record(record: dict | None):
    """Runs table get_item returns {"Item": record} (or {} if record is None).
    Queue table is a separate mock the caller configures per test (for the
    already-sent-phones query and any new writes)."""
    runs_table = MagicMock()
    runs_table.get_item.return_value = {"Item": record} if record is not None else {}
    queue_table = MagicMock()
    ddb = MagicMock()
    ddb.Table.side_effect = lambda name: runs_table if "Runs" in name else queue_table
    return ddb, runs_table, queue_table


# ── _process_recipients: already_sent_phones behavior ────────────────────────


def test_process_recipients_skips_phone_in_already_sent_set_without_counting_it():
    handler = _load_handler()
    mock_sqs = MagicMock()
    mock_sqs.send_message_batch.return_value = {"Failed": []}
    queue_table = MagicMock()

    recipients = [
        {"phone": "+12125551111", "FirstName": "Maria"},  # already sent
        {"phone": "+12125552222", "FirstName": "Jose"},  # new
    ]

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda *_: False)),
        patch.object(handler, "_is_within_quiet_hours", lambda *_a, **_k: True),
    ):
        enqueued, failed, opted_out, outside_qh, rejected = handler._process_recipients(
            recipients,
            campaign_id="cmp-1",
            plan_id="plan-1",
            run_id="run-1",
            message_tmpl="Hi {{FirstName}}!",
            clinic_name="Clinic",
            origination_arn="arn:pn",
            already_sent_phones={"+12125551111"},
            now_iso="2026-09-09T00:00:00+00:00",
            ttl=1234567890,
            queue_table=queue_table,
        )

    assert (enqueued, failed, opted_out, outside_qh, rejected) == (1, 0, 0, 0, None)
    sent_phones = [
        json.loads(e["MessageBody"])["phone"]
        for call in mock_sqs.send_message_batch.call_args_list
        for e in call.kwargs["Entries"]
    ]
    assert sent_phones == ["+12125552222"]


def test_process_recipients_empty_already_sent_set_matches_original_loop_behavior():
    """Regression: with already_sent_phones=set() (lambda_handler's first-pass
    call), behavior must be identical to the pre-refactor inline loop — every
    eligible recipient is sent, same counts."""
    handler = _load_handler()
    mock_sqs = MagicMock()
    mock_sqs.send_message_batch.return_value = {"Failed": []}
    queue_table = MagicMock()

    recipients = [
        {"phone": "+12125551111", "FirstName": "Maria"},
        {"phone": "+12125552222", "FirstName": "Jose"},
    ]

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda *_: False)),
        patch.object(handler, "_is_within_quiet_hours", lambda *_a, **_k: True),
    ):
        enqueued, failed, opted_out, outside_qh, rejected = handler._process_recipients(
            recipients,
            campaign_id="cmp-1",
            plan_id="plan-1",
            run_id="run-1",
            message_tmpl="Hi {{FirstName}}!",
            clinic_name="Clinic",
            origination_arn="arn:pn",
            already_sent_phones=set(),
            now_iso="2026-09-09T00:00:00+00:00",
            ttl=1234567890,
            queue_table=queue_table,
        )

    assert (enqueued, failed, opted_out, outside_qh, rejected) == (2, 0, 0, 0, None)


# ── retry_quiet_hours_skipped: no-op paths ───────────────────────────────────


def test_retry_noop_when_run_record_missing():
    handler = _load_handler()
    ddb, runs_table, queue_table = _mock_ddb_with_runs_record(None)

    with patch.dict(os.environ, _ENV), patch.object(handler, "_ddb", ddb):
        result = handler.retry_quiet_hours_skipped(
            {"campaignId": "c1", "planId": "p1", "runId": "r1"}, None
        )

    assert result == {"retried": 0, "stillSkipped": 0}
    runs_table.update_item.assert_not_called()
    queue_table.query.assert_not_called()


def test_retry_noop_when_nothing_skipped():
    handler = _load_handler()
    record = {"status": "RUNNING", "totalSkippedQuietHours": 0}
    ddb, runs_table, queue_table = _mock_ddb_with_runs_record(record)

    with patch.dict(os.environ, _ENV), patch.object(handler, "_ddb", ddb):
        result = handler.retry_quiet_hours_skipped(
            {"campaignId": "c1", "planId": "p1", "runId": "r1"}, None
        )

    assert result == {"retried": 0, "stillSkipped": 0}
    runs_table.update_item.assert_not_called()
    queue_table.query.assert_not_called()


def test_retry_noop_when_status_completed():
    handler = _load_handler()
    record = {"status": "COMPLETED", "totalSkippedQuietHours": 3}
    ddb, runs_table, queue_table = _mock_ddb_with_runs_record(record)

    with patch.dict(os.environ, _ENV), patch.object(handler, "_ddb", ddb):
        result = handler.retry_quiet_hours_skipped(
            {"campaignId": "c1", "planId": "p1", "runId": "r1"}, None
        )

    assert result == {"retried": 0, "stillSkipped": 0}
    runs_table.update_item.assert_not_called()


def test_retry_noop_when_status_aborted():
    handler = _load_handler()
    record = {"status": "ABORTED", "totalSkippedQuietHours": 3}
    ddb, runs_table, queue_table = _mock_ddb_with_runs_record(record)

    with patch.dict(os.environ, _ENV), patch.object(handler, "_ddb", ddb):
        result = handler.retry_quiet_hours_skipped(
            {"campaignId": "c1", "planId": "p1", "runId": "r1"}, None
        )

    assert result == {"retried": 0, "stillSkipped": 0}
    runs_table.update_item.assert_not_called()


# ── retry_quiet_hours_skipped: real retry passes ─────────────────────────────


def _retry_record(**overrides) -> dict:
    record = {
        "status": "RUNNING",
        "totalSkippedQuietHours": 2,
        "segmentName": "seg-1",
        "messageTemplate": "Hi {{FirstName}}!",
        "clinicName": "VIP Clinic",
        "originationNumberArn": "arn:pn",
    }
    record.update(overrides)
    return record


def test_retry_sends_to_now_open_recipient_and_still_skips_closed_one():
    handler = _load_handler()
    record = _retry_record()
    ddb, runs_table, queue_table = _mock_ddb_with_runs_record(record)
    queue_table.query.return_value = {"Items": []}  # nobody sent yet

    recipients = [
        {"phone": "+12125551111", "FirstName": "Maria"},  # window now open
        {"phone": "+13105552222", "FirstName": "Jose"},  # still closed
    ]
    mock_sqs = MagicMock()
    mock_sqs.send_message_batch.return_value = {"Failed": []}

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_get_segment_recipients", return_value=recipients),
        patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda *_: False)),
        patch.object(
            handler, "_is_within_quiet_hours", lambda p, **_: p == "+12125551111"
        ),
    ):
        result = handler.retry_quiet_hours_skipped(
            {"campaignId": "c1", "planId": "p1", "runId": "r1"}, None
        )

    assert result == {"retried": 1, "stillSkipped": 1}
    update_kwargs = runs_table.update_item.call_args.kwargs
    assert update_kwargs["ExpressionAttributeValues"][":n"] == 1
    assert update_kwargs["ExpressionAttributeValues"][":q"] == 1
    assert "ADD totalEnqueued" in update_kwargs["UpdateExpression"]
    assert "SET totalSkippedQuietHours" in update_kwargs["UpdateExpression"]
    assert "totalEnqueued = :n" not in update_kwargs["UpdateExpression"]


def test_retry_excludes_phones_already_in_queue_table():
    handler = _load_handler()
    record = _retry_record(totalSkippedQuietHours=1)
    ddb, runs_table, queue_table = _mock_ddb_with_runs_record(record)
    queue_table.query.return_value = {"Items": [{"phone": "+12125551111"}]}

    recipients = [{"phone": "+12125551111", "FirstName": "Maria"}]  # already sent
    mock_sqs = MagicMock()

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_get_segment_recipients", return_value=recipients),
        patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda *_: False)),
        patch.object(handler, "_is_within_quiet_hours", lambda *_a, **_k: True),
    ):
        result = handler.retry_quiet_hours_skipped(
            {"campaignId": "c1", "planId": "p1", "runId": "r1"}, None
        )

    assert result == {"retried": 0, "stillSkipped": 0}
    mock_sqs.send_message_batch.assert_not_called()
    query_kwargs = queue_table.query.call_args.kwargs
    assert query_kwargs["KeyConditionExpression"] == "campaignId = :cid"
    assert query_kwargs["ExpressionAttributeValues"][":cid"] == "c1"
    assert "Select" not in query_kwargs  # not the COUNT-only pattern


def test_retry_second_call_recomputes_still_skipped_without_double_counting_enqueued():
    """ADD/SET semantics: a second retry call, after the first already sent
    someone, must report only THIS pass's new sends in :n (DynamoDB's ADD
    action — not this code — is what makes totalEnqueued cumulative in the
    real table), and a freshly-recomputed (not decremented) :q."""
    handler = _load_handler()
    record_pass_1 = _retry_record()
    ddb, runs_table, queue_table = _mock_ddb_with_runs_record(record_pass_1)

    recipients = [
        {"phone": "+12125551111", "FirstName": "Maria"},
        {"phone": "+13105552222", "FirstName": "Jose"},
    ]
    mock_sqs = MagicMock()
    mock_sqs.send_message_batch.return_value = {"Failed": []}

    # Pass 1: nobody sent yet, only Maria's window is open.
    queue_table.query.return_value = {"Items": []}
    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_get_segment_recipients", return_value=recipients),
        patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda *_: False)),
        patch.object(
            handler, "_is_within_quiet_hours", lambda p, **_: p == "+12125551111"
        ),
    ):
        result_1 = handler.retry_quiet_hours_skipped(
            {"campaignId": "c1", "planId": "p1", "runId": "r1"}, None
        )

    assert result_1 == {"retried": 1, "stillSkipped": 1}
    first_call_kwargs = runs_table.update_item.call_args.kwargs
    assert first_call_kwargs["ExpressionAttributeValues"][":n"] == 1
    assert first_call_kwargs["ExpressionAttributeValues"][":q"] == 1

    # Pass 2: Maria is now in the queue table (sent on pass 1, per DynamoDB's
    # real state); Jose's window has now opened too.
    queue_table.query.return_value = {"Items": [{"phone": "+12125551111"}]}
    runs_table.get_item.return_value = {"Item": _retry_record(totalSkippedQuietHours=1)}

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_get_segment_recipients", return_value=recipients),
        patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda *_: False)),
        patch.object(handler, "_is_within_quiet_hours", lambda *_a, **_k: True),
    ):
        result_2 = handler.retry_quiet_hours_skipped(
            {"campaignId": "c1", "planId": "p1", "runId": "r1"}, None
        )

    assert result_2 == {"retried": 1, "stillSkipped": 0}
    second_call_kwargs = runs_table.update_item.call_args.kwargs
    # Only Jose is new this pass — :n must be 1, not 2 (would be double-
    # counting Maria, who ADD already accounted for on pass 1).
    assert second_call_kwargs["ExpressionAttributeValues"][":n"] == 1
    # Freshly recomputed remaining-skip count for this pass, not carried over.
    assert second_call_kwargs["ExpressionAttributeValues"][":q"] == 0


# ── clinicName round-trip ─────────────────────────────────────────────────────


def test_lambda_handler_persists_clinic_name_on_start_record():
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
        patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda *_: False)),
        patch.object(handler, "_is_within_quiet_hours", lambda *_a, **_k: True),
    ):
        handler.lambda_handler(_base_event(clinic_name="VIP Medical Group"), None)

    put_item_kwargs = mock_runs_table.put_item.call_args.kwargs
    assert put_item_kwargs["Item"]["clinicName"] == "VIP Medical Group"


def test_lambda_handler_persists_empty_clinic_name_when_omitted():
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
        patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda *_: False)),
        patch.object(handler, "_is_within_quiet_hours", lambda *_a, **_k: True),
    ):
        handler.lambda_handler(_base_event(), None)

    put_item_kwargs = mock_runs_table.put_item.call_args.kwargs
    assert put_item_kwargs["Item"]["clinicName"] == ""


def test_retry_renders_clinic_name_read_back_from_runs_record():
    handler = _load_handler()
    record = _retry_record(
        totalSkippedQuietHours=1,
        messageTemplate="Hi {{FirstName}}! This is {{ClinicName}}.",
        clinicName="VIP Medical Group",
    )
    ddb, runs_table, queue_table = _mock_ddb_with_runs_record(record)
    queue_table.query.return_value = {"Items": []}

    recipients = [{"phone": "+12125551111", "FirstName": "Maria"}]
    sent_bodies: list[dict] = []
    mock_sqs = MagicMock()
    mock_sqs.send_message_batch.side_effect = lambda **kw: (
        sent_bodies.extend(json.loads(e["MessageBody"]) for e in kw["Entries"]),
        {"Failed": []},
    )[1]

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_get_segment_recipients", return_value=recipients),
        patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda *_: False)),
        patch.object(handler, "_is_within_quiet_hours", lambda *_a, **_k: True),
    ):
        handler.retry_quiet_hours_skipped(
            {"campaignId": "c1", "planId": "p1", "runId": "r1"}, None
        )

    assert sent_bodies[0]["messageTemplate"] == "Hi Maria! This is VIP Medical Group."
