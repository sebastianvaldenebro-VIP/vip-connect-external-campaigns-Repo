"""Tests for store.py — DynamoDB item transforms (no actual AWS calls)."""

from __future__ import annotations

import sys
import os
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

import store  # noqa: E402


def _mock_table():
    t = MagicMock()
    t.scan.return_value = {"Items": []}
    t.query.return_value = {"Items": []}
    t.get_item.return_value = {}
    return t


# ── put_plan / get_plan round-trip ────────────────────────────────────────────


def test_put_plan_generates_plan_id():
    table = _mock_table()
    with patch("store._table", return_value=table):
        plan = store.put_plan({"name": "Test Plan", "buckets": []})
    assert "planId" in plan
    assert plan["name"] == "Test Plan"
    table.put_item.assert_called_once()


def test_put_plan_preserves_existing_id():
    table = _mock_table()
    with patch("store._table", return_value=table):
        plan = store.put_plan({"planId": "fixed-id", "name": "My Plan", "buckets": []})
    assert plan["planId"] == "fixed-id"


def test_get_plan_not_found_returns_none():
    table = _mock_table()
    table.get_item.return_value = {}
    with patch("store._table", return_value=table):
        result = store.get_plan("nonexistent")
    assert result is None


def test_get_plan_found_returns_dict():
    fake_item = {
        "pk": "PLAN#abc",
        "sk": "META",
        "planId": "abc",
        "name": "My Plan",
        "buckets": [],
        "createdAt": "2026-05-01T00:00:00",
        "updatedAt": "2026-05-01T00:00:00",
    }
    table = _mock_table()
    table.get_item.return_value = {"Item": fake_item}
    with patch("store._table", return_value=table):
        result = store.get_plan("abc")
    assert result is not None
    assert result["planId"] == "abc"
    assert result["name"] == "My Plan"


def test_list_plans_filters_meta_items_and_paginates():
    table = _mock_table()
    page1 = {
        "Items": [
            {"pk": "PLAN#a", "sk": "META", "planId": "a", "name": "A", "buckets": []}
        ],
        "LastEvaluatedKey": {"pk": "PLAN#a", "sk": "META"},
    }
    page2 = {
        "Items": [
            {"pk": "PLAN#b", "sk": "META", "planId": "b", "name": "B", "buckets": []}
        ]
    }
    table.scan.side_effect = [page1, page2]
    with patch("store._table", return_value=table):
        plans = store.list_plans()

    assert [p["planId"] for p in plans] == ["a", "b"]
    assert table.scan.call_count == 2
    second_call_kwargs = table.scan.call_args_list[1][1]
    assert second_call_kwargs["ExclusiveStartKey"] == {"pk": "PLAN#a", "sk": "META"}


def test_delete_plan_cascades_run_deletion_and_removes_meta():
    table = _mock_table()
    table.query.return_value = {
        "Items": [
            {"pk": "PLAN#p1", "sk": "RUN#1"},
            {"pk": "PLAN#p1", "sk": "RUN#2"},
        ]
    }
    table.name = "VipAdminPlans"
    with patch("store._table", return_value=table):
        store.delete_plan("p1")

    table.meta.client.batch_write_item.assert_called_once()
    batch_kwargs = table.meta.client.batch_write_item.call_args.kwargs
    deletes = batch_kwargs["RequestItems"]["VipAdminPlans"]
    assert len(deletes) == 2
    table.delete_item.assert_called_once_with(Key={"pk": "PLAN#p1", "sk": "META"})


