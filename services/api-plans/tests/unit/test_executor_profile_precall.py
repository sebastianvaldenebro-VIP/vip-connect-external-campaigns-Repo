"""Profile pre-call ordering at the real Lambda payload/Connect call boundary."""

from contextlib import ExitStack
from copy import deepcopy
from datetime import timedelta
import json
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

from .test_executor_sms_pending import (
    NOW, environment as _shared_environment, executor, model, response, tick_patches,
)

@pytest.fixture
def environment(monkeypatch):
    # Existing lifecycle tests isolate the durable reaper's persistence. Its
    # real provider contract is exercised in the cleanup/integration tests.
    with patch("profile_voice_cleanup.register_start", return_value={
        "workerExpiresAt": int(NOW.timestamp()) + 360,
    }), patch("profile_voice_cleanup.check_start_window"):
        yield from _shared_environment.__wrapped__(monkeypatch)


def profile_model(delivery="campaign", **state):
    run, plan, cs = model(delivery, precallGatePausedAt=None, **state)
    plan["buckets"][0]["campaigns"][0]["campaignConfig"]["precallSms"] = {
        "enabled": True, "mode": "profile", "catalogVersion": "phase1-v1",
        "originationNumberArn": "arn/phone",
    }
    return run, plan, cs


def provider_result(*, enqueued=2, sent=0, failed=0, opted_out=0, initialized=True):
    return response({
        "enqueued": enqueued, "failed": 0,
        "pending": not initialized or sent + failed + opted_out < enqueued,
        "initializationComplete": initialized,
        "totalEnqueued": enqueued, "totalSent": sent,
        "totalFailed": failed, "totalOptedOut": opted_out,
    })


@pytest.mark.parametrize("mode", [None, "manual"])
def test_manual_keeps_enqueue_completion_and_original_payload(environment, mode):
    client, oc = environment
    run, plan, cs = model()
    if mode:
        plan["buckets"][0]["campaigns"][0]["campaignConfig"]["precallSms"]["mode"] = mode
    client.invoke.return_value = response({"enqueued": 1, "failed": 0})
    executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
    assert "precallPolicy" not in json.loads(client.invoke.call_args.kwargs["Payload"])
    assert cs["precallSmsSentAt"]
    oc.resume_campaign.assert_called_once_with("connect")
    oc.start_campaign.assert_not_called()


@pytest.mark.parametrize("delivery", ["campaign", "journey", "branded"])
@pytest.mark.parametrize("clinic,expected_clinic", [
    (None, None), ("", None), (" \t\n ", None),
    ("  Example Clinic  ", "Example Clinic"),
    ("Cli\u0301nica del Valle", "Clínica del Valle"),
])
def test_profile_waits_for_provider_acceptance_before_voice(environment, delivery, clinic, expected_clinic):
    client, oc = environment
    run, plan, cs = profile_model(delivery, status="queued",
                                  connectCampaignId=None if delivery == "branded" else "connect")
    if clinic is not None:
        plan["buckets"][0]["campaigns"][0]["campaignConfig"]["precallSms"]["clinicName"] = clinic
    client.invoke.side_effect = [provider_result(), provider_result(sent=2)]
    with patch.object(executor, "_check_native_queue_collision"), \
         patch.object(executor, "_get_campaign_state", return_value="Initialized"), \
         patch.object(executor, "_invoke_seeder", return_value=2) as seed:
        executor._start_one_campaign(run, plan, 0, 0)
        assert cs["precallSmsState"] == "pending"
        assert not cs.get("precallSmsSentAt")
        oc.start_campaign.assert_not_called()
        oc.pause_campaign.assert_not_called()
        seed.assert_not_called()
        with ExitStack() as stack:
            tick_patches(stack, run)
            stack.enter_context(patch.object(executor, "_get_campaign_state", return_value="Initialized"))
            executor.tick("p", "r", 0)
        assert cs["precallSmsState"] == "complete"
        assert cs["precallSmsSentAt"]
        assert cs["precallSmsAcceptedCount"] == 2
        if delivery == "branded":
            seed.assert_called_once()
        else:
            oc.start_campaign.assert_called_once_with("connect")
        oc.pause_campaign.assert_not_called()
        oc.resume_campaign.assert_not_called()
    expected_policy = {"mode": "profile", "catalogVersion": "phase1-v1"}
    if expected_clinic is not None:
        expected_policy["clinicName"] = expected_clinic
    for call in client.invoke.call_args_list:
        payload = json.loads(call.kwargs["Payload"])
        assert payload["precallPolicy"] == expected_policy
        assert "messageTemplate" not in payload and "clinicName" not in payload


