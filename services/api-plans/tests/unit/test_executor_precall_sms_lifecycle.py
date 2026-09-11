"""Pre-call SMS lifecycle identity and optimistic-save conflict regressions."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import executor  # noqa: E402


def _precalls():
    campaign = {
        "id": "c0",
        "campaignConfig": {
            "precallSms": {
                "enabled": True,
                "messageTemplate": "Hi {{FirstName}}",
                "originationNumberArn": "arn:phone",
            }
        },
    }
    plan = {"planId": "plan-1", "buckets": [{"id": "b0", "campaigns": [campaign]}]}
    cs = {"campaignId": "c0", "status": "cancelled"}
    run = {
        "planId": "plan-1", "runId": "run-1", "status": "running", "_version": 0,
        "planSnapshot": plan,
        "bucketStates": [{"status": "running", "campaignStates": [cs]}],
    }
    return run, plan, cs


@pytest.fixture
def oc_module():
    oc = MagicMock()
    with patch.dict(
        "sys.modules",
        {
            "vip_shared.infrastructure.persistence.outbound_campaigns_client":
                MagicMock(build=MagicMock(return_value=oc))
        },
    ):
        yield oc


def _started(run, plan, bi, ci):
    cs = run["bucketStates"][bi]["campaignStates"][ci]
    cs.update(
        connectCampaignId="new-connect",
        segmentArn="arn/new-segment",
        segmentName="new-segment",
        precallGatePausedAt="new-pause",
        status="running",
    )
    executor._fire_precall_sms_for_campaign(run, plan, bi, ci)


def test_restart_cycles_initialize_distinct_sms_rows(oc_module):
    run, plan, cs = _precalls()
    original_id = executor._precall_sms_campaign_id(run, 0, 0)
    initialized = {original_id}
    cs.update(precallSmsSentAt="old-send", precallGateResumedAt="old-resume")
    claims = []
    sent_ids = []

    def conditional_sender(**kwargs):
        sms_id = kwargs["campaignId"]
        # The sender's runs-table initialization rejects an existing campaign ID.
        if sms_id in initialized:
            raise RuntimeError("ConditionalCheckFailedException")
        assert claims[-1]["precallSmsGeneration"] == cs["precallSmsGeneration"]
        initialized.add(sms_id)
        sent_ids.append(sms_id)

    with (
        patch.object(executor, "get_run", return_value=run),
        patch.object(executor, "save_run", side_effect=lambda r: claims.append(deepcopy(cs))),
        patch.object(executor, "_reset_cascade_cancelled_children"),
        patch.object(executor, "_safe_stop_campaign"),
        patch.object(executor, "_safe_delete_campaign"),
        patch.object(executor, "_invoke_sms_sender", side_effect=conditional_sender),
        patch.object(executor, "_start_one_campaign", side_effect=_started),
    ):
        executor.force_start_campaign("plan-1", "run-1", 0, 0)
        assert cs["precallSmsSentAt"]
        first_id = executor._precall_sms_campaign_id(run, 0, 0)
        assert first_id != original_id
        assert cs["precallSmsGeneration"] == 1
        # Re-activation / recovery of the same persisted lifecycle is idempotent.
        restored = deepcopy(run)
        executor._fire_precall_sms_for_campaign(restored, plan, 0, 0)
        assert executor._precall_sms_campaign_id(restored, 0, 0) == first_id
        assert sent_ids == [first_id]
        cs["status"] = "cancelled"
        executor.force_start_campaign("plan-1", "run-1", 0, 0)
        assert cs["precallSmsGeneration"] == 2
        assert cs["precallSmsSentAt"]

    assert len(initialized) == 3
    assert len(sent_ids) == 2
    assert len(set(sent_ids)) == 2


def test_original_lifecycle_uuid_is_unchanged():
    run, _, cs = _precalls()
    # Pin the deployed name/namespace derivation, including runs without state.
    original = "2ce6561a-f9ce-5db2-91db-5959a7ba217d"
    assert executor._precall_sms_campaign_id(run, 0, 0) == original
    assert executor._precall_sms_campaign_id(
        {"planId": "plan-1", "runId": "run-1"}, 0, 0
    ) == original
    cs["precallSmsGeneration"] = 0
    assert executor._precall_sms_campaign_id(run, 0, 0) == original


@pytest.mark.parametrize("delivery_type", ["campaign", "journey", "branded"])
def test_start_entry_points_use_persisted_sms_generation(oc_module, delivery_type):
    run, plan, cs = _precalls()
    campaign = plan["buckets"][0]["campaigns"][0]
    campaign.update(deliveryType=delivery_type, pinnedSegmentArn="arn/segment")
    campaign["campaignConfig"].update(queueArn="arn/queue", contactFlowId="flow")
    original_id = executor._precall_sms_campaign_id(run, 0, 0)
    cs.update(precallSmsGeneration=2, connectCampaignId="connect", warmupStarted=True,
              segmentArn="arn/segment", segmentName="segment", precallGatePausedAt="pause")
    restarted_id = executor._precall_sms_campaign_id(run, 0, 0)
    assert restarted_id != original_id

    with (
        patch.object(executor, "_invoke_sms_sender") as sender,
        patch.object(executor, "save_run"),
        patch.object(executor, "_ACTIVE_BRANDED_CAMPAIGNS_TABLE", "active"),
        patch.object(executor, "_CAMPAIGN_QUEUE_TABLE_BRANDED", "queue"),
        patch.object(executor, "_get_ddb_client"),
        patch.object(executor, "_invoke_seeder", return_value=1),
        patch.object(executor, "_write_branded_run_start"),
    ):
        executor._start_one_campaign(run, plan, 0, 0)

    sender.assert_called_once()
    assert sender.call_args.kwargs["campaignId"] == restarted_id
    assert cs["precallSmsGeneration"] == 2
    assert cs["precallSmsSentAt"]


@pytest.mark.parametrize("concurrent_status", ["creating", "running", "completed"])
def test_conflict_preserves_precall_evidence_and_concurrent_status(oc_module, concurrent_status):
    run, _, _ = _precalls()
    persisted = {}
    saves = 0

    def save(value):
        nonlocal saves
        saves += 1
        if saves == 3:
            # A tick wrote the same run after our midflight save.
            latest = persisted["run"]["bucketStates"][0]["campaignStates"][0]
            latest["status"] = concurrent_status
            latest["concurrentField"] = "keep"
            if concurrent_status == "completed":
                latest["completedAt"] = "tick-completed"
                latest["exitReason"] = "completed"
            raise executor.ConcurrentWriteError("other tick won")
        persisted["run"] = deepcopy(value)

    def start(value, definition, bi, ci):
        target = value["bucketStates"][bi]["campaignStates"][ci]
        target.update(connectCampaignId="new-connect", segmentArn="arn/segment",
                      segmentName="segment", precallGatePausedAt="new-pause", status="creating")
        save(value)
        target["status"] = "running"
        executor._fire_precall_sms_for_campaign(value, definition, bi, ci)
        assert target["precallSmsSentAt"]
        assert target["precallGateResumedAt"]

    with (
        patch.object(executor, "get_run", side_effect=lambda *a: deepcopy(persisted.get("run", run))),
        patch.object(executor, "save_run", side_effect=save),
        patch.object(executor, "_reset_cascade_cancelled_children"),
        patch.object(executor, "_invoke_sms_sender"),
        patch.object(executor, "_start_one_campaign", side_effect=start),
    ):
        result = executor.force_start_campaign("plan-1", "run-1", 0, 0)

    final_cs = result["bucketStates"][0]["campaignStates"][0]
    assert final_cs["status"] == ("completed" if concurrent_status == "completed" else "running")
    assert final_cs["precallSmsSentAt"]
    assert final_cs["precallGateResumedAt"]
    assert final_cs["precallGatePausedAt"] == "new-pause"
    assert final_cs["precallSmsGeneration"] == 1
    assert final_cs["concurrentField"] == "keep"
    assert persisted["run"] == result
    if concurrent_status == "completed":
        assert final_cs["completedAt"] == "tick-completed"
        assert final_cs["exitReason"] == "completed"


def test_conflict_does_not_overwrite_newer_restart(oc_module):
    run, _, _ = _precalls()
    persisted = {}
    saves = 0

    def save(value):
        nonlocal saves
        saves += 1
        if saves == 2:
            latest = persisted["run"]["bucketStates"][0]["campaignStates"][0]
            latest.update(status="running", connectCampaignId="newer-connect",
                          precallSmsGeneration=2, precallSmsSentAt="newer-send",
                          precallGateResumedAt="newer-resume")
            raise executor.ConcurrentWriteError("newer operator restart")
        persisted["run"] = deepcopy(value)

    with (
        patch.object(executor, "get_run", side_effect=lambda *a: deepcopy(persisted.get("run", run))),
        patch.object(executor, "save_run", side_effect=save),
        patch.object(executor, "_reset_cascade_cancelled_children"),
        patch.object(executor, "_invoke_sms_sender"),
        patch.object(executor, "_start_one_campaign", side_effect=_started),
    ):
        result = executor.force_start_campaign("plan-1", "run-1", 0, 0)

    final_cs = result["bucketStates"][0]["campaignStates"][0]
    assert final_cs["connectCampaignId"] == "newer-connect"
    assert final_cs["precallSmsGeneration"] == 2
    assert final_cs["precallSmsSentAt"] == "newer-send"
    assert final_cs["precallGateResumedAt"] == "newer-resume"
    assert saves == 2