def test_delete_plan_paginates_run_query_and_batches_in_groups_of_25():
    table = _mock_table()
    table.name = "VipAdminPlans"
    page1_items = [{"pk": "PLAN#p1", "sk": f"RUN#{i}"} for i in range(25)]
    page2_items = [{"pk": "PLAN#p1", "sk": "RUN#25"}]
    table.query.side_effect = [
        {"Items": page1_items, "LastEvaluatedKey": {"pk": "PLAN#p1", "sk": "RUN#24"}},
        {"Items": page2_items},
    ]
    with patch("store._table", return_value=table):
        store.delete_plan("p1")

    assert table.query.call_count == 2
    # 26 total run keys -> two batch_write_item calls (25 + 1)
    assert table.meta.client.batch_write_item.call_count == 2


def test_delete_plan_with_no_runs_still_deletes_meta():
    table = _mock_table()
    table.query.return_value = {"Items": []}
    table.name = "VipAdminPlans"
    with patch("store._table", return_value=table):
        store.delete_plan("p1")

    table.meta.client.batch_write_item.assert_not_called()
    table.delete_item.assert_called_once_with(Key={"pk": "PLAN#p1", "sk": "META"})


def test_update_plan_trigger_writes_trigger_field():
    table = _mock_table()
    with patch("store._table", return_value=table):
        store.update_plan_trigger("p1", {"type": "manual"})

    call_kwargs = table.update_item.call_args.kwargs
    assert call_kwargs["Key"] == {"pk": "PLAN#p1", "sk": "META"}
    assert call_kwargs["ExpressionAttributeValues"][":trigger"] == {"type": "manual"}


def test_update_plan_pending_warmup_sets_value():
    table = _mock_table()
    warmup = {"campaigns": [{"campaignId": "c1"}]}
    with patch("store._table", return_value=table):
        store.update_plan_pending_warmup("p1", warmup)

    call_kwargs = table.update_item.call_args.kwargs
    assert call_kwargs["UpdateExpression"] == "SET pendingWarmup = :w"
    assert call_kwargs["ExpressionAttributeValues"][":w"] == warmup


def test_update_plan_pending_warmup_none_removes_attribute():
    table = _mock_table()
    with patch("store._table", return_value=table):
        store.update_plan_pending_warmup("p1", None)

    call_kwargs = table.update_item.call_args.kwargs
    assert call_kwargs["UpdateExpression"] == "REMOVE pendingWarmup"
    assert "ExpressionAttributeValues" not in call_kwargs


def test_lock_plan_run_succeeds_when_not_already_locked():
    table = _mock_table()
    with patch("store._table", return_value=table):
        store.lock_plan_run("p1", "run-1")

    call_kwargs = table.update_item.call_args.kwargs
    assert call_kwargs["ExpressionAttributeValues"][":run_id"] == "run-1"
    assert call_kwargs["ConditionExpression"] == "attribute_not_exists(runLock)"


def test_lock_plan_run_raises_value_error_when_already_locked():
    table = _mock_table()
    table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "locked"}},
        "UpdateItem",
    )
    with patch("store._table", return_value=table):
        with pytest.raises(ValueError, match="already has an active run"):
            store.lock_plan_run("p1", "run-1")


def test_lock_plan_run_reraises_non_conditional_errors():
    table = _mock_table()
    table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}},
        "UpdateItem",
    )
    with patch("store._table", return_value=table):
        with pytest.raises(ClientError, match="ProvisionedThroughputExceededException"):
            store.lock_plan_run("p1", "run-1")


def test_unlock_plan_run_removes_lock():
    table = _mock_table()
    with patch("store._table", return_value=table):
        store.unlock_plan_run("p1")

    call_kwargs = table.update_item.call_args.kwargs
    assert call_kwargs["UpdateExpression"] == "REMOVE runLock"


