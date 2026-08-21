"""Tests for store.py — DynamoDB item transforms (no actual AWS calls)."""

from __future__ import annotations

import sys
import os
from unittest.mock import MagicMock, patch

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
