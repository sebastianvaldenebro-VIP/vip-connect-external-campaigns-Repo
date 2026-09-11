"""Cross-Lambda contract tests for asynchronous SMS segment initialization."""

from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import executor  # noqa: E402

NOW = datetime(2026, 9, 11, 15, tzinfo=timezone.utc)


def model(delivery="campaign", **state):
    cfg = {"precallSms": {"enabled": True, "messageTemplate": "Hi", "originationNumberArn": "arn/phone"},
           "smsMessageTemplate": "Hi", "smsOriginationNumberArn": "arn/phone",
           "queueArn": "arn/queue", "contactFlowId": "flow"}
    campaign = {"id": "c", "deliveryType": delivery, "pinnedSegmentArn": "arn/segment", "campaignConfig": cfg}
    bucket = {"id": "b", "campaigns": [campaign], "run_mode": "status_based"}
    plan = {"planId": "p", "buckets": [bucket]}
    cs = {"campaignId": "c", "status": "running", "segmentArn": "arn/segment", "segmentName": "segment",
          "connectCampaignId": "connect", "precallGatePausedAt": "pause", "startedAt": NOW.isoformat()}
    cs.update(state)
    run = {"planId": "p", "runId": "r", "status": "running", "_version": 0, "planSnapshot": plan,
           "bucketStates": [{"status": "running", "campaignStates": [cs], "startedAt": NOW.isoformat()}]}
    return run, plan, cs


def response(payload, **metadata):
    return {"StatusCode": 200, "Payload": BytesIO(json.dumps(payload).encode()), **metadata}


@pytest.fixture
def environment(monkeypatch):
    monkeypatch.setenv("SMS_SENDER_FUNCTION_ARN", "arn/sender")
    with ExitStack() as stack:
        client = stack.enter_context(patch.object(executor, "_get_lambda_client")).return_value
        oc = MagicMock()
        stack.enter_context(patch.dict("sys.modules", {
            "vip_shared.infrastructure.persistence.outbound_campaigns_client": MagicMock(build=MagicMock(return_value=oc))
        }))
        stack.enter_context(patch.object(executor, "_now_utc", return_value=NOW))
        stack.enter_context(patch.object(executor, "_now_iso", return_value=NOW.isoformat()))
        stack.enter_context(patch.object(executor, "_now_cot_hhmm", return_value=600))
        stack.enter_context(patch.object(executor, "_past_daily_cutoff", return_value=False))
        stack.enter_context(patch.object(executor, "save_run"))
        stack.enter_context(patch.object(executor, "_get_ddb_client"))
        stack.enter_context(patch.object(executor.boto3, "resource"))
        stack.enter_context(patch.object(executor, "_ACTIVE_BRANDED_CAMPAIGNS_TABLE", "active"))
        stack.enter_context(patch.object(executor, "_CAMPAIGN_QUEUE_TABLE_BRANDED", "queue"))
        stack.enter_context(patch.object(executor, "_write_branded_run_start"))
        yield client, oc


def tick_patches(stack, run):
    stack.enter_context(patch.object(executor, "get_run", return_value=run))
    stack.enter_context(patch.object(executor, "_dispatch_ready_campaigns", return_value=False))
    stack.enter_context(patch.object(executor, "_dispatch_cross_bucket_ready", return_value=False))
    stack.enter_context(patch.object(executor, "_all_campaigns_terminal", return_value=False))
    stack.enter_context(patch.object(executor, "_get_campaign_state", return_value="Paused"))
    stack.enter_context(patch.object(executor, "_fire_campaign_chains"))
    stack.enter_context(patch.object(executor, "_prestart_chained_runs"))


def test_lambda_payload_pending_is_returned(environment):
    client, _ = environment
    client.invoke.return_value = response({"pending": True, "enqueued": 0, "failed": 0})
    assert executor._invoke_sms_sender(campaignId="c")["pending"] is True