@pytest.mark.parametrize("kind", ["empty", "failed", "partial", "function_error"])
def test_profile_unsent_or_failed_sms_never_claims_sent_and_voice_continues(environment, kind):
    client, oc = environment
    run, plan, cs = profile_model(status="queued")
    client.invoke.return_value = {
        "empty": provider_result(enqueued=0),
        "failed": provider_result(failed=2),
        "partial": provider_result(sent=1, failed=1),
        "function_error": response({"errorMessage": "private provider detail"}, FunctionError="Unhandled"),
    }[kind]
    with patch.object(executor, "_check_native_queue_collision"), \
         patch.object(executor, "_get_campaign_state", return_value="Initialized"):
        executor._start_one_campaign(run, plan, 0, 0)
    assert cs["precallSmsState"] == ("skipped" if kind == "empty" else "failed")
    assert not cs.get("precallSmsSentAt")
    assert cs["precallSmsSettledAt"]
    oc.start_campaign.assert_called_once_with("connect")
    assert "private provider detail" not in str(cs)


def test_profile_deadline_aborts_outstanding_sms_and_starts_voice(environment):
    client, oc = environment
    run, plan, cs = profile_model(status="queued")
    client.invoke.return_value = provider_result(initialized=False)
    with patch.object(executor, "_check_native_queue_collision"), \
         patch.object(executor, "_get_campaign_state", return_value="Initialized"):
        executor._start_one_campaign(run, plan, 0, 0)
        assert cs["precallSmsPendingAt"] == NOW.isoformat()
        cs["precallSmsPendingAt"] = (NOW - timedelta(minutes=5)).isoformat()
        executor._start_one_campaign(run, plan, 0, 0)
    assert client.invoke.call_count == 1
    assert cs["precallSmsState"] == "failed"
    assert not cs.get("precallSmsSentAt")
    assert cs["precallSmsFailureReason"] == "sms_initialization_timeout"
    oc.start_campaign.assert_called_once_with("connect")


@pytest.mark.parametrize("delivery", ["campaign", "journey", "branded"])
def test_profile_terminal_cancellation_never_starts_voice(environment, delivery):
    client, oc = environment
    run, plan, cs = profile_model(delivery, status="queued",
                                  connectCampaignId=None if delivery == "branded" else "connect")
    client.invoke.return_value = response({"terminal": True, "exitReason": "aborted", "enqueued": 0})
    with patch.object(executor, "_safe_stop_campaign"), \
         patch.object(executor, "_check_native_queue_collision"), \
         patch.object(executor, "_invoke_seeder") as seed:
        executor._start_one_campaign(run, plan, 0, 0)
    assert cs["status"] == "cancelled"
    oc.start_campaign.assert_not_called()
    oc.resume_campaign.assert_not_called()
    seed.assert_not_called()


@pytest.mark.parametrize("delivery", ["campaign", "journey"])
def test_profile_warmup_only_creates_unstarted_campaign(environment, delivery):
    _, oc = environment
    run, plan, _ = profile_model(delivery)
    oc.create_campaign.return_value = {"id": "new-connect"}
    with patch.object(executor, "_account_id", return_value="account"), \
         patch.object(executor, "resolve_campaign_flow_arn", return_value="arn/flow"), \
         patch.object(executor, "resolve_journey_flow_arn", return_value="arn/flow"), \
         patch.object(executor, "build_campaign_params", return_value={"connectCampaignFlowArn": "arn/flow"}):
        result = executor._create_campaign_only(plan["buckets"][0], plan["buckets"][0]["campaigns"][0], run)
    assert result[0] == "new-connect"
    assert result[3] is False
    oc.start_campaign.assert_not_called()
    oc.pause_campaign.assert_not_called()


def test_profile_start_recovery_adopts_running_without_duplicate_start(environment):
    client, oc = environment
    run, plan, cs = profile_model(status="creating", precallStartDeferred=True,
                                  precallSmsState="complete", precallSmsSentAt=NOW.isoformat())
    with patch.object(executor, "_get_campaign_state", return_value="Running"):
        executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
        restored = deepcopy(run)
        executor._fire_precall_sms_for_campaign(restored, plan, 0, 0)
    oc.start_campaign.assert_not_called()
    client.invoke.assert_not_called()
    assert restored["bucketStates"][0]["campaignStates"][0]["precallVoiceStartedAt"]