def test_find_plans_by_trigger_planid_filters_matching_trigger():
    table = _mock_table()
    matching = {
        "pk": "PLAN#b",
        "sk": "META",
        "planId": "b",
        "name": "B",
        "buckets": [],
        "trigger": {"type": "on_plan_complete", "planId": "upstream-1"},
    }
    non_matching_type = {
        "pk": "PLAN#c",
        "sk": "META",
        "planId": "c",
        "name": "C",
        "buckets": [],
        "trigger": {"type": "manual"},
    }
    non_matching_upstream = {
        "pk": "PLAN#d",
        "sk": "META",
        "planId": "d",
        "name": "D",
        "buckets": [],
        "trigger": {"type": "on_plan_complete", "planId": "other-upstream"},
    }
    table.scan.return_value = {
        "Items": [matching, non_matching_type, non_matching_upstream]
    }
    with patch("store._table", return_value=table):
        result = store.find_plans_by_trigger_planid("upstream-1")

    assert [p["planId"] for p in result] == ["b"]


def test_find_plans_by_trigger_planid_paginates():
    table = _mock_table()
    table.scan.side_effect = [
        {"Items": [], "LastEvaluatedKey": {"pk": "x"}},
        {"Items": []},
    ]
    with patch("store._table", return_value=table):
        store.find_plans_by_trigger_planid("upstream-1")

    assert table.scan.call_count == 2


# ── create_run ────────────────────────────────────────────────────────────────


def test_create_run_initializes_bucket_states():
    table = _mock_table()
    plan = {
        "planId": "plan-1",
        "name": "Test",
        "buckets": [
            {
                "id": "b0",
                "name": "Bucket 0",
                "run_mode": "time_based",
                "duration_minutes": 10,
                "campaigns": [],
            },
            {
                "id": "b1",
                "name": "Bucket 1",
                "run_mode": "status_based",
                "campaigns": [],
            },
        ],
    }
    with patch("store._table", return_value=table):
        run = store.create_run("plan-1", plan)
    assert run["status"] == "running"
    assert run["currentBucketIndex"] == 0
    assert len(run["bucketStates"]) == 2
    assert run["bucketStates"][0]["status"] == "queued"
    assert run["bucketStates"][1]["status"] == "queued"
    table.put_item.assert_called_once()


def test_create_run_run_id_is_unique():
    table = _mock_table()
    plan = {
        "planId": "plan-1",
        "name": "Test",
        "buckets": [{"id": "b0", "campaigns": []}],
    }
    with patch("store._table", return_value=table):
        run1 = store.create_run("plan-1", plan)
        run2 = store.create_run("plan-1", plan)
    assert run1["runId"] != run2["runId"]


# ── get_latest_run ────────────────────────────────────────────────────────────


def test_get_latest_run_none_when_no_runs():
    table = _mock_table()
    table.query.return_value = {"Items": []}
    with patch("store._table", return_value=table):
        result = store.get_latest_run("plan-1")
    assert result is None


def test_get_latest_run_returns_first_item():
    fake_item = {
        "pk": "PLAN#p1",
        "sk": "RUN#1234-abc",
        "planId": "p1",
        "runId": "1234-abc",
        "status": "completed",
        "currentBucketIndex": 2,
        "scheduleName": None,
        "bucketStates": [],
        "startedAt": "2026-05-01T00:00:00",
        "completedAt": "2026-05-01T01:00:00",
        "error": None,
    }
    table = _mock_table()
    table.query.return_value = {"Items": [fake_item]}
    with patch("store._table", return_value=table):
        result = store.get_latest_run("p1")
    assert result is not None
    assert result["runId"] == "1234-abc"
    assert result["status"] == "completed"


# ── Bug fix: _run_from_item must return _version ───────────────────────────────
#
# Before the fix, _run_from_item omitted _version from the returned dict.
# save_run reads run.get("_version", 0) — so it always sent current_version=0,
# but DynamoDB already stored _version=1 after the first save. Every subsequent
# save_run call raised ConcurrentWriteError, breaking all ticks after the first.


def test_get_run_returns_none_when_missing():
    table = _mock_table()
    table.get_item.return_value = {}
    with patch("store._table", return_value=table):
        assert store.get_run("p1", "r1") is None