@pytest.mark.parametrize("payload", [[], None, {"pending": "yes"}, {"enqueued": "zero"}])
def test_invalid_lambda_payload_is_failure(environment, payload):
    client, _ = environment
    client.invoke.return_value = response(payload)
    with pytest.raises(RuntimeError):
        executor._invoke_sms_sender(campaignId="c")


def test_connect_pending_then_ready_releases_gate_only_after_ready(environment):
    client, oc = environment
    run, plan, cs = model()
    client.invoke.side_effect = [response({"pending": True, "enqueued": 0, "failed": 0}),
                                 response({"enqueued": 1, "failed": 0})]
    executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
    assert not cs.get("precallSmsSentAt")
    oc.resume_campaign.assert_not_called()
    with ExitStack() as stack:
        tick_patches(stack, run)
        executor.tick("p", "r", 0)
    assert cs["precallSmsSentAt"]
    oc.resume_campaign.assert_called_once_with("connect")


def test_connect_pending_timeout_fails_open_without_another_sender_call(environment):
    client, oc = environment
    run, plan, cs = model()
    client.invoke.return_value = response({"pending": True, "enqueued": 0, "failed": 0})
    executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
    cs["precallSmsPendingAt"] = (NOW - timedelta(minutes=5)).isoformat()
    executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
    assert client.invoke.call_count == 1
    assert cs["precallSmsState"] == "failed"
    assert not cs.get("precallSmsSentAt")
    oc.resume_campaign.assert_called_once()


def test_branded_wait_reuses_segment_and_defers_registration_and_seeding(environment):
    client, _ = environment
    run, plan, cs = model("branded", status="queued", connectCampaignId=None)
    plan["buckets"][0]["campaigns"][0].pop("pinnedSegmentArn")
    cs.pop("segmentArn")
    cs.pop("segmentName")
    client.invoke.side_effect = [response({"pending": True, "enqueued": 0, "failed": 0}),
                                 response({"enqueued": 1, "failed": 0})]
    with patch.object(executor, "_create_segment", return_value=("segment", "arn/segment", 1, 1)) as create, \
         patch.object(executor, "_invoke_seeder", return_value=1) as seed:
        executor._start_one_campaign(run, plan, 0, 0)
        seed.assert_not_called()
        executor._get_ddb_client.return_value.put_item.assert_not_called()
        saved = deepcopy(run)
        with ExitStack() as stack:
            tick_patches(stack, saved)
            stack.enter_context(patch.object(executor, "_count_branded_queue", side_effect=AssertionError("not seeded yet")))
            executor.tick("p", "r", 0)
        create.assert_called_once()
        seed.assert_called_once()
        assert seed.call_args.kwargs["segment_name"] == "segment"
        requests = [json.loads(call.kwargs["Payload"]) for call in client.invoke.call_args_list]
        assert {r["segmentName"] for r in requests} == {"segment"}


def test_bulk_pending_does_not_complete_empty_queue(environment):
    client, _ = environment
    run, plan, cs = model("sms", status="queued", connectCampaignId=None)
    client.invoke.side_effect = [response({"pending": True, "enqueued": 0, "failed": 0}),
                                 response({"pending": True, "enqueued": 0, "failed": 0})]
    executor._start_one_campaign(run, plan, 0, 0)
    with ExitStack() as stack:
        tick_patches(stack, run)
        count = stack.enter_context(patch.object(executor, "_count_sms_queue", return_value=0))
        executor.tick("p", "r", 0)
    count.assert_not_called()
    assert cs["status"] == "running"
    assert client.invoke.call_count == 2


@pytest.mark.parametrize("delivery", ["campaign", "branded"])
def test_pending_function_error_fails_open_for_voice(environment, delivery):
    client, oc = environment
    run, plan, cs = model(delivery, connectCampaignId="connect" if delivery == "campaign" else None)
    client.invoke.side_effect = [response({"pending": True, "enqueued": 0, "failed": 0}),
                                 response({"errorMessage": "failed"}, FunctionError="Unhandled")]
    with patch.object(executor, "_invoke_seeder", return_value=1) as seed:
        if delivery == "campaign":
            executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
            executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
            oc.resume_campaign.assert_called_once()
        else:
            executor._start_one_campaign(run, plan, 0, 0)
            seed.assert_not_called()
            executor._start_one_campaign(run, plan, 0, 0)
            seed.assert_called_once()
    assert cs["precallSmsState"] == "failed"
    assert not cs.get("precallSmsSentAt")