def test_profile_cancellation_save_conflict_prevents_start(environment):
    client, oc = environment
    run, plan, cs = profile_model(precallStartDeferred=True,
                                  precallSmsState="complete", precallSmsSentAt=NOW.isoformat())
    with patch.object(executor, "_get_campaign_state", return_value="Initialized"), \
         patch.object(executor, "save_run", side_effect=executor.ConcurrentWriteError("stop won")):
        executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
    client.invoke.assert_not_called()
    oc.start_campaign.assert_not_called()
    assert cs["precallStartDeferred"]


def creation_patches(stack):
    stack.enter_context(patch.object(executor, "_check_native_queue_collision"))
    stack.enter_context(patch.object(executor, "_account_id", return_value="account"))
    stack.enter_context(patch.object(executor, "resolve_campaign_flow_arn", return_value="arn/flow"))
    stack.enter_context(patch.object(executor, "resolve_journey_flow_arn", return_value="arn/flow"))
    stack.enter_context(patch.object(executor, "build_campaign_params", return_value={"connectCampaignFlowArn": "arn/flow"}))
    stack.enter_context(patch.object(executor, "_record_plan_event"))
    stack.enter_context(patch.object(executor, "_get_campaign_state", return_value="Initialized"))


@pytest.mark.parametrize("delivery", ["campaign", "journey"])
def test_dependent_profile_creates_cold_only_after_parent_then_waits_for_acceptance(environment, delivery):
    client, oc = environment
    run, plan, cs = profile_model(delivery, status="queued", connectCampaignId=None)
    plan["buckets"][0]["campaigns"][0]["dependsOn"] = ["parent"]
    plan["buckets"][0]["campaigns"].append({"id": "parent"})
    parent = {"campaignId": "parent", "status": "running"}
    run["bucketStates"][0]["campaignStates"].append(parent)
    oc.create_campaign.return_value = {"id": "new-connect"}
    client.invoke.side_effect = [provider_result(), provider_result(sent=2)]
    with ExitStack() as stack:
        creation_patches(stack)
        assert executor._dispatch_ready_campaigns(run, plan, 0) is False
        oc.create_campaign.assert_not_called()
        parent["status"] = "completed"
        assert executor._dispatch_ready_campaigns(run, plan, 0)
        oc.create_campaign.assert_called_once()
        oc.start_campaign.assert_not_called()
        assert cs["status"] == "running"
        assert cs["precallStartDeferred"]
        tick_patches(stack, run)
        stack.enter_context(patch.object(executor, "_get_campaign_state", return_value="Initialized"))
        executor.tick("p", "r", 0)
    oc.start_campaign.assert_called_once_with("new-connect")
    oc.pause_campaign.assert_not_called()
    assert cs["precallSmsAcceptedCount"] == 2
    payload = json.loads(client.invoke.call_args.kwargs["Payload"])
    assert "messageTemplate" not in payload and "clinicName" not in payload


@pytest.mark.parametrize("mode", [None, "manual", "disabled_profile"])
def test_existing_cold_start_modes_still_start_during_creation(environment, mode):
    client, oc = environment
    run, plan, cs = model(status="queued", connectCampaignId=None)
    precall = plan["buckets"][0]["campaigns"][0]["campaignConfig"]["precallSms"]
    if mode == "disabled_profile":
        precall.update(enabled=False, mode="profile", catalogVersion="phase1-v1")
    elif mode:
        precall["mode"] = mode
    oc.create_campaign.return_value = {"id": "new-connect"}
    client.invoke.return_value = response({"enqueued": 2})
    with ExitStack() as stack:
        creation_patches(stack)
        executor._start_one_campaign(run, plan, 0, 0)
    oc.start_campaign.assert_called_once_with("new-connect")
    if mode == "disabled_profile":
        client.invoke.assert_not_called()
        oc.pause_campaign.assert_not_called()
    else:
        oc.pause_campaign.assert_called_once_with("new-connect")
        oc.resume_campaign.assert_called_once_with("new-connect")
        assert "precallPolicy" not in json.loads(client.invoke.call_args.kwargs["Payload"])
    assert not cs.get("precallStartDeferred")


