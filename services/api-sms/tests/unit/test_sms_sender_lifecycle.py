"""Persisted sender lifecycle regressions across real entry-point invocations."""

from __future__ import annotations

import copy
import importlib
import json
import os
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError
from segment_recipients import SegmentRecipientsError, SegmentRecipientsPending
import pytest

_ENV = {
    "SMS_CAMPAIGN_QUEUE_TABLE": "queue",
    "SMS_CAMPAIGN_RUNS_TABLE": "runs",
    "SMS_SQS_QUEUE_URL": "queue-url",
    "PROFILES_DOMAIN_NAME": "test-domain",
    "OPT_OUT_TABLE": "optout",
}
_PHONE = "+12125551111"
_EVENT = {
    "campaignId": "c1",
    "planId": "p1",
    "runId": "r1",
    "segmentName": "original-segment",
    "segmentArn": "arn:segment",
    "messageTemplate": "Hi {{FirstName}} from {{ClinicName}}!",
    "clinicName": "Original Clinic",
    "originationNumberArn": "arn:original",
}


def _conflict():
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem"
    )


class _Queue:
    def __init__(self):
        self.items = {}

    def put_item(
        self, *, Item, ConditionExpression=None, ExpressionAttributeValues=None
    ):
        key = Item["sk"]
        old = self.items.get(key)
        if ConditionExpression and old:
            if (
                old.get("claimedAtEpoch", float("inf"))
                >= ExpressionAttributeValues[":stale_before"]
            ):
                raise _conflict()
        self.items[key] = copy.deepcopy(Item)

    def delete_item(self, *, Key):
        self.items.pop(Key["sk"], None)

    def batch_writer(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def query(self, **kwargs):
        return {"Items": copy.deepcopy(list(self.items.values()))}


class _Runs:
    def __init__(self):
        self.record = None
        self.reads = []

    def put_item(self, *, Item, ConditionExpression=None):
        if self.record and ConditionExpression:
            raise _conflict()
        self.record = copy.deepcopy(Item)

    def get_item(self, **kwargs):
        self.reads.append(kwargs)
        return {"Item": copy.deepcopy(self.record)} if self.record else {}

    def update_item(self, *, UpdateExpression, ExpressionAttributeValues, **kwargs):
        if ":snapshot" in ExpressionAttributeValues:
            if (
                not self.record
                or self.record.get("status") != "RUNNING"
                or self.record.get("segmentName")
                != ExpressionAttributeValues[":segment"]
                or "recipientSnapshot" in self.record
            ):
                raise _conflict()
            self.record["recipientSnapshot"] = copy.deepcopy(
                ExpressionAttributeValues[":snapshot"]
            )
            return
        # Apply DynamoDB's additive versus replacement counter semantics to
        # persistent state; tests never reseed state between entry-point calls.
        for field, value in {
            "totalEnqueued": ":n",
            "totalSqsSendFailed": ":f",
            "totalSkippedOptOut": ":o",
            "totalSkippedQuietHours": ":q",
        }.items():
            if value not in ExpressionAttributeValues or field not in UpdateExpression:
                continue
            if f"{field} = {value}" in UpdateExpression:
                self.record[field] = ExpressionAttributeValues[value]
            else:
                self.record[field] = (
                    self.record.get(field, 0) + ExpressionAttributeValues[value]
                )


@pytest.fixture
def sender():
    with patch.dict(os.environ, _ENV), patch("boto3.client"), patch("boto3.resource"):
        import sms_sender_handler

        handler = importlib.reload(sms_sender_handler)
    queue, runs, sqs, clock = _Queue(), _Runs(), MagicMock(), [1000]
    ddb = MagicMock()
    ddb.Table.side_effect = lambda name: queue if name == "queue" else runs
    sqs.send_message_batch.return_value = {"Failed": []}
    reader = MagicMock(return_value=[{"phone": _PHONE, "FirstName": "Maria"}])
    with (
        patch.object(handler, "_ddb", ddb),
        patch.object(handler, "_sqs", sqs),
        patch.object(handler, "_load_segment_recipients", reader),
        patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda p: False)),
        patch.object(handler, "_is_within_quiet_hours", lambda p: True),
        patch.object(handler.time, "time", lambda: clock[0]),
    ):
        yield handler, queue, runs, sqs, reader, clock


def _defer_first_send(handler):
    with patch.object(handler, "_is_within_quiet_hours", lambda p: False):
        assert handler.lambda_handler(_EVENT, None)["enqueued"] == 0