def test_bulk_pending_deadline_is_error_without_poll_or_send(environment):
    client, _ = environment
    run, plan, cs = model("sms", connectCampaignId=None)
    client.invoke.return_value = response({"pending": True, "enqueued": 0, "failed": 0})
    executor._start_one_campaign(run, plan, 0, 0)
    cs["smsInitializationPendingAt"] = (NOW - timedelta(minutes=5)).isoformat()
    with ExitStack() as stack:
        tick_patches(stack, run)
        count = stack.enter_context(patch.object(executor, "_count_sms_queue"))
        executor.tick("p", "r", 0)
    count.assert_not_called()
    assert client.invoke.call_count == 1
    assert cs["status"] == "error"
    assert cs["exitReason"] == "sms_initialization_timeout"


def test_aborted_sender_does_not_resume_or_seed(environment):
    client, oc = environment
    client.invoke.side_effect = [response({"terminal": True, "exitReason": "aborted", "enqueued": 0}) for _ in range(2)]
    with patch.object(executor, "_safe_stop_campaign") as stop, patch.object(executor, "_invoke_seeder") as seed:
        run, plan, cs = model()
        executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
        assert cs["status"] == "cancelled"
        stop.assert_called_once_with("connect")
        run, plan, cs = model("branded", connectCampaignId=None)
        executor._start_one_campaign(run, plan, 0, 0)
        assert cs["status"] == "cancelled"
        seed.assert_not_called()
    oc.resume_campaign.assert_not_called()


def test_warming_unstarted_campaign_waits_before_start(environment):
    client, oc = environment
    run, plan, cs = model(status="warming", warmupStarted=False, precallGatePausedAt=None)
    client.invoke.side_effect = [response({"pending": True, "enqueued": 0}), response({"enqueued": 1})]
    with ExitStack() as stack:
        tick_patches(stack, run)
        stack.enter_context(patch.object(executor, "_schedule_tick", return_value="schedule"))
        stack.enter_context(patch.object(executor, "_record_plan_event"))
        stack.enter_context(patch.object(executor, "_check_native_queue_collision"))
        executor._activate_warming_bucket(run, plan, 0)
        oc.start_campaign.assert_not_called()
        assert cs["status"] == "warming"
        executor.tick("p", "r", 0)
    oc.start_campaign.assert_called_once_with("connect")
    oc.resume_campaign.assert_called_once_with("connect")
    assert cs["status"] == "running"


def test_expired_pending_branded_never_seeds(environment):
    client, _ = environment
    run, plan, cs = model("branded", connectCampaignId=None)
    client.invoke.return_value = response({"pending": True, "enqueued": 0})
    with patch.object(executor, "_invoke_seeder") as seed, patch.object(executor, "_stop_branded_campaign"):
        executor._start_one_campaign(run, plan, 0, 0)
        plan["buckets"][0]["campaigns"][0]["duration_minutes"] = 1
        cs["startedAt"] = (NOW - timedelta(minutes=2)).isoformat()
        with ExitStack() as stack:
            tick_patches(stack, run)
            executor.tick("p", "r", 0)
        seed.assert_not_called()
    assert cs["status"] == "expired"
    assert client.invoke.call_count == 1


def test_branded_preparation_conflict_precedes_any_sms_or_registration(environment):
    client, _ = environment
    run, plan, _ = model("branded", connectCampaignId=None)
    with patch.object(executor, "save_run", side_effect=executor.ConcurrentWriteError("different segment won")):
        with pytest.raises(executor.ConcurrentWriteError):
            executor._start_one_campaign(run, plan, 0, 0)
    client.invoke.assert_not_called()
    executor._get_ddb_client.return_value.put_item.assert_not_called()