def test_warm_activation_never_bypasses_profile_gate(environment):
    client, oc = environment
    run, plan, cs = profile_model(status="warming", warmupStarted=False)
    run["bucketStates"][0]["status"] = "warming"
    client.invoke.side_effect = [provider_result(), provider_result(sent=2)]
    with patch.object(executor, "_record_plan_event"), \
         patch.object(executor, "_schedule_tick", return_value="tick"), \
         patch.object(executor, "_dispatch_ready_campaigns", return_value=False), \
         patch.object(executor, "_get_campaign_state", return_value="Initialized"):
        executor._activate_warming_bucket(run, plan, 0)
        oc.start_campaign.assert_not_called()
        assert cs["status"] == "warming"
        executor._continue_pending_campaign(run, plan, 0, 0)
    oc.start_campaign.assert_called_once_with("connect")
    assert cs["status"] == "running"
    assert not cs["precallStartDeferred"]


@pytest.mark.parametrize("payload", [
    {"enqueued": 1},
    {"enqueued": 1, "initializationComplete": "yes", "totalEnqueued": 1, "totalSent": 1, "totalFailed": 0, "totalOptedOut": 0},
    {"enqueued": 1, "initializationComplete": True, "totalEnqueued": 1, "totalSent": -1, "totalFailed": 0, "totalOptedOut": 0},
    {"enqueued": 1, "initializationComplete": True, "totalEnqueued": 1, "totalSent": True, "totalFailed": 0, "totalOptedOut": 0},
])
def test_profile_rejects_missing_or_invalid_provider_aggregates(environment, payload):
    client, oc = environment
    run, plan, cs = profile_model()
    client.invoke.return_value = response(payload)
    with patch.object(executor, "_get_campaign_state", return_value="Initialized"):
        executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
    assert cs["precallSmsState"] == "failed"
    assert cs["precallSmsFailureReason"] == "sms_initialization_failed"
    assert not cs.get("precallSmsSentAt")
    oc.start_campaign.assert_called_once()


@pytest.mark.parametrize("initialized,sent", [(False, 2), (True, 1)])
def test_inconsistent_pending_false_cannot_bypass_aggregate_gate(environment, initialized, sent):
    client, oc = environment
    run, plan, cs = profile_model()
    client.invoke.return_value = response({"enqueued": 2, "pending": False,
        "initializationComplete": initialized, "totalEnqueued": 2, "totalSent": sent,
        "totalFailed": 0, "totalOptedOut": 0})
    executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
    assert cs["precallSmsState"] == "pending"
    oc.start_campaign.assert_not_called()


@pytest.mark.parametrize("exception", [executor.ConcurrentWriteError("stop won"), RuntimeError("DDB down")])
def test_failed_initial_save_performs_no_sms_or_voice_side_effect(environment, exception):
    client, oc = environment
    run, plan, cs = profile_model()
    with patch.object(executor, "save_run", side_effect=exception):
        executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
    client.invoke.assert_not_called()
    oc.start_campaign.assert_not_called()
    assert cs["precallSmsState"] == "pending"


def test_deadline_identity_are_saved_before_first_invoke_and_survive_slow_response(environment):
    client, oc = environment
    run, plan, cs = profile_model()
    saved = []
    def invoke(**kwargs):
        assert saved[-1]["bucketStates"][0]["campaignStates"][0]["precallSmsPendingAt"] == NOW.isoformat()
        assert saved[-1]["bucketStates"][0]["campaignStates"][0]["_precallSmsRunsSk"]
        executor._now_utc.return_value = NOW + timedelta(minutes=5)
        return provider_result()
    client.invoke.side_effect = invoke
    with patch.object(executor, "save_run", side_effect=lambda item: saved.append(deepcopy(item))), \
         patch.object(executor, "_get_campaign_state", return_value="Initialized"):
        executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
    assert cs["precallSmsFailureReason"] == "sms_initialization_timeout"
    oc.start_campaign.assert_called_once()


def test_lost_start_response_unknown_reads_then_running_never_restarts_or_falsely_fails(environment):
    client, oc = environment
    run, plan, cs = profile_model(precallSmsState="complete", precallSmsSentAt=NOW.isoformat())
    oc.start_campaign.side_effect = TimeoutError("lost response")
    with patch.object(executor, "_get_campaign_state", side_effect=["Initialized", "Unknown", "Unknown", "Unknown", "Running"]):
        for _ in range(5):
            executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
            assert cs["status"] != "error"
    oc.start_campaign.assert_called_once()
    client.invoke.assert_not_called()
    assert cs["precallVoiceStartedAt"]
    assert cs["precallVoiceStartAttempts"] == 1