def test_get_run_returns_dict_when_found():
    fake_item = {
        "pk": "PLAN#p1",
        "sk": "RUN#r1",
        "planId": "p1",
        "runId": "r1",
        "status": "running",
        "currentBucketIndex": 0,
        "bucketStates": [],
        "startedAt": "now",
        "completedAt": None,
        "error": None,
    }
    table = _mock_table()
    table.get_item.return_value = {"Item": fake_item}
    with patch("store._table", return_value=table):
        result = store.get_run("p1", "r1")
    assert result is not None
    assert result["runId"] == "r1"


def test_list_runs_returns_run_dicts_with_limit():
    fake_item = {
        "pk": "PLAN#p1",
        "sk": "RUN#r1",
        "planId": "p1",
        "runId": "r1",
        "status": "completed",
        "currentBucketIndex": 0,
        "bucketStates": [],
        "startedAt": "now",
        "completedAt": "later",
        "error": None,
    }
    table = _mock_table()
    table.query.return_value = {"Items": [fake_item]}
    with patch("store._table", return_value=table):
        result = store.list_runs("p1", limit=5)

    assert len(result) == 1
    assert result[0]["runId"] == "r1"
    call_kwargs = table.query.call_args.kwargs
    assert call_kwargs["Limit"] == 5
    assert call_kwargs["ScanIndexForward"] is False


def test_save_run_raises_concurrent_write_error_and_reverts_version():
    table = _mock_table()
    table.put_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "conflict"}},
        "PutItem",
    )
    run = {
        "planId": "p1",
        "runId": "r1",
        "status": "running",
        "currentBucketIndex": 0,
        "bucketStates": [],
        "_version": 3,
    }
    with patch("store._table", return_value=table):
        with pytest.raises(store.ConcurrentWriteError, match="version conflict"):
            store.save_run(run)

    # In-memory version reverted to the original pre-attempt value.
    assert run["_version"] == 3


def test_save_run_reraises_non_conditional_errors_and_reverts_version():
    table = _mock_table()
    table.put_item.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}},
        "PutItem",
    )
    run = {
        "planId": "p1",
        "runId": "r1",
        "status": "running",
        "currentBucketIndex": 0,
        "bucketStates": [],
        "_version": 2,
    }
    with patch("store._table", return_value=table):
        with pytest.raises(ClientError, match="ProvisionedThroughputExceededException"):
            store.save_run(run)

    assert run["_version"] == 2


class TestEnsureCampaigns:
    def test_returns_existing_campaigns_list_unchanged(self):
        bucket = {"id": "b0", "campaigns": [{"id": "c1", "name": "Campaign 1"}]}
        result = store._ensure_campaigns(bucket)
        assert result == [{"id": "c1", "name": "Campaign 1"}]

    def test_synthesizes_single_campaign_for_legacy_bucket(self):
        bucket = {
            "bucketId": "legacy-b0",
            "name": "Legacy Bucket",
            "segmentFilters": {"state": ["NJ", "FL"]},
        }
        result = store._ensure_campaigns(bucket)
        assert len(result) == 1
        assert result[0]["id"] == "legacy-b0"
        assert result[0]["states"] == ["NJ", "FL"]
        assert result[0]["_legacyBucket"] is True