def test_settled_initialization_is_not_resurrected_by_conflict_merge():
    for state in ("failed", "complete", "aborted"):
        current = {"precallSmsState": state, "brandedPrecallPreparing": False}
        old = {"precallSmsState": "pending", "precallSmsPendingAt": NOW.isoformat(), "brandedPrecallPreparing": True}
        merged = executor._merge_sms_initialization_state(current, old)
        assert merged["precallSmsState"] == state
        assert not merged.get("precallSmsPendingAt")
        assert merged["brandedPrecallPreparing"] is False


def test_force_restart_clears_preparation_and_aborts_previous_sms(environment):
    run, _, cs = model(status="cancelled", precallSmsGeneration=3,
                       precallSmsCampaignId="old-sms", _precallSmsRunsPlanId="p", _precallSmsRunsSk="r#old-sms",
                       precallSmsState="pending", precallSmsPendingAt=NOW.isoformat(),
                       precallSmsSegmentArn="arn/old", precallSmsSegmentName="old", brandedPrecallPreparing=True)
    with patch.object(executor, "get_run", return_value=run), \
         patch.object(executor, "_reset_cascade_cancelled_children"), \
         patch.object(executor, "_safe_stop_campaign"), patch.object(executor, "_safe_delete_campaign"), \
         patch.object(executor, "_stop_sms_campaign") as abort, \
         patch.object(executor, "_start_one_campaign") as start:
        executor.force_start_campaign("p", "r", 0, 0)
    start.assert_called_once()
    assert cs["precallSmsGeneration"] == 4
    for field in executor._SMS_INITIALIZATION_FIELDS:
        assert field not in cs
    assert abort.call_args.args[0]["smsCampaignId"] == "old-sms"


@pytest.mark.parametrize("generation", [0, 3])
def test_abort_midflight_image_derives_sms_tombstone_before_identity_was_saved(environment, generation):
    run, _, cs = model(status="creating", precallSmsGeneration=generation)
    expected = executor._precall_sms_campaign_id(run, 0, 0)
    assert "precallSmsCampaignId" not in cs
    with patch.object(executor, "_stop_sms_campaign") as abort:
        executor._abort_precall_sms(cs, "aborted", run=run)
    item = abort.call_args.args[0]
    assert item["smsCampaignId"] == expected
    assert item["_smsRunsSk"] == f"r#{expected}"
    assert cs["precallSmsState"] == "aborted"


def test_two_branded_continuations_still_seed_only_once(environment):
    client, _ = environment
    run, plan, _ = model("branded", connectCampaignId=None)
    client.invoke.side_effect = [response({"pending": True, "enqueued": 0}),
                                 response({"enqueued": 1}), response({"enqueued": 0})]
    executor._start_one_campaign(run, plan, 0, 0)
    other = deepcopy(run)
    executor._get_ddb_client.return_value.put_item.side_effect = [
        {}, ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
    ]
    with patch.object(executor, "_invoke_seeder", return_value=1) as seed:
        executor._start_one_campaign(run, plan, 0, 0)
        executor._start_one_campaign(other, plan, 0, 0)
    seed.assert_called_once()
    assert executor._get_ddb_client.return_value.put_item.call_count == 2


def test_first_function_error_aborts_initialization_before_fail_open(environment):
    client, oc = environment
    run, plan, cs = model()
    client.invoke.return_value = response({"errorMessage": "failed"}, FunctionError="Unhandled")
    with patch.object(executor, "_stop_sms_campaign") as abort:
        executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
    abort.assert_called_once()
    item = abort.call_args.args[0]
    assert item["smsCampaignId"] == executor._precall_sms_campaign_id(run, 0, 0)
    assert item["exitReason"] == "sms_initialization_failed"
    assert cs["precallSmsState"] == "failed"
    assert not cs.get("precallSmsSentAt")
    oc.resume_campaign.assert_called_once_with("connect")