def test_orphan_is_reclaimed_after_intermediate_retry_loses_fresh_claim(sender):
    handler, queue, runs, sqs, reader, clock = sender
    _defer_first_send(handler)
    queue.items["CLAIM#" + _PHONE] = {
        "campaignId": "c1",
        "sk": "CLAIM#" + _PHONE,
        "claimedAtEpoch": 950,
        "ttl": 1850,
    }
    handler.retry_quiet_hours_skipped(_EVENT, None)
    assert runs.record["totalSkippedQuietHours"] == 0  # reporting remains truthful
    clock[0] = 2000
    handler.retry_quiet_hours_skipped(_EVENT, None)
    handler.retry_quiet_hours_skipped(_EVENT, None)
    assert sqs.send_message_batch.call_count == 1
    assert runs.record["totalEnqueued"] == 1


def test_final_quiet_hours_recipient_retries_sqs_failure_and_counts_it_once(sender):
    handler, queue, runs, sqs, reader, clock = sender
    _defer_first_send(handler)
    sqs.send_message_batch.side_effect = lambda **kw: {
        "Failed": [
            {"Id": e["Id"], "Code": "ThrottlingException"} for e in kw["Entries"]
        ]
    }
    handler.retry_quiet_hours_skipped(_EVENT, None)
    assert "CLAIM#" + _PHONE not in queue.items
    sqs.send_message_batch.side_effect = None
    handler.retry_quiet_hours_skipped(_EVENT, None)
    handler.retry_quiet_hours_skipped(_EVENT, None)
    assert runs.record["totalEnqueued"] == 1
    assert runs.record["totalSqsSendFailed"] == 1
    assert sqs.send_message_batch.call_count == 2


def test_failed_recipient_read_preserves_run_until_successful_next_tick(sender):
    handler, queue, runs, sqs, reader, clock = sender
    _defer_first_send(handler)
    before = copy.deepcopy(runs.record)
    reader.side_effect = RuntimeError("transient failure")
    with pytest.raises(RuntimeError):
        handler.retry_quiet_hours_skipped(_EVENT, None)
    assert runs.record == before
    reader.side_effect = None
    handler.retry_quiet_hours_skipped(_EVENT, None)
    assert runs.record["totalEnqueued"] == 1


@pytest.mark.parametrize("reason", ["incomplete membership", "incomplete profiles"])
def test_partial_recipient_response_is_not_treated_as_complete(sender, reason):
    handler, queue, runs, sqs, reader, clock = sender
    _defer_first_send(handler)
    before = copy.deepcopy(runs.record)
    reader.side_effect = SegmentRecipientsError(reason)
    with pytest.raises(RuntimeError, match="SMS recipient read failed"):
        handler.retry_quiet_hours_skipped(_EVENT, None)
    assert runs.record == before
    sqs.send_message_batch.assert_not_called()


def test_first_send_reentry_recovers_failed_read_with_original_immutable_metadata(
    sender,
):
    handler, queue, runs, sqs, reader, clock = sender
    reader.side_effect = RuntimeError("read failed")
    with pytest.raises(RuntimeError):
        handler.lambda_handler(_EVENT, None)
    original = copy.deepcopy(runs.record)
    reader.side_effect = None
    changed_event = dict(
        _EVENT,
        segmentName="changed-segment",
        clinicName="Changed Clinic",
        messageTemplate="Changed",
        originationNumberArn="arn:changed",
    )
    handler.lambda_handler(changed_event, None)
    assert runs.record["startedAt"] == original["startedAt"]
    assert runs.record["messageTemplate"] == original["messageTemplate"]
    assert runs.reads[-1].get("ConsistentRead") is True
    assert reader.call_args.kwargs["segment_name"] == "original-segment"
    body = json.loads(
        sqs.send_message_batch.call_args.kwargs["Entries"][0]["MessageBody"]
    )
    assert body["messageTemplate"] == "Hi Maria from Original Clinic!"
    assert body["originationNumberArn"] == "arn:original"
    assert runs.record["totalEnqueued"] == 1


def test_first_send_reentry_does_not_resend_or_erase_processor_counters(sender):
    handler, queue, runs, sqs, reader, clock = sender
    handler.lambda_handler(_EVENT, None)
    runs.record["totalSent"] = 1
    # Historical claim may have expired; the actual message ledger must still
    # prevent duplicate delivery on an explicit entry-point retry.
    queue.items.pop("CLAIM#" + _PHONE)
    handler.lambda_handler(_EVENT, None)
    assert sqs.send_message_batch.call_count == 1
    assert runs.record["totalEnqueued"] == 1
    assert runs.record["totalSent"] == 1