class TestRunFromItemLegacyMigration:
    def test_migrates_old_flat_bucket_state_to_campaign_states(self):
        item = {
            "planId": "p1",
            "runId": "r1",
            "status": "completed",
            "currentBucketIndex": 0,
            "scheduleName": "vip-sched-old",
            "bucketStates": [
                {
                    "bucketId": "b0",
                    "status": "completed",
                    "campaignId": "connect-camp-1",
                    "segmentName": "seg-1",
                    "startedAt": "t0",
                    "completedAt": "t1",
                    "exitReason": "queue_drained",
                }
            ],
            "startedAt": "t0",
            "completedAt": "t1",
            "error": None,
        }
        result = store._run_from_item(item)
        bs = result["bucketStates"][0]
        assert "campaignStates" in bs
        assert bs["scheduleName"] == "vip-sched-old"
        campaign_state = bs["campaignStates"][0]
        assert campaign_state["campaignId"] == "b0"
        assert campaign_state["connectCampaignId"] == "connect-camp-1"
        assert campaign_state["status"] == "completed"

    def test_leaves_new_schema_bucket_states_untouched(self):
        item = {
            "planId": "p1",
            "runId": "r1",
            "status": "running",
            "currentBucketIndex": 0,
            "bucketStates": [
                {"bucketId": "b0", "status": "running", "campaignStates": []}
            ],
            "startedAt": "t0",
            "completedAt": None,
            "error": None,
        }
        result = store._run_from_item(item)
        assert result["bucketStates"][0]["campaignStates"] == []


class TestMapOldBucketStatus:
    @pytest.mark.parametrize(
        "old_status,expected",
        [
            ("pending", "queued"),
            ("running", "running"),
            ("completed", "completed"),
            ("failed", "error"),
            ("aborted", "cancelled"),
            ("cancelled", "cancelled"),
        ],
    )
    def test_maps_known_statuses(self, old_status, expected):
        assert store._map_old_bucket_status(old_status, None) == expected

    def test_passes_through_unknown_status_unchanged(self):
        assert store._map_old_bucket_status("some_future_status", None) == "some_future_status"


class TestApplyPlanToRun:
    def test_raises_when_run_not_found(self):
        table = _mock_table()
        table.get_item.return_value = {}
        with patch("store._table", return_value=table):
            with pytest.raises(ValueError, match="not found"):
                store.apply_plan_to_run("p1", "r1", {"buckets": []})

    def test_raises_when_run_not_running(self):
        fake_item = {
            "pk": "PLAN#p1",
            "sk": "RUN#r1",
            "planId": "p1",
            "runId": "r1",
            "status": "completed",
            "currentBucketIndex": 0,
            "bucketStates": [],
            "startedAt": "t0",
            "completedAt": "t1",
            "error": None,
        }
        table = _mock_table()
        table.get_item.return_value = {"Item": fake_item}
        with patch("store._table", return_value=table):
            with pytest.raises(ValueError, match="is not running"):
                store.apply_plan_to_run("p1", "r1", {"buckets": []})

    def test_merges_queued_buckets_only(self):
        fake_item = {
            "pk": "PLAN#p1",
            "sk": "RUN#r1",
            "planId": "p1",
            "runId": "r1",
            "status": "running",
            "currentBucketIndex": 0,
            "planSnapshot": {
                "buckets": [{"id": "b0", "name": "Old B0"}, {"id": "b1", "name": "Old B1"}],
            },
            "bucketStates": [
                {"bucketId": "b0", "status": "running", "campaignStates": []},
                {"bucketId": "b1", "status": "queued", "campaignStates": []},
            ],
            "startedAt": "t0",
            "completedAt": None,
            "error": None,
            "_version": 0,
        }
        table = _mock_table()
        table.get_item.return_value = {"Item": fake_item}
        live_plan = {
            "buckets": [{"id": "b0", "name": "New B0"}, {"id": "b1", "name": "New B1"}],
            "workingHours": {"start": "08:00"},
            "loop": False,
        }
        with patch("store._table", return_value=table):
            result = store.apply_plan_to_run("p1", "r1", live_plan)

        merged = result["planSnapshot"]["buckets"]
        # Running bucket (b0) keeps its OLD snapshot; queued bucket (b1) gets the new one.
        assert merged[0]["name"] == "Old B0"
        assert merged[1]["name"] == "New B1"
        assert result["planSnapshot"]["workingHours"] == {"start": "08:00"}
        table.put_item.assert_called_once()  # save_run was called