def test_three_failed_starts_resolve_as_error_only_when_initialized_is_confirmed(environment):
    _, oc = environment
    run, plan, cs = profile_model(precallSmsState="failed")
    oc.start_campaign.side_effect = RuntimeError("provider detail")
    with patch.object(executor, "_get_campaign_state", return_value="Initialized"):
        for _ in range(4):
            executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
    assert oc.start_campaign.call_count == 3
    assert cs["status"] == "error"
    assert not cs.get("precallStartDeferred")
    assert "provider detail" not in str(cs)


def test_fresh_start_claim_blocks_duplicate_then_stale_claim_retries(environment):
    _, oc = environment
    run, plan, cs = profile_model(precallSmsState="complete", precallVoiceStartClaimedAt=NOW.isoformat())
    with patch.object(executor, "_get_campaign_state", return_value="Initialized"):
        executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
        oc.start_campaign.assert_not_called()
        cs["precallVoiceStartClaimedAt"] = (NOW - timedelta(minutes=5)).isoformat()
        executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
    oc.start_campaign.assert_called_once()


def test_force_restart_clears_profile_outcomes_and_rotates_only_explicit_generation(environment):
    run, plan, cs = profile_model(status="cancelled", precallSmsGeneration=2,
        precallSmsState="complete", precallSmsSentAt=NOW.isoformat(), precallSmsSettledAt=NOW.isoformat(),
        precallSmsAcceptedCount=2, precallVoiceStartedAt=NOW.isoformat(), precallVoiceStartClaimedAt=NOW.isoformat())
    old_id = executor._precall_sms_campaign_id(run, 0, 0)
    cs.update(precallSmsCampaignId=old_id, _precallSmsRunsPlanId="p", _precallSmsRunsSk=f"r#{old_id}")
    with patch.object(executor, "get_run", return_value=run), \
         patch.object(executor, "_reset_cascade_cancelled_children"), \
         patch.object(executor, "_safe_stop_campaign"), patch.object(executor, "_safe_delete_campaign"), \
         patch.object(executor, "_stop_sms_campaign") as abort, \
         patch.object(executor, "_start_one_campaign"):
        executor.force_start_campaign("p", "r", 0, 0)
    assert cs["precallSmsGeneration"] == 3
    assert executor._precall_sms_campaign_id(run, 0, 0) != old_id
    assert abort.call_args.args[0]["smsCampaignId"] == old_id
    assert not cs.get("precallSmsSentAt")
    for field in executor._SMS_INITIALIZATION_FIELDS:
        assert field not in cs


def test_settled_profile_merge_retains_acceptance_and_never_revives_gate():
    merged = executor._merge_sms_initialization_state(
        {"precallSmsState": "skipped", "precallSmsSettledAt": NOW.isoformat(),
         "precallSmsAcceptedCount": 0, "precallStartDeferred": False, "precallVoiceStartedAt": NOW.isoformat()},
        {"precallSmsState": "pending", "precallSmsPendingAt": NOW.isoformat(), "precallStartDeferred": True},
    )
    assert merged["precallSmsState"] == "skipped"
    assert not merged["precallSmsPendingAt"]
    assert not merged["precallStartDeferred"]
    assert merged["precallSmsAcceptedCount"] == 0
    assert merged["precallVoiceStartedAt"]


@pytest.mark.parametrize("reason", ["sms_initialization_timeout", "sms_initialization_failed", "sms_provider_failed"])
def test_recover_fail_open_tombstone_does_not_mistake_failure_for_cancellation(environment, reason):
    client, oc = environment
    run, plan, cs = profile_model(precallSmsState="pending", precallSmsPendingAt=NOW.isoformat())
    client.invoke.return_value = response({"terminal": True, "exitReason": reason, "enqueued": 0})
    with patch.object(executor, "_get_campaign_state", return_value="Initialized"):
        executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
    assert cs["precallSmsState"] == "failed"
    assert cs["precallSmsFailureReason"] == reason
    oc.start_campaign.assert_called_once()


@pytest.mark.parametrize("delivery", ["campaign", "journey", "branded"])
def test_profile_pending_campaign_expiry_never_starts_or_seeds(environment, delivery):
    client, oc = environment
    run, plan, cs = profile_model(delivery, connectCampaignId=None if delivery == "branded" else "connect")
    client.invoke.return_value = provider_result()
    with patch.object(executor, "_check_native_queue_collision"), \
         patch.object(executor, "_safe_stop_campaign"), patch.object(executor, "_stop_branded_campaign"), \
         patch.object(executor, "_invoke_seeder") as seed:
        executor._start_one_campaign(run, plan, 0, 0)
        plan["buckets"][0]["campaigns"][0]["duration_minutes"] = 1
        cs["startedAt"] = (NOW - timedelta(minutes=2)).isoformat()
        with ExitStack() as stack:
            tick_patches(stack, run)
            executor.tick("p", "r", 0)
    assert cs["status"] == "expired"
    assert client.invoke.call_count == 1
    oc.start_campaign.assert_not_called()
    seed.assert_not_called()