@pytest.mark.parametrize("status", ["COMPLETED", "ABORTED"])
def test_sender_and_retry_reentry_leave_terminal_run_unchanged(sender, status):
    handler, queue, runs, sqs, reader, clock = sender
    _defer_first_send(handler)
    runs.record["status"] = status
    runs.record["exitReason"] = "stopped_by_operator"
    before = copy.deepcopy(runs.record)
    reader.reset_mock()
    result = handler.lambda_handler(_EVENT, None)
    assert result == {
        "terminal": True,
        "exitReason": "stopped_by_operator",
        "enqueued": 0,
        "failed": 0,
    }
    handler.retry_quiet_hours_skipped(_EVENT, None)
    assert runs.record == before
    reader.assert_not_called()
    sqs.send_message_batch.assert_not_called()


def test_retry_reports_new_optout_without_accumulating_repeated_skip_counts(sender):
    handler, queue, runs, sqs, reader, clock = sender
    _defer_first_send(handler)
    with patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda p: True)):
        handler.retry_quiet_hours_skipped(_EVENT, None)
        handler.retry_quiet_hours_skipped(_EVENT, None)
    assert runs.record["totalSkippedOptOut"] == 1
    assert runs.record["totalOptedOut"] == 0
    assert runs.record["totalEnqueued"] == 0


_SNAPSHOT = {
    "snapshotId": "snap-1",
    "destinationUri": "s3://test-snapshots/precall-sms/one/",
    "segmentName": "original-segment",
    "requestedAtEpoch": 1000,
}


def test_pending_snapshot_persists_then_resumes_same_audience_without_advancing_counters(
    sender,
):
    handler, queue, runs, sqs, reader, clock = sender

    def pending(**kwargs):
        assert kwargs["load_snapshot"]() is None
        assert kwargs["publish_snapshot"](_SNAPSHOT) == _SNAPSHOT
        raise SegmentRecipientsPending("snapshot is still being generated")

    reader.side_effect = pending
    result = handler.lambda_handler(_EVENT, None)
    assert result == {"pending": True, "enqueued": 0, "failed": 0}
    assert runs.record["recipientSnapshot"] == _SNAPSHOT
    assert runs.record["totalEnqueued"] == 0
    assert runs.record["totalSkippedQuietHours"] == 0
    sqs.send_message_batch.assert_not_called()

    def completed(**kwargs):
        assert kwargs["load_snapshot"]() == _SNAPSHOT
        return [{"phone": "212-555-1111", "FirstName": "Maria"}]

    reader.side_effect = completed
    assert handler.lambda_handler(_EVENT, None)["enqueued"] == 1
    assert runs.record["recipientSnapshot"] == _SNAPSHOT
    message = json.loads(
        sqs.send_message_batch.call_args.kwargs["Entries"][0]["MessageBody"]
    )
    assert message["phone"] == _PHONE


def test_pending_snapshot_retry_preserves_existing_suppression_counts(sender):
    handler, queue, runs, sqs, reader, clock = sender
    _defer_first_send(handler)
    before = copy.deepcopy(runs.record)
    reader.side_effect = SegmentRecipientsPending("in progress")
    result = handler.retry_quiet_hours_skipped(_EVENT, None)
    assert result == {"pending": True, "retried": 0, "stillSkipped": 1}
    assert runs.record == before
    sqs.send_message_batch.assert_not_called()


def test_concurrent_snapshot_publisher_reuses_first_winner(sender):
    handler, queue, runs, sqs, reader, clock = sender

    def snapshot_race(**kwargs):
        assert kwargs["load_snapshot"]() is None
        assert kwargs["publish_snapshot"](_SNAPSHOT) == _SNAPSHOT
        loser = dict(
            _SNAPSHOT,
            snapshotId="snap-2",
            destinationUri="s3://test-snapshots/precall-sms/two/",
        )
        assert kwargs["publish_snapshot"](loser) == _SNAPSHOT
        return [{"phone": _PHONE, "FirstName": "Maria"}]

    reader.side_effect = snapshot_race
    assert handler.lambda_handler(_EVENT, None)["enqueued"] == 1
    assert runs.record["recipientSnapshot"] == _SNAPSHOT
    assert all(call.get("ConsistentRead") for call in runs.reads)


@pytest.mark.parametrize("status", ["ABORTED", "COMPLETED"])
def test_run_ending_while_snapshot_finishes_never_enqueues(sender, status):
    handler, queue, runs, sqs, reader, clock = sender

    def finish_after_stop(**kwargs):
        runs.record["status"] = status
        return [{"phone": _PHONE, "FirstName": "Maria"}]

    reader.side_effect = finish_after_stop
    assert handler.lambda_handler(_EVENT, None)["enqueued"] == 0
    assert runs.record["status"] == status
    assert runs.record["totalEnqueued"] == 0
    assert not queue.items
    sqs.send_message_batch.assert_not_called()


