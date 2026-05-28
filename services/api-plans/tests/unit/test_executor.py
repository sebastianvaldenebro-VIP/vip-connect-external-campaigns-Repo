"""Tests for executor.py — residual v1 edge-case coverage (pre-schema behaviour)."""

from __future__ import annotations

import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))


def _make_run_v1(status="running", bucket_index=0):
    return {
        "planId": "plan-1",
        "runId": "run-1",
        "status": status,
        "currentBucketIndex": bucket_index,
        "bucketStates": [
            {
                "bucketId": str(i),
                "status": "pending" if i > 0 else "running",
                "campaignStates": [],
            }
            for i in range(2)
        ],
        "startedAt": "2026-05-01T10:00:00",
        "completedAt": None,
    }


def test_tick_not_running_is_skipped():
    run = _make_run_v1(status="completed")
    import executor

    with (
        patch("executor.get_run", return_value=run),
        patch("executor._delete_schedule_safe") as del_sched,
    ):
        result = executor.tick("plan-1", "run-1", 0)
    assert result.get("reason") == "already_terminal"
    del_sched.assert_called_once()


def test_abort_run_not_running_raises():
    import executor
    import pytest

    with patch("executor.get_run", return_value=_make_run_v1(status="completed")):
        with pytest.raises(ValueError, match="not running"):
            executor.abort_run("plan-1", "run-1")