def test_profile_branded_timeout_releases_seed_once_despite_overlapping_continuation(environment):
    client, _ = environment
    run, plan, cs = profile_model("branded", connectCampaignId=None)
    client.invoke.return_value = provider_result()
    executor._start_one_campaign(run, plan, 0, 0)
    cs["precallSmsPendingAt"] = (NOW - timedelta(minutes=5)).isoformat()
    other = deepcopy(run)
    executor._get_ddb_client.return_value.put_item.side_effect = [
        {}, ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
    ]
    with patch.object(executor, "_invoke_seeder", return_value=2) as seed:
        executor._start_one_campaign(run, plan, 0, 0)
        executor._start_one_campaign(other, plan, 0, 0)
    seed.assert_called_once()
    assert client.invoke.call_count == 1
    assert cs["precallSmsState"] == "failed"
    assert not cs.get("precallSmsSentAt")


def test_concurrent_abort_during_sender_completion_wins_before_voice(environment):
    client, oc = environment
    run, plan, cs = profile_model()
    client.invoke.return_value = provider_result(sent=2)
    saves = [None, executor.ConcurrentWriteError("abort saved while SMS in flight")]
    with patch.object(executor, "save_run", side_effect=saves), \
         patch.object(executor, "_get_campaign_state", return_value="Initialized"):
        executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
    client.invoke.assert_called_once()
    oc.start_campaign.assert_not_called()
    assert cs["precallStartDeferred"]


def test_profile_cold_midflight_conflict_never_overwrites_concurrent_abort(environment):
    client, oc = environment
    run, plan, cs = profile_model(status="queued", connectCampaignId=None)
    oc.create_campaign.return_value = {"id": "new-connect"}
    aborted = deepcopy(run)
    aborted["status"] = "aborted"
    aborted["bucketStates"][0]["campaignStates"][0]["status"] = "cancelled"
    with ExitStack() as stack:
        creation_patches(stack)
        stack.enter_context(patch.object(executor, "save_run", side_effect=executor.ConcurrentWriteError("abort won")))
        reread = stack.enter_context(patch.object(executor, "get_run", return_value=aborted))
        executor._start_one_campaign(run, plan, 0, 0)
    reread.assert_any_call("p", "r", consistent_read=True)
    oc.delete_campaign.assert_called_once_with("new-connect")
    assert aborted["status"] == "aborted"
    client.invoke.assert_not_called()
    oc.start_campaign.assert_not_called()


def test_profile_processor_cancelled_outcome_is_not_sent(environment):
    client, oc = environment
    run, plan, cs = profile_model()
    client.invoke.return_value = response({"enqueued": 1, "pending": False,
        "initializationComplete": True, "totalEnqueued": 1, "totalSent": 0,
        "totalFailed": 0, "totalOptedOut": 0, "totalCancelled": 1})
    with patch.object(executor, "_get_campaign_state", return_value="Initialized"):
        executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
    assert cs["precallSmsState"] == "failed"
    assert cs["precallSmsCancelledCount"] == 1
    assert not cs.get("precallSmsSentAt")
    oc.start_campaign.assert_called_once()


def test_profile_quiet_retry_failure_never_logs_lambda_payload(environment, monkeypatch, caplog):
    client, _ = environment
    monkeypatch.setenv("SMS_RETRY_FUNCTION_ARN", "arn/retry")
    run, _, cs = profile_model(precallSmsState="complete", precallSmsSentAt=NOW.isoformat(),
                               precallVoiceStartedAt=NOW.isoformat())
    client.invoke.return_value = response({"errorMessage": "private provider payload"}, FunctionError="Unhandled")
    with ExitStack() as stack:
        tick_patches(stack, run)
        stack.enter_context(patch.object(executor, "_get_campaign_state", return_value="Running"))
        executor.tick("p", "r", 0)
    assert cs["status"] == "running"
    assert "private provider payload" not in caplog.text
    assert "RuntimeError" in caplog.text