def test_normalize_converts_integral_decimal_to_int():
    assert store._normalize(Decimal("30")) == 30
    assert isinstance(store._normalize(Decimal("30")), int)


def test_normalize_converts_fractional_decimal_to_float():
    assert store._normalize(Decimal("30.5")) == 30.5
    assert isinstance(store._normalize(Decimal("30.5")), float)


def test_run_from_item_returns_version():
    """_run_from_item must include _version so save_run optimistic locking works.

    Without _version in the returned dict, save_run always reads 0 from memory
    while DynamoDB holds a higher value, causing every tick after the first to
    fail with ConcurrentWriteError.
    """
    item = {
        "pk": "PLAN#p1",
        "sk": "RUN#1234-abc",
        "planId": "p1",
        "runId": "1234-abc",
        "status": "running",
        "currentBucketIndex": 0,
        "bucketStates": [],
        "startedAt": "2026-05-01T00:00:00",
        "completedAt": None,
        "error": None,
        "_version": 7,
    }
    result = store._run_from_item(item)
    assert "_version" in result, "_version must be present in the run dict"
    assert result["_version"] == 7, "_version must match the value stored in DynamoDB"


def test_run_from_item_version_defaults_to_zero():
    """_run_from_item must default _version to 0 for runs created before versioning."""
    item = {
        "pk": "PLAN#p1",
        "sk": "RUN#old-run",
        "planId": "p1",
        "runId": "old-run",
        "status": "completed",
        "currentBucketIndex": 0,
        "bucketStates": [],
        "startedAt": "2026-01-01T00:00:00",
        "completedAt": "2026-01-01T01:00:00",
        "error": None,
        # No _version field — simulates pre-versioning run items
    }
    result = store._run_from_item(item)
    assert result["_version"] == 0


def test_save_run_increments_version_and_uses_condition():
    """save_run must increment _version in memory and pass current_version in ConditionExpression.

    This test guards the full optimistic-locking contract:
      - run["_version"] goes from N to N+1 after a successful save
      - The DynamoDB put_item call carries :current_v = N so a concurrent writer
        with an outdated version loses the race
    """
    table = _mock_table()
    run = {
        "planId": "p1",
        "runId": "r1",
        "status": "running",
        "currentBucketIndex": 0,
        "bucketStates": [],
        "startedAt": "now",
        "completedAt": None,
        "triggeredBy": "manual",
        "error": None,
        "scheduleName": None,
        "_version": 3,
    }
    with patch("store._table", return_value=table):
        store.save_run(run)

    assert run["_version"] == 4, (
        "save_run must increment _version in-memory after a successful write"
    )
    call_kwargs = table.put_item.call_args[1]
    assert call_kwargs["ExpressionAttributeValues"][":current_v"] == 3, (
        "ConditionExpression must check the PRE-increment version to reject concurrent stale writers"
    )


def test_record_bucket_schedule_name_bypasses_version_lock():
    """record_bucket_schedule_name must use update_item on the specific bucket's
    scheduleName path, with no ConditionExpression/version check — it's the
    recovery write used when save_run's own version-locked write already
    failed, so it must succeed regardless of the run's current _version.
    """
    table = _mock_table()
    with patch("store._table", return_value=table):
        store.record_bucket_schedule_name("plan-1", "run-1", 3, "vip-plan-p1-run-r1-b3")

    table.update_item.assert_called_once()
    call_kwargs = table.update_item.call_args[1]
    assert call_kwargs["Key"] == {"pk": "PLAN#plan-1", "sk": "RUN#run-1"}
    assert "ConditionExpression" not in call_kwargs, (
        "Must not carry a version condition — this write exists precisely to "
        "succeed after the version-conditional save_run already failed"
    )
    assert call_kwargs["UpdateExpression"] == "SET bucketStates[3].scheduleName = :sched"
    assert call_kwargs["ExpressionAttributeValues"][":sched"] == "vip-plan-p1-run-r1-b3"
