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

Also covers a follow-up 2026-09 adversarial review of THIS retry mechanism
itself (see the "Claim gate" section below):
  - Finding (Critical): already_sent_phones is a read-then-decide in-memory
    check with no DB-level uniqueness guard — two overlapping tick-driven
    retry invocations (this Lambda's 5-minute timeout outlives the ~1-minute
    tick cadence) could both read an empty/stale already_sent_phones for the
    same phone and both send. Fixed with an atomic conditional put_item claim
    in _process_recipients, mirroring executor.py's own claim-before-act idiom.
  - Finding (Important): _get_already_sent_phones treated ANY existing queue
    item — including a genuinely-failed SQS_SEND_FAILED one — as "already
    sent," permanently and silently excluding a failed send from every future
    retry. Fixed by excluding SQS_SEND_FAILED-only phones from that query, and
    by releasing (deleting) the phone's claim record in _flush_sms_batch when
    its send fails, so a later pass can genuinely re-claim and retry it.
"""

from __future__ import annotations

import json
import os
import sys
import time
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

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


def _make_recipient_reader(phones: list[str] | None = None):
    return MagicMock(
        return_value=[{"phone": phone, "FirstName": ""} for phone in phones or []]
    )


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


def test_retry_rechecks_active_run_even_when_quiet_hours_count_is_zero():
    handler = _load_handler()
    record = {"status": "RUNNING", "totalSkippedQuietHours": 0}
    ddb, runs_table, queue_table = _mock_ddb_with_runs_record(record)
    queue_table.query.return_value = {"Items": []}

    with (
        patch.object(handler, "_ddb", ddb),
        patch.object(handler, "_get_segment_recipients", return_value=[]) as read,
    ):
        result = handler.retry_quiet_hours_skipped(
            {"campaignId": "c1", "planId": "p1", "runId": "r1"}, None
        )

    assert result == {"retried": 0, "stillSkipped": 0}
    read.assert_called_once()
    queue_table.query.assert_called_once()
    runs_table.update_item.assert_called_once()


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
    reader = _make_recipient_reader(phones=[])

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_get_segment_recipients", reader),
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


# ── Claim gate: Finding 1 (Critical) — DB-level dedup under concurrent ticks ──
# already_sent_phones alone is a read-then-decide in-memory check with no
# DB-level uniqueness guard. These tests exercise the atomic conditional
# put_item claim in _process_recipients directly, without relying on
# already_sent_phones to have observed anything.


def _conditional_check_failed(message: str = "claim already taken") -> ClientError:
    """Build the exact ClientError shape DynamoDB raises for a failed
    ConditionExpression, so _process_recipients' `exc.response["Error"]["Code"]`
    check exercises the real code path rather than a stand-in."""
    return ClientError(
        error_response={
            "Error": {"Code": "ConditionalCheckFailedException", "Message": message}
        },
        operation_name="PutItem",
    )


def _make_claim_aware_queue_table() -> MagicMock:
    """A queue_table mock that enforces the same claim semantics DynamoDB's real
    ConditionExpression provides post stale-claim-reclaim fix:
    "attribute_not_exists(sk) OR claimedAtEpoch < :stale_before". The first
    put_item for a given (campaignId, sk) always succeeds. A later put_item for
    the SAME key succeeds only if the stored item's claimedAtEpoch is older
    than the given :stale_before value (i.e. the existing claim is stale) —
    otherwise it raises ConditionalCheckFailedException, exactly like a real
    DynamoDB table would. This lets a test simulate two "concurrent" executions
    racing for the same phone's claim (Finding 1's original race), and,
    separately, a later execution legitimately reclaiming an orphaned one
    (the staleness-gap fix) — without a real DynamoDB table.

    A put_item call with ConditionExpression=None (used by tests to seed a
    pre-existing claim) bypasses the check entirely, same as a real
    unconditional put_item would."""
    claimed_epoch: dict[tuple[str, str], int] = {}
    table = MagicMock()

    def _put_item(
        Item, ConditionExpression=None, ExpressionAttributeValues=None, **_kwargs
    ):
        key = (Item["campaignId"], Item["sk"])
        if ConditionExpression and key in claimed_epoch:
            stale_before = (ExpressionAttributeValues or {}).get(":stale_before")
            if stale_before is None or claimed_epoch[key] >= stale_before:
                raise _conditional_check_failed()
        claimed_epoch[key] = Item.get("claimedAtEpoch", 0)

    table.put_item.side_effect = _put_item
    return table


def test_concurrent_process_recipients_calls_for_same_phone_only_send_once():
    """Direct regression test for Finding 1: two calls to _process_recipients
    for the SAME phone, both with already_sent_phones=set() (simulating both
    concurrent tick invocations reading an empty/stale pre-check — the exact
    race described in the finding), must not both enqueue an SQS message. The
    claim gate, not already_sent_phones, is what prevents the second one."""
    handler = _load_handler()
    queue_table = _make_claim_aware_queue_table()
    recipients = [{"phone": "+12125551111", "FirstName": "Maria"}]

    mock_sqs_first = MagicMock()
    mock_sqs_first.send_message_batch.return_value = {"Failed": []}
    mock_sqs_second = MagicMock()
    mock_sqs_second.send_message_batch.return_value = {"Failed": []}

    common_kwargs = dict(
        campaign_id="cmp-1",
        plan_id="plan-1",
        run_id="run-1",
        message_tmpl="Hi {{FirstName}}!",
        clinic_name="Clinic",
        origination_arn="arn:pn",
        already_sent_phones=set(),
        ttl=1234567890,
        queue_table=queue_table,
    )

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda *_: False)),
        patch.object(handler, "_is_within_quiet_hours", lambda *_a, **_k: True),
    ):
        with patch.object(handler, "_sqs", mock_sqs_first):
            enqueued_1, failed_1, *_rest_1 = handler._process_recipients(
                recipients, now_iso="2026-09-09T00:00:00+00:00", **common_kwargs
            )
        # "Concurrent": a second execution processing the same recipient list
        # moments later, its own pre-check having observed the same stale
        # (empty) already_sent_phones as the first.
        with patch.object(handler, "_sqs", mock_sqs_second):
            enqueued_2, failed_2, *_rest_2 = handler._process_recipients(
                recipients, now_iso="2026-09-09T00:00:05+00:00", **common_kwargs
            )

    assert enqueued_1 == 1
    assert enqueued_2 == 0
    assert failed_2 == 0  # skipped via the claim gate, not counted as a failure
    mock_sqs_first.send_message_batch.assert_called_once()
    mock_sqs_second.send_message_batch.assert_not_called()


def test_process_recipients_claim_conflict_skipped_silently_not_counted():
    """A claim conflict must not be miscounted as opted_out or
    outside_quiet_hours — it is its own, silent, no-count skip category, same
    treatment as already_sent_phones."""
    handler = _load_handler()
    queue_table = MagicMock()
    queue_table.put_item.side_effect = _conditional_check_failed()

    recipients = [{"phone": "+12125551111", "FirstName": "Maria"}]
    mock_sqs = MagicMock()

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

    assert (enqueued, failed, opted_out, outside_qh, rejected) == (0, 0, 0, 0, None)
    mock_sqs.send_message_batch.assert_not_called()


def test_process_recipients_claim_put_item_uses_distinguishable_sk_prefix():
    """The claim's sk must be CLAIM#-prefixed so it can never collide with a
    real message item's f"{iso_timestamp}#{random_hex}" sk, and must carry no
    "phone" attribute (so it's naturally excluded from _get_already_sent_phones'
    ProjectionExpression-based scan without special-casing). The
    ConditionExpression must allow BOTH a brand-new claim (attribute_not_exists)
    AND reclaiming one whose own recorded age exceeds _CLAIM_STALE_SECONDS —
    not item non-existence alone (the staleness-gap fix)."""
    handler = _load_handler()
    queue_table = MagicMock()
    recipients = [{"phone": "+12125551111", "FirstName": "Maria"}]
    mock_sqs = MagicMock()
    mock_sqs.send_message_batch.return_value = {"Failed": []}

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda *_: False)),
        patch.object(handler, "_is_within_quiet_hours", lambda *_a, **_k: True),
    ):
        handler._process_recipients(
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

    claim_call = queue_table.put_item.call_args
    item = claim_call.kwargs["Item"]
    assert item["sk"] == "CLAIM#+12125551111"
    assert "phone" not in item
    assert isinstance(item["claimedAtEpoch"], int)
    assert claim_call.kwargs["ConditionExpression"] == (
        "attribute_not_exists(sk) OR claimedAtEpoch < :stale_before"
    )
    stale_before = claim_call.kwargs["ExpressionAttributeValues"][":stale_before"]
    assert stale_before == item["claimedAtEpoch"] - handler._CLAIM_STALE_SECONDS


def test_process_recipients_reclaims_stale_claim_and_sends():
    """Finding (Important): a claim whose OWN recorded age (claimedAtEpoch)
    exceeds _CLAIM_STALE_SECONDS must be atomically reclaimable. Without this,
    an orphaned claim — left behind by a crashed invocation, or written just
    before a template-rejection `break` aborts the rest of the loop — would
    block ALL future retries for that phone until DynamoDB's TTL sweep
    physically deletes the row, which has no delivery-time guarantee (AWS:
    "typically within 48 hours") and could far outlive any real retry
    cadence."""
    handler = _load_handler()
    queue_table = _make_claim_aware_queue_table()
    stale_epoch = int(time.time()) - handler._CLAIM_STALE_SECONDS - 60
    # Seed a pre-existing, now-stale claim for this phone. ConditionExpression
    # is omitted so the fake's own check is bypassed while seeding state
    # directly — equivalent to an unconditional put_item.
    queue_table.put_item(
        Item={
            "campaignId": "cmp-1",
            "sk": "CLAIM#+12125551111",
            "claimedAt": "irrelevant",
            "claimedAtEpoch": stale_epoch,
            "ttl": stale_epoch + handler._CLAIM_TTL_SECONDS,
        }
    )

    recipients = [{"phone": "+12125551111", "FirstName": "Maria"}]
    mock_sqs = MagicMock()
    mock_sqs.send_message_batch.return_value = {"Failed": []}

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
            now_iso="2026-09-09T00:10:00+00:00",
            ttl=1234567890,
            queue_table=queue_table,
        )

    assert (enqueued, failed, opted_out, outside_qh, rejected) == (1, 0, 0, 0, None)
    mock_sqs.send_message_batch.assert_called_once()


def test_process_recipients_does_not_reclaim_recent_claim():
    """Regression guard: a claim whose recorded age is WITHIN
    _CLAIM_STALE_SECONDS must still block a reclaim attempt exactly like
    18f3ceb's original attribute_not_exists(sk)-only condition did for a
    genuinely active claim — the new staleness OR-clause must never widen who
    can steal a fresh, still-relevant claim."""
    handler = _load_handler()
    queue_table = _make_claim_aware_queue_table()
    recent_epoch = int(time.time()) - 30  # well within _CLAIM_STALE_SECONDS
    queue_table.put_item(
        Item={
            "campaignId": "cmp-1",
            "sk": "CLAIM#+12125551111",
            "claimedAt": "irrelevant",
            "claimedAtEpoch": recent_epoch,
            "ttl": recent_epoch + handler._CLAIM_TTL_SECONDS,
        }
    )

    recipients = [{"phone": "+12125551111", "FirstName": "Maria"}]
    mock_sqs = MagicMock()

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
            now_iso="2026-09-09T00:00:30+00:00",
            ttl=1234567890,
            queue_table=queue_table,
        )

    assert (enqueued, failed, opted_out, outside_qh, rejected) == (0, 0, 0, 0, None)
    mock_sqs.send_message_batch.assert_not_called()


# ── Claim gate: Finding 2 (Important) — SQS_SEND_FAILED must not permanently
# exclude a phone from retry ───────────────────────────────────────────────────


def test_get_already_sent_phones_excludes_sqs_send_failed_only_phone():
    """Part 1: a phone whose only queue item is SQS_SEND_FAILED must NOT be
    treated as already-sent — nothing was ever actually delivered to it."""
    handler = _load_handler()
    queue_table = MagicMock()
    queue_table.query.return_value = {
        "Items": [
            {"phone": "+12125551111", "status": "SQS_SEND_FAILED"},
            {"phone": "+12125552222", "status": "PENDING"},
        ]
    }
    mock_ddb = MagicMock()
    mock_ddb.Table.return_value = queue_table

    with patch.dict(os.environ, _ENV), patch.object(handler, "_ddb", mock_ddb):
        result = handler._get_already_sent_phones("cmp-1")

    assert result == {"+12125552222"}


def test_get_already_sent_phones_still_includes_phone_with_a_later_successful_item():
    """A phone with BOTH a failed item (from one pass) and a non-failed item
    (from a later, successful pass) must still count as already-sent — the
    exclusion is only for phones with NO non-failed item at all."""
    handler = _load_handler()
    queue_table = MagicMock()
    queue_table.query.return_value = {
        "Items": [
            {"phone": "+12125551111", "status": "SQS_SEND_FAILED"},
            {"phone": "+12125551111", "status": "PENDING"},
        ]
    }
    mock_ddb = MagicMock()
    mock_ddb.Table.return_value = queue_table

    with patch.dict(os.environ, _ENV), patch.object(handler, "_ddb", mock_ddb):
        result = handler._get_already_sent_phones("cmp-1")

    assert result == {"+12125551111"}


def test_get_already_sent_phones_ignores_claim_records_with_no_phone_attribute():
    """CLAIM# records (no "phone" attribute) must not crash the scan or pollute
    the returned set — including now that they also carry the claimedAtEpoch
    attribute added by the stale-claim-reclaim fix, which
    _get_already_sent_phones never reads (its ProjectionExpression only ever
    fetches phone + status, so this is also true at the DynamoDB level, not
    just in this mock)."""
    handler = _load_handler()
    queue_table = MagicMock()
    queue_table.query.return_value = {
        "Items": [
            {
                "sk": "CLAIM#+12125551111",
                "claimedAt": "2026-09-09T00:00:00+00:00",
                "claimedAtEpoch": 1234567890,
            },
            {"phone": "+12125552222", "status": "PENDING"},
        ]
    }
    mock_ddb = MagicMock()
    mock_ddb.Table.return_value = queue_table

    with patch.dict(os.environ, _ENV), patch.object(handler, "_ddb", mock_ddb):
        result = handler._get_already_sent_phones("cmp-1")

    assert result == {"+12125552222"}


def test_flush_sms_batch_deletes_claim_for_failed_phone_not_for_sent_phone():
    """Part 2: _flush_sms_batch must release (delete) the CLAIM# record for a
    phone whose entry came back SQS_SEND_FAILED, and must NOT touch the claim
    for a phone whose entry was written PENDING (successfully sent to SQS)."""
    handler = _load_handler()
    queue_table = MagicMock()
    mock_sqs = MagicMock()

    sqs_batch = [
        {"Id": "id-ok", "MessageBody": json.dumps({"phone": "+12125551111"})},
        {"Id": "id-fail", "MessageBody": json.dumps({"phone": "+12125552222"})},
    ]
    ddb_items_by_id = {
        "id-ok": {
            "campaignId": "cmp-1",
            "sk": "2026-09-09T00:00:00+00:00#aaaaaaaa",
            "phone": "+12125551111",
            "status": "PENDING",
            "createdAt": "2026-09-09T00:00:00+00:00",
            "updatedAt": "2026-09-09T00:00:00+00:00",
            "ttl": 1234567890,
        },
        "id-fail": {
            "campaignId": "cmp-1",
            "sk": "2026-09-09T00:00:00+00:00#bbbbbbbb",
            "phone": "+12125552222",
            "status": "PENDING",
            "createdAt": "2026-09-09T00:00:00+00:00",
            "updatedAt": "2026-09-09T00:00:00+00:00",
            "ttl": 1234567890,
        },
    }
    mock_sqs.send_message_batch.return_value = {
        "Failed": [{"Id": "id-fail", "Code": "ThrottlingException"}]
    }

    with patch.object(handler, "_sqs", mock_sqs):
        handler._flush_sms_batch(sqs_batch, ddb_items_by_id, queue_table, "cmp-1")

    delete_calls = [c.kwargs["Key"] for c in queue_table.delete_item.call_args_list]
    assert delete_calls == [{"campaignId": "cmp-1", "sk": "CLAIM#+12125552222"}]


def test_flush_sms_batch_claim_release_failure_logs_warning_not_raises():
    """A delete_item failure while releasing a claim must not crash the batch
    flush — it's a backstop-covered, non-fatal condition (Finding 2's own
    design: worst case is a stale claim that self-heals via TTL)."""
    handler = _load_handler()
    queue_table = MagicMock()
    queue_table.delete_item.side_effect = _conditional_check_failed("boom")
    mock_sqs = MagicMock()

    sqs_batch = [
        {"Id": "id-fail", "MessageBody": json.dumps({"phone": "+12125552222"})}
    ]
    ddb_items_by_id = {
        "id-fail": {
            "campaignId": "cmp-1",
            "sk": "2026-09-09T00:00:00+00:00#bbbbbbbb",
            "phone": "+12125552222",
            "status": "PENDING",
            "createdAt": "2026-09-09T00:00:00+00:00",
            "updatedAt": "2026-09-09T00:00:00+00:00",
            "ttl": 1234567890,
        },
    }
    mock_sqs.send_message_batch.return_value = {
        "Failed": [{"Id": "id-fail", "Code": "ThrottlingException"}]
    }

    with (
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_logger") as mock_logger,
    ):
        # Must not raise.
        enqueued, failed = handler._flush_sms_batch(
            sqs_batch, ddb_items_by_id, queue_table, "cmp-1"
        )

    assert (enqueued, failed) == (0, 1)
    mock_logger.warn.assert_any_call(
        "sms_sender_claim_release_failed",
        campaign_id="cmp-1",
        error="ClientError",
    )


def test_flush_sms_batch_claim_release_catches_non_client_errors_too():
    """The release-cleanup catch is deliberately broader than ClientError —
    ANY failure releasing the claim (network hiccup, etc.) must degrade to a
    logged warning, never a crashed/re-raised batch flush."""
    handler = _load_handler()
    queue_table = MagicMock()
    queue_table.delete_item.side_effect = RuntimeError("network blip")
    mock_sqs = MagicMock()

    sqs_batch = [
        {"Id": "id-fail", "MessageBody": json.dumps({"phone": "+12125552222"})}
    ]
    ddb_items_by_id = {
        "id-fail": {
            "campaignId": "cmp-1",
            "sk": "2026-09-09T00:00:00+00:00#bbbbbbbb",
            "phone": "+12125552222",
            "status": "PENDING",
            "createdAt": "2026-09-09T00:00:00+00:00",
            "updatedAt": "2026-09-09T00:00:00+00:00",
            "ttl": 1234567890,
        },
    }
    mock_sqs.send_message_batch.return_value = {
        "Failed": [{"Id": "id-fail", "Code": "ThrottlingException"}]
    }

    with (
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_logger") as mock_logger,
    ):
        # Must not raise, even for a non-ClientError exception.
        enqueued, failed = handler._flush_sms_batch(
            sqs_batch, ddb_items_by_id, queue_table, "cmp-1"
        )

    assert (enqueued, failed) == (0, 1)
    mock_logger.warn.assert_any_call(
        "sms_sender_claim_release_failed",
        campaign_id="cmp-1",
        error="RuntimeError",
    )


# ── End-to-end: Findings 1 and 2 working together ─────────────────────────────


def test_retry_resends_after_prior_sqs_send_failed_and_claim_release():
    """End-to-end proof that parts 1 and 2 of Finding 2 work together with
    Finding 1's claim gate, not just in isolation: a phone whose earlier
    attempt ended SQS_SEND_FAILED (its claim already released by
    _flush_sms_batch) is NOT excluded by _get_already_sent_phones and has no
    live claim blocking it — so a genuine retry send goes out for it on the
    very next retry_quiet_hours_skipped call."""
    handler = _load_handler()
    record = _retry_record(totalSkippedQuietHours=2)
    ddb, runs_table, queue_table = _mock_ddb_with_runs_record(record)

    # Queue table state reflects the outcome of a PRIOR pass: Maria's send
    # failed and, per Finding 2 part 2, her CLAIM# record was already deleted
    # by that prior _flush_sms_batch call — so no CLAIM# item exists for her
    # here. Jose has no queue item at all (still awaiting his first attempt).
    queue_table.query.return_value = {
        "Items": [{"phone": "+12125551111", "status": "SQS_SEND_FAILED"}]
    }
    queue_table.put_item.return_value = None  # every claim attempt succeeds

    recipients = [
        {"phone": "+12125551111", "FirstName": "Maria"},  # now retry-eligible
        {"phone": "+13105552222", "FirstName": "Jose"},  # window still closed
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
    sent_phones = [
        json.loads(e["MessageBody"])["phone"]
        for call in mock_sqs.send_message_batch.call_args_list
        for e in call.kwargs["Entries"]
    ]
    assert sent_phones == ["+12125551111"]