def test_snapshot_publication_cannot_revive_a_run_aborted_during_export_creation(
    sender,
):
    handler, queue, runs, sqs, reader, clock = sender

    def abort_before_publication(**kwargs):
        runs.record["status"] = "ABORTED"
        kwargs["publish_snapshot"](_SNAPSHOT)
        raise AssertionError("publication must reject the terminal run")

    reader.side_effect = abort_before_publication
    assert handler.lambda_handler(_EVENT, None)["enqueued"] == 0
    assert runs.record["status"] == "ABORTED"
    assert "recipientSnapshot" not in runs.record
    assert not queue.items
    sqs.send_message_batch.assert_not_called()


def test_real_snapshot_reader_resumes_across_sender_calls_and_sends_once(sender):
    import io
    from segment_recipients import load_segment_recipients

    handler, queue, runs, sqs, reader, clock = sender
    cp, s3 = MagicMock(), MagicMock()
    cp.get_segment_definition.return_value = {
        "SegmentDefinitionName": "original-segment"
    }
    destination = []

    def create_snapshot(**kwargs):
        destination.append(kwargs["DestinationUri"])
        return {"SnapshotId": "snapshot-1"}

    statuses = iter(["IN_PROGRESS", "COMPLETED", "COMPLETED"])
    cp.create_segment_snapshot.side_effect = create_snapshot
    cp.get_segment_snapshot.side_effect = lambda **kwargs: {
        "SnapshotId": "snapshot-1",
        "DestinationUri": destination[0],
        "Status": next(statuses),
        "DataFormat": "CSV",
    }
    cp.batch_get_profile.return_value = {
        "Profiles": [
            {"ProfileId": "a" * 32, "PhoneNumber": _PHONE, "FirstName": "Maria"}
        ]
    }
    s3.get_paginator.return_value.paginate.side_effect = lambda **kwargs: [
        {"Contents": [{"Key": kwargs["Prefix"] + "part.csv"}]}
    ]
    s3.get_object.side_effect = lambda **kwargs: {
        "Body": io.BytesIO(("ProfileId\n" + "a" * 32 + "\n").encode())
    }
    with (
        patch.object(handler, "_load_segment_recipients", load_segment_recipients),
        patch.object(handler, "_cp", cp),
        patch.object(handler, "_s3", s3),
        patch.object(handler, "_SNAPSHOT_BUCKET", "test-snapshots"),
        patch.object(handler, "_SNAPSHOT_ROLE_ARN", "arn:snapshot-role"),
        patch.object(handler, "_SNAPSHOT_KEY_ARN", "arn:snapshot-key"),
    ):
        assert handler.lambda_handler(_EVENT, None)["pending"] is True
        assert handler.lambda_handler(_EVENT, None)["enqueued"] == 1
        assert handler.lambda_handler(_EVENT, None)["enqueued"] == 0

    cp.create_segment_snapshot.assert_called_once()
    cp.get_segment_definition.assert_called_once()
    assert runs.record["recipientSnapshot"]["destinationUri"] == destination[0]
    assert runs.record["totalEnqueued"] == 1
    sqs.send_message_batch.assert_called_once()


def test_real_reader_preserves_terminal_cancellation_from_snapshot_callback(sender):
    from segment_recipients import load_segment_recipients

    handler, queue, runs, sqs, reader, clock = sender
    cp = MagicMock()
    cp.get_segment_definition.return_value = {
        "SegmentDefinitionName": "original-segment"
    }

    def abort_during_create(**kwargs):
        runs.record["status"] = "ABORTED"
        runs.record["exitReason"] = "stopped_by_operator"
        return {"SnapshotId": "snapshot-1"}

    cp.create_segment_snapshot.side_effect = abort_during_create
    with (
        patch.object(handler, "_load_segment_recipients", load_segment_recipients),
        patch.object(handler, "_cp", cp),
        patch.object(handler, "_SNAPSHOT_BUCKET", "test-snapshots"),
        patch.object(handler, "_SNAPSHOT_ROLE_ARN", "arn:snapshot-role"),
        patch.object(handler, "_SNAPSHOT_KEY_ARN", "arn:snapshot-key"),
    ):
        result = handler.lambda_handler(_EVENT, None)
    assert result == {
        "terminal": True,
        "exitReason": "stopped_by_operator",
        "enqueued": 0,
        "failed": 0,
    }
    assert "recipientSnapshot" not in runs.record
    sqs.send_message_batch.assert_not_called()
