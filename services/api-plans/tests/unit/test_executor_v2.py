"""Tests for executor.py v2 — DAG campaigns, parallel buckets, pre-start, loop."""

from __future__ import annotations

import sys
import os
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

sys.modules.setdefault("vip_shared", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.persistence", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.persistence.audit", MagicMock())


# ── Helpers ───────────────────────────────────────────────────────────────────


def _campaign_def(cid: str, name: str = "", depends_on: list | None = None) -> dict:
    return {
        "id": cid,
        "name": name or cid,
        "states": ["NY"],
        "groups": [],
        "dependsOn": depends_on or [],
    }


def _campaign_state(
    cid: str,
    status: str = "queued",
    connect_id: str | None = None,
    exit_reason: str | None = None,
) -> dict:
    return {
        "campaignId": cid,
        "name": cid,
        "status": status,
        "connectCampaignId": connect_id,
        "segmentName": None,
        "segmentArn": None,
        "leadCount": None,
        "startedAt": None,
        "completedAt": None,
        "exitReason": exit_reason,
        "errorDetail": None,
    }


def _bucket_state(
    bucket_id: str,
    campaign_states: list,
    status: str = "running",
    schedule_name: str = "sched-x",
) -> dict:
    return {
        "bucketId": bucket_id,
        "name": bucket_id,
        "status": status,
        "scheduleName": schedule_name,
        "startedAt": "2026-05-08T10:00:00+00:00",
        "completedAt": None,
        "campaignStates": campaign_states,
    }


def _bucket_def(
    bid: str,
    campaigns: list,
    run_mode: str = "status_based",
    duration: int = 30,
    parallel: bool = False,
    cleanup: bool = False,
    prestart_next: bool = True,
) -> dict:
    return {
        "id": bid,
        "name": bid,
        "run_mode": run_mode,
        "duration_minutes": duration,
        "cleanup": cleanup,
        "prestart_next": prestart_next,
        "parallel": parallel,
        "campaigns": campaigns,
        "campaignConfig": {
            "queueId": "q-1",
            "contactFlowId": "cf-1",
            "sourcePhoneNumber": "+12125550100",
            "dialerType": "progressive",
            "bandwidthAllocation": 1.0,
            "dialingCapacity": 1.0,
            "amdEnabled": True,
        },
    }


def _make_plan(buckets: list) -> dict:
    return {
        "planId": "plan-1",
        "name": "Test",
        "trigger": {"type": "manual"},
        "isTemplate": False,
        "buckets": buckets,
    }


def _started_mock(run: dict, bucket_index: int, status: str = "running"):
    """side_effect for a mocked _start_one_campaign that mimics real behavior:
    advancing cs["status"] away from "queued". Without this, _dispatch_ready_campaigns'
    made_progress check (added to prevent the Redis-rebuilding busy-loop) sees every
    mocked campaign as stuck in "queued" and reports changed=False, which is only
    correct for the _RedisRebuildingError/_EmptySegmentError revert path.

    `bucket_index` is accepted for call-site clarity/documentation only — the real
    bucket_index is always taken from the call's own `_bucket_index` arg (the one
    _dispatch_ready_campaigns actually passes), so a stale/mismatched value here
    can't silently mutate the wrong bucket's state.
    """

    def _side_effect(_run, _plan, _bucket_index, campaign_index):
        assert _bucket_index == bucket_index, (
            f"_started_mock(bucket_index={bucket_index}) called for bucket "
            f"{_bucket_index} — pass the real bucket_index used in the test's "
            f"_dispatch_ready_campaigns()/tick() call"
        )
        _run["bucketStates"][_bucket_index]["campaignStates"][campaign_index][
            "status"
        ] = status

    return _side_effect


def _make_run(
    plan: dict,
    bucket_states: list,
    status: str = "running",
    bucket_index: int = 0,
) -> dict:
    return {
        "planId": "plan-1",
        "runId": "run-1",
        "status": status,
        "planSnapshot": plan,
        "currentBucketIndex": bucket_index,
        "bucketStates": bucket_states,
        "startedAt": "2026-05-08T10:00:00+00:00",
        "completedAt": None,
        "_version": 0,
        "triggeredBy": "manual",
        "error": None,
    }


# ── _find_campaign_state ──────────────────────────────────────────────────────


def test_find_campaign_state_same_bucket():
    import executor

    run = _make_run(
        _make_plan([]),
        [_bucket_state("b0", [_campaign_state("c1"), _campaign_state("c2")])],
    )
    assert executor._find_campaign_state(run, "c2")["campaignId"] == "c2"


def test_find_campaign_state_cross_bucket():
    import executor

    run = _make_run(
        _make_plan([]),
        [
            _bucket_state("b0", [_campaign_state("c0")]),
            _bucket_state("b1", [_campaign_state("c1")]),
        ],
    )
    assert executor._find_campaign_state(run, "c0")["campaignId"] == "c0"
    assert executor._find_campaign_state(run, "c1")["campaignId"] == "c1"


def test_find_campaign_state_missing_returns_none():
    import executor

    run = _make_run(_make_plan([]), [_bucket_state("b0", [_campaign_state("c0")])])
    assert executor._find_campaign_state(run, "nope") is None


# ── _all_campaigns_terminal ───────────────────────────────────────────────────


def test_all_campaigns_terminal_true():
    import executor

    run = _make_run(
        _make_plan([]),
        [
            _bucket_state(
                "b0",
                [
                    _campaign_state("c1", "completed"),
                    _campaign_state("c2", "cancelled"),
                ],
            )
        ],
    )
    assert executor._all_campaigns_terminal(run, 0) is True


def test_all_campaigns_terminal_false():
    import executor

    run = _make_run(
        _make_plan([]),
        [
            _bucket_state(
                "b0",
                [
                    _campaign_state("c1", "completed"),
                    _campaign_state("c2", "running"),
                ],
            )
        ],
    )
    assert executor._all_campaigns_terminal(run, 0) is False


def test_all_campaigns_terminal_error_counts_as_terminal():
    import executor

    run = _make_run(
        _make_plan([]),
        [_bucket_state("b0", [_campaign_state("c1", "error")])],
    )
    assert executor._all_campaigns_terminal(run, 0) is True


# ── _dispatch_ready_campaigns: Redis-rebuilding busy-loop guard ──────────────
# Regression coverage for the production incident where tick()'s `while changed:`
# fixed-point loop re-invoked _dispatch_ready_campaigns immediately (no backoff)
# whenever a campaign was reverted to "queued" mid-Phase-4 — busy-looping against
# Redis (~90 retries in 19s in one Lambda invocation) until it timed out or Redis
# finished rebuilding, exhausting the 5 reserved concurrency slots and triggering
# the vip-plans-throttles CloudWatch alarm.


def test_dispatch_reports_no_progress_when_campaign_reverted_to_queued():
    """A campaign left in 'queued' after Phase 4 (e.g. _RedisRebuildingError revert)
    must not report changed=True — that would busy-loop tick()'s fixed-point retry."""
    import executor

    plan = _make_plan(
        [
            _bucket_def("b0", [_campaign_def("c1")]),
        ]
    )
    run = _make_run(
        plan,
        [_bucket_state("b0", [_campaign_state("c1", "queued")])],
    )

    # No side_effect — mimics _start_one_campaign's real behavior on
    # _RedisRebuildingError: it reverts cs["status"] back to "queued" and returns.
    with (
        patch("executor.save_run"),
        patch("executor._start_one_campaign") as start,
        patch("boto3.client"),
    ):
        changed = executor._dispatch_ready_campaigns(run, plan, 0)

    start.assert_called_once()
    assert changed is False


def test_dispatch_reports_progress_when_some_campaigns_advance():
    """If at least one of the newly-ready campaigns advances past 'queued',
    changed=True — the fixed-point loop should keep dispatching the rest."""
    import executor

    plan = _make_plan(
        [
            _bucket_def("b0", [_campaign_def("c1"), _campaign_def("c2")]),
        ]
    )
    run = _make_run(
        plan,
        [_bucket_state("b0", [_campaign_state("c1", "queued"), _campaign_state("c2", "queued")])],
    )

    def _mixed_side_effect(_run, _plan, _bucket_index, campaign_index):
        # c1 (index 0) advances; c2 (index 1) hits Redis-rebuilding and reverts.
        if campaign_index == 0:
            _run["bucketStates"][0]["campaignStates"][0]["status"] = "running"

    with patch("executor.save_run"), patch("executor._start_one_campaign") as start:
        start.side_effect = _mixed_side_effect
        changed = executor._dispatch_ready_campaigns(run, plan, 0)

    assert start.call_count == 2
    assert changed is True


def test_dispatch_excludes_stalled_campaign_on_subsequent_call():
    """A campaign marked stalled by a prior call (same `while changed:` loop, same
    tick) must NOT be re-attempted on a later call with the same `stalled` set —
    even though it's still "queued" and would otherwise re-qualify in Phase 2."""
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c1")])])
    run = _make_run(plan, [_bucket_state("b0", [_campaign_state("c1", "queued")])])
    stalled: set[int] = set()

    with (
        patch("executor.save_run"),
        patch("executor._start_one_campaign") as start,
        patch("boto3.client"),
    ):
        first = executor._dispatch_ready_campaigns(run, plan, 0, stalled)
        second = executor._dispatch_ready_campaigns(run, plan, 0, stalled)

    assert first is False
    assert second is False
    assert stalled == {0}
    start.assert_called_once()  # NOT called again on the second pass


def test_dispatch_stalled_stage1_campaign_not_retried_while_dependency_chain_progresses():
    """A stage-1 campaign stuck in Redis-rebuilding must stop being retried once
    marked stalled, even while an UNRELATED dependency chain in the same bucket
    keeps the `while changed:` loop alive across multiple calls."""
    import executor

    plan = _make_plan(
        [
            _bucket_def(
                "b0",
                [
                    _campaign_def("c1"),  # stage-1, independent — stalls on Redis
                    _campaign_def("c2"),  # parent of the chain
                    _campaign_def("c3", depends_on=["c2"]),  # child, unlocks after c2
                ],
            ),
        ]
    )
    run = _make_run(
        plan,
        [
            _bucket_state(
                "b0",
                [
                    _campaign_state("c1", "queued"),
                    _campaign_state("c2", "queued"),
                    _campaign_state("c3", "queued"),
                ],
            )
        ],
    )
    stalled: set[int] = set()

    def _side_effect(_run, _plan, _bucket_index, campaign_index):
        if campaign_index == 0:
            return  # c1: Redis-rebuilding revert — stays "queued" (mocked default)
        # c2 (and later c3) advance for real.
        _run["bucketStates"][0]["campaignStates"][campaign_index]["status"] = "running"

    with (
        patch("executor.save_run"),
        patch("executor._start_one_campaign") as start,
        patch("boto3.client"),
    ):
        start.side_effect = _side_effect
        # Pass 1: c1 and c2 are newly_ready (c3 still blocked). c1 stalls, c2 advances.
        changed = executor._dispatch_ready_campaigns(run, plan, 0, stalled)
        assert changed is True
        assert stalled == {0}
        assert start.call_count == 2  # c1, c2

        # Pass 2 (same tick, loop continues because changed was True): c3 now
        # unblocked by c2="running"->terminal isn't quite right for this dep model,
        # so simulate c2 reaching a terminal state before pass 2.
        run["bucketStates"][0]["campaignStates"][1]["status"] = "completed"
        changed = executor._dispatch_ready_campaigns(run, plan, 0, stalled)

    # c1 must NOT be retried again — only c3 (newly unblocked by c2) is attempted.
    assert start.call_count == 3
    call_indices = [c.args[3] for c in start.call_args_list]
    assert call_indices == [0, 1, 2]
    assert changed is True  # c3 advanced


# ── _dispatch_ready_campaigns: stage-1 auto-start ────────────────────────────


def test_dispatch_stage1_starts_immediately():
    """Stage-1 (empty dependsOn) campaigns in bucket 0 start without waiting."""
    import executor

    plan = _make_plan(
        [
            _bucket_def("b0", [_campaign_def("c1"), _campaign_def("c2")]),
        ]
    )
    run = _make_run(
        plan,
        [
            _bucket_state("b0", [_campaign_state("c1"), _campaign_state("c2")]),
        ],
    )

    with patch("executor.save_run"), patch("executor._start_one_campaign") as start:
        start.side_effect = _started_mock(run, 0)
        changed = executor._dispatch_ready_campaigns(run, plan, 0)

    assert changed is True
    assert start.call_count == 2


def test_dispatch_stage2_waits_for_parent():
    """Stage-2 (dependsOn populated) stays queued while parent is still running."""
    import executor

    plan = _make_plan(
        [
            _bucket_def(
                "b0", [_campaign_def("c1"), _campaign_def("c2", depends_on=["c1"])]
            ),
        ]
    )
    c1 = _campaign_state("c1", "running")
    c2 = _campaign_state("c2", "queued")
    run = _make_run(plan, [_bucket_state("b0", [c1, c2])])

    with patch("executor._start_one_campaign") as start:
        changed = executor._dispatch_ready_campaigns(run, plan, 0)

    start.assert_not_called()
    assert changed is False


def test_dispatch_stage2_starts_after_parent_completes():
    """Stage-2 is dispatched once its parent reaches 'completed'."""
    import executor

    plan = _make_plan(
        [
            _bucket_def(
                "b0", [_campaign_def("c1"), _campaign_def("c2", depends_on=["c1"])]
            ),
        ]
    )
    c1 = _campaign_state("c1", "completed")
    c2 = _campaign_state("c2", "queued")
    run = _make_run(plan, [_bucket_state("b0", [c1, c2])])

    with patch("executor.save_run"), patch("executor._start_one_campaign") as start:
        start.side_effect = _started_mock(run, 0)
        changed = executor._dispatch_ready_campaigns(run, plan, 0)

    start.assert_called_once_with(run, plan, 0, 1)
    assert changed is True


def test_dispatch_dependent_starts_after_parent_error():
    """Errored parent unblocks the dependent — it always attempts to start."""
    import executor

    plan = _make_plan(
        [
            _bucket_def(
                "b0", [_campaign_def("c1"), _campaign_def("c2", depends_on=["c1"])]
            ),
        ]
    )
    c1 = _campaign_state("c1", "error")
    c2 = _campaign_state("c2", "queued")
    run = _make_run(plan, [_bucket_state("b0", [c1, c2])])

    with patch("executor.save_run"), patch("executor._start_one_campaign") as start:
        start.side_effect = _started_mock(run, 0)
        changed = executor._dispatch_ready_campaigns(run, plan, 0)

    start.assert_called_once_with(run, plan, 0, 1)
    assert changed is True
    # c2 status is set to "creating" during Phase 3 claim, then advanced by
    # _start_one_campaign (mocked) — assert it was dispatched, not cancelled.
    assert c2.get("exitReason") != "parent_cancelled"


def test_dispatch_dependent_starts_after_parent_cancelled():
    """Cancelled parent unblocks the dependent — no cascade-cancel."""
    import executor

    plan = _make_plan(
        [
            _bucket_def(
                "b0", [_campaign_def("c1"), _campaign_def("c2", depends_on=["c1"])]
            ),
        ]
    )
    c1 = _campaign_state("c1", "cancelled")
    c2 = _campaign_state("c2", "queued")
    run = _make_run(plan, [_bucket_state("b0", [c1, c2])])

    with patch("executor.save_run"), patch("executor._start_one_campaign") as start:
        start.side_effect = _started_mock(run, 0)
        executor._dispatch_ready_campaigns(run, plan, 0)

    start.assert_called_once_with(run, plan, 0, 1)
    assert c2.get("exitReason") != "parent_cancelled"


def test_dispatch_dependent_starts_after_parent_expired():
    """Expired parent unblocks the dependent — no cascade-cancel."""
    import executor

    plan = _make_plan(
        [
            _bucket_def(
                "b0", [_campaign_def("c1"), _campaign_def("c2", depends_on=["c1"])]
            ),
        ]
    )
    c1 = _campaign_state("c1", "expired")
    c2 = _campaign_state("c2", "queued")
    run = _make_run(plan, [_bucket_state("b0", [c1, c2])])

    with patch("executor.save_run"), patch("executor._start_one_campaign") as start:
        start.side_effect = _started_mock(run, 0)
        executor._dispatch_ready_campaigns(run, plan, 0)

    start.assert_called_once_with(run, plan, 0, 1)
    assert c2.get("exitReason") != "parent_cancelled"


# ── _dispatch_ready_campaigns: cross-bucket deps ─────────────────────────────


def test_dispatch_cross_bucket_dep_waits_for_previous_bucket_campaign():
    """Campaign in bucket 1 with dependsOn=[c0] waits until c0 is completed."""
    import executor

    plan = _make_plan(
        [
            _bucket_def("b0", [_campaign_def("c0")]),
            _bucket_def("b1", [_campaign_def("c1", depends_on=["c0"])]),
        ]
    )
    c0 = _campaign_state("c0", "running")
    c1 = _campaign_state("c1", "queued")
    run = _make_run(
        plan,
        [
            _bucket_state("b0", [c0], status="running"),
            _bucket_state("b1", [c1], status="running"),
        ],
    )

    with patch("executor._start_one_campaign") as start:
        changed = executor._dispatch_ready_campaigns(run, plan, 1)

    start.assert_not_called()
    assert changed is False


def test_dispatch_cross_bucket_dep_starts_after_parent_completes():
    """Campaign in bucket 1 starts once c0 in bucket 0 is completed."""
    import executor

    plan = _make_plan(
        [
            _bucket_def("b0", [_campaign_def("c0")]),
            _bucket_def("b1", [_campaign_def("c1", depends_on=["c0"])]),
        ]
    )
    c0 = _campaign_state("c0", "completed")
    c1 = _campaign_state("c1", "queued")
    run = _make_run(
        plan,
        [
            _bucket_state("b0", [c0], status="completed"),
            _bucket_state("b1", [c1], status="running"),
        ],
    )

    with patch("executor.save_run"), patch("executor._start_one_campaign") as start:
        start.side_effect = _started_mock(run, 1)
        changed = executor._dispatch_ready_campaigns(run, plan, 1)

    start.assert_called_once_with(run, plan, 1, 0)
    assert changed is True


def test_dispatch_cross_bucket_dep_starts_after_parent_cancelled():
    """Cross-bucket dep starts when parent campaign is cancelled (no cascade)."""
    import executor

    plan = _make_plan(
        [
            _bucket_def("b0", [_campaign_def("c0")]),
            _bucket_def("b1", [_campaign_def("c1", depends_on=["c0"])]),
        ]
    )
    c0 = _campaign_state("c0", "cancelled")
    c1 = _campaign_state("c1", "queued")
    run = _make_run(
        plan,
        [
            _bucket_state("b0", [c0], status="completed"),
            _bucket_state("b1", [c1], status="running"),
        ],
    )

    with patch("executor.save_run"), patch("executor._start_one_campaign") as start:
        start.side_effect = _started_mock(run, 1)
        changed = executor._dispatch_ready_campaigns(run, plan, 1)

    start.assert_called_once_with(run, plan, 1, 0)
    assert changed is True
    assert c1.get("exitReason") != "parent_cancelled"


def test_dispatch_cross_bucket_dep_starts_after_parent_error():
    """Cross-bucket dep starts when parent campaign errored (no cascade)."""
    import executor

    plan = _make_plan(
        [
            _bucket_def("b0", [_campaign_def("c0")]),
            _bucket_def("b1", [_campaign_def("c1", depends_on=["c0"])]),
        ]
    )
    c0 = _campaign_state("c0", "error")
    c1 = _campaign_state("c1", "queued")
    run = _make_run(
        plan,
        [
            _bucket_state("b0", [c0], status="completed"),
            _bucket_state("b1", [c1], status="running"),
        ],
    )

    with patch("executor.save_run"), patch("executor._start_one_campaign") as start:
        start.side_effect = _started_mock(run, 1)
        changed = executor._dispatch_ready_campaigns(run, plan, 1)

    start.assert_called_once_with(run, plan, 1, 0)
    assert changed is True


# ── Sequential bucket: stage-1 waits for previous bucket ─────────────────────


def test_dispatch_sequential_stage1_waits_for_previous_bucket():
    """Stage-1 (empty dependsOn) campaign in sequential bucket 1 waits for bucket 0 to complete."""
    import executor

    plan = _make_plan(
        [
            _bucket_def("b0", [_campaign_def("c0")]),
            _bucket_def("b1", [_campaign_def("c1")]),
        ]
    )
    run = _make_run(
        plan,
        [
            _bucket_state("b0", [_campaign_state("c0", "running")], status="running"),
            _bucket_state("b1", [_campaign_state("c1", "queued")], status="running"),
        ],
    )

    with patch("executor._start_one_campaign") as start:
        executor._dispatch_ready_campaigns(run, plan, 1)

    start.assert_not_called()


def test_dispatch_sequential_stage1_starts_when_previous_bucket_done():
    """Stage-1 in sequential bucket 1 starts once bucket 0 is completed."""
    import executor

    plan = _make_plan(
        [
            _bucket_def("b0", [_campaign_def("c0")]),
            _bucket_def("b1", [_campaign_def("c1")]),
        ]
    )
    run = _make_run(
        plan,
        [
            _bucket_state(
                "b0", [_campaign_state("c0", "completed")], status="completed"
            ),
            _bucket_state("b1", [_campaign_state("c1", "queued")], status="running"),
        ],
    )

    with patch("executor.save_run"), patch("executor._start_one_campaign") as start:
        start.side_effect = _started_mock(run, 1)
        changed = executor._dispatch_ready_campaigns(run, plan, 1)

    start.assert_called_once()
    assert changed is True


# ── Parallel bucket ───────────────────────────────────────────────────────────


def test_dispatch_parallel_bucket_starts_immediately():
    """Stage-1 campaign in a parallel bucket starts without waiting for previous bucket."""
    import executor

    plan = _make_plan(
        [
            _bucket_def("b0", [_campaign_def("c0")]),
            _bucket_def("b1", [_campaign_def("c1")], parallel=True),
        ]
    )
    run = _make_run(
        plan,
        [
            _bucket_state("b0", [_campaign_state("c0", "running")], status="running"),
            _bucket_state("b1", [_campaign_state("c1", "queued")], status="running"),
        ],
    )

    with patch("executor.save_run"), patch("executor._start_one_campaign") as start:
        start.side_effect = _started_mock(run, 1)
        changed = executor._dispatch_ready_campaigns(run, plan, 1)

    start.assert_called_once()
    assert changed is True


# ── tick: stale / terminal guard ─────────────────────────────────────────────


def test_tick_stale_when_bucket_not_active():
    """tick for a bucket whose status is 'queued' is a stale tick."""
    import executor

    plan = _make_plan(
        [
            _bucket_def("b0", [_campaign_def("c0")]),
            _bucket_def("b1", [_campaign_def("c1")]),
        ]
    )
    run = _make_run(
        plan,
        [
            _bucket_state("b0", [_campaign_state("c0", "running")], status="running"),
            _bucket_state("b1", [_campaign_state("c1", "queued")], status="queued"),
        ],
    )

    with (
        patch("executor.get_run", return_value=run),
        patch("executor._delete_bucket_schedule_safe"),
    ):
        result = executor.tick("plan-1", "run-1", 1)

    assert result["reason"] == "stale_tick"


def test_tick_already_terminal_run():
    """tick returns already_terminal when run.status != running."""
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    run = _make_run(
        plan,
        [_bucket_state("b0", [_campaign_state("c0", "completed")], status="completed")],
        status="completed",
    )

    with (
        patch("executor.get_run", return_value=run),
        patch("executor._delete_bucket_schedule_safe"),
    ):
        result = executor.tick("plan-1", "run-1", 0)

    assert result["reason"] == "already_terminal"


# ── tick: status-based all-terminal advances bucket ──────────────────────────


def test_tick_advances_when_all_campaigns_terminal():
    """tick calls _advance_bucket when all campaigns reach a terminal state."""
    import executor

    plan = _make_plan(
        [
            _bucket_def("b0", [_campaign_def("c0")]),
            _bucket_def("b1", [_campaign_def("c1")]),
        ]
    )
    cs = _campaign_state("c0", "running", connect_id="conn-0")
    run = _make_run(
        plan,
        [
            _bucket_state("b0", [cs]),
            _bucket_state("b1", [_campaign_state("c1")], status="queued"),
        ],
    )

    def poll_to_completed(campaign_state):
        campaign_state["status"] = "completed"
        campaign_state["exitReason"] = "completed"

    with (
        patch("executor.get_run", return_value=run),
        patch("executor._poll_campaign_state", side_effect=poll_to_completed),
        patch("executor._advance_bucket") as advance,
        patch("executor._past_daily_cutoff", return_value=False),
        patch("executor.save_run"),
    ):
        result = executor.tick("plan-1", "run-1", 0)

    advance.assert_called_once()
    assert result["reason"] == "advanced"


def test_tick_does_not_advance_when_campaigns_still_running():
    """tick does NOT advance when a campaign is still running."""
    import executor

    plan = _make_plan(
        [
            _bucket_def("b0", [_campaign_def("c0"), _campaign_def("c1")]),
        ]
    )
    cs0 = _campaign_state("c0", "completed")
    cs1 = _campaign_state("c1", "running", connect_id="conn-1")
    run = _make_run(plan, [_bucket_state("b0", [cs0, cs1])])

    with (
        patch("executor.get_run", return_value=run),
        patch("executor._poll_campaign_state"),
        patch("executor._advance_bucket") as advance,
        patch("executor._past_daily_cutoff", return_value=False),
        patch("executor.save_run"),
    ):
        executor.tick("plan-1", "run-1", 0)

    advance.assert_not_called()


def test_tick_redis_rebuilding_does_not_busy_loop_within_one_invocation():
    """End-to-end regression for the production incident: a queued campaign that
    hits _RedisRebuildingError (mocked here via _start_one_campaign leaving status
    untouched — its real behavior on that error) must be attempted exactly ONCE per
    tick() call, not retried in a tight loop until the Lambda times out.

    Real incident: RequestId 4dd76121-... on 2026-08-12T18:06:37Z — _start_one_campaign
    was called ~90 times in 19s inside a single Lambda invocation because tick()'s
    `while changed:` fixed-point loop had no way to know the campaign hadn't advanced.
    """
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    run = _make_run(plan, [_bucket_state("b0", [_campaign_state("c0", "queued")])])

    with (
        patch("executor.get_run", return_value=run),
        patch("executor._start_one_campaign") as start,  # no side_effect: stays "queued"
        patch("executor._dispatch_cross_bucket_ready", return_value=False),
        patch("executor._past_daily_cutoff", return_value=False),
        patch("executor.save_run"),
        patch("boto3.client"),
    ):
        executor.tick("plan-1", "run-1", 0)

    start.assert_called_once()  # NOT busy-looped


# ── tick: time-based expiry ───────────────────────────────────────────────────


def test_tick_expires_time_based_bucket():
    """tick calls _expire_bucket when elapsed >= duration_minutes."""
    import executor

    plan = _make_plan(
        [
            _bucket_def(
                "b0", [_campaign_def("c0")], run_mode="time_based", duration=10
            ),
            _bucket_def("b1", [_campaign_def("c1")]),
        ]
    )
    # Started 15 minutes ago → elapsed > 10 min
    past_iso = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    cs = _campaign_state("c0", "running", connect_id="conn-0")
    bs = _bucket_state("b0", [cs])
    bs["startedAt"] = past_iso
    run = _make_run(
        plan, [bs, _bucket_state("b1", [_campaign_state("c1")], status="queued")]
    )

    with (
        patch("executor.get_run", return_value=run),
        patch("executor._poll_campaign_state"),
        patch("executor._expire_bucket") as expire,
        patch("executor._prestart_next_bucket"),
        patch("executor._past_daily_cutoff", return_value=False),
        patch("executor.save_run"),
    ):
        result = executor.tick("plan-1", "run-1", 0)

    expire.assert_called_once_with(run, plan, 0)
    assert result["reason"] == "expired"


# ── tick: pre-start warming ───────────────────────────────────────────────────


def test_tick_triggers_prestart_when_within_window():
    """tick calls _prestart_next_bucket when elapsed >= duration - PRESTART_MINUTES."""
    import executor

    plan = _make_plan(
        [
            _bucket_def(
                "b0",
                [_campaign_def("c0")],
                run_mode="time_based",
                duration=20,
                prestart_next=True,
            ),
            _bucket_def("b1", [_campaign_def("c1")]),
        ]
    )
    # Started 16 minutes ago → within 5-min pre-start window (20 - 5 = 15)
    past_iso = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
    cs = _campaign_state("c0", "running", connect_id="conn-0")
    bs = _bucket_state("b0", [cs])
    bs["startedAt"] = past_iso
    run = _make_run(
        plan, [bs, _bucket_state("b1", [_campaign_state("c1")], status="queued")]
    )

    with (
        patch("executor.get_run", return_value=run),
        patch("executor._poll_campaign_state"),
        patch("executor._prestart_next_bucket") as prestart,
        patch("executor._next_bucket_warming", return_value=False),
        patch("executor._all_campaigns_terminal", return_value=False),
        patch("executor._dispatch_ready_campaigns", return_value=False),
        patch("executor._past_daily_cutoff", return_value=False),
        patch("executor.save_run"),
    ):
        executor.tick("plan-1", "run-1", 0)

    prestart.assert_called_once_with(run, plan, 0)


def test_tick_does_not_double_prestart_if_already_warming():
    """tick skips _prestart_next_bucket if next bucket is already warming."""
    import executor

    plan = _make_plan(
        [
            _bucket_def(
                "b0",
                [_campaign_def("c0")],
                run_mode="time_based",
                duration=20,
                prestart_next=True,
            ),
            _bucket_def("b1", [_campaign_def("c1")]),
        ]
    )
    past_iso = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
    bs = _bucket_state("b0", [_campaign_state("c0", "running", connect_id="conn-0")])
    bs["startedAt"] = past_iso
    run = _make_run(
        plan, [bs, _bucket_state("b1", [_campaign_state("c1")], status="warming")]
    )

    with (
        patch("executor.get_run", return_value=run),
        patch("executor._poll_campaign_state"),
        patch("executor._prestart_next_bucket") as prestart,
        patch("executor._next_bucket_warming", return_value=True),
        patch("executor._all_campaigns_terminal", return_value=False),
        patch("executor._dispatch_ready_campaigns", return_value=False),
        patch("executor._past_daily_cutoff", return_value=False),
        patch("executor.save_run"),
    ):
        executor.tick("plan-1", "run-1", 0)

    prestart.assert_not_called()


# ── _prestart_next_bucket ─────────────────────────────────────────────────────


def test_prestart_sets_next_bucket_to_warming():
    """_prestart_next_bucket sets next bucket state to 'warming'."""
    import executor

    plan = _make_plan(
        [
            _bucket_def(
                "b0", [_campaign_def("c0")], run_mode="time_based", duration=20
            ),
            _bucket_def("b1", [_campaign_def("c1")]),
        ]
    )
    run = _make_run(
        plan,
        [
            _bucket_state("b0", [_campaign_state("c0", "running")]),
            _bucket_state("b1", [_campaign_state("c1", "queued")], status="queued"),
        ],
    )

    with (
        patch(
            "executor._create_campaign_only",
            return_value=("conn-w", "seg-w", "arn:seg-w", True),
        ),
        patch("executor.save_run"),
    ):
        executor._prestart_next_bucket(run, plan, 0)

    assert run["bucketStates"][1]["status"] == "warming"


def test_prestart_only_creates_stage1_campaigns():
    """_prestart_next_bucket creates campaigns only for stage-1 (empty dependsOn)."""
    import executor

    plan = _make_plan(
        [
            _bucket_def(
                "b0", [_campaign_def("c0")], run_mode="time_based", duration=20
            ),
            _bucket_def(
                "b1", [_campaign_def("c1"), _campaign_def("c2", depends_on=["c1"])]
            ),
        ]
    )
    run = _make_run(
        plan,
        [
            _bucket_state("b0", [_campaign_state("c0", "running")]),
            _bucket_state(
                "b1", [_campaign_state("c1"), _campaign_state("c2")], status="queued"
            ),
        ],
    )

    with (
        patch(
            "executor._create_campaign_only",
            return_value=("conn-w", "seg-w", "arn-w", True),
        ) as create,
        patch("executor.save_run"),
    ):
        executor._prestart_next_bucket(run, plan, 0)

    # Only c1 has no dependsOn → only 1 create call
    assert create.call_count == 1
    assert run["bucketStates"][1]["campaignStates"][0]["status"] == "warming"
    assert run["bucketStates"][1]["campaignStates"][1]["status"] == "queued"


def test_prestart_skips_if_next_bucket_already_warming():
    """_prestart_next_bucket is a no-op when next bucket status != queued."""
    import executor

    plan = _make_plan(
        [
            _bucket_def(
                "b0", [_campaign_def("c0")], run_mode="time_based", duration=20
            ),
            _bucket_def("b1", [_campaign_def("c1")]),
        ]
    )
    run = _make_run(
        plan,
        [
            _bucket_state("b0", [_campaign_state("c0", "running")]),
            _bucket_state("b1", [_campaign_state("c1")], status="warming"),
        ],
    )

    with patch("executor._create_campaign_only") as create, patch("executor.save_run"):
        executor._prestart_next_bucket(run, plan, 0)

    create.assert_not_called()


def test_prestart_claim_save_persists_warming_before_campaigns():
    """_prestart_next_bucket saves 'warming' to DDB before creating any Connect campaigns."""
    import executor

    plan = _make_plan(
        [
            _bucket_def(
                "b0", [_campaign_def("c0")], run_mode="time_based", duration=20
            ),
            _bucket_def("b1", [_campaign_def("c1")]),
        ]
    )
    run = _make_run(
        plan,
        [
            _bucket_state("b0", [_campaign_state("c0", "running")]),
            _bucket_state("b1", [_campaign_state("c1")], status="queued"),
        ],
    )

    call_order = []

    def track_save(_run):
        call_order.append(("save", _run["bucketStates"][1]["status"]))

    def track_create(*_args, **_kwargs):
        call_order.append(("create",))
        return ("conn-w", "seg-w", "arn-w", True)

    with (
        patch("executor.save_run", side_effect=track_save),
        patch("executor._create_campaign_only", side_effect=track_create),
    ):
        executor._prestart_next_bucket(run, plan, 0)

    # Claim save ("warming") must come before the create call
    assert call_order[0] == ("save", "warming")
    assert ("create",) in call_order
    claim_idx = call_order.index(("save", "warming"))
    create_idx = call_order.index(("create",))
    assert claim_idx < create_idx


def test_prestart_mid_flight_save_persists_connect_id():
    """_prestart_next_bucket saves connectCampaignId immediately after _create_campaign_only."""
    import executor

    plan = _make_plan(
        [
            _bucket_def(
                "b0", [_campaign_def("c0")], run_mode="time_based", duration=20
            ),
            _bucket_def("b1", [_campaign_def("c1")]),
        ]
    )
    run = _make_run(
        plan,
        [
            _bucket_state("b0", [_campaign_state("c0", "running")]),
            _bucket_state("b1", [_campaign_state("c1")], status="queued"),
        ],
    )

    saved_ids = []

    def track_save(_run):
        cs = _run["bucketStates"][1]["campaignStates"][0]
        saved_ids.append(cs.get("connectCampaignId"))

    with (
        patch("executor.save_run", side_effect=track_save),
        patch(
            "executor._create_campaign_only",
            return_value=("conn-warm", "seg-w", "arn-w", True),
        ),
    ):
        executor._prestart_next_bucket(run, plan, 0)

    # Second save (mid-flight) must have the connect ID
    assert "conn-warm" in saved_ids


def test_prestart_claim_save_failure_reverts_to_queued():
    """_prestart_next_bucket reverts bucket to 'queued' and does not create campaigns if claim save fails."""
    import executor

    plan = _make_plan(
        [
            _bucket_def(
                "b0", [_campaign_def("c0")], run_mode="time_based", duration=20
            ),
            _bucket_def("b1", [_campaign_def("c1")]),
        ]
    )
    run = _make_run(
        plan,
        [
            _bucket_state("b0", [_campaign_state("c0", "running")]),
            _bucket_state("b1", [_campaign_state("c1")], status="queued"),
        ],
    )

    with (
        patch("executor.save_run", side_effect=Exception("DDB timeout")),
        patch("executor._create_campaign_only") as create,
    ):
        executor._prestart_next_bucket(run, plan, 0)

    create.assert_not_called()
    assert run["bucketStates"][1]["status"] == "queued"


# ── _expire_bucket ────────────────────────────────────────────────────────────


def test_expire_bucket_stops_running_and_cancels_queued():
    """_expire_bucket: running → expired, queued → cancelled, then advances."""
    import executor

    plan = _make_plan(
        [
            _bucket_def("b0", [_campaign_def("c0"), _campaign_def("c1")]),
            _bucket_def("b1", [_campaign_def("c2")]),
        ]
    )
    c0 = _campaign_state("c0", "running", connect_id="conn-0")
    c1 = _campaign_state("c1", "queued")
    run = _make_run(
        plan,
        [
            _bucket_state("b0", [c0, c1]),
            _bucket_state("b1", [_campaign_state("c2")], status="queued"),
        ],
    )

    with (
        patch("executor._safe_stop_campaign") as stop,
        patch("executor._advance_bucket") as advance,
    ):
        executor._expire_bucket(run, plan, 0)

    stop.assert_called_once_with("conn-0")
    assert c0["status"] == "expired"
    assert c0["exitReason"] == "expired"
    assert c1["status"] == "cancelled"
    assert c1["exitReason"] == "bucket_expired"
    advance.assert_called_once_with(run, plan, 0, reason="time_expired")


# ── _advance_bucket ───────────────────────────────────────────────────────────


def test_advance_bucket_starts_next_sequential():
    """_advance_bucket calls _start_bucket for the next sequential (non-parallel) bucket."""
    import executor

    plan = _make_plan(
        [
            _bucket_def("b0", [_campaign_def("c0")]),
            _bucket_def("b1", [_campaign_def("c1")]),
        ]
    )
    run = _make_run(
        plan,
        [
            _bucket_state("b0", [_campaign_state("c0", "completed")]),
            _bucket_state("b1", [_campaign_state("c1")], status="queued"),
        ],
    )

    with (
        patch("executor._delete_bucket_schedule_safe"),
        patch("executor._start_bucket") as start_next,
        patch("executor.save_run"),
    ):
        executor._advance_bucket(run, plan, 0, "all_campaigns_done")

    start_next.assert_called_once_with(run, 1)
    assert run["bucketStates"][0]["status"] == "completed"


def test_advance_bucket_activates_warming_next():
    """_advance_bucket calls _activate_warming_bucket when next bucket is pre-warmed."""
    import executor

    plan = _make_plan(
        [
            _bucket_def("b0", [_campaign_def("c0")], run_mode="time_based"),
            _bucket_def("b1", [_campaign_def("c1")]),
        ]
    )
    run = _make_run(
        plan,
        [
            _bucket_state("b0", [_campaign_state("c0", "completed")]),
            _bucket_state(
                "b1",
                [_campaign_state("c1", "warming", connect_id="conn-w")],
                status="warming",
            ),
        ],
    )

    with (
        patch("executor._delete_bucket_schedule_safe"),
        patch("executor._activate_warming_bucket") as activate,
        patch("executor.save_run"),
    ):
        executor._advance_bucket(run, plan, 0, "time_expired")

    activate.assert_called_once_with(run, plan, 1)


def test_advance_last_bucket_completes_run():
    """_advance_bucket sets run.status=completed when all buckets are done."""
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    run = _make_run(plan, [_bucket_state("b0", [_campaign_state("c0", "completed")])])

    with (
        patch("executor._delete_bucket_schedule_safe"),
        patch("executor.save_run"),
        patch("executor.unlock_plan_run") as unlock,
        patch("executor._maybe_loop"),
        patch("executor.start_run_chained") as chained,
    ):
        executor._advance_bucket(run, plan, 0, "all_campaigns_done")

    assert run["status"] == "completed"
    unlock.assert_called_once_with("plan-1")
    chained.assert_called_once_with("plan-1")


def test_advance_parallel_bucket_does_not_start_next():
    """_advance_bucket does not start the next bucket if it is parallel (already running)."""
    import executor

    plan = _make_plan(
        [
            _bucket_def("b0", [_campaign_def("c0")]),
            _bucket_def("b1", [_campaign_def("c1")], parallel=True),
        ]
    )
    run = _make_run(
        plan,
        [
            _bucket_state("b0", [_campaign_state("c0", "completed")]),
            _bucket_state("b1", [_campaign_state("c1", "running")], status="running"),
        ],
    )

    with (
        patch("executor._delete_bucket_schedule_safe"),
        patch("executor._start_bucket") as start_next,
        patch("executor._activate_warming_bucket") as activate,
        patch("executor.save_run"),
    ):
        executor._advance_bucket(run, plan, 0, "all_campaigns_done")

    start_next.assert_not_called()
    activate.assert_not_called()


def test_advance_parallel_bucket_completes_run_when_all_done():
    """When both parallel buckets complete (out-of-order), run is marked completed."""
    import executor

    plan = _make_plan(
        [
            _bucket_def("b0", [_campaign_def("c0")]),
            _bucket_def("b1", [_campaign_def("c1")], parallel=True),
        ]
    )
    # b0 advances last; b1 is already completed
    run = _make_run(
        plan,
        [
            _bucket_state("b0", [_campaign_state("c0", "completed")]),
            _bucket_state(
                "b1", [_campaign_state("c1", "completed")], status="completed"
            ),
        ],
    )

    with (
        patch("executor._delete_bucket_schedule_safe"),
        patch("executor.save_run"),
        patch("executor.unlock_plan_run"),
        patch("executor._maybe_loop"),
        patch("executor.start_run_chained") as chained,
    ):
        executor._advance_bucket(run, plan, 0, "all_campaigns_done")

    assert run["status"] == "completed"
    chained.assert_called_once_with("plan-1")


# ── abort_run ─────────────────────────────────────────────────────────────────


def test_abort_run_not_running_raises():
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    run = _make_run(
        plan, [_bucket_state("b0", [_campaign_state("c0")])], status="completed"
    )

    with patch("executor.get_run", return_value=run):
        with pytest.raises(ValueError, match="not running"):
            executor.abort_run("plan-1", "run-1")


def test_abort_run_stops_running_campaigns_in_all_buckets():
    """abort_run stops campaigns in ALL active buckets (handles parallel)."""
    import executor

    plan = _make_plan(
        [
            _bucket_def("b0", [_campaign_def("c0")]),
            _bucket_def("b1", [_campaign_def("c1")], parallel=True),
        ]
    )
    c0 = _campaign_state("c0", "running", connect_id="conn-0")
    c1 = _campaign_state("c1", "running", connect_id="conn-1")
    run = _make_run(
        plan,
        [
            _bucket_state("b0", [c0], status="running"),
            _bucket_state("b1", [c1], status="running"),
        ],
    )

    with (
        patch("executor.get_run", return_value=run),
        patch("executor._safe_stop_campaign") as stop,
        patch("executor._delete_bucket_schedule_safe"),
        patch("executor.save_run"),
        patch("executor.update_plan_pending_warmup"),
        patch("executor.unlock_plan_run"),
    ):
        result = executor.abort_run("plan-1", "run-1")

    assert stop.call_count == 2
    calls = {c[0][0] for c in stop.call_args_list}
    assert "conn-0" in calls
    assert "conn-1" in calls
    assert result["status"] == "aborted"


def test_abort_run_cancels_queued_and_warming_campaigns():
    """abort_run marks queued and warming campaigns as cancelled."""
    import executor

    plan = _make_plan(
        [
            _bucket_def(
                "b0", [_campaign_def("c0"), _campaign_def("c1"), _campaign_def("c2")]
            ),
        ]
    )
    c0 = _campaign_state("c0", "running", connect_id="conn-0")
    c1 = _campaign_state("c1", "queued")
    c2 = _campaign_state("c2", "warming", connect_id="conn-w")
    run = _make_run(plan, [_bucket_state("b0", [c0, c1, c2])])

    with (
        patch("executor.get_run", return_value=run),
        patch("executor._safe_stop_campaign"),
        patch("executor._safe_delete_campaign"),
        patch("executor._safe_delete_segment"),
        patch("executor._delete_bucket_schedule_safe"),
        patch("executor.save_run"),
        patch("executor.update_plan_pending_warmup"),
        patch("executor.unlock_plan_run"),
    ):
        executor.abort_run("plan-1", "run-1")

    assert c0["status"] == "cancelled"
    assert c0["exitReason"] == "aborted"
    assert c1["status"] == "cancelled"
    assert c1["exitReason"] == "aborted"
    assert c2["status"] == "cancelled"
    assert c2["exitReason"] == "aborted"


def test_abort_run_deletes_warming_connect_campaign():
    """abort_run deletes (not just stops) a warming Connect campaign."""
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    c0 = _campaign_state("c0", "warming", connect_id="conn-w")
    c0["segmentName"] = "seg-w"
    run = _make_run(plan, [_bucket_state("b0", [c0])])

    with (
        patch("executor.get_run", return_value=run),
        patch("executor._safe_stop_campaign") as stop,
        patch("executor._safe_delete_campaign") as delete_camp,
        patch("executor._safe_delete_segment") as delete_seg,
        patch("executor._delete_bucket_schedule_safe"),
        patch("executor.save_run"),
        patch("executor.update_plan_pending_warmup"),
        patch("executor.unlock_plan_run"),
    ):
        executor.abort_run("plan-1", "run-1")

    stop.assert_called_with("conn-w")
    delete_camp.assert_called_with("conn-w")
    delete_seg.assert_called_with("seg-w")


def test_abort_run_cancels_creating_campaigns():
    """abort_run must cancel campaigns in 'creating' state (two-phase claim in progress)."""
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0"), _campaign_def("c1")])])
    c0 = _campaign_state("c0", "running", connect_id="conn-0")
    c1 = _campaign_state("c1", "creating")  # stuck in two-phase claim
    run = _make_run(plan, [_bucket_state("b0", [c0, c1])])

    with (
        patch("executor.get_run", return_value=run),
        patch("executor._safe_stop_campaign"),
        patch("executor._safe_delete_campaign"),
        patch("executor._safe_delete_segment"),
        patch("executor._delete_bucket_schedule_safe"),
        patch("executor.save_run"),
        patch("executor.update_plan_pending_warmup"),
        patch("executor.unlock_plan_run"),
    ):
        executor.abort_run("plan-1", "run-1")

    assert c1["status"] == "cancelled"
    assert c1["exitReason"] == "aborted"


# ── start_run_chained ─────────────────────────────────────────────────────────


def test_start_run_chained_fires_downstream_plans():
    """start_run_chained starts all plans triggered by the upstream plan."""
    import executor

    downstream = {
        "planId": "plan-2",
        "trigger": {"type": "on_plan_complete", "planId": "plan-1", "repeat": True},
    }

    with (
        patch("executor.find_plans_by_trigger_planid", return_value=[downstream]),
        patch("executor.start_run") as start,
        patch("executor.update_plan_trigger"),
    ):
        executor.start_run_chained("plan-1")

    start.assert_called_once_with("plan-2", triggered_by="chained")


def test_start_run_chained_resets_trigger_when_repeat_false():
    """start_run_chained resets trigger to manual after firing when repeat=False."""
    import executor

    downstream = {
        "planId": "plan-2",
        "trigger": {"type": "on_plan_complete", "planId": "plan-1", "repeat": False},
    }

    with (
        patch("executor.find_plans_by_trigger_planid", return_value=[downstream]),
        patch("executor.start_run"),
        patch("executor.update_plan_trigger") as reset,
    ):
        executor.start_run_chained("plan-1")

    reset.assert_called_once_with("plan-2", {"type": "manual"})


def test_start_run_chained_does_not_reset_when_repeat_true():
    """start_run_chained does NOT reset trigger when repeat=True."""
    import executor

    downstream = {
        "planId": "plan-2",
        "trigger": {"type": "on_plan_complete", "planId": "plan-1", "repeat": True},
    }

    with (
        patch("executor.find_plans_by_trigger_planid", return_value=[downstream]),
        patch("executor.start_run"),
        patch("executor.update_plan_trigger") as reset,
    ):
        executor.start_run_chained("plan-1")

    reset.assert_not_called()


def test_start_run_chained_continues_on_error():
    """start_run_chained swallows errors so one failure doesn't block other chains."""
    import executor

    d1 = {
        "planId": "plan-2",
        "trigger": {"type": "on_plan_complete", "planId": "plan-1", "repeat": True},
    }
    d2 = {
        "planId": "plan-3",
        "trigger": {"type": "on_plan_complete", "planId": "plan-1", "repeat": True},
    }

    call_log = []

    def _start(plan_id, triggered_by):
        call_log.append(plan_id)
        if plan_id == "plan-2":
            raise RuntimeError("boom")

    with (
        patch("executor.find_plans_by_trigger_planid", return_value=[d1, d2]),
        patch("executor.start_run", side_effect=_start),
        patch("executor.update_plan_trigger"),
    ):
        executor.start_run_chained("plan-1")

    assert "plan-3" in call_log  # plan-3 still fires despite plan-2 failure


# ── _maybe_loop ───────────────────────────────────────────────────────────────


def test_maybe_loop_fires_when_before_until():
    """_maybe_loop starts the plan again if current COT time is within loop window."""
    import executor

    plan = {
        "planId": "plan-1",
        "loop": {"startTime": "08:00", "endTime": "19:00"},
        "buckets": [],
    }

    _COT = timezone(timedelta(hours=-5))
    mock_now = datetime(
        2024, 1, 15, 10, 0, tzinfo=_COT
    )  # 10:00 COT — within 08:00–19:00

    with (
        patch("executor.get_plan", return_value=plan),
        patch("executor.get_latest_run", return_value=None),
        patch("executor.datetime") as mock_dt,
        patch("executor.start_run") as start,
    ):
        mock_dt.now.return_value = mock_now
        executor._maybe_loop("plan-1")

    start.assert_called_once_with("plan-1", triggered_by="loop")


def test_maybe_loop_skips_when_past_until():
    """_maybe_loop does not start again if current COT time is past loop endTime."""
    import executor

    plan = {
        "planId": "plan-1",
        "loop": {"startTime": "08:00", "endTime": "19:00"},
        "buckets": [],
    }

    _COT = timezone(timedelta(hours=-5))
    mock_now = datetime(2024, 1, 15, 20, 0, tzinfo=_COT)  # 20:00 COT — past end

    with (
        patch("executor.get_plan", return_value=plan),
        patch("executor.get_latest_run", return_value=None),
        patch("executor.datetime") as mock_dt,
        patch("executor.start_run") as start,
    ):
        mock_dt.now.return_value = mock_now
        executor._maybe_loop("plan-1")

    start.assert_not_called()


def test_maybe_loop_skips_when_no_loop():
    """_maybe_loop is a no-op when plan has no loop field."""
    import executor

    plan = {"planId": "plan-1", "loop": None, "buckets": []}

    with (
        patch("executor.get_plan", return_value=plan),
        patch("executor.get_latest_run", return_value=None),
        patch("executor.start_run") as start,
    ):
        executor._maybe_loop("plan-1")

    start.assert_not_called()


# ── Bug fix: _dispatch_cross_bucket_ready must NOT cascade-cancel ─────────────
#
# Root cause: when eager-start failed in bucket [bi], its campaign was set to
# "error"/"cancelled" in-memory, and the cascade block in the same loop iteration
# would immediately cascade-cancel campaigns in bucket [bi+1], [bi+2], … — wiping
# out the entire downstream chain before any bucket had a chance to run.


def test_cross_bucket_ready_no_cascade_when_parent_already_cancelled():
    """_dispatch_cross_bucket_ready must NOT cascade-cancel future queued campaigns.

    Even when a parent in a previous bucket is already cancelled, the function only
    handles eager starts (parent completed). Cascade happens in _dispatch_ready_campaigns
    when the target bucket actually activates.
    """
    import executor

    plan = _make_plan(
        [
            _bucket_def("b0", [_campaign_def("c0")]),
            _bucket_def("b1", [_campaign_def("c1", depends_on=["c0"])]),
            _bucket_def("b2", [_campaign_def("c2", depends_on=["c1"])]),
        ]
    )
    c0 = _campaign_state("c0", "cancelled")  # already cancelled
    c1 = _campaign_state("c1", "queued")
    c2 = _campaign_state("c2", "queued")
    run = _make_run(
        plan,
        [
            _bucket_state("b0", [c0], status="completed"),
            _bucket_state("b1", [c1], status="queued"),
            _bucket_state("b2", [c2], status="queued"),
        ],
    )

    with (
        patch("executor._start_one_campaign") as start,
        patch("executor._schedule_tick"),
    ):
        changed = executor._dispatch_cross_bucket_ready(run, plan, 0)

    # Cross-bucket eager dispatch does NOT cascade-cancel c1 or c2
    start.assert_not_called()
    assert c1["status"] == "queued", (
        "c1 must stay queued — cascade is _dispatch_ready_campaigns' job"
    )
    assert c2["status"] == "queued", (
        "c2 must stay queued — cascade is _dispatch_ready_campaigns' job"
    )
    assert changed is False


def test_cross_bucket_ready_no_cascade_from_inline_eager_start_failure():
    """Phase 2 removed: _dispatch_cross_bucket_ready never calls _start_one_campaign directly.

    c1 is left in "creating" state after save_run (Phase 1 claim). The next tick for
    that bucket will reset it to "queued" via the _dispatch_ready_campaigns recovery path.
    c2 must remain "queued" regardless — cascade-cancel is _dispatch_ready_campaigns' job.
    """
    import executor

    plan = _make_plan(
        [
            _bucket_def("b0", [_campaign_def("c0")]),
            _bucket_def("b1", [_campaign_def("c1", depends_on=["c0"])]),
            _bucket_def("b2", [_campaign_def("c2", depends_on=["c1"])]),
        ]
    )
    c0 = _campaign_state("c0", "completed")
    c1 = _campaign_state("c1", "queued")
    c2 = _campaign_state("c2", "queued")
    run = _make_run(
        plan,
        [
            _bucket_state("b0", [c0], status="completed"),
            _bucket_state("b1", [c1], status="queued"),
            _bucket_state("b2", [c2], status="queued"),
        ],
    )

    with (
        patch("executor._start_one_campaign") as mock_start,
        patch("executor._schedule_tick", return_value="sched-1"),
        patch("executor.save_run"),
    ):
        executor._dispatch_cross_bucket_ready(run, plan, 0)

    # Phase 2 removed — _start_one_campaign must never be called by this function
    mock_start.assert_not_called()
    # c1 is left in "creating" (Phase 1 claim persisted) — next tick resets to "queued"
    assert c1["status"] == "creating"
    # c2 must NOT be cascade-cancelled — it stays queued
    assert c2["status"] == "queued", (
        "Phase 2 removal must not cascade-cancel downstream campaigns"
    )


# ── Bug fix: force_start_campaign must reject completed buckets ───────────────
#
# A force-start in a completed bucket creates an orphan Connect campaign with no
# tick to poll its state, so DynamoDB never reflects completion.


def test_force_start_campaign_rejects_completed_bucket_non_cancelled():
    """force_start_campaign must raise ValueError for error campaigns in completed buckets."""
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    c0 = _campaign_state("c0", "error")
    run = _make_run(plan, [_bucket_state("b0", [c0], status="completed")])

    with (
        patch("executor.get_run", return_value=run),
        patch("executor.get_plan", return_value=plan),
    ):
        with pytest.raises(ValueError, match="completed"):
            executor.force_start_campaign("plan-1", "run-1", 0, 0)


def test_force_start_campaign_allows_completed_bucket_with_queued_campaign():
    """force_start_campaign must allow queued campaigns in completed buckets.

    This recovers the inconsistent state where a campaign stayed 'queued' in DDB
    while its bucket advanced (ConcurrentWriteError during cascade-cancel save).
    """
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    c0 = _campaign_state("c0", "queued")
    run = _make_run(plan, [_bucket_state("b0", [c0], status="completed")])

    with (
        patch("executor.get_run", return_value=run),
        patch("executor.get_plan", return_value=plan),
        patch("executor._schedule_tick", return_value="sched-1"),
        patch("executor._start_one_campaign"),
        patch("executor.save_run"),
    ):
        executor.force_start_campaign("plan-1", "run-1", 0, 0)

    assert run["bucketStates"][0]["status"] == "running", "bucket must be reactivated"
    assert c0["status"] == "queued"


def test_force_start_campaign_allows_completed_bucket_with_cancelled_campaign():
    """force_start_campaign must allow cancelled campaigns in completed buckets (parent_cancelled recovery)."""
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    c0 = _campaign_state("c0", "cancelled", exit_reason="parent_cancelled")
    run = _make_run(plan, [_bucket_state("b0", [c0], status="completed")])

    with (
        patch("executor.get_run", return_value=run),
        patch("executor.get_plan", return_value=plan),
        patch("executor._schedule_tick", return_value="sched-1"),
        patch("executor._start_one_campaign"),
        patch("executor.save_run"),
    ):
        executor.force_start_campaign("plan-1", "run-1", 0, 0)

    assert run["bucketStates"][0]["status"] == "running"
    assert c0["status"] == "queued"


def test_force_start_campaign_resets_cascade_cancelled_children():
    """force_start_campaign must reset parent_cancelled descendants so the dispatcher can resume them."""
    import executor

    # c0 → c1 → c2 (chain); c0 was cancelled, cascade-cancelled c1 and c2
    plan = _make_plan(
        [
            _bucket_def(
                "b0",
                [
                    _campaign_def("c0"),
                    _campaign_def("c1", depends_on=["c0"]),
                    _campaign_def("c2", depends_on=["c1"]),
                ],
            )
        ]
    )
    c0 = _campaign_state("c0", "cancelled", exit_reason="parent_cancelled")
    c1 = _campaign_state("c1", "cancelled", exit_reason="parent_cancelled")
    c2 = _campaign_state("c2", "cancelled", exit_reason="parent_cancelled")
    run = _make_run(plan, [_bucket_state("b0", [c0, c1, c2], status="running")])

    with (
        patch("executor.get_run", return_value=run),
        patch("executor.get_plan", return_value=plan),
        patch("executor._schedule_tick", return_value="sched-1"),
        patch("executor._start_one_campaign"),
        patch("executor.save_run"),
    ):
        executor.force_start_campaign("plan-1", "run-1", 0, 0)

    assert c1["status"] == "queued", "direct child must be reset to queued"
    assert c2["status"] == "queued", "grandchild must be reset to queued recursively"


# ── Bug fix: _advance_bucket rescues tick for eagerly-activated next bucket ───
#
# If _dispatch_cross_bucket_ready set the next bucket to "running" but failed to
# schedule a tick (exception), _advance_bucket must schedule it so campaigns are
# polled and the bucket can advance.


def test_advance_bucket_rescues_tick_for_already_running_next_bucket():
    """_advance_bucket must schedule a tick when the next bucket is 'running' but has no scheduleName."""
    import executor

    plan = _make_plan(
        [
            _bucket_def("b0", [_campaign_def("c0")]),
            _bucket_def("b1", [_campaign_def("c1", depends_on=["c0"])]),
        ]
    )
    c0 = _campaign_state("c0", "completed")
    c1 = _campaign_state("c1", "running", connect_id="conn-1")
    run = _make_run(
        plan,
        [
            _bucket_state("b0", [c0], status="running"),
            {**_bucket_state("b1", [c1], status="running"), "scheduleName": None},
        ],
    )

    with (
        patch("executor._schedule_tick", return_value="sched-rescue") as mock_sched,
        patch("executor._delete_bucket_schedule_safe"),
        patch("executor._fire_bucket_chains"),
        patch("executor.save_run"),
        patch("executor._safe_stop_campaign"),
        patch("executor._safe_delete_campaign"),
        patch("executor._safe_delete_segment"),
    ):
        executor._advance_bucket(run, plan, 0, reason="all_campaigns_done")

    mock_sched.assert_called_once_with(plan_id="plan-1", run_id="run-1", bucket_index=1)
    assert run["bucketStates"][1]["scheduleName"] == "sched-rescue"


# ── Bug fix: two-phase claim in _dispatch_cross_bucket_ready ──────────────────
#
# Before the fix, two concurrent ticks both called _start_one_campaign (Connect API)
# before saving DynamoDB state — creating duplicate campaigns.
# The fix: mark campaigns "creating" and call save_run BEFORE touching Connect.


def test_cross_bucket_ready_saves_claim_without_phase2():
    """Phase 2 removed: save_run (Phase 1 claim) is called but _start_one_campaign is NOT.

    Concurrent-tick safety is achieved by the claim (campaign → "creating") persisted to
    DynamoDB. The actual Connect campaign creation happens on the next tick for the bucket
    via the _dispatch_ready_campaigns "creating" → "queued" recovery path.
    """
    import executor

    call_order: list[str] = []

    plan = _make_plan(
        [
            _bucket_def("b0", [_campaign_def("c0")]),
            _bucket_def("b1", [_campaign_def("c1", depends_on=["c0"])]),
        ]
    )
    c0 = _campaign_state("c0", "completed")
    c1 = _campaign_state("c1", "queued")
    run = _make_run(
        plan,
        [
            _bucket_state("b0", [c0], status="completed"),
            _bucket_state("b1", [c1], status="queued"),
        ],
    )

    with (
        patch("executor.save_run", side_effect=lambda _: call_order.append("save_run")),
        patch(
            "executor._start_one_campaign",
            side_effect=lambda *a: call_order.append("start_one_campaign"),
        ),
        patch("executor._schedule_tick", return_value="sched-1"),
    ):
        executor._dispatch_cross_bucket_ready(run, plan, 0)

    assert "save_run" in call_order, "Phase 1 claim (save_run) must always be called"
    assert "start_one_campaign" not in call_order, (
        "Phase 2 (_start_one_campaign) must NOT be called — removed to eliminate 30s sleep in tick"
    )


def test_cross_bucket_ready_marks_creating_during_claim():
    """Campaign status must be 'creating' at the moment save_run is called (Phase 1).

    If save_run wins the optimistic lock, the campaign is visible as 'creating' to
    any other tick that reads the run before Phase 2 completes.
    """
    import executor

    status_at_save: list[str] = []

    plan = _make_plan(
        [
            _bucket_def("b0", [_campaign_def("c0")]),
            _bucket_def("b1", [_campaign_def("c1", depends_on=["c0"])]),
        ]
    )
    c0 = _campaign_state("c0", "completed")
    c1 = _campaign_state("c1", "queued")
    run = _make_run(
        plan,
        [
            _bucket_state("b0", [c0], status="completed"),
            _bucket_state("b1", [c1], status="queued"),
        ],
    )

    def capture(r):
        status_at_save.append(r["bucketStates"][1]["campaignStates"][0]["status"])

    with (
        patch("executor.save_run", side_effect=capture),
        patch("executor._start_one_campaign"),
        patch("executor._schedule_tick", return_value="sched-1"),
    ):
        executor._dispatch_cross_bucket_ready(run, plan, 0)

    assert status_at_save == ["creating"], (
        "Campaign must be 'creating' when save_run is called so concurrent ticks see the claim"
    )


# ── Bug fix: _EmptySegmentError is retried, not immediately cancelled ─────────
#
# Mid-rebuild Redis passes is_ready() (California leads load first) but northeastern
# state filters return 0 results — causing immediate cancellation before the fix.
# The fix: treat _EmptySegmentError like _RedisRebuildingError — retry with 30s delay.


def test_start_one_campaign_retries_generic_exception_before_succeeding():
    """_start_one_campaign retries generic segment-creation errors up to reconcileRetryLimit times."""
    import executor

    bucket = _bucket_def("b0", [_campaign_def("c0")])
    cs = _campaign_state("c0", "queued")
    run = _make_run(_make_plan([bucket]), [_bucket_state("b0", [cs])])

    attempt_count = {"n": 0}

    def fail_twice_then_succeed(b, c):
        attempt_count["n"] += 1
        if attempt_count["n"] < 3:
            raise RuntimeError("Transient segment error")
        return (
            "seg-name",
            "arn:aws:profiles:us-east-1:123:domains/d/segment-definitions/s",
        )

    with (
        patch("executor._create_segment", side_effect=fail_twice_then_succeed),
        patch(
            "executor._create_and_start_campaign", return_value=("conn-1", "camp-name")
        ),
    ):
        executor._start_one_campaign(run, run["planSnapshot"], 0, 0)

    assert attempt_count["n"] == 3, (
        "Should retry generic errors up to reconcileRetryLimit+1 times"
    )
    assert cs["status"] == "running", "Campaign must be running after eventual success"


def test_start_one_campaign_retries_empty_segment_before_cancelling():
    """_EmptySegmentError retries up to reconcileRetryLimit times across ticks before permanent cancel.

    On first attempt: status stays 'queued' with reconcileRetries=1 (will retry next tick).
    After exhausting retries: permanent cancel with skipped_empty.

    This handles partial Redis rebuilds where other-state leads arrive first — state-filtered
    queries return empty before all leads have been indexed.
    """
    import executor

    bucket = _bucket_def("b0", [_campaign_def("c0")])

    # First attempt: queued for retry
    cs = _campaign_state("c0", "queued")
    run = _make_run(_make_plan([bucket]), [_bucket_state("b0", [cs])])
    with patch(
        "executor._create_segment", side_effect=executor._EmptySegmentError("No leads")
    ):
        executor._start_one_campaign(run, run["planSnapshot"], 0, 0)
    assert cs["status"] == "queued", (
        "First empty segment: campaign stays queued for next tick"
    )
    assert cs.get("reconcileRetries") == 1

    # Attempts 2-5: still queued (reconcileRetryLimit default=5, so N < 5)
    for expected_retries in range(2, 6):
        with patch(
            "executor._create_segment",
            side_effect=executor._EmptySegmentError("No leads"),
        ):
            executor._start_one_campaign(run, run["planSnapshot"], 0, 0)
        assert cs["status"] == "queued"
        assert cs.get("reconcileRetries") == expected_retries

    # 6th attempt: retries exhausted (5 >= 5) → permanent cancel
    with patch(
        "executor._create_segment", side_effect=executor._EmptySegmentError("No leads")
    ):
        executor._start_one_campaign(run, run["planSnapshot"], 0, 0)
    assert cs["status"] == "cancelled", (
        "Empty segment after exhausting retries must cancel"
    )
    assert cs["exitReason"] == "skipped_empty"


def test_start_one_campaign_redis_rebuilding_leaves_queued():
    """_RedisRebuildingError leaves campaign 'queued' so the next tick retries cleanly.

    No time.sleep is called — returning early avoids holding the Lambda execution open
    and triggering ConcurrentWriteError races with sibling bucket ticks.
    """
    import executor

    bucket = _bucket_def("b0", [_campaign_def("c0")])
    cs = _campaign_state("c0", "queued")
    run = _make_run(_make_plan([bucket]), [_bucket_state("b0", [cs])])

    with patch(
        "executor._create_segment",
        side_effect=executor._RedisRebuildingError("rebuilding"),
    ):
        executor._start_one_campaign(run, run["planSnapshot"], 0, 0)

    assert cs["status"] == "queued", (
        "Redis rebuilding must leave campaign queued for next tick retry"
    )


# Bug: _start_one_campaign's segment-retry path logs via stdlib `logger`, discarded
# silently per S14-A (root-caused 2026-08-27, CT campaign investigation) — the sibling
# _prestart_next_bucket already migrated the identical retry logic to _slog.


def test_start_one_campaign_empty_segment_retry_uses_slog_not_stdlib_logger():
    """The empty-segment retry path must log via _slog so it survives to CloudWatch —
    stdlib `logger` calls here are discarded per the known S14-A root-logger filter,
    which is exactly why today's CT investigation had no log trail and needed
    DynamoDB archaeology instead."""
    import executor

    bucket = _bucket_def("b0", [_campaign_def("c0")])
    cs = _campaign_state("c0", "queued")
    run = _make_run(_make_plan([bucket]), [_bucket_state("b0", [cs])])

    with (
        patch(
            "executor._create_segment",
            side_effect=executor._EmptySegmentError("No leads"),
        ),
        patch("executor._slog") as mock_slog,
    ):
        executor._start_one_campaign(run, run["planSnapshot"], 0, 0)

    mock_slog.warn.assert_called_once()
    assert mock_slog.warn.call_args.args[0] == "start_one_campaign_empty_segment_retry"


def test_start_one_campaign_quota_exceeded_reverts_to_queued():
    """ServiceQuotaExceededException from Connect must revert to 'queued', not 'error'.

    The campaign will be retried automatically on the next tick.
    """
    import executor
    from botocore.exceptions import ClientError

    bucket = _bucket_def("b0", [_campaign_def("c0")])
    cs = _campaign_state("c0", "queued")
    run = _make_run(_make_plan([bucket]), [_bucket_state("b0", [cs])])

    quota_exc = ClientError(
        {"Error": {"Code": "ServiceQuotaExceededException", "Message": "quota"}},
        "CreateCampaign",
    )
    with (
        patch("executor._create_segment", return_value=("seg", "arn:seg")),
        patch("executor._create_and_start_campaign", side_effect=quota_exc),
        patch("executor._notify_sns"),
    ):
        executor._start_one_campaign(run, run["planSnapshot"], 0, 0)

    assert cs["status"] == "queued", "Quota exceeded must revert to queued for retry"
    assert cs.get("connectCampaignId") is None


def test_start_one_campaign_throttle_reverts_to_queued():
    """ThrottlingException from Connect must revert to 'queued', not 'error'."""
    import executor
    from botocore.exceptions import ClientError

    bucket = _bucket_def("b0", [_campaign_def("c0")])
    cs = _campaign_state("c0", "queued")
    run = _make_run(_make_plan([bucket]), [_bucket_state("b0", [cs])])

    throttle_exc = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "throttled"}},
        "CreateCampaign",
    )
    with (
        patch("executor._create_segment", return_value=("seg", "arn:seg")),
        patch("executor._create_and_start_campaign", side_effect=throttle_exc),
        patch("executor._notify_sns"),
    ):
        executor._start_one_campaign(run, run["planSnapshot"], 0, 0)

    assert cs["status"] == "queued", "Throttle must revert to queued for retry"


# Bug: a 4th revert-to-queued path (ClientError throttle/quota, above) never
# reset reconcileRetries — same sticky-field masking risk as BD-020, just a
# path BD-020 missed (root-caused 2026-08-27, second adversarial review round).


def test_start_one_campaign_throttle_resets_reconcile_retries():
    """A throttle/quota revert to queued must clear reconcileRetries too —
    otherwise a stale value from an earlier, unrelated empty-segment retry
    cycle survives into this revert and can mask a genuinely stuck campaign
    as legitimately waiting."""
    import executor
    from botocore.exceptions import ClientError

    bucket = _bucket_def("b0", [_campaign_def("c0")])
    cs = _campaign_state("c0", "queued")
    cs["reconcileRetries"] = 3  # stale, from an earlier finished retry cycle
    run = _make_run(_make_plan([bucket]), [_bucket_state("b0", [cs])])

    throttle_exc = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "throttled"}},
        "CreateCampaign",
    )
    with (
        patch("executor._create_segment", return_value=("seg", "arn:seg")),
        patch("executor._create_and_start_campaign", side_effect=throttle_exc),
        patch("executor._notify_sns"),
    ):
        executor._start_one_campaign(run, run["planSnapshot"], 0, 0)

    assert cs["status"] == "queued"
    assert cs["reconcileRetries"] == 0


def test_start_one_campaign_succeeds_when_segment_already_existed():
    """Campaign must start successfully when the segment was orphaned by a prior crashed Lambda.

    Scenario: previous tick created the CP segment but crashed before persisting
    connectCampaignId. Campaign is still 'queued'. _create_segment now handles the
    'already exists' ClientError and returns the existing ARN. This test verifies
    _start_one_campaign proceeds to 'running' in that case.
    """
    import executor

    existing_arn = "arn:aws:profile:us-east-1:123:domains/d/segment-definitions/11-5-26-NY-NL-2-1953"

    bucket = _bucket_def("b0", [_campaign_def("c0")])
    cs = _campaign_state("c0", "queued")
    run = _make_run(_make_plan([bucket]), [_bucket_state("b0", [cs])])

    with (
        patch(
            "executor._create_segment",
            return_value=("11-5-26-NY-NL-2-1953", existing_arn),
        ),
        patch(
            "executor._create_and_start_campaign", return_value=("conn-1", "camp-name")
        ),
    ):
        executor._start_one_campaign(run, run["planSnapshot"], 0, 0)

    assert cs["status"] == "running", (
        "Campaign must reach running even when segment already existed"
    )
    assert cs["segmentName"] == "11-5-26-NY-NL-2-1953"
    assert cs["connectCampaignId"] == "conn-1"


# ── native/branded queue collision alert ───────────────────────────────────────
#
# Alert-only, one-directional by design: a native campaign starting checks
# VipActiveBrandedCampaigns (indexed by queue ARN) for an already-active branded
# campaign on the same Connect queue. The reverse (branded checking for an
# already-active native campaign) has no equivalent cheap/indexed lookup and is
# out of scope — see docs/BUGLOG.md. Never blocks or alters the campaign start.


def test_start_one_campaign_native_alerts_when_branded_active_on_same_queue():
    import executor

    bucket = _bucket_def("b0", [_campaign_def("c0")])
    cs = _campaign_state("c0", "queued")
    run = _make_run(_make_plan([bucket]), [_bucket_state("b0", [cs])])

    mock_ddb = MagicMock()
    mock_ddb.query.return_value = {
        "Items": [{"campaignId": {"S": "bc-1"}}]
    }

    with (
        patch("executor._create_segment", return_value=("seg-1", "seg-arn-1")),
        patch("executor._create_and_start_campaign", return_value=("conn-1", "camp-name")),
        patch("executor._ACTIVE_BRANDED_CAMPAIGNS_TABLE", "VipActiveBrandedCampaigns"),
        patch("executor.CONNECT_INSTANCE_ID", "instance-1"),
        patch("executor._account_id", return_value="123456789012"),
        patch("executor._get_ddb_client", return_value=mock_ddb),
        patch("executor._notify_sns") as mock_notify,
    ):
        executor._start_one_campaign(run, run["planSnapshot"], 0, 0)

    assert cs["status"] == "running", "Collision alert must never block the native start"
    mock_ddb.query.assert_called_once()
    call_kwargs = mock_ddb.query.call_args.kwargs
    assert call_kwargs["ExpressionAttributeValues"][":pk"] == {
        "S": "QUEUE#arn:aws:connect:us-east-1:123456789012:instance/instance-1/queue/q-1"
    }
    mock_notify.assert_called_once()
    _, notify_kwargs = mock_notify.call_args
    assert "bc-1" in notify_kwargs.get("detail", notify_kwargs.get("attributes", {}).get("BrandedCampaignId", ""))


def test_start_one_campaign_native_no_alert_when_no_branded_active():
    import executor

    bucket = _bucket_def("b0", [_campaign_def("c0")])
    cs = _campaign_state("c0", "queued")
    run = _make_run(_make_plan([bucket]), [_bucket_state("b0", [cs])])

    mock_ddb = MagicMock()
    mock_ddb.query.return_value = {"Items": []}

    with (
        patch("executor._create_segment", return_value=("seg-1", "seg-arn-1")),
        patch("executor._create_and_start_campaign", return_value=("conn-1", "camp-name")),
        patch("executor._ACTIVE_BRANDED_CAMPAIGNS_TABLE", "VipActiveBrandedCampaigns"),
        patch("executor.CONNECT_INSTANCE_ID", "instance-1"),
        patch("executor._account_id", return_value="123456789012"),
        patch("executor._get_ddb_client", return_value=mock_ddb),
        patch("executor._notify_sns") as mock_notify,
    ):
        executor._start_one_campaign(run, run["planSnapshot"], 0, 0)

    assert cs["status"] == "running"
    mock_notify.assert_not_called()


def test_start_one_campaign_native_skips_collision_check_when_table_not_configured():
    import executor

    bucket = _bucket_def("b0", [_campaign_def("c0")])
    cs = _campaign_state("c0", "queued")
    run = _make_run(_make_plan([bucket]), [_bucket_state("b0", [cs])])

    mock_ddb = MagicMock()

    with (
        patch("executor._create_segment", return_value=("seg-1", "seg-arn-1")),
        patch("executor._create_and_start_campaign", return_value=("conn-1", "camp-name")),
        patch("executor._ACTIVE_BRANDED_CAMPAIGNS_TABLE", ""),
        patch("executor._get_ddb_client", return_value=mock_ddb),
        patch("executor._notify_sns") as mock_notify,
    ):
        executor._start_one_campaign(run, run["planSnapshot"], 0, 0)

    assert cs["status"] == "running"
    mock_ddb.query.assert_not_called()
    mock_notify.assert_not_called()


def test_start_one_campaign_native_collision_check_query_error_never_blocks_start():
    import executor

    bucket = _bucket_def("b0", [_campaign_def("c0")])
    cs = _campaign_state("c0", "queued")
    run = _make_run(_make_plan([bucket]), [_bucket_state("b0", [cs])])

    mock_ddb = MagicMock()
    mock_ddb.query.side_effect = RuntimeError("ProvisionedThroughputExceeded")

    with (
        patch("executor._create_segment", return_value=("seg-1", "seg-arn-1")),
        patch("executor._create_and_start_campaign", return_value=("conn-1", "camp-name")),
        patch("executor._ACTIVE_BRANDED_CAMPAIGNS_TABLE", "VipActiveBrandedCampaigns"),
        patch("executor.CONNECT_INSTANCE_ID", "instance-1"),
        patch("executor._account_id", return_value="123456789012"),
        patch("executor._get_ddb_client", return_value=mock_ddb),
        patch("executor._notify_sns") as mock_notify,
    ):
        executor._start_one_campaign(run, run["planSnapshot"], 0, 0)

    assert cs["status"] == "running", (
        "A failed collision check must never prevent the native campaign from starting"
    )
    mock_notify.assert_not_called()


# ── skip_campaign ─────────────────────────────────────────────────────────────
#
# skip_campaign marks a campaign cancelled(skipped) — transparent to cascade-cancel.
# Unlike force_stop (exitReason=manually_stopped), skip does NOT cascade-cancel children.


def test_skip_queued_campaign_marks_skipped():
    """skip_campaign on a queued campaign sets status=cancelled, exitReason=skipped."""
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0"), _campaign_def("c1")])])
    c0 = _campaign_state("c0", "queued")
    c1 = _campaign_state("c1", "queued")
    run = _make_run(plan, [_bucket_state("b0", [c0, c1])])

    with (
        patch("executor.get_run", return_value=run),
        patch("executor.get_plan", return_value=plan),
        patch("executor.save_run"),
        patch("executor._safe_stop_campaign"),
        patch("executor._dispatch_ready_campaigns", return_value=False),
        patch("executor._advance_bucket"),
    ):
        executor.skip_campaign("plan-1", "run-1", 0, 0)

    assert c0["status"] == "cancelled"
    assert c0["exitReason"] == "skipped", (
        "exitReason must be 'skipped' to be transparent to cascade-cancel"
    )


def test_skip_running_campaign_stops_connect_campaign():
    """skip_campaign on a running campaign calls _safe_stop_campaign before marking skipped."""
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    c0 = _campaign_state("c0", "running", connect_id="conn-abc")
    run = _make_run(plan, [_bucket_state("b0", [c0])])

    with (
        patch("executor.get_run", return_value=run),
        patch("executor.get_plan", return_value=plan),
        patch("executor.save_run"),
        patch("executor._safe_stop_campaign") as mock_stop,
        patch("executor._advance_bucket"),
    ):
        executor.skip_campaign("plan-1", "run-1", 0, 0)

    mock_stop.assert_called_once_with("conn-abc")
    assert c0["status"] == "cancelled"
    assert c0["exitReason"] == "skipped"


def test_skip_does_not_cascade_cancel_children():
    """Skipping a parent must NOT cascade-cancel children — exitReason=skipped is transparent.

    This is the core regression guard: force_stop sets exitReason=stopped which cascades;
    skip_campaign sets exitReason=skipped which the cascade-cancel logic ignores.
    """
    import executor

    plan = _make_plan(
        [
            _bucket_def("b0", [_campaign_def("c0")]),
            _bucket_def("b1", [_campaign_def("c1", depends_on=["c0"])]),
        ]
    )
    c0 = _campaign_state("c0", "queued")
    c1 = _campaign_state("c1", "queued")
    run = _make_run(
        plan,
        [
            _bucket_state("b0", [c0]),
            _bucket_state("b1", [c1], status="queued"),
        ],
    )

    with (
        patch("executor.get_run", return_value=run),
        patch("executor.get_plan", return_value=plan),
        patch("executor.save_run"),
        patch("executor._safe_stop_campaign"),
        patch("executor._advance_bucket"),
    ):
        executor.skip_campaign("plan-1", "run-1", 0, 0)

    assert c0["exitReason"] == "skipped"
    # c1 must NOT be cascade-cancelled — skipped parent is transparent
    assert c1["status"] == "queued", (
        "Children of a skipped campaign must remain queued — "
        "skipped parents are transparent to the cascade-cancel logic"
    )


def test_skip_unblocks_dependent_campaigns_in_same_bucket():
    """Skipping campaign c0 must unblock c1 (depends on c0) in the same bucket via dispatch."""
    import executor

    plan = _make_plan(
        [
            _bucket_def(
                "b0",
                [
                    _campaign_def("c0"),
                    _campaign_def("c1", depends_on=["c0"]),
                ],
            ),
        ]
    )
    c0 = _campaign_state("c0", "queued")
    c1 = _campaign_state("c1", "queued")
    run = _make_run(plan, [_bucket_state("b0", [c0, c1])])

    started = []

    def fake_start(r, p, bi, ci):
        started.append(ci)
        # Must advance campaign status so dispatch loop doesn't re-fire infinitely
        r["bucketStates"][bi]["campaignStates"][ci]["status"] = "running"

    with (
        patch("executor.get_run", return_value=run),
        patch("executor.get_plan", return_value=plan),
        patch("executor.save_run"),
        patch("executor._safe_stop_campaign"),
        patch("executor._advance_bucket"),
        patch("executor._start_one_campaign", side_effect=fake_start),
    ):
        executor.skip_campaign("plan-1", "run-1", 0, 0)

    # c1 (index 1) must have been dispatched after c0 was skipped
    assert 1 in started, (
        "Skipping c0 must trigger _dispatch_ready_campaigns and start c1"
    )


def test_skip_terminal_campaign_raises():
    """skip_campaign must reject campaigns that are already terminal (completed, error, expired)."""
    import executor

    for terminal_status in ("completed", "error", "expired"):
        plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
        cs = _campaign_state("c0", terminal_status)
        run = _make_run(plan, [_bucket_state("b0", [cs])])

        with (
            patch("executor.get_run", return_value=run),
            patch("executor.get_plan", return_value=plan),
        ):
            with pytest.raises(ValueError, match="terminal"):
                executor.skip_campaign("plan-1", "run-1", 0, 0)


def test_skip_retries_on_concurrent_write():
    """skip_campaign must retry on ConcurrentWriteError and succeed on the next attempt."""
    import executor
    from store import ConcurrentWriteError

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])

    save_n = {"n": 0}

    def fresh_run(*_a, **_k):
        # Return a new dict each time so the retry starts with unmodified state.
        cs = _campaign_state("c0", "queued")
        return _make_run(plan, [_bucket_state("b0", [cs])])

    def save_once_then_succeed(r):
        save_n["n"] += 1
        if save_n["n"] == 1:
            raise ConcurrentWriteError("simulated conflict")

    # Patch _all_campaigns_terminal to False so skip_campaign calls save_run directly
    # (rather than delegating to _advance_bucket which would swallow the mock).
    with (
        patch("executor.get_run", side_effect=fresh_run),
        patch("executor.get_plan", return_value=plan),
        patch("executor.save_run", side_effect=save_once_then_succeed),
        patch("executor._safe_stop_campaign"),
        patch("executor._all_campaigns_terminal", return_value=False),
    ):
        result = executor.skip_campaign("plan-1", "run-1", 0, 0)

    assert result is not None
    assert save_n["n"] == 2, "Must have retried once after ConcurrentWriteError"


def test_skip_retry_succeeds_silently_when_tick_already_advanced():
    """On retry, if the tick already set campaign to terminal, skip_campaign returns without error."""
    import executor
    from store import ConcurrentWriteError

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    cs_queued = _campaign_state("c0", "queued")
    cs_completed = _campaign_state("c0", "completed")
    run_first = _make_run(plan, [_bucket_state("b0", [cs_queued])])
    run_second = _make_run(plan, [_bucket_state("b0", [cs_completed])])

    reads = iter([run_first, run_second])

    # First attempt: _advance_bucket raises ConcurrentWriteError → retry
    # Second attempt: campaign is already completed (tick advanced it) → return silently
    with (
        patch("executor.get_run", side_effect=reads),
        patch("executor.get_plan", return_value=plan),
        patch("executor._dispatch_ready_campaigns", return_value=False),
        patch("executor._all_campaigns_terminal", return_value=True),
        patch("executor._advance_bucket", side_effect=ConcurrentWriteError("tick won")),
        patch("executor._safe_stop_campaign"),
    ):
        result = executor.skip_campaign("plan-1", "run-1", 0, 0)

    assert result is run_second


def test_dispatch_recovery_adopts_running_connect_campaign():
    """Phase 1 recovery with connectCampaignId must poll Connect and restore to running, not queue."""
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    cs = _campaign_state("c0", "creating")
    cs["connectCampaignId"] = "conn-orphan"
    run = _make_run(plan, [_bucket_state("b0", [cs], status="running")])

    with (
        patch("executor._get_campaign_state", return_value="Running"),
        patch("executor.save_run"),
        patch("executor._schedule_tick", return_value="sched-1"),
    ):
        executor._dispatch_ready_campaigns(run, plan, 0)

    assert cs["status"] == "running", (
        "Recovery must adopt the running Connect campaign, not reset to queued"
    )
    assert cs["connectCampaignId"] == "conn-orphan", (
        "connectCampaignId must be preserved"
    )


def test_dispatch_recovery_resets_to_queued_when_connect_terminated():
    """Phase 1 recovery must reset to queued if the Connect campaign already terminated."""
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    cs = _campaign_state("c0", "creating")
    cs["connectCampaignId"] = "conn-deleted"
    cs["segmentArn"] = "old-arn"
    run = _make_run(plan, [_bucket_state("b0", [cs], status="running")])

    with (
        patch("executor._get_campaign_state", return_value="Deleted"),
        patch("executor.save_run"),
        patch("executor._start_one_campaign"),
        patch("executor._schedule_tick", return_value="sched-1"),
    ):
        executor._dispatch_ready_campaigns(run, plan, 0)

    assert cs["status"] == "queued", (
        "Terminated Connect campaign must trigger clean reset to queued"
    )
    assert cs["connectCampaignId"] is None
    assert cs["segmentArn"] is None


def test_dispatch_recovery_skips_fresh_creating_claim_no_conn_id():
    """Phase 1 recovery must NOT reset a 'creating' campaign when the claim is fresh (< 5 min).

    Root cause of the force_start duplicate bug: force_start Phase 1 saves {creating, null},
    then a concurrent tick's Phase 1 Recovery resets it to 'queued', then Phase 2 sees LI
    completed and re-dispatches NJ → duplicate Connect campaign.
    The fix: creatingAt timestamp gates the reset — fresh claims are skipped.
    """
    import executor
    from datetime import datetime, timezone

    plan = _make_plan(
        [
            _bucket_def(
                "b0", [_campaign_def("c0"), _campaign_def("c1", depends_on=["c0"])]
            ),
        ]
    )
    # c1 is "creating" with no conn_id but a fresh timestamp (force_start in progress)
    c0 = _campaign_state("c0", "completed")
    c1 = _campaign_state("c1", "creating")
    c1["creatingAt"] = datetime.now(timezone.utc).isoformat()  # just now
    run = _make_run(plan, [_bucket_state("b0", [c0, c1], status="running")])

    with patch("executor.save_run"), patch("executor._start_one_campaign") as start:
        executor._dispatch_ready_campaigns(run, plan, 0)

    # c1 must remain "creating" — force_start is still in flight
    assert c1["status"] == "creating", (
        "Fresh creating claim must not be reset by Phase 1 Recovery"
    )
    start.assert_not_called()


def test_dispatch_recovery_resets_stale_creating_claim_no_conn_id():
    """Phase 1 recovery DOES reset a 'creating' campaign when the claim is stale (> 5 min).

    If force_start crashed before the mid-flight save, the claim will be old enough to reset.
    """
    import executor
    from datetime import datetime, timezone, timedelta

    plan = _make_plan(
        [
            _bucket_def(
                "b0", [_campaign_def("c0"), _campaign_def("c1", depends_on=["c0"])]
            ),
        ]
    )
    c0 = _campaign_state("c0", "completed")
    c1 = _campaign_state("c1", "creating")
    # Claim from 10 minutes ago — stale
    c1["creatingAt"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    run = _make_run(plan, [_bucket_state("b0", [c0, c1], status="running")])

    with patch("executor.save_run"), patch("executor._start_one_campaign") as start:
        executor._dispatch_ready_campaigns(run, plan, 0)

    # c1 should be reset to queued and then re-dispatched
    start.assert_called_once()


def test_dispatch_recovery_resets_creating_no_conn_id_when_no_timestamp():
    """Phase 1 recovery resets 'creating + no conn_id' when creatingAt is absent (legacy records)."""
    import executor

    plan = _make_plan(
        [
            _bucket_def(
                "b0", [_campaign_def("c0"), _campaign_def("c1", depends_on=["c0"])]
            ),
        ]
    )
    c0 = _campaign_state("c0", "completed")
    c1 = _campaign_state("c1", "creating")
    # No creatingAt field — old record, treat as stale
    run = _make_run(plan, [_bucket_state("b0", [c0, c1], status="running")])

    with patch("executor.save_run"), patch("executor._start_one_campaign") as start:
        executor._dispatch_ready_campaigns(run, plan, 0)

    start.assert_called_once()


def test_tick_force_finishes_when_working_hours_end_reached():
    """tick force-finishes run when _now_cot_hhmm() >= workingHours.endTime."""
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    plan["workingHours"] = {
        "days": ["MON", "TUE", "WED", "THU", "FRI"],
        "startTime": "08:00",
        "endTime": "17:00",
    }
    cs = _campaign_state("c0", "running", connect_id="conn-0")
    run = _make_run(plan, [_bucket_state("b0", [cs])])

    with (
        patch("executor.get_run", return_value=run),
        patch("executor.get_plan", return_value=plan),
        patch("executor._now_cot_hhmm", return_value=17 * 60),
        patch("executor._force_finish_internal") as mock_finish,
        patch("executor._past_daily_cutoff", return_value=False),
    ):
        result = executor.tick("plan-1", "run-1", 0)

    mock_finish.assert_called_once()
    assert result.get("reason") == "working_hours_cutoff"


def test_tick_does_not_cutoff_before_working_hours_end():
    """tick does NOT force-finish when current COT time is before workingHours.endTime."""
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    plan["workingHours"] = {
        "days": ["MON", "TUE", "WED", "THU", "FRI"],
        "startTime": "08:00",
        "endTime": "17:00",
    }
    cs = _campaign_state("c0", "running", connect_id="conn-0")
    run = _make_run(plan, [_bucket_state("b0", [cs])])

    with (
        patch("executor.get_run", return_value=run),
        patch("executor.get_plan", return_value=plan),
        patch("executor._now_cot_hhmm", return_value=16 * 60 + 59),
        patch("executor._force_finish_internal") as mock_finish,
        patch("executor._poll_campaign_state"),
        patch("executor._past_daily_cutoff", return_value=False),
        patch("executor.save_run"),
    ):
        result = executor.tick("plan-1", "run-1", 0)

    mock_finish.assert_not_called()
    assert result.get("reason") != "working_hours_cutoff"


# ── Bug A fix: _dispatch_cross_bucket_ready Phase 2 removed ─────────────────


def test_dispatch_cross_bucket_no_phase2():
    """After Phase 1 (save_run), _dispatch_cross_bucket_ready must NOT call _start_one_campaign.

    The newly-activated bucket's campaign is left in "creating" state.
    The next tick for that bucket resets it to "queued" via _dispatch_ready_campaigns recovery,
    starting the campaign within ~1 minute without any 30s sleep inside a tick handler.
    """
    import executor

    plan = _make_plan(
        [
            _bucket_def("b0", [_campaign_def("c0")]),
            _bucket_def("b1", [_campaign_def("c1", depends_on=["c0"])]),
        ]
    )
    c0 = _campaign_state("c0", "completed")
    c1 = _campaign_state("c1", "queued")
    run = _make_run(
        plan,
        [
            _bucket_state("b0", [c0], status="completed"),
            _bucket_state("b1", [c1], status="queued"),
        ],
    )

    with (
        patch("executor.save_run"),
        patch("executor._start_one_campaign") as mock_start,
        patch("executor._schedule_tick", return_value="sched-1"),
    ):
        result = executor._dispatch_cross_bucket_ready(run, plan, 0)

    assert result is True, "Must return True (state changed)"
    mock_start.assert_not_called()
    # Campaign remains "creating" — next tick for the bucket resets it to "queued"
    assert c1["status"] == "creating", (
        "Campaign must stay 'creating' after Phase 1 so the next tick's recovery path starts it"
    )


# ── Bug B fix: _advance_bucket reschedules on ConcurrentWriteError ────────────


def test_advance_bucket_reschedules_on_concurrent_write():
    """If save_run raises ConcurrentWriteError after the EventBridge rule was already deleted,
    _advance_bucket must reschedule the bucket tick before re-raising so the bucket is not
    stranded forever with no tick to drive it forward.
    """
    import executor
    from store import ConcurrentWriteError

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    run = _make_run(plan, [_bucket_state("b0", [_campaign_state("c0", "completed")])])

    with (
        patch("executor._delete_bucket_schedule_safe"),
        patch(
            "executor.save_run", side_effect=ConcurrentWriteError("version mismatch")
        ),
        patch("executor._schedule_tick") as mock_sched,
        patch("executor.record_bucket_schedule_name"),
        patch("executor._fire_bucket_chains"),
    ):
        with pytest.raises(ConcurrentWriteError):
            executor._advance_bucket(run, plan, 0, "all_campaigns_done")

    mock_sched.assert_called_once_with(plan_id="plan-1", run_id="run-1", bucket_index=0)


def test_advance_bucket_persists_rescheduled_tick_name_on_concurrent_write():
    """BD-013 regression: the rescheduled tick's name from the ConcurrentWriteError
    path above must be persisted (bypassing the version lock save_run just failed),
    or the resulting EventBridge rule + Lambda permission can never be found again to
    delete once the bucket completes — becoming a permanent orphan that accumulates
    until it hits the Lambda resource policy's hard 20KB size limit and silently
    stalls a completely unrelated run's bucket transition (root-caused 2026-08-21
    after a run sat "running" for 23h with bucket 7 never starting).
    """
    import executor
    from store import ConcurrentWriteError

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    run = _make_run(plan, [_bucket_state("b0", [_campaign_state("c0", "completed")])])

    with (
        patch("executor._delete_bucket_schedule_safe"),
        patch(
            "executor.save_run", side_effect=ConcurrentWriteError("version mismatch")
        ),
        patch("executor._schedule_tick", return_value="vip-plan-plan1-run-run1-b0"),
        patch("executor.record_bucket_schedule_name") as mock_record,
        patch("executor._fire_bucket_chains"),
    ):
        with pytest.raises(ConcurrentWriteError):
            executor._advance_bucket(run, plan, 0, "all_campaigns_done")

    mock_record.assert_called_once_with(
        "plan-1", "run-1", 0, "vip-plan-plan1-run-run1-b0"
    )


def test_advance_bucket_survives_record_schedule_name_failure_on_concurrent_write():
    """If persisting the rescheduled tick's name also fails, _advance_bucket must
    still re-raise ConcurrentWriteError (not mask it with the secondary failure) —
    the original error is what the caller/tick handler needs to see and retry on.
    """
    import executor
    from store import ConcurrentWriteError
    from botocore.exceptions import ClientError

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    run = _make_run(plan, [_bucket_state("b0", [_campaign_state("c0", "completed")])])

    with (
        patch("executor._delete_bucket_schedule_safe"),
        patch(
            "executor.save_run", side_effect=ConcurrentWriteError("version mismatch")
        ),
        patch("executor._schedule_tick", return_value="vip-plan-plan1-run-run1-b0"),
        patch(
            "executor.record_bucket_schedule_name",
            side_effect=ClientError({"Error": {"Code": "Foo"}}, "UpdateItem"),
        ),
        patch("executor._fire_bucket_chains"),
    ):
        with pytest.raises(ConcurrentWriteError):
            executor._advance_bucket(run, plan, 0, "all_campaigns_done")


# ── Bug C fix: _start_bucket cleans orphan EventBridge rule on ConcurrentWriteError ─


def test_start_bucket_cleans_orphan_on_concurrent_write():
    """If save_run raises ConcurrentWriteError on the pre-dispatch save (after _schedule_tick),
    _start_bucket must delete the orphaned EventBridge rule before re-raising.

    B1-D: schedule is created first, then a single save persists running+scheduleName.
    If that save loses an optimistic-lock race, the rule is cleaned up.
    """
    import executor
    from store import ConcurrentWriteError

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    run = _make_run(
        plan, [_bucket_state("b0", [_campaign_state("c0", "queued")], status="queued")]
    )
    run["planSnapshot"] = plan

    with (
        patch(
            "executor.save_run", side_effect=ConcurrentWriteError("version mismatch")
        ),
        patch("executor._schedule_tick", return_value="sched-orphan") as mock_sched,
        patch("executor._delete_schedule_safe") as mock_delete,
        patch("executor._dispatch_ready_campaigns", return_value=False),
    ):
        with pytest.raises(ConcurrentWriteError):
            executor._start_bucket(run, 0)

    mock_sched.assert_called_once()
    mock_delete.assert_called_once_with("sched-orphan")


# ── Bug D fix: _activate_warming_bucket cleans orphan EventBridge rule on ConcurrentWriteError ─


def test_activate_warming_bucket_cleans_orphan_on_concurrent_write():
    """If save_run raises ConcurrentWriteError after _schedule_tick already created the rule,
    _activate_warming_bucket must delete the orphaned EventBridge rule before re-raising.
    """
    import executor
    from store import ConcurrentWriteError

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    cs = _campaign_state("c0", "warming", connect_id="conn-w")
    cs["warmupStarted"] = True
    run = _make_run(plan, [_bucket_state("b0", [cs], status="warming")])

    with (
        patch(
            "executor.save_run", side_effect=ConcurrentWriteError("version mismatch")
        ),
        patch("executor._schedule_tick", return_value="sched-orphan") as mock_sched,
        patch("executor._delete_schedule_safe") as mock_delete,
        patch("executor._dispatch_ready_campaigns", return_value=False),
    ):
        with pytest.raises(ConcurrentWriteError):
            executor._activate_warming_bucket(run, plan, 0)

    mock_sched.assert_called_once()
    mock_delete.assert_called_once_with("sched-orphan")


# Bug: reconcileRetries is a sticky signal meaning "mid empty-segment retry"
# (see _bucket_has_only_legitimate_waits) but several unrelated queued-revert
# paths left it untouched, so a stale value from an earlier, already-finished
# retry cycle could mask a genuinely crashed/stuck campaign as "legitimately
# waiting" (root-caused 2026-08-27, adversarial code review).


def test_activate_warming_bucket_resets_reconcile_retries_on_error_recovery():
    """The error->queued pre-warm-failure recovery must clear reconcileRetries."""
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    cs = _campaign_state("c0", "error")
    cs["reconcileRetries"] = 3  # stale, from an earlier finished retry cycle
    run = _make_run(plan, [_bucket_state("b0", [cs], status="warming")])

    with (
        patch("executor.save_run"),
        patch("executor._schedule_tick", return_value="sched-1"),
        patch("executor._dispatch_ready_campaigns", return_value=False),
    ):
        executor._activate_warming_bucket(run, plan, 0)

    assert cs["status"] == "queued"
    assert cs["reconcileRetries"] == 0


def test_reset_cascade_cancelled_children_resets_reconcile_retries():
    """A parent_cancelled child reset to queued must have reconcileRetries
    cleared too."""
    import executor

    child_def = _campaign_def("child", depends_on=["parent"])
    plan = _make_plan([_bucket_def("b0", [_campaign_def("parent"), child_def])])
    child_cs = _campaign_state("child", "cancelled", exit_reason="parent_cancelled")
    child_cs["reconcileRetries"] = 2
    parent_cs = _campaign_state("parent", "cancelled")
    run = _make_run(plan, [_bucket_state("b0", [parent_cs, child_cs])])

    executor._reset_cascade_cancelled_children(run, plan, "parent")

    assert child_cs["status"] == "queued"
    assert child_cs["reconcileRetries"] == 0


def test_dispatch_ready_campaigns_phase1_stale_claim_resets_reconcile_retries():
    """A stale 'creating' claim (no connectCampaignId, >5min old) reverted to
    queued must have reconcileRetries cleared too."""
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0", depends_on=["never"])])])
    cs = _campaign_state("c0", "creating")
    cs["creatingAt"] = (
        datetime.now(timezone.utc) - timedelta(minutes=10)
    ).isoformat()
    cs["reconcileRetries"] = 4
    run = _make_run(plan, [_bucket_state("b0", [cs], status="running")])

    with patch("executor.save_run"), patch("executor._start_one_campaign"):
        executor._dispatch_ready_campaigns(run, plan, 0)

    assert cs["status"] == "queued"
    assert cs["reconcileRetries"] == 0


# ── New tests for 26-issue bug sweep ─────────────────────────────────────────


# B1-A: Two-phase claim in _dispatch_ready_campaigns


def test_dispatch_ready_claims_before_connect():
    """save_run (Phase 3 claim) is called BEFORE _start_one_campaign (Phase 4 Connect)."""
    import executor

    call_order: list[str] = []

    def _track_save(r):
        call_order.append("save_run")

    def _track_start(run, plan, bi, ci):
        call_order.append("start_one")

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    run = _make_run(plan, [_bucket_state("b0", [_campaign_state("c0", "queued")])])

    with (
        patch("executor.save_run", side_effect=_track_save),
        patch("executor._start_one_campaign", side_effect=_track_start),
    ):
        executor._dispatch_ready_campaigns(run, plan, 0)

    assert call_order.index("save_run") < call_order.index("start_one"), (
        "Phase 3 claim save must happen before Phase 4 Connect call"
    )


def test_dispatch_ready_concurrent_write_no_connect():
    """ConcurrentWriteError on Phase 3 claim must prevent any _start_one_campaign call."""
    import executor
    from store import ConcurrentWriteError

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    run = _make_run(plan, [_bucket_state("b0", [_campaign_state("c0", "queued")])])

    with (
        patch("executor.save_run", side_effect=ConcurrentWriteError("race")),
        patch("executor._start_one_campaign") as mock_start,
    ):
        with pytest.raises(ConcurrentWriteError):
            executor._dispatch_ready_campaigns(run, plan, 0)

    mock_start.assert_not_called()


def test_dispatch_ready_confirm_save_after_start():
    """Phase 5 confirm save is called after _start_one_campaign to persist connectCampaignId."""
    import executor

    save_count = {"n": 0}

    def _count_save(r):
        save_count["n"] += 1

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    run = _make_run(plan, [_bucket_state("b0", [_campaign_state("c0", "queued")])])

    with (
        patch("executor.save_run", side_effect=_count_save),
        patch("executor._start_one_campaign"),
    ):
        executor._dispatch_ready_campaigns(run, plan, 0)

    assert save_count["n"] == 2, (
        "Expected Phase 3 (claim) + Phase 5 (confirm) = 2 saves"
    )


# B1-B: _dispatch_cross_bucket_ready schedule-failure rollback


def test_dispatch_cross_bucket_schedule_failure_no_claim():
    """If _schedule_tick raises, the bucket must stay 'queued' and no campaign is claimed."""
    import executor

    plan = _make_plan(
        [
            _bucket_def("b0", [_campaign_def("c0")]),
            _bucket_def("b1", [_campaign_def("c1", depends_on=["c0"])]),
        ]
    )
    run = _make_run(
        plan,
        [
            _bucket_state(
                "b0", [_campaign_state("c0", "completed")], status="completed"
            ),
            _bucket_state("b1", [_campaign_state("c1", "queued")], status="queued"),
        ],
    )

    with (
        patch("executor._schedule_tick", side_effect=RuntimeError("EventBridge error")),
        patch("executor.save_run") as mock_save,
    ):
        result = executor._dispatch_cross_bucket_ready(run, plan, 0)

    assert result is False, "Should return False — no successful claim"
    mock_save.assert_not_called()
    assert run["bucketStates"][1]["status"] == "queued", (
        "Bucket must remain queued on schedule failure"
    )
    assert run["bucketStates"][1]["campaignStates"][0]["status"] == "queued", (
        "Campaign must remain queued"
    )


# ── _schedule_tick atomicity (audit follow-up, 2026-08-21) ───────────────────
# The 4 call sites above all assumed _schedule_tick either fully succeeds or
# raises with NOTHING created. That was false for an add_permission-stage
# failure specifically: put_rule/put_targets had already succeeded, leaving a
# live-but-unreferenced EventBridge rule — the exact BD-013 orphan shape, just
# triggered by a different failure than the ConcurrentWriteError already
# fixed. _schedule_tick now rolls back its own partial work before
# re-raising, so every caller's existing "log and move on" handling is safe
# by construction instead of by accident.


def test_schedule_tick_rolls_back_rule_on_add_permission_failure():
    """A non-ResourceConflictException failure from add_permission must delete
    the rule+target that put_rule/put_targets already created, before
    re-raising — otherwise it's a permanent (or, now, up-to-24h) orphan."""
    import executor
    from unittest.mock import MagicMock
    from botocore.exceptions import ClientError

    events_client = MagicMock()
    lambda_client = MagicMock()
    lambda_client.get_policy.return_value = {"Policy": '{"Statement": []}'}
    lambda_client.add_permission.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
        "AddPermission",
    )

    sts_client = MagicMock()
    sts_client.get_caller_identity.return_value = {"Account": "165505826690"}

    def _client(service_name, *a, **kw):
        return {"events": events_client, "lambda": lambda_client, "sts": sts_client}[service_name]

    with (
        patch("boto3.client", side_effect=_client),
        patch("executor._delete_schedule_safe") as mock_delete,
    ):
        with pytest.raises(ClientError, match="ThrottlingException"):
            executor._schedule_tick(plan_id="plan-1", run_id="run-1", bucket_index=0)

    events_client.put_rule.assert_called_once()
    events_client.put_targets.assert_called_once()
    mock_delete.assert_called_once_with("vip-plan-plan-1-run-run-1-b0")


def test_schedule_tick_does_not_roll_back_on_resource_conflict():
    """ResourceConflictException means the permission already exists (a
    concurrent caller won the race) — this is the expected idempotent case,
    not a failure. Must NOT delete the rule the caller is relying on."""
    import executor
    from unittest.mock import MagicMock
    from botocore.exceptions import ClientError

    events_client = MagicMock()
    lambda_client = MagicMock()
    lambda_client.get_policy.return_value = {"Policy": '{"Statement": []}'}
    lambda_client.add_permission.side_effect = ClientError(
        {"Error": {"Code": "ResourceConflictException", "Message": "already exists"}},
        "AddPermission",
    )

    sts_client = MagicMock()
    sts_client.get_caller_identity.return_value = {"Account": "165505826690"}

    def _client(service_name, *a, **kw):
        return {"events": events_client, "lambda": lambda_client, "sts": sts_client}[service_name]

    with (
        patch("boto3.client", side_effect=_client),
        patch("executor._delete_schedule_safe") as mock_delete,
    ):
        result = executor._schedule_tick(plan_id="plan-1", run_id="run-1", bucket_index=0)

    assert result == "vip-plan-plan-1-run-run-1-b0"
    mock_delete.assert_not_called()


# B1-C: _force_finish_internal and abort_run always unlock


def test_force_finish_unlocks_on_save_failure():
    """unlock_plan_run is called even when save_run raises."""
    import executor
    from store import ConcurrentWriteError

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    run = _make_run(
        plan,
        [_bucket_state("b0", [_campaign_state("c0", "running", connect_id="conn-1")])],
    )

    with (
        patch("executor.save_run", side_effect=ConcurrentWriteError("race")),
        patch("executor.unlock_plan_run") as mock_unlock,
        patch("executor.update_plan_pending_warmup"),
        patch("executor._safe_stop_campaign"),
        patch("executor._delete_bucket_schedule_safe"),
    ):
        with pytest.raises(ConcurrentWriteError):
            executor._force_finish_internal(run, plan)

    mock_unlock.assert_called_once_with("plan-1")


def test_abort_run_unlocks_on_save_failure():
    """unlock_plan_run is called exactly once when all save_run attempts raise ConcurrentWriteError."""
    import executor
    from store import ConcurrentWriteError

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])

    def fresh_run(*_a, **_k):
        # Return a fresh dict each time so the retry sees status="running" unmodified.
        return _make_run(
            plan,
            [
                _bucket_state(
                    "b0", [_campaign_state("c0", "running", connect_id="conn-1")]
                )
            ],
        )

    with (
        patch("executor.get_run", side_effect=fresh_run),
        patch("executor.save_run", side_effect=ConcurrentWriteError("race")),
        patch("executor.unlock_plan_run") as mock_unlock,
        patch("executor.update_plan_pending_warmup"),
        patch("executor._safe_stop_campaign"),
        patch("executor._safe_delete_campaign"),
        patch("executor._safe_delete_segment"),
        patch("executor._delete_bucket_schedule_safe"),
    ):
        with pytest.raises(ConcurrentWriteError):
            executor.abort_run("plan-1", "run-1")

    mock_unlock.assert_called_once_with("plan-1")


# B1-D: _start_bucket raises when schedule fails


def test_start_bucket_raises_when_schedule_fails():
    """_start_bucket must raise (not dispatch) if _schedule_tick fails."""
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    run = _make_run(
        plan, [_bucket_state("b0", [_campaign_state("c0", "queued")], status="queued")]
    )
    run["planSnapshot"] = plan

    with (
        patch("executor._schedule_tick", side_effect=RuntimeError("EventBridge down")),
        patch("executor._dispatch_ready_campaigns") as mock_dispatch,
        patch("executor.save_run"),
    ):
        with pytest.raises(RuntimeError):
            executor._start_bucket(run, 0)

    mock_dispatch.assert_not_called()


# B2-A: Redis rebuilding and empty segment behavior


def test_start_one_campaign_empty_segment_retries_then_cancels():
    """_EmptySegmentError retries up to reconcileRetryLimit times then cancels permanently.

    This protects against partial Redis rebuilds where some states load before others.
    reconcileRetryLimit default = 5, so 6th attempt cancels.
    """
    import executor

    bucket = _bucket_def("b0", [_campaign_def("c0")])
    cs = _campaign_state("c0", "queued")
    run = _make_run(_make_plan([bucket]), [_bucket_state("b0", [cs])])

    call_count = {"n": 0}

    def _count_calls(b, c):
        call_count["n"] += 1
        raise executor._EmptySegmentError("No leads")

    # Simulate 6 tick invocations (each tick calls _start_one_campaign once)
    for _ in range(6):
        with patch("executor._create_segment", side_effect=_count_calls):
            executor._start_one_campaign(run, run["planSnapshot"], 0, 0)

    assert call_count["n"] == 6, (
        "_EmptySegmentError should be called once per tick across 6 ticks"
    )
    assert cs["status"] == "cancelled", "After 6 attempts, must permanently cancel"
    assert cs["exitReason"] == "skipped_empty"


# B2-B: start_run lock-before-create


def test_start_run_no_orphan_on_lock_race():
    """If lock_plan_run raises, no DynamoDB run record should be created."""
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])

    with (
        patch("executor.get_plan", return_value=plan),
        patch("executor.get_latest_run", return_value=None),
        patch("executor.lock_plan_run", side_effect=ValueError("already locked")),
        patch("executor.create_run") as mock_create,
    ):
        with pytest.raises(ValueError, match="already locked"):
            executor.start_run("plan-1")

    mock_create.assert_not_called()


# B2-C: Working hours and template guards in chain firing


def test_fire_bucket_chains_respects_working_hours():
    """_fire_bucket_chains skips plans that are outside their working hours window."""
    import executor

    outside_hours_plan = _make_plan([])
    outside_hours_plan["planId"] = "plan-outside"
    outside_hours_plan["workingHours"] = {
        "startTime": "08:00",
        "endTime": "09:00",
        "days": [],
    }

    with (
        patch(
            "executor.find_plans_by_trigger_planid", return_value=[outside_hours_plan]
        ),
        patch("executor._within_working_hours", return_value=False),
        patch("executor.start_run") as mock_start,
    ):
        executor._fire_bucket_chains("plan-upstream", 0)

    mock_start.assert_not_called()


def test_fire_bucket_chains_skips_templates():
    """_fire_bucket_chains must not start template plans."""
    import executor

    template_plan = _make_plan([])
    template_plan["planId"] = "plan-tmpl"
    template_plan["isTemplate"] = True
    template_plan["trigger"] = {
        "type": "on_plan_complete",
        "planId": "plan-upstream",
        "afterBucket": 0,
    }

    with (
        patch("executor.find_plans_by_trigger_planid", return_value=[template_plan]),
        patch("executor.start_run") as mock_start,
    ):
        executor._fire_bucket_chains("plan-upstream", 0)

    mock_start.assert_not_called()


def test_scheduled_run_skips_template_and_logs(caplog):
    """scheduled_run must return 'is_template' and log a warning for template plans."""
    import executor
    import logging

    template_plan = _make_plan([])
    template_plan["isTemplate"] = True

    with (
        patch("executor.get_latest_run", return_value=None),
        patch("executor.get_plan", return_value=template_plan),
        patch("executor.start_run") as mock_start,
    ):
        with caplog.at_level(logging.DEBUG):
            result = executor.scheduled_run("plan-tmpl")

    assert result == {"ok": True, "reason": "is_template"}
    mock_start.assert_not_called()


def test_prestart_fallback_skips_template_plans():
    """prestart_check fallback must not emit the ScheduledRunFallback metric for template plans."""
    import executor

    now_cot = datetime(2025, 1, 1, 8, 41, tzinfo=None)  # 08:41 COT → delta == -1 for 08:40 trigger

    template_plan = _make_plan([])
    template_plan["planId"] = "plan-tmpl"
    template_plan["isTemplate"] = True
    template_plan["trigger"] = {"type": "time", "time": "08:40"}

    with (
        patch(
            "executor.datetime",
            **{
                "now.return_value": now_cot,
                "fromisoformat.side_effect": datetime.fromisoformat,
            },
        ),
        patch("executor.list_plans", return_value=[template_plan]),
        patch("executor.get_latest_run", return_value=None),
        patch("executor.scheduled_run") as mock_scheduled_run,
        patch("boto3.client") as mock_boto,
    ):
        executor.prestart_check()

    mock_scheduled_run.assert_not_called()
    mock_boto.return_value.put_metric_data.assert_not_called()


def test_prestart_fallback_skips_plans_outside_working_hours():
    """prestart_check fallback must not fire ScheduledRunFallback when today isn't
    an allowed working day for the plan — absence of a run is expected, not a missed cron.
    """
    import executor

    now_cot = datetime(2025, 1, 1, 8, 41, tzinfo=None)  # Wednesday, 08:41 COT -> delta=-1 for 08:40

    plan = _make_plan([])
    plan["planId"] = "plan-wed-off"
    plan["trigger"] = {"type": "time", "time": "08:40"}
    plan["workingHours"] = {"days": ["MON", "TUE", "THU", "FRI", "SAT", "SUN"]}  # no WED

    with (
        patch(
            "executor.datetime",
            **{
                "now.return_value": now_cot,
                "fromisoformat.side_effect": datetime.fromisoformat,
            },
        ),
        patch("executor.list_plans", return_value=[plan]),
        patch("executor.get_latest_run", return_value=None),
        patch("executor.scheduled_run") as mock_scheduled_run,
        patch("boto3.client") as mock_boto,
    ):
        executor.prestart_check()

    mock_scheduled_run.assert_not_called()
    mock_boto.return_value.put_metric_data.assert_not_called()


def test_prestart_prewarm_skips_plans_outside_working_hours():
    """prestart_check pre-warm must not warm campaigns (or touch EventBridge permissions)
    when today isn't an allowed working day for the plan.
    """
    import executor

    now_cot = datetime(2025, 1, 1, 8, 35, tzinfo=None)  # Wednesday, 08:35 COT -> delta=5 for 08:40

    plan = _make_plan([])
    plan["planId"] = "plan-wed-off"
    plan["trigger"] = {"type": "time", "time": "08:40"}
    plan["workingHours"] = {"days": ["MON", "TUE", "THU", "FRI", "SAT", "SUN"]}  # no WED

    with (
        patch(
            "executor.datetime",
            **{
                "now.return_value": now_cot,
                "fromisoformat.side_effect": datetime.fromisoformat,
            },
        ),
        patch("executor.list_plans", return_value=[plan]),
        patch("executor._ensure_scheduled_run_permission") as mock_ensure_perm,
        patch("executor._prestart_plan") as mock_prestart_plan,
        patch("boto3.client"),
    ):
        result = executor.prestart_check()

    mock_ensure_perm.assert_not_called()
    mock_prestart_plan.assert_not_called()
    assert result["warmed"] == []


def test_prestart_prewarm_skips_template_plans():
    """Templates never run on a cron — _ensure_scheduled_run_permission must not
    recreate a template's vip-sched-* rule just because it's 4-6 min from its old
    trigger.time. Live incident, 2026-08-25: plan c63d695c-b99e-4885-808a-
    8eca91d08e8e (isTemplate=true, trigger.time=08:40) had its vip-sched-* rule
    recreated by this exact path every day, undoing the janitor's cleanup daily.
    """
    import executor

    now_cot = datetime(2025, 1, 1, 8, 35, tzinfo=None)  # delta=5 for 08:40

    plan = _make_plan([])
    plan["planId"] = "plan-template"
    plan["isTemplate"] = True
    plan["trigger"] = {"type": "time", "time": "08:40"}

    with (
        patch(
            "executor.datetime",
            **{
                "now.return_value": now_cot,
                "fromisoformat.side_effect": datetime.fromisoformat,
            },
        ),
        patch("executor.list_plans", return_value=[plan]),
        patch("executor._ensure_scheduled_run_permission") as mock_ensure_perm,
        patch("executor._prestart_plan") as mock_prestart_plan,
        patch("boto3.client"),
    ):
        result = executor.prestart_check()

    mock_ensure_perm.assert_not_called()
    mock_prestart_plan.assert_not_called()
    assert result["warmed"] == []


# ── No-active-campaign detection (BD-013 follow-up) ───────────────────────────


def test_prestart_flags_running_bucket_with_no_active_campaign_after_5_min():
    """A bucket stuck at status='running' for 5+ minutes with every campaign
    still 'queued' (none creating/warming/running) must emit NoActiveCampaign
    — this is the exact BD-013 shape: the tick that should have created the
    bucket's campaigns crashed, so nothing ever left 'queued'.
    """
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    run = _make_run(
        plan, [_bucket_state("b0", [_campaign_state("c0", "queued")], status="running")]
    )
    now_utc = datetime(2026, 5, 8, 10, 6, tzinfo=timezone.utc)  # 6 min after startedAt

    with (
        patch(
            "executor.datetime",
            **{"now.return_value": now_utc, "fromisoformat.side_effect": datetime.fromisoformat},
        ),
        patch("executor.list_plans", return_value=[plan]),
        patch("executor.get_latest_run", return_value=run),
        patch("boto3.client") as mock_boto,
    ):
        result = executor.prestart_check()

    assert result["no_active_campaign"] == ["plan-1"]
    calls = mock_boto.return_value.put_metric_data.call_args_list
    assert len(calls) == 1
    metric_data = calls[0].kwargs["MetricData"]
    assert metric_data[0]["MetricName"] == "NoActiveCampaign"
    assert metric_data[0]["Dimensions"] == [{"Name": "PlanId", "Value": "plan-1"}]
    assert metric_data[1]["Dimensions"] == [], (
        "must also emit the no-dimension aggregate, same convention as "
        "CampaignDispatchStalled, so one CLI alarm can watch it directly"
    )


def test_prestart_does_not_flag_before_5_minutes():
    """A bucket that just started (< 5 min ago) with no active campaign yet must
    NOT be flagged — campaigns take a few seconds to create; this is normal, not
    the BD-013 failure mode.
    """
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    run = _make_run(
        plan, [_bucket_state("b0", [_campaign_state("c0", "queued")], status="running")]
    )
    now_utc = datetime(2026, 5, 8, 10, 2, tzinfo=timezone.utc)  # 2 min after startedAt

    with (
        patch(
            "executor.datetime",
            **{"now.return_value": now_utc, "fromisoformat.side_effect": datetime.fromisoformat},
        ),
        patch("executor.list_plans", return_value=[plan]),
        patch("executor.get_latest_run", return_value=run),
        patch("boto3.client") as mock_boto,
    ):
        result = executor.prestart_check()

    assert result["no_active_campaign"] == []
    mock_boto.return_value.put_metric_data.assert_not_called()


# ── Cross-bucket dependency waits are not "stuck" (audit follow-up, 2026-08-21) ──
# Live incident, plan 6203a0b5-a82d-42c7-b523-6d1b1893ae30: bucket 11's only
# non-terminal campaign depends on bucket 10's campaign, which was genuinely still
# actively dialing in Connect (real schedule, hours from its own endTime) — not
# crashed. The alarm paged every single minute for over an hour anyway, because
# it only checked "is anything active IN THIS bucket", with no notion of a
# same-run cross-bucket dependency that's still legitimately in flight elsewhere.


def test_prestart_does_not_flag_bucket_blocked_on_live_cross_bucket_dependency():
    import executor

    plan = _make_plan(
        [
            _bucket_def("b0", [_campaign_def("c0")]),
            _bucket_def("b1", [_campaign_def("c1", depends_on=["c0"])]),
        ]
    )
    # c0 (bucket 0) is still actively running — c1 (bucket 1) correctly waits.
    run = _make_run(
        plan,
        [
            _bucket_state("b0", [_campaign_state("c0", "running")], status="running"),
            _bucket_state("b1", [_campaign_state("c1", "queued")], status="running"),
        ],
    )
    now_utc = datetime(2026, 5, 8, 10, 6, tzinfo=timezone.utc)  # 6 min after b1 startedAt

    with (
        patch(
            "executor.datetime",
            **{"now.return_value": now_utc, "fromisoformat.side_effect": datetime.fromisoformat},
        ),
        patch("executor.list_plans", return_value=[plan]),
        patch("executor.get_latest_run", return_value=run),
        patch("boto3.client") as mock_boto,
    ):
        result = executor.prestart_check()

    assert result["no_active_campaign"] == []
    mock_boto.return_value.put_metric_data.assert_not_called()


def test_prestart_flags_bucket_whose_dependency_is_already_terminal():
    """If the dependency is DONE (terminal) but the dependent campaign never
    started, that's the real BD-013 shape again — dispatch should have already
    happened and didn't. Must still alarm."""
    import executor

    plan = _make_plan(
        [
            _bucket_def("b0", [_campaign_def("c0")]),
            _bucket_def("b1", [_campaign_def("c1", depends_on=["c0"])]),
        ]
    )
    run = _make_run(
        plan,
        [
            _bucket_state("b0", [_campaign_state("c0", "completed")], status="completed"),
            _bucket_state("b1", [_campaign_state("c1", "queued")], status="running"),
        ],
    )
    now_utc = datetime(2026, 5, 8, 10, 6, tzinfo=timezone.utc)

    with (
        patch(
            "executor.datetime",
            **{"now.return_value": now_utc, "fromisoformat.side_effect": datetime.fromisoformat},
        ),
        patch("executor.list_plans", return_value=[plan]),
        patch("executor.get_latest_run", return_value=run),
        patch("boto3.client") as mock_boto,
    ):
        result = executor.prestart_check()

    assert result["no_active_campaign"] == ["plan-1"]
    mock_boto.return_value.put_metric_data.assert_called_once()


def test_prestart_flags_bucket_with_no_dependency_still_queued():
    """A queued campaign with NO dependsOn at all has nothing legitimate to wait
    on — must still alarm (this is the original, unmodified BD-013 case)."""
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    run = _make_run(
        plan, [_bucket_state("b0", [_campaign_state("c0", "queued")], status="running")]
    )
    now_utc = datetime(2026, 5, 8, 10, 6, tzinfo=timezone.utc)

    with (
        patch(
            "executor.datetime",
            **{"now.return_value": now_utc, "fromisoformat.side_effect": datetime.fromisoformat},
        ),
        patch("executor.list_plans", return_value=[plan]),
        patch("executor.get_latest_run", return_value=run),
        patch("boto3.client") as mock_boto,
    ):
        result = executor.prestart_check()

    assert result["no_active_campaign"] == ["plan-1"]
    mock_boto.return_value.put_metric_data.assert_called_once()


# ── Empty-segment retry is not "stuck" either (root-caused 2026-08-27) ────────
# vip-plans-no-active-campaign-sustained false-paged on plan 1a29f025's frequent
# re-triggers: each new run's first campaign normally takes 5-13 min to find a
# non-empty Redis segment (_EmptySegmentError retry in _start_one_campaign,
# reconcileRetries incremented each time), which is the exact BD-013 shape by
# status alone ("queued", no dependsOn) but isn't a crashed tick — it's a live,
# bounded retry. Confirmed against a real incident (2026-08-25, plan 6203a0b5):
# a campaign sat "queued" for 70 min on this exact path before a matching lead
# appeared and it started normally.


def test_prestart_does_not_flag_bucket_mid_empty_segment_retry():
    """A queued campaign with reconcileRetries set (mid _EmptySegmentError retry,
    no dependsOn) must NOT alarm — it's actively retrying, not crashed."""
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    retrying_campaign = _campaign_state("c0", "queued")
    retrying_campaign["reconcileRetries"] = 5
    run = _make_run(
        plan, [_bucket_state("b0", [retrying_campaign], status="running")]
    )
    now_utc = datetime(2026, 5, 8, 11, 20, tzinfo=timezone.utc)  # 80 min after bucket startedAt

    with (
        patch(
            "executor.datetime",
            **{"now.return_value": now_utc, "fromisoformat.side_effect": datetime.fromisoformat},
        ),
        patch("executor.list_plans", return_value=[plan]),
        patch("executor.get_latest_run", return_value=run),
        patch("boto3.client") as mock_boto,
    ):
        result = executor.prestart_check()

    assert result["no_active_campaign"] == []
    mock_boto.return_value.put_metric_data.assert_not_called()


# ── PrewarmFailure metric (audit follow-up, 2026-08-21) ───────────────────────
# RUNBOOKS.md/INTEGRATION_CONTRACTS.md documented an alarmable "PrewarmFailure"
# metric for months; it was never implemented anywhere — every pre-warm failure
# path only logged an ERROR and moved on, with zero telemetry. These tests lock
# in the real emission across all four failure sites.


def test_prestart_plan_emits_metric_when_a_campaign_fails_to_warm():
    import executor

    plan = {
        "planId": "plan-target",
        "buckets": [_bucket_def("b0", [_campaign_def("c0")])],
    }

    with (
        patch("executor._create_campaign_only", side_effect=RuntimeError("Connect quota")),
        patch("executor.get_plan", return_value=plan),
        patch("executor.get_latest_run", return_value=None),
        patch("executor.update_plan_pending_warmup"),
        patch("boto3.client") as mock_boto,
    ):
        executor._prestart_plan("plan-target")

    calls = mock_boto.return_value.put_metric_data.call_args_list
    assert len(calls) == 1
    metric_data = calls[0].kwargs["MetricData"]
    assert metric_data[0]["MetricName"] == "PrewarmFailure"
    assert metric_data[0]["Value"] == 1
    assert metric_data[0]["Dimensions"] == [{"Name": "PlanId", "Value": "plan-target"}]
    assert metric_data[1]["Dimensions"] == []


def test_prestart_plan_does_not_emit_metric_when_warmup_succeeds():
    import executor

    plan = {
        "planId": "plan-target",
        "buckets": [_bucket_def("b0", [_campaign_def("c0")])],
    }

    with (
        patch(
            "executor._create_campaign_only",
            return_value=("conn-1", "seg-1", "arn:seg-1", True),
        ),
        patch("executor.get_plan", return_value=plan),
        patch("executor.get_latest_run", return_value=None),
        patch("executor.update_plan_pending_warmup"),
        patch("boto3.client") as mock_boto,
    ):
        executor._prestart_plan("plan-target")

    mock_boto.return_value.put_metric_data.assert_not_called()


def test_prestart_chained_runs_emits_metric_when_prestart_plan_crashes():
    """on_plan_complete chain: _prestart_plan itself raising (not a per-campaign
    failure it already swallows) must still surface via the metric."""
    import executor

    run = {"planId": "plan-1", "runId": "run-1"}
    plan = {"planId": "plan-1", "loop": None}
    downstream = {"planId": "plan-2"}

    with (
        patch("executor.find_plans_by_trigger_planid", return_value=[downstream]),
        patch("executor._prestart_plan", side_effect=RuntimeError("boom")),
        patch("boto3.client") as mock_boto,
    ):
        executor._prestart_chained_runs(run, plan, 0)

    calls = mock_boto.return_value.put_metric_data.call_args_list
    assert len(calls) == 1
    metric_data = calls[0].kwargs["MetricData"]
    assert metric_data[0]["MetricName"] == "PrewarmFailure"
    assert metric_data[0]["Dimensions"] == [{"Name": "PlanId", "Value": "plan-2"}]


def test_prestart_after_campaign_emits_metric_when_prestart_plan_crashes():
    import executor

    downstream = {
        "planId": "plan-2",
        "trigger": {"afterCampaign": "camp-1"},
    }

    with (
        patch("executor.find_plans_by_trigger_planid", return_value=[downstream]),
        patch("executor._prestart_plan", side_effect=RuntimeError("boom")),
        patch("boto3.client") as mock_boto,
    ):
        executor._prestart_after_campaign("plan-1", "camp-1")

    calls = mock_boto.return_value.put_metric_data.call_args_list
    assert len(calls) == 1
    metric_data = calls[0].kwargs["MetricData"]
    assert metric_data[0]["MetricName"] == "PrewarmFailure"
    assert metric_data[0]["Dimensions"] == [{"Name": "PlanId", "Value": "plan-2"}]


def test_prestart_does_not_flag_bucket_with_an_active_campaign():
    """A bucket running 5+ minutes with at least one campaign actually
    creating/warming/running must NOT be flagged — this is healthy, not stalled.
    """
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    run = _make_run(
        plan, [_bucket_state("b0", [_campaign_state("c0", "running")], status="running")]
    )
    now_utc = datetime(2026, 5, 8, 10, 10, tzinfo=timezone.utc)  # 10 min after startedAt

    with (
        patch(
            "executor.datetime",
            **{"now.return_value": now_utc, "fromisoformat.side_effect": datetime.fromisoformat},
        ),
        patch("executor.list_plans", return_value=[plan]),
        patch("executor.get_latest_run", return_value=run),
        patch("boto3.client") as mock_boto,
    ):
        result = executor.prestart_check()

    assert result["no_active_campaign"] == []
    mock_boto.return_value.put_metric_data.assert_not_called()


# ── Janitor: daily orphan-schedule cleanup (BD-013 follow-up) ────────────────


def _prefix_dispatch_list_rules(vip_plan_pages: list | None = None, vip_sched_pages: list | None = None):
    """side_effect for events.list_rules dispatching by NamePrefix — the janitor
    now sweeps vip-plan-* and vip-sched-* as two separate paginated calls through
    the SAME boto3 client, so a single static return_value can't distinguish them."""
    plan_iter = iter(vip_plan_pages or [{"Rules": []}])
    sched_iter = iter(vip_sched_pages or [{"Rules": []}])

    def _side_effect(**kwargs):
        prefix = kwargs.get("NamePrefix")
        if prefix == "vip-plan-":
            return next(plan_iter)
        if prefix == "vip-sched-":
            return next(sched_iter)
        raise AssertionError(f"unexpected NamePrefix: {prefix}")

    return _side_effect


def test_janitor_deletes_rule_not_referenced_by_any_running_run():
    """A vip-plan-* rule that no currently-running run's bucketState.scheduleName
    points to must be deleted — this is the actual orphan-detection gap
    _cleanup_orphan_plan_permissions can't cover (its rule is still alive).
    """
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    run = _make_run(
        plan,
        [_bucket_state("b0", [_campaign_state("c0", "running")], schedule_name="vip-plan-p1-run-r1-b0")],
    )

    with (
        patch("executor.list_plans", return_value=[plan]),
        patch("executor.get_latest_run", return_value=run),
        patch("executor._delete_schedule_safe") as mock_delete,
        patch("executor._notify_sns") as mock_notify,
        patch("boto3.client") as mock_boto,
    ):
        mock_boto.return_value.list_rules.side_effect = _prefix_dispatch_list_rules(
            vip_plan_pages=[
                {"Rules": [{"Name": "vip-plan-p1-run-r1-b0"}, {"Name": "vip-plan-orphan-rule"}]}
            ]
        )
        result = executor.janitor_cleanup_orphan_schedules()

    assert result["deleted"] == ["vip-plan-orphan-rule"]
    mock_delete.assert_called_once_with("vip-plan-orphan-rule")
    mock_notify.assert_called_once()


def test_janitor_preserves_the_current_running_bucket_schedule():
    """The rule backing the currently-running run's own bucket must survive —
    this is the exact rule the tick mechanism still needs.
    """
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    run = _make_run(
        plan,
        [_bucket_state("b0", [_campaign_state("c0", "running")], schedule_name="vip-plan-p1-run-r1-b0")],
    )

    with (
        patch("executor.list_plans", return_value=[plan]),
        patch("executor.get_latest_run", return_value=run),
        patch("executor._delete_schedule_safe") as mock_delete,
        patch("executor._notify_sns"),
        patch("boto3.client") as mock_boto,
    ):
        mock_boto.return_value.list_rules.side_effect = _prefix_dispatch_list_rules(
            vip_plan_pages=[{"Rules": [{"Name": "vip-plan-p1-run-r1-b0"}]}]
        )
        result = executor.janitor_cleanup_orphan_schedules()

    assert result["deleted"] == []
    mock_delete.assert_not_called()


def test_janitor_treats_non_running_plans_schedules_as_fair_game():
    """If a plan's latest run isn't 'running' (completed/aborted), NONE of its
    scheduleNames protect anything — a finished run never needs its tick
    again, whether or not the janitor happens to look at it.
    """
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    finished_run = _make_run(
        plan,
        [_bucket_state("b0", [_campaign_state("c0", "completed")], schedule_name="vip-plan-p1-run-r1-b0")],
        status="completed",
    )

    with (
        patch("executor.list_plans", return_value=[plan]),
        patch("executor.get_latest_run", return_value=finished_run),
        patch("executor._delete_schedule_safe") as mock_delete,
        patch("executor._notify_sns") as mock_notify,
        patch("boto3.client") as mock_boto,
    ):
        mock_boto.return_value.list_rules.side_effect = _prefix_dispatch_list_rules(
            vip_plan_pages=[{"Rules": [{"Name": "vip-plan-p1-run-r1-b0"}]}]
        )
        result = executor.janitor_cleanup_orphan_schedules()

    assert result["deleted"] == ["vip-plan-p1-run-r1-b0"]
    mock_delete.assert_called_once_with("vip-plan-p1-run-r1-b0")
    mock_notify.assert_called_once()


def test_janitor_paginates_through_all_rules():
    """list_rules is paginated (NextToken) — the janitor must follow every
    page, not just the first, or it silently misses a growing tail of orphans.
    """
    import executor

    with (
        patch("executor.list_plans", return_value=[]),
        patch("executor._delete_schedule_safe") as mock_delete,
        patch("executor._notify_sns"),
        patch("boto3.client") as mock_boto,
    ):
        mock_boto.return_value.list_rules.side_effect = _prefix_dispatch_list_rules(
            vip_plan_pages=[
                {"Rules": [{"Name": "vip-plan-a"}], "NextToken": "page2"},
                {"Rules": [{"Name": "vip-plan-b"}]},
            ]
        )
        result = executor.janitor_cleanup_orphan_schedules()

    assert sorted(result["deleted"]) == ["vip-plan-a", "vip-plan-b"]
    assert mock_delete.call_count == 2
    second_call_kwargs = mock_boto.return_value.list_rules.call_args_list[1].kwargs
    assert second_call_kwargs.get("NextToken") == "page2"


def test_janitor_does_not_notify_when_nothing_was_orphaned():
    """No deletions -> no SNS noise. This runs daily; alerting on a clean run
    every single day would be exactly the kind of alarm fatigue this whole
    fix is trying to avoid."""
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    run = _make_run(
        plan,
        [_bucket_state("b0", [_campaign_state("c0", "running")], schedule_name="vip-plan-p1-run-r1-b0")],
    )

    with (
        patch("executor.list_plans", return_value=[plan]),
        patch("executor.get_latest_run", return_value=run),
        patch("executor._delete_schedule_safe"),
        patch("executor._notify_sns") as mock_notify,
        patch("boto3.client") as mock_boto,
    ):
        mock_boto.return_value.list_rules.side_effect = _prefix_dispatch_list_rules(
            vip_plan_pages=[{"Rules": [{"Name": "vip-plan-p1-run-r1-b0"}]}]
        )
        executor.janitor_cleanup_orphan_schedules()

    mock_notify.assert_not_called()


# ── janitor vip-sched-* sweep (audit follow-up, 2026-08-21) ───────────────────


def test_janitor_deletes_vip_sched_rule_for_template_plan():
    """A vip-sched-* rule whose owning plan is now isTemplate=true is an orphan
    — update_plan's own cleanup only fires when the PATCH resends "trigger",
    so this sweep is the backstop (confirmed live: plan
    c63d695c-b99e-4885-808a-8eca91d08e8e)."""
    import executor
    from scheduler_manager import _rule_name

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    plan["planId"] = "c63d695c-b99e-4885-808a-8eca91d08e8e"
    plan["isTemplate"] = True
    plan["trigger"] = {"type": "time", "time": "08:40"}
    rule_name = _rule_name(plan["planId"])

    with (
        patch("executor.list_plans", return_value=[plan]),
        patch("executor.get_latest_run", return_value=None),
        patch("executor._delete_schedule_safe") as mock_delete,
        patch("executor._notify_sns"),
        patch("boto3.client") as mock_boto,
    ):
        mock_boto.return_value.list_rules.side_effect = _prefix_dispatch_list_rules(
            vip_sched_pages=[{"Rules": [{"Name": rule_name}]}]
        )
        result = executor.janitor_cleanup_orphan_schedules()

    assert result["deleted"] == [rule_name]
    mock_delete.assert_called_once_with(rule_name)


def test_janitor_preserves_vip_sched_rule_for_live_time_triggered_plan():
    """A non-template plan with an active time trigger must keep its rule."""
    import executor
    from scheduler_manager import _rule_name

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    plan["planId"] = "plan-with-time-trigger"
    plan["isTemplate"] = False
    plan["trigger"] = {"type": "time", "time": "08:40"}
    rule_name = _rule_name(plan["planId"])

    with (
        patch("executor.list_plans", return_value=[plan]),
        patch("executor.get_latest_run", return_value=None),
        patch("executor._delete_schedule_safe") as mock_delete,
        patch("executor._notify_sns"),
        patch("boto3.client") as mock_boto,
    ):
        mock_boto.return_value.list_rules.side_effect = _prefix_dispatch_list_rules(
            vip_sched_pages=[{"Rules": [{"Name": rule_name}]}]
        )
        result = executor.janitor_cleanup_orphan_schedules()

    assert result["deleted"] == []
    mock_delete.assert_not_called()


def test_janitor_deletes_vip_sched_rule_for_manual_trigger_plan():
    """A plan whose trigger changed away from "time" (e.g. to manual) but still
    has a live rule — same orphan shape, different cause than isTemplate."""
    import executor
    from scheduler_manager import _rule_name

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    plan["planId"] = "plan-now-manual"
    plan["isTemplate"] = False
    plan["trigger"] = {"type": "manual"}
    rule_name = _rule_name(plan["planId"])

    with (
        patch("executor.list_plans", return_value=[plan]),
        patch("executor.get_latest_run", return_value=None),
        patch("executor._delete_schedule_safe") as mock_delete,
        patch("executor._notify_sns"),
        patch("boto3.client") as mock_boto,
    ):
        mock_boto.return_value.list_rules.side_effect = _prefix_dispatch_list_rules(
            vip_sched_pages=[{"Rules": [{"Name": rule_name}]}]
        )
        result = executor.janitor_cleanup_orphan_schedules()

    assert result["deleted"] == [rule_name]
    mock_delete.assert_called_once_with(rule_name)


def test_prestart_prewarm_and_fallback_still_fire_on_allowed_day():
    """Regression guard: a plan whose allowed days include today must still be
    pre-warmed and still trigger the fallback exactly as before this fix.
    """
    import executor

    prewarm_plan = _make_plan([])
    prewarm_plan["planId"] = "plan-wed-on"
    prewarm_plan["trigger"] = {"type": "time", "time": "08:40"}
    prewarm_plan["workingHours"] = {"days": ["WED"]}

    now_cot = datetime(2025, 1, 1, 8, 35, tzinfo=None)  # Wednesday, delta=5
    with (
        patch(
            "executor.datetime",
            **{
                "now.return_value": now_cot,
                "fromisoformat.side_effect": datetime.fromisoformat,
            },
        ),
        patch("executor.list_plans", return_value=[prewarm_plan]),
        patch("executor._ensure_scheduled_run_permission") as mock_ensure_perm,
        patch("executor._prestart_plan") as mock_prestart_plan,
        patch("boto3.client"),
    ):
        result = executor.prestart_check()

    mock_ensure_perm.assert_called_once_with("plan-wed-on")
    mock_prestart_plan.assert_called_once_with("plan-wed-on")
    assert result["warmed"] == ["plan-wed-on"]

    fallback_plan = _make_plan([])
    fallback_plan["planId"] = "plan-wed-on"
    fallback_plan["trigger"] = {"type": "time", "time": "08:40"}
    fallback_plan["workingHours"] = {"days": ["WED"]}

    now_cot_fallback = datetime(2025, 1, 1, 8, 41, tzinfo=None)  # Wednesday, delta=-1
    with (
        patch(
            "executor.datetime",
            **{
                "now.return_value": now_cot_fallback,
                "fromisoformat.side_effect": datetime.fromisoformat,
            },
        ),
        patch("executor.list_plans", return_value=[fallback_plan]),
        patch("executor.get_latest_run", return_value=None),
        patch("executor.scheduled_run") as mock_scheduled_run,
        patch("boto3.client") as mock_boto,
    ):
        executor.prestart_check()

    mock_scheduled_run.assert_called_once_with("plan-wed-on")
    mock_boto.return_value.put_metric_data.assert_called_once()


def test_is_working_day_no_working_hours_configured():
    """No workingHours at all -> unrestricted, always a working day."""
    import executor

    assert executor._is_working_day(_make_plan([])) is True


def test_is_working_day_no_days_list_configured():
    """workingHours present but no 'days' key -> unrestricted, always a working day."""
    import executor

    plan = _make_plan([])
    plan["workingHours"] = {"startTime": "08:00"}
    assert executor._is_working_day(plan) is True


def test_is_working_day_today_allowed():
    import executor

    plan = _make_plan([])
    plan["workingHours"] = {"days": ["WED"]}
    fake_now = datetime(2025, 1, 1, 12, 0, tzinfo=None)  # Wednesday

    with patch("executor.datetime", **{"now.return_value": fake_now}):
        assert executor._is_working_day(plan) is True


def test_is_working_day_today_not_allowed():
    import executor

    plan = _make_plan([])
    plan["workingHours"] = {"days": ["MON", "TUE", "THU", "FRI", "SAT", "SUN"]}
    fake_now = datetime(2025, 1, 1, 12, 0, tzinfo=None)  # Wednesday, not in list

    with patch("executor.datetime", **{"now.return_value": fake_now}):
        assert executor._is_working_day(plan) is False


# B2-D: force_start_campaign startedAt reset


def test_force_start_resets_started_at():
    """Reactivating a completed bucket must reset startedAt so elapsed_min restarts at 0."""
    import executor

    plan = _make_plan(
        [_bucket_def("b0", [_campaign_def("c0")], run_mode="time_based", duration=30)]
    )
    cs = _campaign_state("c0", "cancelled", exit_reason="parent_cancelled")
    bs = _bucket_state("b0", [cs], status="completed")
    bs["startedAt"] = "2026-05-10T08:00:00+00:00"  # hours ago
    run = _make_run(plan, [bs])

    with (
        patch("executor.get_run", return_value=run),
        patch("executor.save_run"),
        patch("executor._schedule_tick", return_value="sched-1"),
        patch("executor._start_one_campaign"),
        patch("executor._reset_cascade_cancelled_children"),
    ):
        executor.force_start_campaign("plan-1", "run-1", 0, 0)

    assert bs["startedAt"] != "2026-05-10T08:00:00+00:00", (
        "startedAt must be reset on bucket reactivation"
    )
    assert bs["status"] == "running"


# Bug: a 5th revert-to-queued path (the warming-bucket sibling cleanup below)
# never reset reconcileRetries — same sticky-field masking risk as BD-020,
# just a path BD-020 missed (root-caused 2026-08-27, second adversarial
# review round).


def test_force_start_campaign_resets_sibling_reconcile_retries_in_warming_cleanup():
    """The warming-bucket sibling cleanup in force_start_campaign must also
    clear reconcileRetries on the siblings it reverts to queued."""
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0"), _campaign_def("c1")])])
    target_cs = _campaign_state("c0", "queued")
    sibling_cs = _campaign_state("c1", "warming")
    sibling_cs["reconcileRetries"] = 2  # stale, from an earlier finished retry cycle
    run = _make_run(
        plan, [_bucket_state("b0", [target_cs, sibling_cs], status="warming")]
    )

    with (
        patch("executor.get_run", return_value=run),
        patch("executor.save_run"),
        patch("executor._schedule_tick", return_value="sched-1"),
        patch("executor._start_one_campaign"),
        patch("executor._reset_cascade_cancelled_children"),
    ):
        executor.force_start_campaign("plan-1", "run-1", 0, 0)

    assert sibling_cs["status"] == "queued"
    assert sibling_cs["reconcileRetries"] == 0


# Bug: force_start Phase 1 must clear connectCampaignId to prevent duplicate Connect campaigns


def test_force_start_clears_connect_campaign_id_in_phase1_save():
    """Phase 1 claim save must clear connectCampaignId.

    If Phase 3 creates a Connect campaign but the final save_run crashes, tick recovery
    sees creating + null connectCampaignId and safely resets to queued. Without this fix,
    recovery would see the stale old connectCampaignId, try to restart a deleted campaign,
    set the campaign to error, and a subsequent force-start would create a duplicate in Connect.
    """
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    cs = _campaign_state("c0", "error")
    cs["connectCampaignId"] = "old-connect-id"
    cs["segmentArn"] = "old-segment-arn"
    cs["segmentName"] = "old-segment-name"
    run = _make_run(plan, [_bucket_state("b0", [cs], status="running")])

    saved_states: list[dict] = []

    def capture_save(r):
        import copy

        saved_states.append(copy.deepcopy(r["bucketStates"][0]["campaignStates"][0]))

    with (
        patch("executor.get_run", return_value=run),
        patch("executor.save_run", side_effect=capture_save),
        patch("executor._safe_stop_campaign"),
        patch("executor._safe_delete_campaign"),
        patch("executor._start_one_campaign"),
        patch("executor._reset_cascade_cancelled_children"),
    ):
        executor.force_start_campaign("plan-1", "run-1", 0, 0)

    # Phase 1 save (first call) must have connectCampaignId cleared
    phase1_save = saved_states[0]
    assert phase1_save["status"] == "creating"
    assert phase1_save["connectCampaignId"] is None, (
        "Phase 1 must save connectCampaignId=None so that if Phase 3 Connect call succeeds "
        "but final save_run crashes, tick recovery can safely reset to queued"
    )
    assert phase1_save["segmentArn"] is None
    assert phase1_save["segmentName"] is None


# Bug: force_start Phase 1 must reset reconcileRetries — otherwise manual force-starts
# consume the same limited empty-segment retry budget as automatic ticks, and once a
# prior run of attempts exhausts it, every subsequent force-start is an instant
# skipped_empty no-op with zero real retries left (root-caused 2026-08-27, CT campaign).


def test_force_start_resets_reconcile_retries_in_phase1_save():
    """Phase 1 claim save must reset reconcileRetries to 0.

    A manual force-start is a deliberate fresh attempt — it must not inherit a
    retry count left over from earlier automatic or manual attempts, or it can
    exhaust reconcile_retry_limit and cancel with skipped_empty before ever
    checking whether the segment is populated on this attempt.
    """
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    cs = _campaign_state("c0", "cancelled", exit_reason="skipped_empty")
    cs["reconcileRetries"] = 5
    run = _make_run(plan, [_bucket_state("b0", [cs], status="running")])

    saved_states: list[dict] = []

    def capture_save(r):
        import copy

        saved_states.append(copy.deepcopy(r["bucketStates"][0]["campaignStates"][0]))

    with (
        patch("executor.get_run", return_value=run),
        patch("executor.save_run", side_effect=capture_save),
        patch("executor._safe_stop_campaign"),
        patch("executor._safe_delete_campaign"),
        patch("executor._start_one_campaign"),
        patch("executor._reset_cascade_cancelled_children"),
    ):
        executor.force_start_campaign("plan-1", "run-1", 0, 0)

    phase1_save = saved_states[0]
    assert phase1_save["reconcileRetries"] == 0, (
        "Phase 1 must reset reconcileRetries so a manual force-start gets a fresh "
        "empty-segment retry budget instead of inheriting a previously exhausted one"
    )


# S10-F: force_start_campaign Phase-1 save rolls back a just-created schedule
# on ConcurrentWriteError (audit follow-up, 2026-08-21) — otherwise the schedule's
# name never persists anywhere and it orphans forever, same shape as BD-013.


def test_force_start_phase1_save_rolls_back_new_schedule_on_concurrent_write():
    import executor
    from executor import ConcurrentWriteError

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    cs = _campaign_state("c0", "queued")
    bs = _bucket_state("b0", [cs], status="queued", schedule_name=None)
    run = _make_run(plan, [bs])

    with (
        patch("executor.get_run", return_value=run),
        patch("executor.save_run", side_effect=ConcurrentWriteError("raced")),
        patch("executor._schedule_tick", return_value="vip-plan-plan-1-run-run-1-b0"),
        patch("executor._reset_cascade_cancelled_children"),
        patch("executor._delete_schedule_safe") as mock_delete,
    ):
        with pytest.raises(ConcurrentWriteError):
            executor.force_start_campaign("plan-1", "run-1", 0, 0)

    mock_delete.assert_called_once_with("vip-plan-plan-1-run-run-1-b0")


def test_force_start_phase1_save_no_rollback_when_no_schedule_created():
    import executor
    from executor import ConcurrentWriteError

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    cs = _campaign_state("c0", "queued")
    # Bucket already "running" — force_start_campaign does not call _schedule_tick
    # for an already-running bucket, so nothing new exists to roll back.
    bs = _bucket_state("b0", [cs], status="running", schedule_name="sched-existing")
    run = _make_run(plan, [bs])

    with (
        patch("executor.get_run", return_value=run),
        patch("executor.save_run", side_effect=ConcurrentWriteError("raced")),
        patch("executor._reset_cascade_cancelled_children"),
        patch("executor._delete_schedule_safe") as mock_delete,
    ):
        with pytest.raises(ConcurrentWriteError):
            executor.force_start_campaign("plan-1", "run-1", 0, 0)

    mock_delete.assert_not_called()


# S10-E: force_start_campaign final-save retry loop


def test_force_start_final_save_retries_on_concurrent_write():
    """force_start_campaign retries the final save on ConcurrentWriteError and succeeds on second attempt.

    The first save_run fails (tick raced), then get_run returns fresh state and the second save_run succeeds.
    The campaign must end up in 'running' state.
    """
    import executor
    from executor import ConcurrentWriteError

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    cs = _campaign_state("c0", "queued")
    run = _make_run(plan, [_bucket_state("b0", [cs], status="running")])

    # After _start_one_campaign, cs is modified in-place to "running"
    def fake_start(r, p, bi, ci):
        r["bucketStates"][bi]["campaignStates"][ci]["status"] = "running"
        r["bucketStates"][bi]["campaignStates"][ci]["connectCampaignId"] = "conn-new"
        r["bucketStates"][bi]["campaignStates"][ci]["startedAt"] = (
            "2026-05-19T10:00:00+00:00"
        )

    # Fresh run returned by get_run after the concurrent write (still "creating", DDB version advanced)
    def make_fresh_run():
        fresh_cs = _campaign_state("c0", "creating")
        fresh_cs["creatingAt"] = "2026-05-19T09:59:50+00:00"
        fresh_cs["connectCampaignId"] = None
        fresh_run = _make_run(plan, [_bucket_state("b0", [fresh_cs], status="running")])
        fresh_run["_version"] = 99  # advanced by the concurrent tick
        return fresh_run

    save_calls = []

    def fake_save(r):
        save_calls.append(1)
        if (
            len(save_calls) == 2
        ):  # Phase 1 save succeeds; only Phase 3 first attempt fails
            raise ConcurrentWriteError("concurrent write")

    with (
        patch("executor.get_run", side_effect=[run, make_fresh_run()]),
        patch("executor.save_run", side_effect=fake_save),
        patch("executor._schedule_tick", return_value="sched-1"),
        patch("executor._start_one_campaign", side_effect=fake_start),
        patch("executor._reset_cascade_cancelled_children"),
    ):
        result = executor.force_start_campaign("plan-1", "run-1", 0, 0)

    final_cs = result["bucketStates"][0]["campaignStates"][0]
    assert final_cs["status"] == "running"
    assert final_cs["connectCampaignId"] == "conn-new"
    assert "creatingAt" not in final_cs or final_cs.get("creatingAt") is None


def test_force_start_final_save_returns_early_when_tick_already_adopted():
    """force_start_campaign returns success when a concurrent tick already set status=running with the same ID.

    After the first final save_run fails, get_run returns state where the tick already
    adopted the campaign to 'running' with the expected connectCampaignId — no re-apply needed.
    """
    import executor
    from executor import ConcurrentWriteError

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    cs = _campaign_state("c0", "queued")
    run = _make_run(plan, [_bucket_state("b0", [cs], status="running")])

    def fake_start(r, p, bi, ci):
        r["bucketStates"][bi]["campaignStates"][ci]["status"] = "running"
        r["bucketStates"][bi]["campaignStates"][ci]["connectCampaignId"] = (
            "conn-adopted"
        )

    # Fresh run from DDB: tick already set status=running with the same connectCampaignId
    def make_tick_adopted_run():
        adopted_cs = _campaign_state("c0", "running", connect_id="conn-adopted")
        return _make_run(plan, [_bucket_state("b0", [adopted_cs], status="running")])

    phase1_calls = [False]

    def fake_save(r):
        if not phase1_calls[0]:
            phase1_calls[0] = True
            return  # Phase 1 save succeeds
        raise ConcurrentWriteError("concurrent write on final save")

    with (
        patch("executor.get_run", side_effect=[run, make_tick_adopted_run()]),
        patch("executor.save_run", side_effect=fake_save),
        patch("executor._schedule_tick", return_value="sched-1"),
        patch("executor._start_one_campaign", side_effect=fake_start),
        patch("executor._reset_cascade_cancelled_children"),
    ):
        result = executor.force_start_campaign("plan-1", "run-1", 0, 0)

    final_cs = result["bucketStates"][0]["campaignStates"][0]
    assert final_cs["status"] == "running"
    assert final_cs["connectCampaignId"] == "conn-adopted"


# B3-A: _get_campaign_state distinguishes 404


def test_get_campaign_state_returns_deleted_on_404():
    """ResourceNotFoundException from Connect must return 'Deleted', not 'Unknown'."""
    import sys
    from unittest.mock import MagicMock
    import executor
    from botocore.exceptions import ClientError

    not_found = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}},
        "GetCampaignState",
    )

    oc_mock = type(
        "OC",
        (),
        {"get_campaign_state": lambda self, cid: (_ for _ in ()).throw(not_found)},
    )()

    modules_to_stub = [
        "vip_shared",
        "vip_shared.infrastructure",
        "vip_shared.infrastructure.persistence",
        "vip_shared.infrastructure.persistence.outbound_campaigns_client",
    ]
    vip_stub = MagicMock()
    originals = {m: sys.modules.get(m) for m in modules_to_stub}
    for m in modules_to_stub:
        sys.modules[m] = vip_stub
    vip_stub.build = MagicMock(return_value=oc_mock)

    try:
        result = executor._get_campaign_state("camp-deleted")
    finally:
        for m, orig in originals.items():
            if orig is None:
                sys.modules.pop(m, None)
            else:
                sys.modules[m] = orig

    assert result == "Deleted"


# B3-B: _within_working_hours inclusive end


def test_within_working_hours_inclusive_end():
    """Plan must still be within working hours at 23:59 when endTime defaults to 24:00."""
    import executor
    from datetime import timezone as tz, timedelta

    plan = _make_plan([])
    plan["workingHours"] = {"startTime": "08:00"}  # no endTime → defaults to 24:00

    _COT_TZ = tz(timedelta(hours=-5))
    fake_now = datetime(2026, 5, 10, 23, 59, 0, tzinfo=_COT_TZ)

    with patch("executor.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat
        result = executor._within_working_hours(plan)

    assert result is True, (
        "Plan must be within working hours at 23:59 when endTime is unset"
    )


# ── Timezone fix tests (2026-05-19 Session 7) ────────────────────────────────
# Regression: _daily_cutoff_iso and _past_daily_cutoff previously used
# America/New_York (EDT = UTC-4 in summer), which caused schedule errors.
# These tests verify both functions use COT (UTC-5 fixed) consistently.


def test_daily_cutoff_iso_uses_cot_not_eastern():
    """At 22:56 UTC (17:56 COT), end_time must be 00:00 UTC next day, NOT 23:00 UTC.

    With America/New_York in summer (EDT = UTC-4):
      22:56 UTC → 18:56 EDT → 7 PM EDT would be 23:00 UTC  ← the broken value
    With COT (UTC-5):
      22:56 UTC → 17:56 COT → 7 PM COT = 00:00 UTC next day  ← correct
    """
    import executor

    # 2026-05-19 22:56 UTC = 17:56 COT (still before 19:00 cutoff)
    now_utc = datetime(2026, 5, 19, 22, 56, 0, tzinfo=timezone.utc)
    result = executor._daily_cutoff_iso(now_utc)

    # Expected: 2026-05-20 00:00:00 UTC
    expected = datetime(2026, 5, 20, 0, 0, 0, tzinfo=timezone.utc).isoformat()
    assert result == expected, (
        f"Expected 2026-05-20T00:00:00+00:00 (7 PM COT) but got {result!r} — "
        "timezone mismatch: should use COT (UTC-5), not America/New_York"
    )


def test_daily_cutoff_iso_returns_same_day_before_cutoff():
    """At 15:00 UTC (10:00 COT), end_time is today's 7 PM COT = 00:00 UTC next day."""
    import executor

    now_utc = datetime(2026, 5, 19, 15, 0, 0, tzinfo=timezone.utc)
    result = executor._daily_cutoff_iso(now_utc)

    expected = datetime(2026, 5, 20, 0, 0, 0, tzinfo=timezone.utc).isoformat()
    assert result == expected


def test_daily_cutoff_iso_advances_to_next_day_when_past_cutoff():
    """At 19:30 COT (00:30 UTC next day), end_time advances to the following day's 00:00 UTC."""
    import executor

    # 19:30 COT on May 19 = 00:30 UTC on May 20
    now_utc = datetime(2026, 5, 20, 0, 30, 0, tzinfo=timezone.utc)
    result = executor._daily_cutoff_iso(now_utc)

    expected = datetime(2026, 5, 21, 0, 0, 0, tzinfo=timezone.utc).isoformat()
    assert result == expected, (
        f"When past daily cutoff, end_time must advance to next day. Got {result!r}"
    )


def test_past_daily_cutoff_false_at_1800_cot():
    """18:00 COT must NOT trigger the cutoff — old bug with Eastern fired 1 hour early here."""
    import executor

    # 18:00 COT = 23:00 UTC
    now_utc = datetime(2026, 5, 19, 23, 0, 0, tzinfo=timezone.utc)
    assert executor._past_daily_cutoff(now_utc) is False, (
        "18:00 COT is before the 19:00 cutoff — must return False"
    )


def test_past_daily_cutoff_true_at_1900_cot():
    """19:00 COT (00:00 UTC) must trigger the cutoff."""
    import executor

    # 19:00 COT = 00:00 UTC next day
    now_utc = datetime(2026, 5, 20, 0, 0, 0, tzinfo=timezone.utc)
    assert executor._past_daily_cutoff(now_utc) is True, (
        "19:00 COT is the cutoff boundary — must return True"
    )


def test_past_daily_cutoff_true_at_2000_cot():
    """20:00 COT (01:00 UTC) is past the cutoff."""
    import executor

    now_utc = datetime(2026, 5, 20, 1, 0, 0, tzinfo=timezone.utc)
    assert executor._past_daily_cutoff(now_utc) is True


# S10-F: _start_one_campaign mid-flight save retry on ConcurrentWriteError


def test_start_one_campaign_mid_flight_save_retries_on_concurrent_write():
    """Mid-flight save retries when first attempt hits ConcurrentWriteError.

    Scenario: Phase-3 claim wrote {creating, null}.  _create_and_start_campaign
    creates campaign 'conn-new'.  First mid-flight save raises ConcurrentWriteError
    (concurrent tick bumped version).  get_run returns fresh run at version+1.
    Second mid-flight save succeeds — DDB now has {creating, conn-new}.
    Phase-5 outer save then writes {running, conn-new}.
    """
    import executor
    from store import ConcurrentWriteError

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")], cleanup=False)])
    run = _make_run(plan, [_bucket_state("b0", [_campaign_state("c0", "creating")])])
    run["_version"] = 10

    fresh_run = _make_run(
        plan, [_bucket_state("b0", [_campaign_state("c0", "creating")])]
    )
    fresh_run["_version"] = 11

    save_calls = []

    def mock_save(_run):
        save_calls.append(_run["_version"])
        if len(save_calls) == 1:
            raise ConcurrentWriteError("version conflict")
        # second call succeeds

    with (
        patch(
            "executor._create_and_start_campaign", return_value=("conn-new", "seg-name")
        ),
        patch("executor._create_segment", return_value=("seg-name", "arn:seg")),
        patch("executor.save_run", side_effect=mock_save),
        patch("executor.get_run", return_value=fresh_run),
    ):
        executor._start_one_campaign(run, plan, 0, 0)

    # Two mid-flight save attempts were made
    assert len(save_calls) == 2
    # Version was refreshed before the retry
    assert save_calls[1] == 11
    # Final status is running and connect ID is set
    cs = run["bucketStates"][0]["campaignStates"][0]
    assert cs["status"] == "running"
    assert cs["connectCampaignId"] == "conn-new"


def test_start_one_campaign_mid_flight_save_exhausts_retries_logs_warning():
    """Mid-flight save logs warning after exhausting all 3 ConcurrentWriteError retries.

    The campaign still transitions to status=running in memory so the outer
    Phase-5 save has a chance to persist the Connect campaign ID.
    """
    import executor
    from store import ConcurrentWriteError

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")], cleanup=False)])
    run = _make_run(plan, [_bucket_state("b0", [_campaign_state("c0", "creating")])])
    run["_version"] = 10

    fresh_run = _make_run(
        plan, [_bucket_state("b0", [_campaign_state("c0", "creating")])]
    )
    fresh_run["_version"] = 11

    with (
        patch(
            "executor._create_and_start_campaign", return_value=("conn-new", "seg-name")
        ),
        patch("executor._create_segment", return_value=("seg-name", "arn:seg")),
        patch("executor.save_run", side_effect=ConcurrentWriteError("always fails")),
        patch("executor.get_run", return_value=fresh_run),
    ):
        executor._start_one_campaign(run, plan, 0, 0)

    # Despite all mid-flight saves failing, status is running and ID is in memory
    # so Phase-5 outer save can still persist it
    cs = run["bucketStates"][0]["campaignStates"][0]
    assert cs["status"] == "running"
    assert cs["connectCampaignId"] == "conn-new"


# _safe_delete_campaign: Completed campaigns must not trigger stop before delete


def _stub_vip_delete_client(oc):
    """Return (originals, vip_stub) with sys.modules patched for _safe_delete_campaign."""
    import sys
    from unittest.mock import MagicMock

    vip_stub = MagicMock()
    vip_stub.build.return_value = oc
    modules = [
        "vip_shared",
        "vip_shared.infrastructure",
        "vip_shared.infrastructure.persistence",
        "vip_shared.infrastructure.persistence.outbound_campaigns_client",
    ]
    originals = {m: sys.modules.get(m) for m in modules}
    for m in modules:
        sys.modules[m] = vip_stub
    return originals


def _restore_vip_modules(originals):
    import sys

    for m, orig in originals.items():
        if orig is None:
            sys.modules.pop(m, None)
        else:
            sys.modules[m] = orig


def test_safe_delete_campaign_skips_stop_for_completed():
    """_safe_delete_campaign must NOT call stop_campaign for a COMPLETED campaign.

    Previously, 'Completed' was not in the skip-stop list, so stop_campaign raised
    ConflictException which short-circuited delete_campaign — campaigns were never
    actually deleted (delete-after-run silent failure).
    """
    import executor
    from unittest.mock import MagicMock

    oc = MagicMock()
    originals = _stub_vip_delete_client(oc)
    try:
        with patch("executor._get_campaign_state", return_value="Completed"):
            executor._safe_delete_campaign("camp-completed")
    finally:
        _restore_vip_modules(originals)

    oc.stop_campaign.assert_not_called()
    oc.delete_campaign.assert_called_once_with("camp-completed")


def test_safe_delete_campaign_stops_before_delete_for_running():
    """_safe_delete_campaign calls stop_campaign then delete_campaign for Running campaigns."""
    import executor
    from unittest.mock import MagicMock

    oc = MagicMock()
    originals = _stub_vip_delete_client(oc)
    try:
        with patch("executor._get_campaign_state", return_value="Running"):
            executor._safe_delete_campaign("camp-running")
    finally:
        _restore_vip_modules(originals)

    oc.stop_campaign.assert_called_once_with("camp-running")
    oc.delete_campaign.assert_called_once_with("camp-running")


def test_create_campaign_only_guard_raises_when_start_gte_end():
    """_create_campaign_only must raise ValueError when startTime >= endTime.

    Simulates the scenario where _campaign_end_time returns a time <= start_time
    (e.g., a stale Eastern-based end_time, or any regression in end-time computation).
    The guard must catch this before passing invalid params to Connect.
    """
    import sys
    from unittest.mock import MagicMock
    import executor

    bucket = {
        "id": "B1",
        "name": "B1",
        "duration": 60,
        "campaigns": [],
        "segmentFilters": {"state": ["TX"]},
    }
    campaign = {
        "id": "c1",
        "name": "TX-NL",
        "states": ["TX"],
        "groups": [],
        "dependsOn": [],
    }

    run = {"runId": "r1", "planId": "p1", "campaigns": []}

    now_utc = datetime(2026, 5, 19, 22, 56, 0, tzinfo=timezone.utc)
    # start_dt = now + 6min = 23:02 UTC
    # Simulate the OLD Eastern bug: _campaign_end_time returns 23:00 UTC (before start)
    stale_end_time = datetime(2026, 5, 19, 23, 0, 0, tzinfo=timezone.utc).isoformat()

    # Stub vip_shared into sys.modules so the local import inside _create_campaign_only succeeds
    vip_stub = MagicMock()
    modules_to_stub = [
        "vip_shared",
        "vip_shared.infrastructure",
        "vip_shared.infrastructure.persistence",
        "vip_shared.infrastructure.persistence.outbound_campaigns_client",
    ]
    originals = {m: sys.modules.get(m) for m in modules_to_stub}
    for m in modules_to_stub:
        sys.modules[m] = vip_stub

    try:
        with (
            patch("executor._now_utc", return_value=now_utc),
            patch("executor._create_segment", return_value=("seg-name", "arn:seg")),
            patch("executor._campaign_end_time", return_value=stale_end_time),
        ):
            with pytest.raises(
                executor._CutoffTooCloseError, match="too close to daily cutoff"
            ):
                executor._create_campaign_only(bucket, campaign, run)
    finally:
        for m, orig in originals.items():
            if orig is None:
                sys.modules.pop(m, None)
            else:
                sys.modules[m] = orig


def test_create_and_start_campaign_guard_raises_when_start_gte_end():
    """_create_and_start_campaign must raise _CutoffTooCloseError when startTime >= endTime."""
    import sys
    from unittest.mock import MagicMock
    import executor

    bucket = {
        "id": "B1",
        "name": "B1",
        "campaigns": [],
        "segmentFilters": {"state": ["NJ"]},
    }
    campaign = {"id": "c1", "name": "NJ-NL", "states": ["NJ"], "run_type": "full"}

    now_utc = datetime(2026, 5, 19, 22, 56, 0, tzinfo=timezone.utc)
    stale_end_time = datetime(2026, 5, 19, 23, 0, 0, tzinfo=timezone.utc).isoformat()

    vip_stub = MagicMock()
    modules_to_stub = [
        "vip_shared",
        "vip_shared.infrastructure",
        "vip_shared.infrastructure.persistence",
        "vip_shared.infrastructure.persistence.outbound_campaigns_client",
    ]
    originals = {m: sys.modules.get(m) for m in modules_to_stub}
    for m in modules_to_stub:
        sys.modules[m] = vip_stub

    try:
        with (
            patch("executor._now_utc", return_value=now_utc),
            patch("executor._campaign_end_time", return_value=stale_end_time),
            patch("executor._account_id", return_value="123456789012"),
            patch("executor.resolve_campaign_flow_arn", return_value="arn:flow"),
        ):
            with pytest.raises(
                executor._CutoffTooCloseError, match="too close to daily cutoff"
            ):
                executor._create_and_start_campaign(
                    bucket, campaign, "arn:seg", "seg-name", now_utc
                )
    finally:
        for m, orig in originals.items():
            if orig is None:
                sys.modules.pop(m, None)
            else:
                sys.modules[m] = orig


def test_start_one_campaign_cutoff_too_close_marks_expired():
    """_start_one_campaign marks campaign expired (not error) when too close to daily cutoff."""
    import executor

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c0")])])
    run = _make_run(plan, [_bucket_state("b0", [_campaign_state("c0")])])
    bi, ci = 0, 0
    cs = run["bucketStates"][bi]["campaignStates"][ci]
    cs["status"] = "creating"

    with (
        patch("executor._create_segment", return_value=("seg", "arn:seg")),
        patch(
            "executor._create_and_start_campaign",
            side_effect=executor._CutoffTooCloseError(
                "start >= end — too close to daily cutoff"
            ),
        ),
        patch("executor._safe_delete_segment"),
        patch("executor.save_run"),
    ):
        executor._start_one_campaign(run, plan, bi, ci)

    assert cs["status"] == "expired"
    assert cs["exitReason"] == "cutoff_too_close"
    assert cs["completedAt"] is not None


# ── store.apply_plan_to_run ───────────────────────────────────────────────────


def _make_store_run(plan: dict, bucket_statuses: list[str]) -> dict:
    """Build a minimal run dict as store.get_run would return it."""
    bucket_states = [
        _bucket_state(f"b{i}", [_campaign_state(f"c{i}0")], status=s)
        for i, s in enumerate(bucket_statuses)
    ]
    return _make_run(plan, bucket_states)


def test_apply_plan_to_run_updates_queued_buckets():
    import store

    old_bucket = _bucket_def("b0", [_campaign_def("c00")], duration=30)
    new_bucket = _bucket_def(
        "b0-new", [_campaign_def("c00"), _campaign_def("c01")], duration=60
    )
    plan = _make_plan([old_bucket])
    run = _make_store_run(plan, ["queued"])

    new_plan = _make_plan([new_bucket])
    with patch("store.get_run", return_value=run), patch("store.save_run"):
        result = store.apply_plan_to_run("plan-1", "run-1", new_plan)

    assert result["planSnapshot"]["buckets"][0]["duration_minutes"] == 60
    assert len(result["planSnapshot"]["buckets"][0]["campaigns"]) == 2


def test_apply_plan_to_run_preserves_started_buckets():
    import store

    old_bucket = _bucket_def("b0", [_campaign_def("c00")], duration=30)
    new_bucket = _bucket_def("b0-new", [_campaign_def("c00")], duration=99)
    plan = _make_plan([old_bucket])
    run = _make_store_run(plan, ["running"])  # already started

    new_plan = _make_plan([new_bucket])
    with patch("store.get_run", return_value=run), patch("store.save_run"):
        result = store.apply_plan_to_run("plan-1", "run-1", new_plan)

    # Running bucket must keep original duration
    assert result["planSnapshot"]["buckets"][0]["duration_minutes"] == 30


def test_apply_plan_to_run_rejects_non_running_run():
    import store

    plan = _make_plan([_bucket_def("b0", [_campaign_def("c00")])])
    run = _make_store_run(plan, ["queued"])
    run["status"] = "completed"

    with patch("store.get_run", return_value=run):
        with pytest.raises(ValueError, match="not running"):
            store.apply_plan_to_run("plan-1", "run-1", plan)


def test_apply_plan_to_run_live_plan_shorter():
    """Queued buckets beyond live_plan length keep their original config."""
    import store

    b0 = _bucket_def("b0", [_campaign_def("c00")], duration=30)
    b1 = _bucket_def("b1", [_campaign_def("c10")], duration=45)
    plan = _make_plan([b0, b1])
    run = _make_store_run(plan, ["completed", "queued"])  # b0 done, b1 queued

    new_plan = _make_plan([b0])  # only 1 bucket in live plan
    with patch("store.get_run", return_value=run), patch("store.save_run"):
        result = store.apply_plan_to_run("plan-1", "run-1", new_plan)

    # b1 (index 1) is queued but beyond live_plan length — keep original
    assert result["planSnapshot"]["buckets"][1]["duration_minutes"] == 45


# ── _is_branded ──────────────────────────────────────────────────────────────

class TestIsBranded:
    def test_returns_true_for_branded_delivery_type(self):
        from executor import _is_branded
        assert _is_branded({"deliveryType": "branded"}) is True

    def test_returns_false_for_connect_campaign(self):
        from executor import _is_branded
        assert _is_branded({"campaignConfig": {"dialerType": "progressive"}}) is False

    def test_returns_false_when_delivery_type_absent(self):
        from executor import _is_branded
        assert _is_branded({}) is False

    def test_returns_false_for_other_delivery_type(self):
        from executor import _is_branded
        assert _is_branded({"deliveryType": "journey"}) is False


class TestInitialCampaignStateFields:
    def test_branded_campaign_id_initialized_to_none(self):
        from store import _initial_campaign_state
        cs = _initial_campaign_state({"id": "c-1"})
        assert "brandedCampaignId" in cs
        assert cs["brandedCampaignId"] is None

    def test_queue_arn_initialized_to_none(self):
        from store import _initial_campaign_state
        cs = _initial_campaign_state({"id": "c-1"})
        assert "queueArn" in cs
        assert cs["queueArn"] is None


class TestStartBrandedCampaign:
    """_start_one_campaign with deliveryType='branded'."""

    @pytest.fixture(autouse=True)
    def _branded_env(self, mocker):
        """Ensure module-level branded env vars are non-empty so the validation guard passes."""
        mocker.patch("executor._ACTIVE_BRANDED_CAMPAIGNS_TABLE", "VipActiveBrandedCampaigns")
        mocker.patch("executor._CAMPAIGN_QUEUE_TABLE_BRANDED", "VipProgressiveCampaignQueue")

    def _branded_campaign(
        self, campaign_id: str = "bc-1", queue_arn: str = "arn:aws:connect:::queue/q1"
    ) -> dict:
        return {
            "id": campaign_id,
            "name": "Branded Test",
            "deliveryType": "branded",
            "campaignConfig": {
                "dialerType": "progressive",
                "queueArn": queue_arn,
                "contactFlowId": "flow-abc",
                "sourcePhone": "+12125550199",
            },
        }

    def _make_run_with_branded(self, campaign_id: str = "bc-1"):
        import json  # noqa: F401 — used for payload assertions in tests below
        bucket = _bucket_def("b-branded", campaigns=[self._branded_campaign(campaign_id)])
        plan = {"planId": "p-1", "buckets": [bucket]}
        cs = _campaign_state(campaign_id, status="queued")
        run = {
            "planId": "p-1",
            "runId": "r-1",
            "bucketStates": [{"status": "running", "campaignStates": [cs]}],
        }
        return run, plan

    def test_invokes_seeder_lambda(self, mocker):
        import json
        from unittest.mock import MagicMock

        run, plan = self._make_run_with_branded()
        mocker.patch("executor._create_segment", return_value=("seg-name", "seg-arn"))
        lam = mocker.patch("executor._get_lambda_client").return_value
        lam.invoke.return_value = {
            "Payload": MagicMock(read=lambda: json.dumps({"seeded": 5}).encode())
        }
        mocker.patch("executor._get_ddb_client").return_value.put_item.return_value = {}

        from executor import _start_one_campaign
        _start_one_campaign(run, plan, 0, 0)

        lam.invoke.assert_called_once()
        call_kwargs = lam.invoke.call_args.kwargs or lam.invoke.call_args[1]
        raw_payload = call_kwargs.get("Payload") or lam.invoke.call_args[0][0].get("Payload")
        payload = json.loads(raw_payload)
        # campaignId is now a deterministic uuid5(planId#runId#bucket#campaign)
        assert payload["campaignId"] == "51577901-1d23-54c8-a268-6ab3b2acd289"
        assert payload["segmentName"] == "seg-name"

    def test_writes_vip_active_branded_campaigns(self, mocker):
        import json
        from unittest.mock import MagicMock

        run, plan = self._make_run_with_branded()
        mocker.patch("executor._create_segment", return_value=("seg-name", "seg-arn"))
        lam = mocker.patch("executor._get_lambda_client").return_value
        lam.invoke.return_value = {
            "Payload": MagicMock(read=lambda: json.dumps({"seeded": 3}).encode())
        }
        ddb = mocker.patch("executor._get_ddb_client").return_value

        from executor import _start_one_campaign
        _start_one_campaign(run, plan, 0, 0)

        ddb.put_item.assert_called_once()
        item = ddb.put_item.call_args.kwargs["Item"]
        assert item["pk"]["S"] == "QUEUE#arn:aws:connect:::queue/q1"
        assert item["sk"]["S"] == "CAMPAIGN#51577901-1d23-54c8-a268-6ab3b2acd289"
        assert item["campaignId"]["S"] == "51577901-1d23-54c8-a268-6ab3b2acd289"

    def test_sets_status_running_and_branded_campaign_id(self, mocker):
        import json
        from unittest.mock import MagicMock

        run, plan = self._make_run_with_branded()
        mocker.patch("executor._create_segment", return_value=("seg-name", "seg-arn"))
        lam = mocker.patch("executor._get_lambda_client").return_value
        lam.invoke.return_value = {
            "Payload": MagicMock(read=lambda: json.dumps({"seeded": 2}).encode())
        }
        mocker.patch("executor._get_ddb_client").return_value.put_item.return_value = {}

        from executor import _start_one_campaign
        cs = run["bucketStates"][0]["campaignStates"][0]
        _start_one_campaign(run, plan, 0, 0)

        assert cs["status"] == "running"
        assert cs["brandedCampaignId"] == "51577901-1d23-54c8-a268-6ab3b2acd289"
        assert cs["queueArn"] == "arn:aws:connect:::queue/q1"
        assert cs["connectCampaignId"] is None

    def test_empty_segment_sets_completed_immediately(self, mocker):
        import json
        from unittest.mock import MagicMock

        run, plan = self._make_run_with_branded()
        mocker.patch("executor._create_segment", return_value=("seg-name", "seg-arn"))
        lam = mocker.patch("executor._get_lambda_client").return_value
        lam.invoke.return_value = {
            "Payload": MagicMock(read=lambda: json.dumps({"seeded": 0}).encode())
        }
        ddb = mocker.patch("executor._get_ddb_client").return_value
        # Isolate _stop_branded_campaign's expire call (tested separately in TestStopBrandedCampaign)
        mocker.patch("executor._expire_branded_queue_items")

        from executor import _start_one_campaign
        cs = run["bucketStates"][0]["campaignStates"][0]
        _start_one_campaign(run, plan, 0, 0)

        assert cs["status"] == "completed"
        ddb.put_item.assert_called_once()  # lock claimed before invoking seeder

    def test_seeder_error_sets_error_status(self, mocker):
        run, plan = self._make_run_with_branded()
        mocker.patch("executor._create_segment", return_value=("seg-name", "seg-arn"))
        lam = mocker.patch("executor._get_lambda_client").return_value
        lam.invoke.side_effect = Exception("Lambda timeout")
        ddb = mocker.patch("executor._get_ddb_client").return_value
        # Cleanup is exercised by its own tests; isolate the status transition here.
        mocker.patch("executor._expire_branded_queue_items")

        from executor import _start_one_campaign
        cs = run["bucketStates"][0]["campaignStates"][0]
        _start_one_campaign(run, plan, 0, 0)

        assert cs["status"] == "error"
        ddb.put_item.assert_called_once()  # lock was claimed before seeder ran

    def test_does_not_call_create_and_start_campaign(self, mocker):
        import json
        from unittest.mock import MagicMock

        run, plan = self._make_run_with_branded()
        mocker.patch("executor._create_segment", return_value=("seg-name", "seg-arn"))
        lam = mocker.patch("executor._get_lambda_client").return_value
        lam.invoke.return_value = {
            "Payload": MagicMock(read=lambda: json.dumps({"seeded": 1}).encode())
        }
        mocker.patch("executor._get_ddb_client").return_value.put_item.return_value = {}
        create_fn = mocker.patch("executor._create_and_start_campaign")

        from executor import _start_one_campaign
        _start_one_campaign(run, plan, 0, 0)

        create_fn.assert_not_called()


# ── TestTickBrandedPoll ───────────────────────────────────────────────────────


class TestStopBrandedCampaign:
    def _cs(self, campaign_id="bc-1", queue_arn="arn::queue/q1"):
        return {
            "campaignId": campaign_id,
            "brandedCampaignId": campaign_id,
            "queueArn": queue_arn,
            "status": "running",
        }

    def test_deletes_from_vip_active_branded_campaigns(self, mocker):
        ddb = mocker.patch("executor._get_ddb_client").return_value
        mocker.patch("executor._expire_branded_queue_items")

        from executor import _stop_branded_campaign
        _stop_branded_campaign(self._cs())

        ddb.delete_item.assert_called_once_with(
            TableName=mocker.ANY,
            Key={
                "pk": {"S": "QUEUE#arn::queue/q1"},
                "sk": {"S": "CAMPAIGN#bc-1"},
            },
            ConditionExpression="attribute_exists(pk)",
        )

    def test_expires_queue_items(self, mocker):
        mocker.patch("executor._get_ddb_client").return_value.delete_item.return_value = {}
        expire = mocker.patch("executor._expire_branded_queue_items")

        from executor import _stop_branded_campaign
        _stop_branded_campaign(self._cs())

        expire.assert_called_once_with("bc-1")

    def test_noop_when_no_branded_campaign_id(self, mocker):
        ddb = mocker.patch("executor._get_ddb_client").return_value

        from executor import _stop_branded_campaign
        _stop_branded_campaign({"campaignId": "c-1", "status": "running"})

        ddb.delete_item.assert_not_called()

    def test_delete_failure_does_not_raise(self, mocker):
        mocker.patch("executor._get_ddb_client").return_value.delete_item.side_effect = (
            Exception("DDB error")
        )
        mocker.patch("executor._expire_branded_queue_items")

        from executor import _stop_branded_campaign
        _stop_branded_campaign(self._cs())  # must not raise

    def test_expire_failure_does_not_raise(self, mocker):
        mocker.patch("executor._get_ddb_client").return_value.delete_item.return_value = {}
        mocker.patch("executor._expire_branded_queue_items", side_effect=Exception("batch fail"))

        from executor import _stop_branded_campaign
        _stop_branded_campaign(self._cs())  # must not raise


class TestPrestartSkipsBranded:
    """_prestart_next_bucket must skip branded campaigns and only warmup non-branded ones."""

    def _branded_campaign_def(self, cid: str = "bc-1") -> dict:
        return {
            "id": cid,
            "name": cid,
            "deliveryType": "branded",
            "states": ["NY"],
            "groups": [],
            "dependsOn": [],
            "campaignConfig": {"queueArn": "arn::queue/q1"},
        }

    def test_prestart_next_bucket_skips_branded(self, mocker):
        """_create_campaign_only must NOT be called for a branded campaign during prewarm."""
        import executor

        plan = _make_plan(
            [
                _bucket_def("b0", [_campaign_def("c0")], run_mode="time_based", duration=20),
                _bucket_def("b1", [self._branded_campaign_def("bc-1")]),
            ]
        )
        run = _make_run(
            plan,
            [
                _bucket_state("b0", [_campaign_state("c0", "running")]),
                _bucket_state("b1", [_campaign_state("bc-1", "queued")], status="queued"),
            ],
        )

        create = mocker.patch("executor._create_campaign_only")
        mocker.patch("executor.save_run")

        executor._prestart_next_bucket(run, plan, 0)

        # Branded campaign must be skipped — no warmup phase
        create.assert_not_called()
        # Bucket status set to warming regardless (the bucket-level claim is still made)
        assert run["bucketStates"][1]["status"] == "warming"
        # Campaign itself stays queued — it will be started directly by _start_one_campaign
        assert run["bucketStates"][1]["campaignStates"][0]["status"] == "queued"

    def test_prestart_next_bucket_warms_nonbranded_skips_branded(self, mocker):
        """_create_campaign_only is called for non-branded but not for branded in the same bucket."""
        import executor

        plan = _make_plan(
            [
                _bucket_def("b0", [_campaign_def("c0")], run_mode="time_based", duration=20),
                _bucket_def(
                    "b1",
                    [
                        _campaign_def("c-connect"),
                        self._branded_campaign_def("bc-2"),
                    ],
                ),
            ]
        )
        run = _make_run(
            plan,
            [
                _bucket_state("b0", [_campaign_state("c0", "running")]),
                _bucket_state(
                    "b1",
                    [
                        _campaign_state("c-connect", "queued"),
                        _campaign_state("bc-2", "queued"),
                    ],
                    status="queued",
                ),
            ],
        )

        create = mocker.patch(
            "executor._create_campaign_only",
            return_value=("conn-w", "seg-w", "arn:seg-w", True),
        )
        mocker.patch("executor.save_run")

        executor._prestart_next_bucket(run, plan, 0)

        # Exactly one create call — only for the non-branded campaign
        assert create.call_count == 1
        # Non-branded campaign is now warming
        assert run["bucketStates"][1]["campaignStates"][0]["status"] == "warming"
        assert run["bucketStates"][1]["campaignStates"][0]["connectCampaignId"] == "conn-w"
        # Branded campaign stays queued — never passed to _create_campaign_only
        assert run["bucketStates"][1]["campaignStates"][1]["status"] == "queued"
        assert run["bucketStates"][1]["campaignStates"][1].get("connectCampaignId") is None

    def test_prestart_plan_skips_branded_campaign(self, mocker):
        """_prestart_plan must skip branded campaigns and only store non-branded warmups."""
        import executor

        plan = {
            "planId": "plan-target",
            "buckets": [
                _bucket_def(
                    "b0",
                    [
                        _campaign_def("c-connect"),
                        self._branded_campaign_def("bc-3"),
                    ],
                )
            ],
        }

        create = mocker.patch(
            "executor._create_campaign_only",
            return_value=("conn-w2", "seg-w2", "arn:seg-w2", False),
        )
        mocker.patch("executor.get_plan", return_value=plan)
        mocker.patch("executor.get_latest_run", return_value=None)
        update_warmup = mocker.patch("executor.update_plan_pending_warmup")

        executor._prestart_plan("plan-target")

        # Only one create call — for the non-branded campaign
        assert create.call_count == 1
        # The warmup stored must only contain the non-branded campaign
        update_warmup.assert_called_once()
        warmup_arg = update_warmup.call_args[0][1]
        warmed_ids = [c["campaignId"] for c in warmup_arg["campaigns"]]
        assert "c-connect" in warmed_ids
        assert "bc-3" not in warmed_ids


class TestTickBrandedPoll:
    def _run_with_running_branded(self, campaign_id="bc-1"):
        cs = _campaign_state(campaign_id, status="running")
        cs["brandedCampaignId"] = campaign_id
        cs["queueArn"] = "arn::queue/q1"
        cs["connectCampaignId"] = None
        bucket = _bucket_def("b-1", campaigns=[{
            "id": campaign_id, "name": "B", "deliveryType": "branded",
            "campaignConfig": {"queueArn": "arn::queue/q1"},
        }])
        run = {"planId": "p-1", "runId": "r-1", "status": "running",
               "bucketStates": [{"status": "running", "campaignStates": [cs],
                                  "startedAt": datetime.now(timezone.utc).isoformat()}]}
        plan = {"planId": "p-1", "buckets": [bucket]}
        return run, plan, cs

    def test_completes_branded_when_queue_empty(self, mocker):
        run, plan, cs = self._run_with_running_branded()
        mocker.patch("executor._count_branded_queue", return_value=0)
        stop = mocker.patch("executor._stop_branded_campaign")
        mocker.patch("executor.save_run")
        mocker.patch("executor.get_run", return_value=run)
        mocker.patch("executor.get_plan", return_value=plan)
        mocker.patch("executor._advance_bucket")
        mocker.patch("executor._fire_campaign_chains")

        from executor import tick
        tick("p-1", "r-1", 0)

        assert cs["status"] == "completed"
        assert cs["exitReason"] == "queue_drained"
        assert cs.get("completedAt") is not None
        stop.assert_called_once_with(cs)

    def test_does_not_poll_connect_v2_for_branded(self, mocker):
        run, plan, cs = self._run_with_running_branded()
        mocker.patch("executor._count_branded_queue", return_value=5)
        poll = mocker.patch("executor._poll_campaign_state")
        mocker.patch("executor.save_run")
        mocker.patch("executor.get_run", return_value=run)
        mocker.patch("executor.get_plan", return_value=plan)

        from executor import tick
        tick("p-1", "r-1", 0)

        poll.assert_not_called()
        assert cs["status"] == "running"  # still running, queue has 5 items

    def test_count_error_does_not_transition_status(self, mocker):
        run, plan, cs = self._run_with_running_branded()
        mocker.patch("executor._count_branded_queue", side_effect=Exception("DDB error"))
        mocker.patch("executor.save_run")
        mocker.patch("executor.get_run", return_value=run)
        mocker.patch("executor.get_plan", return_value=plan)

        from executor import tick
        tick("p-1", "r-1", 0)  # must not raise

        assert cs["status"] == "running"  # unchanged on poll error

    # Bug: branded campaigns ignore run_duration_minutes entirely (root-caused
    # 2026-08-27) — the telephony branch force-stops on elapsed > duration+2 min
    # (see tick's connectCampaignId branch), but the branded elif never checked
    # elapsed time at all, only queue-drain. A branded campaign configured for
    # 45 min ran 107 min today until a human force-finished it, abandoning 994
    # of 1085 seeded contacts as EXPIRED.

    def test_force_stops_branded_when_duration_exceeded(self, mocker):
        run, plan, cs = self._run_with_running_branded()
        plan["buckets"][0]["campaigns"][0]["run_duration_minutes"] = 45
        cs["startedAt"] = (
            datetime.now(timezone.utc) - timedelta(minutes=50)
        ).isoformat()
        mocker.patch("executor._count_branded_queue", return_value=12)  # still has pending
        stop = mocker.patch("executor._stop_branded_campaign")
        mocker.patch("executor.save_run")
        mocker.patch("executor.get_run", return_value=run)
        mocker.patch("executor.get_plan", return_value=plan)
        mocker.patch("executor._advance_bucket")
        mocker.patch("executor._fire_campaign_chains")

        from executor import tick
        tick("p-1", "r-1", 0)

        assert cs["status"] == "expired"
        assert cs["exitReason"] == "expired"
        assert cs.get("completedAt") is not None
        stop.assert_called_once_with(cs)

    def test_branded_within_duration_does_not_force_stop(self, mocker):
        run, plan, cs = self._run_with_running_branded()
        plan["buckets"][0]["campaigns"][0]["run_duration_minutes"] = 45
        cs["startedAt"] = (
            datetime.now(timezone.utc) - timedelta(minutes=10)
        ).isoformat()
        mocker.patch("executor._count_branded_queue", return_value=12)
        stop = mocker.patch("executor._stop_branded_campaign")
        mocker.patch("executor.save_run")
        mocker.patch("executor.get_run", return_value=run)
        mocker.patch("executor.get_plan", return_value=plan)

        from executor import tick
        tick("p-1", "r-1", 0)

        assert cs["status"] == "running"
        stop.assert_not_called()

    # Bug: duration was checked BEFORE polling the queue, so a campaign that
    # genuinely finished (queue drained) in the same tick its duration was
    # exceeded got marked expired/ABORTED instead of completed (root-caused
    # 2026-08-27, adversarial code review).

    def test_branded_queue_drained_same_tick_duration_exceeded_completes_not_expires(
        self, mocker
    ):
        """count==0 must win over an exceeded duration — the campaign finished
        on its own; it must not be misreported as force-stopped/ABORTED."""
        run, plan, cs = self._run_with_running_branded()
        plan["buckets"][0]["campaigns"][0]["run_duration_minutes"] = 45
        cs["startedAt"] = (
            datetime.now(timezone.utc) - timedelta(minutes=50)
        ).isoformat()
        mocker.patch("executor._count_branded_queue", return_value=0)  # drained
        stop = mocker.patch("executor._stop_branded_campaign")
        mocker.patch("executor.save_run")
        mocker.patch("executor.get_run", return_value=run)
        mocker.patch("executor.get_plan", return_value=plan)
        mocker.patch("executor._advance_bucket")
        mocker.patch("executor._fire_campaign_chains")

        from executor import tick
        tick("p-1", "r-1", 0)

        assert cs["status"] == "completed"
        assert cs["exitReason"] == "queue_drained"
        stop.assert_called_once_with(cs)


class TestAbortStopCallsStopBranded:
    def _cs_branded(self, cid="bc-1"):
        return {
            "campaignId": cid, "brandedCampaignId": cid,
            "queueArn": "arn::queue/q1", "status": "running",
        }

    def test_abort_run_stops_branded_campaign(self, mocker):
        cs = self._cs_branded()
        run = {"planId": "p-1", "runId": "r-1", "status": "running",
               "bucketStates": [{"status": "running", "campaignStates": [cs]}]}
        mocker.patch("executor.get_run", return_value=run)
        stop = mocker.patch("executor._stop_branded_campaign")
        mocker.patch("executor.save_run")
        mocker.patch("executor.update_plan_pending_warmup")
        mocker.patch("executor.unlock_plan_run")
        mocker.patch("executor._delete_bucket_schedule_safe")

        from executor import abort_run
        abort_run("p-1", "r-1")

        stop.assert_called_once_with(cs)

    def test_abort_run_no_branded_campaign_id_does_not_call_stop(self, mocker):
        cs = {"campaignId": "c-1", "status": "running"}
        run = {"planId": "p-1", "runId": "r-1", "status": "running",
               "bucketStates": [{"status": "running", "campaignStates": [cs]}]}
        mocker.patch("executor.get_run", return_value=run)
        stop = mocker.patch("executor._stop_branded_campaign")
        mocker.patch("executor.save_run")
        mocker.patch("executor.update_plan_pending_warmup")
        mocker.patch("executor.unlock_plan_run")
        mocker.patch("executor._delete_bucket_schedule_safe")

        from executor import abort_run
        abort_run("p-1", "r-1")

        stop.assert_not_called()

    def test_expire_bucket_stops_branded_campaign(self, mocker):
        cs = self._cs_branded()
        run = {"planId": "p-1", "runId": "r-1",
               "bucketStates": [{"status": "running", "campaignStates": [cs]}]}
        plan = {"planId": "p-1", "buckets": [_bucket_def("b-1", [])]}
        stop = mocker.patch("executor._stop_branded_campaign")
        mocker.patch("executor._advance_bucket")

        from executor import _expire_bucket
        _expire_bucket(run, plan, 0)

        stop.assert_called_once_with(cs)

    def test_expire_bucket_queued_does_not_stop_branded(self, mocker):
        cs = {**self._cs_branded(), "status": "queued"}
        run = {"planId": "p-1", "runId": "r-1",
               "bucketStates": [{"status": "running", "campaignStates": [cs]}]}
        plan = {"planId": "p-1", "buckets": [_bucket_def("b-1", [])]}
        stop = mocker.patch("executor._stop_branded_campaign")
        mocker.patch("executor._advance_bucket")

        from executor import _expire_bucket
        _expire_bucket(run, plan, 0)

        stop.assert_not_called()

    def test_force_stop_campaign_stops_branded(self, mocker):
        cs = self._cs_branded()
        run = {"planId": "p-1", "runId": "r-1", "status": "running",
               "bucketStates": [{"status": "running", "campaignStates": [cs]}]}
        mocker.patch("executor.get_run", return_value=run)
        stop = mocker.patch("executor._stop_branded_campaign")
        mocker.patch("executor.save_run")
        mocker.patch("executor.unlock_plan_run")
        mocker.patch("executor.get_plan", return_value={"planId": "p-1", "buckets": [_bucket_def("b-1", [])]})
        mocker.patch("executor._all_campaigns_terminal", return_value=False)

        from executor import force_stop_campaign
        force_stop_campaign("p-1", "r-1", 0, 0)

        stop.assert_called_once_with(cs)

    def test_skip_campaign_stops_branded_when_running(self, mocker):
        cs = self._cs_branded()
        run = {"planId": "p-1", "runId": "r-1", "status": "running",
               "bucketStates": [{"status": "running", "campaignStates": [cs]}]}
        mocker.patch("executor.get_run", return_value=run)
        stop = mocker.patch("executor._stop_branded_campaign")
        mocker.patch("executor.save_run")
        mocker.patch("executor.get_plan", return_value={"planId": "p-1", "buckets": [_bucket_def("b-1", [])]})
        mocker.patch("executor._all_campaigns_terminal", return_value=False)
        mocker.patch("executor._dispatch_ready_campaigns", return_value=False)

        from executor import skip_campaign
        skip_campaign("p-1", "r-1", 0, 0)

        stop.assert_called_once_with(cs)

    def test_force_finish_internal_stops_branded(self, mocker):
        cs = self._cs_branded()
        run = {"planId": "p-1", "runId": "r-1", "status": "running",
               "bucketStates": [{"status": "running", "campaignStates": [cs]}]}
        plan = {"planId": "p-1", "buckets": [_bucket_def("b-1", [])]}
        stop = mocker.patch("executor._stop_branded_campaign")
        mocker.patch("executor.save_run")
        mocker.patch("executor.unlock_plan_run")
        mocker.patch("executor.update_plan_pending_warmup")
        mocker.patch("executor._delete_bucket_schedule_safe")

        from executor import _force_finish_internal
        _force_finish_internal(run, plan)

        stop.assert_called_once_with(cs)

    def test_force_start_campaign_stops_branded_of_previous_campaign(self, mocker):
        """force_start_campaign Phase 2: _stop_branded_campaign is called with a synthetic
        dict built from the OLD brandedCampaignId captured before the state reset."""
        cs = {
            "campaignId": "c-1",
            "brandedCampaignId": "bc-old",
            "queueArn": "arn::queue/q1",
            "status": "cancelled",
            "connectCampaignId": None,
            "segmentName": None,
            "segmentArn": None,
            "leadCount": None,
            "startedAt": None,
            "completedAt": None,
            "exitReason": "parent_cancelled",
            "errorDetail": None,
        }
        plan = _make_plan([_bucket_def("b-1", [_campaign_def("c-1")])])
        run = _make_run(plan, [_bucket_state("b-1", [cs], status="running")])

        stop = mocker.patch("executor._stop_branded_campaign")
        mocker.patch("executor.get_run", return_value=run)
        mocker.patch("executor.save_run")
        mocker.patch("executor._start_one_campaign")
        mocker.patch("executor._reset_cascade_cancelled_children")

        from executor import force_start_campaign
        force_start_campaign("plan-1", "run-1", 0, 0)

        stop.assert_called_once_with(
            {"brandedCampaignId": "bc-old", "queueArn": "arn::queue/q1"}
        )

    def test_force_start_campaign_no_branded_does_not_call_stop(self, mocker):
        """force_start_campaign Phase 2: _stop_branded_campaign is NOT called when
        brandedCampaignId is absent from the campaign state."""
        cs = {
            "campaignId": "c-1",
            "brandedCampaignId": None,
            "queueArn": "arn::queue/q1",
            "status": "cancelled",
            "connectCampaignId": None,
            "segmentName": None,
            "segmentArn": None,
            "leadCount": None,
            "startedAt": None,
            "completedAt": None,
            "exitReason": "parent_cancelled",
            "errorDetail": None,
        }
        plan = _make_plan([_bucket_def("b-1", [_campaign_def("c-1")])])
        run = _make_run(plan, [_bucket_state("b-1", [cs], status="running")])

        stop = mocker.patch("executor._stop_branded_campaign")
        mocker.patch("executor.get_run", return_value=run)
        mocker.patch("executor.save_run")
        mocker.patch("executor._start_one_campaign")
        mocker.patch("executor._reset_cascade_cancelled_children")

        from executor import force_start_campaign
        force_start_campaign("plan-1", "run-1", 0, 0)

        stop.assert_not_called()


# ── Fix: _invoke_seeder must check FunctionError ──────────────────────────────


class TestInvokeSeederFunctionError:
    """_invoke_seeder must raise RuntimeError when Lambda returns FunctionError."""

    def test_seeder_function_error_raises(self, mocker):
        """When Lambda invoke returns HTTP 200 but FunctionError='Unhandled',
        _invoke_seeder must raise RuntimeError so _start_one_campaign marks
        the campaign as error — not silently treat it as 0 seeded contacts.
        """
        from unittest.mock import MagicMock
        import executor

        lam = mocker.patch("executor._get_lambda_client").return_value
        # boto3 returns HTTP 200 with FunctionError field — does NOT raise
        lam.invoke.return_value = {
            "StatusCode": 200,
            "FunctionError": "Unhandled",
            "Payload": MagicMock(read=lambda: b'{"errorMessage": "something crashed"}'),
        }

        with pytest.raises(RuntimeError, match="seeder invocation failed"):
            executor._invoke_seeder("bc-1", "seg-name", "flow-abc", "+15550001234")

    def test_seeder_function_error_causes_error_status_in_start_one_campaign(
        self, mocker
    ):
        """When _invoke_seeder raises (due to FunctionError), _start_one_campaign
        must set cs['status'] = 'error', not 'completed' with exitReason='empty_segment'.
        """
        from unittest.mock import MagicMock
        import executor

        campaign_id = "bc-fe"
        bucket = _bucket_def("b0", campaigns=[{
            "id": campaign_id,
            "name": "FunctionError branded",
            "deliveryType": "branded",
            "campaignConfig": {
                "dialerType": "progressive",
                "queueArn": "arn:aws:connect:::queue/q1",
                "contactFlowId": "flow-abc",
                "sourcePhone": "+15550001234",
            },
        }])
        plan = {"planId": "p-1", "buckets": [bucket]}
        cs = _campaign_state(campaign_id, status="queued")
        run = {
            "planId": "p-1",
            "runId": "r-1",
            "bucketStates": [{"status": "running", "campaignStates": [cs]}],
        }

        mocker.patch("executor._create_segment", return_value=("seg-name", "seg-arn"))

        lam = mocker.patch("executor._get_lambda_client").return_value
        lam.invoke.return_value = {
            "StatusCode": 200,
            "FunctionError": "Unhandled",
            "Payload": MagicMock(read=lambda: b'{"errorMessage": "handler crashed"}'),
        }
        mocker.patch("executor._get_ddb_client")

        executor._start_one_campaign(run, plan, 0, 0)

        assert cs["status"] == "error", (
            "FunctionError on seeder must set status=error, not complete with 0 seeded"
        )


# ── Fix: _expire_branded_queue_items must retry UnprocessedItems ──────────────


class TestExpireHandlesUnprocessedItems:
    """_expire_branded_queue_items must retry DynamoDB UnprocessedItems under throttling."""

    def test_expire_handles_unprocessed_items(self, mocker):
        """batch_write_item returns UnprocessedItems on first call, empty on second.
        Verify the retry loop fires and the warning is NOT raised as an exception.
        """
        import executor

        table = "VipProgressiveCampaignQueue"
        mocker.patch("executor._CAMPAIGN_QUEUE_TABLE_BRANDED", table)

        items = [
            {"campaignId": {"S": "bc-1"}, "sk": {"S": f"CONTACT#{i}"}}
            for i in range(3)
        ]
        write_requests = [
            {
                "PutRequest": {
                    "Item": {
                        "campaignId": item["campaignId"],
                        "sk": item["sk"],
                        "status": {"S": "EXPIRED"},
                        "ttl": {"N": mocker.ANY},
                    }
                }
            }
            for item in items
        ]

        ddb = mocker.patch("executor._get_ddb_client").return_value
        # Query returns 3 items in one page
        ddb.query.return_value = {"Items": items, "Count": 3}

        batch_write_calls = []

        def fake_batch_write(RequestItems):
            batch_write_calls.append(len(RequestItems.get(table, [])))
            if len(batch_write_calls) == 1:
                # First call: return 1 unprocessed item
                return {"UnprocessedItems": {table: [write_requests[0]]}}
            # Second call: all processed
            return {"UnprocessedItems": {}}

        ddb.batch_write_item.side_effect = fake_batch_write

        mocker.patch("executor.time.sleep")  # avoid real sleep in tests

        # Must not raise even with UnprocessedItems on first attempt
        executor._expire_branded_queue_items("bc-1")

        assert len(batch_write_calls) == 2, (
            "Expected 2 batch_write_item calls: first with all items, second with unprocessed retry"
        )


# ── Round-2 audit fixes: BL-1, H-1, H-2, H-3, H-4, H-5 ─────────────────────


def _branded_campaign_def_for_start(cid: str) -> dict:
    """Minimal branded campaign definition suitable for _start_one_campaign tests."""
    return {
        "id": cid,
        "name": cid,
        "deliveryType": "branded",
        "campaignConfig": {
            "queueArn": "arn:aws:connect:us-east-1:123:instance/abc/queue/q1",
            "contactFlowId": "flow-abc",
            "sourcePhone": "+15550001234",
        },
    }


def _branded_run_and_plan(cid="bc-1"):
    """Return a (run, plan) pair for a single branded campaign in status=queued."""
    bucket = {
        "id": "b0",
        "name": "b0",
        "run_mode": "status_based",
        "duration_minutes": 30,
        "cleanup": False,
        "prestart_next": True,
        "parallel": False,
        "campaigns": [_branded_campaign_def_for_start(cid)],
        "campaignConfig": {},
    }
    plan = {"planId": "plan-1", "buckets": [bucket]}
    cs = _campaign_state(cid, "queued")
    run = {
        "planId": "plan-1",
        "runId": "run-1",
        "bucketStates": [{"status": "running", "campaignStates": [cs]}],
    }
    return run, plan, cs


class TestBL1ExpireOnSeederException:
    """BL-1: _expire_branded_queue_items called on seeder exception."""

    @pytest.fixture(autouse=True)
    def _branded_env(self, mocker):
        mocker.patch("executor._ACTIVE_BRANDED_CAMPAIGNS_TABLE", "VipActiveBrandedCampaigns")
        mocker.patch("executor._CAMPAIGN_QUEUE_TABLE_BRANDED", "VipProgressiveCampaignQueue")

    def test_expire_called_on_seeder_exception(self, mocker):
        """When _invoke_seeder raises, _expire_branded_queue_items must be called
        with campaign_id so partially-seeded contacts don't sit in the queue for 24h.
        """
        import executor

        run, plan, cs = _branded_run_and_plan("bc-bl1")

        mocker.patch(
            "executor._create_segment",
            return_value=("seg-bl1", "arn:seg-bl1"),
        )
        mocker.patch(
            "executor._invoke_seeder",
            side_effect=RuntimeError("seeder boom"),
        )
        expire = mocker.patch("executor._expire_branded_queue_items")
        mocker.patch("executor._get_ddb_client")

        executor._start_one_campaign(run, plan, 0, 0)

        expire.assert_called_once_with("deb4e78c-8a2f-58fc-858a-c14812bb839b")
        assert cs["status"] == "error"


class TestH1ExpireOnDdbWriteFailure:
    """H-1: _expire_branded_queue_items called when active-campaigns DDB put_item fails."""

    @pytest.fixture(autouse=True)
    def _branded_env(self, mocker):
        mocker.patch("executor._ACTIVE_BRANDED_CAMPAIGNS_TABLE", "VipActiveBrandedCampaigns")
        mocker.patch("executor._CAMPAIGN_QUEUE_TABLE_BRANDED", "VipProgressiveCampaignQueue")

    def test_expire_not_called_on_ddb_lock_failure(self, mocker):
        """When put_item (the distributed lock) raises a non-conditional error,
        the seeder was never invoked — no contacts exist to expire.
        _expire_branded_queue_items must NOT be called and status must be 'error'.
        """
        import executor
        from botocore.exceptions import ClientError

        run, plan, cs = _branded_run_and_plan("bc-h1")

        mocker.patch(
            "executor._create_segment",
            return_value=("seg-h1", "arn:seg-h1"),
        )
        invoke_seeder = mocker.patch("executor._invoke_seeder")
        ddb = mocker.patch("executor._get_ddb_client").return_value
        ddb.put_item.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "throttled"}},
            "PutItem",
        )
        expire = mocker.patch("executor._expire_branded_queue_items")

        executor._start_one_campaign(run, plan, 0, 0)

        invoke_seeder.assert_not_called()  # lock failed before seeder could run
        expire.assert_not_called()         # no contacts exist to expire
        assert cs["status"] == "error"


class TestH2AbortCreatingBrandedCampaign:
    """H-2: abort_run and _force_finish_internal must clean up creating-status branded campaigns."""

    def test_abort_run_cleans_creating_branded_campaign(self, mocker):
        """Campaign in status='creating' with brandedCampaignId set (seeder already ran,
        put_item not yet succeeded) must trigger _stop_branded_campaign on abort.
        """
        import executor

        cs = {
            "campaignId": "bc-h2",
            "brandedCampaignId": "bc-h2",
            "queueArn": "arn::queue/q1",
            "status": "creating",
            "connectCampaignId": None,
            "segmentName": None,
            "segmentArn": None,
            "leadCount": None,
            "startedAt": None,
            "completedAt": None,
            "exitReason": None,
            "errorDetail": None,
        }
        run = {
            "planId": "p-h2",
            "runId": "r-h2",
            "status": "running",
            "bucketStates": [{"status": "running", "campaignStates": [cs]}],
        }
        mocker.patch("executor.get_run", return_value=run)
        stop = mocker.patch("executor._stop_branded_campaign")
        mocker.patch("executor.save_run")
        mocker.patch("executor.update_plan_pending_warmup")
        mocker.patch("executor.unlock_plan_run")
        mocker.patch("executor.lock_plan_run", return_value=run)
        mocker.patch("executor._delete_bucket_schedule_safe")

        executor.abort_run("p-h2", "r-h2")

        stop.assert_called_once_with(cs)
        assert cs["status"] == "cancelled"


class TestH3DeleteConditionExpression:
    """H-3: delete_item in _stop_branded_campaign must use ConditionExpression."""

    def test_stop_branded_delete_condition_expression(self, mocker):
        """delete_item must be called with ConditionExpression='attribute_exists(pk)'."""
        import executor

        ddb = mocker.patch("executor._get_ddb_client").return_value
        mocker.patch("executor._expire_branded_queue_items")

        cs = {
            "brandedCampaignId": "bc-h3",
            "queueArn": "arn:aws:connect:us-east-1:123:instance/abc/queue/q3",
        }
        executor._stop_branded_campaign(cs)

        delete_kwargs = ddb.delete_item.call_args[1]
        assert delete_kwargs.get("ConditionExpression") == "attribute_exists(pk)", (
            "delete_item must include ConditionExpression='attribute_exists(pk)'"
        )

    def test_stop_branded_already_deleted_is_noop(self, mocker):
        """ConditionalCheckFailedException on delete must be a no-op — another invocation
        already cleaned up the record; the calling path must not see an exception.
        """
        import executor
        from botocore.exceptions import ClientError

        ddb = mocker.patch("executor._get_ddb_client").return_value
        ddb.delete_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "already deleted"}},
            "DeleteItem",
        )
        mocker.patch("executor._expire_branded_queue_items")

        cs = {
            "brandedCampaignId": "bc-h3-idem",
            "queueArn": "arn:aws:connect:us-east-1:123:instance/abc/queue/q3",
        }
        # Must not raise
        executor._stop_branded_campaign(cs)


class TestH4PutItemConditionalCheck:
    """H-4: ConditionalCheckFailedException on active-campaigns put_item is a no-op."""

    @pytest.fixture(autouse=True)
    def _branded_env(self, mocker):
        mocker.patch("executor._ACTIVE_BRANDED_CAMPAIGNS_TABLE", "VipActiveBrandedCampaigns")
        mocker.patch("executor._CAMPAIGN_QUEUE_TABLE_BRANDED", "VipProgressiveCampaignQueue")

    def test_start_branded_put_item_conditional_check_is_noop(self, mocker):
        """When put_item raises ConditionalCheckFailedException (concurrent start already
        registered the campaign), status must transition to 'running', not 'error'.
        """
        import executor
        from botocore.exceptions import ClientError

        run, plan, cs = _branded_run_and_plan("bc-h4")

        mocker.patch(
            "executor._create_segment",
            return_value=("seg-h4", "arn:seg-h4"),
        )
        mocker.patch("executor._invoke_seeder", return_value=3)

        ddb = mocker.patch("executor._get_ddb_client").return_value
        ddb.put_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "already exists"}},
            "PutItem",
        )

        executor._start_one_campaign(run, plan, 0, 0)

        assert cs["status"] == "running", (
            "ConditionalCheckFailed on put_item must be a no-op — campaign proceeds to running"
        )


class TestH5EnvVarValidation:
    """H-5 Part A: Missing branded env vars must set status='error'."""

    def test_branded_env_var_missing_sets_error(self, mocker):
        """When ACTIVE_BRANDED_CAMPAIGNS_TABLE or CAMPAIGN_QUEUE_TABLE_BRANDED is empty,
        _start_one_campaign must set status='error' (ValueError caught by outer try/except).
        """
        import executor

        run, plan, cs = _branded_run_and_plan("bc-h5")

        mocker.patch("executor._ACTIVE_BRANDED_CAMPAIGNS_TABLE", "")
        mocker.patch("executor._CAMPAIGN_QUEUE_TABLE_BRANDED", "")

        executor._start_one_campaign(run, plan, 0, 0)

        assert cs["status"] == "error", (
            "Missing branded env vars must cause status='error'"
        )


class TestH5ConsecutivePollFailures:
    """H-5 Part B: Consecutive branded poll failures must transition campaign to error."""

    def _run_plan_cs(self, cid="bc-poll"):
        cs = _campaign_state(cid, status="running")
        cs["brandedCampaignId"] = cid
        cs["queueArn"] = "arn::queue/q1"
        cs["connectCampaignId"] = None
        bucket = {
            "id": "b0",
            "name": "b0",
            "run_mode": "status_based",
            "duration_minutes": 30,
            "cleanup": False,
            "prestart_next": True,
            "parallel": False,
            "campaigns": [{
                "id": cid, "name": "B", "deliveryType": "branded",
                "campaignConfig": {"queueArn": "arn::queue/q1"},
            }],
            "campaignConfig": {},
        }
        run = {
            "planId": "p-1", "runId": "r-1", "status": "running",
            "bucketStates": [{"status": "running", "campaignStates": [cs],
                              "startedAt": datetime.utcnow().isoformat()}],
        }
        plan = {"planId": "p-1", "buckets": [bucket]}
        return run, plan, cs

    def test_consecutive_poll_failures_transition_to_error(self, mocker):
        """After 5 consecutive poll failures, campaign must transition to status='error'
        with exitReason='poll_failure' and _stop_branded_campaign called.
        """
        import executor

        # Reset module-level counter between test runs
        executor._branded_poll_failures.clear()

        run, plan, cs = self._run_plan_cs("bc-poll-h5")

        mocker.patch("executor._count_branded_queue", side_effect=Exception("DDB down"))
        stop = mocker.patch("executor._stop_branded_campaign")
        mocker.patch("executor.save_run")
        mocker.patch("executor.get_plan", return_value=plan)
        # After the 5th failure the campaign is terminal → tick advances the
        # bucket → run completion fans out to unlock/loop/SNS. That orchestration
        # has its own tests; isolate this test to the poll-failure transition.
        mocker.patch("executor._advance_bucket")

        from executor import tick

        for i in range(4):
            mocker.patch("executor.get_run", return_value=run)
            tick("p-1", "r-1", 0)
            assert cs["status"] == "running", f"Should still be running after {i+1} failures"

        # 5th failure must trigger error transition
        mocker.patch("executor.get_run", return_value=run)
        tick("p-1", "r-1", 0)

        assert cs["status"] == "error", (
            "5 consecutive poll failures must transition campaign to error"
        )
        assert cs["exitReason"] == "poll_failure"
        stop.assert_called_once_with(cs)

        # Counter must be cleared after transition
        assert "bc-poll-h5" not in executor._branded_poll_failures


class TestNormalizePhoneE164:
    """_normalize_phone_e164 must never emit an E.164 value with an invalid NANP
    area code (NPA). NPAs can't start with 0 or 1 — a 10-digit CRM value that
    already starts with 0/1 is malformed input, not a normalizable phone number,
    and blindly prepending '+1' to it produces an undialable number that's
    silently dialed anyway (segment_phones_excluded only fires on None).
    """

    def test_normalizes_valid_10_digit_number(self):
        from executor import _normalize_phone_e164
        assert _normalize_phone_e164("2125551234") == "+12125551234"

    def test_normalizes_valid_11_digit_number_with_leading_1(self):
        from executor import _normalize_phone_e164
        assert _normalize_phone_e164("12125551234") == "+12125551234"

    def test_passes_through_already_e164(self):
        from executor import _normalize_phone_e164
        assert _normalize_phone_e164("+12125551234") == "+12125551234"

    def test_returns_none_for_empty_string(self):
        from executor import _normalize_phone_e164
        assert _normalize_phone_e164("") is None

    def test_rejects_10_digit_value_starting_with_1(self):
        """Malformed CRM value '1347XXXXXXX' (10 digits, NPA would be 134 —
        invalid, NPAs can't start with 1). Must not become '+11347XXXXXXX'.
        """
        from executor import _normalize_phone_e164
        assert _normalize_phone_e164("1347555123") is None

    def test_rejects_10_digit_value_starting_with_0(self):
        """Malformed/placeholder CRM value '0000000000' (NPA 000 — invalid).
        Must not become '+10000000000'.
        """
        from executor import _normalize_phone_e164
        assert _normalize_phone_e164("0000000000") is None

    def test_rejects_already_e164_with_invalid_npa(self):
        """Same NPA-starts-with-0/1 defect, but arriving pre-formatted with a
        '+' prefix — must be rejected by the passthrough branch too.
        """
        from executor import _normalize_phone_e164
        assert _normalize_phone_e164("+11347555123") is None

    def test_rejects_11_digit_leading_1_with_invalid_npa(self):
        """11-digit value starting with '1' (the country code), but the NPA
        that follows also starts with '1' — invalid NPA, not a valid US number.
        """
        from executor import _normalize_phone_e164
        assert _normalize_phone_e164("11347555123") is None


# ── _create_segment — maxLeadAgeMinutes → createdAt GTE rule (2026-08-27) ────
#
# Calls the real executor._max_age_cutoff directly (not a local mirror) — it's
# a pure module-level function with zero vip_shared dependency, so importing
# it doesn't collide with other test modules' vip_shared MagicMock stubs.
#
# Regression (adversarial review, 2026-08-27): campaigns read back from a
# run's DynamoDB planSnapshot come back as decimal.Decimal (store._run_from_item
# never normalizes planSnapshot the way _plan_from_item normalizes buckets),
# and timedelta() raises TypeError on Decimal. This broke every real dispatch
# path except the first campaign of a freshly-started run. The Decimal case
# below is the regression test that would have caught it.


class TestMaxLeadAgeRule:
    def test_no_cutoff_when_none(self):
        from executor import _max_age_cutoff
        assert _max_age_cutoff(None, datetime.now(timezone.utc)) is None

    def test_no_cutoff_when_zero(self):
        from executor import _max_age_cutoff
        assert _max_age_cutoff(0, datetime.now(timezone.utc)) is None

    def test_no_cutoff_when_negative(self):
        """A negative value must not produce a cutoff in the future — that
        would silently match zero leads (empty_segment) instead of erroring.
        """
        from executor import _max_age_cutoff
        assert _max_age_cutoff(-5, datetime.now(timezone.utc)) is None

    def test_decimal_from_dynamodb_does_not_raise(self):
        """Regression: boto3's Table resource deserializes DynamoDB Number
        attributes as decimal.Decimal, not int. timedelta() rejects Decimal
        with TypeError — this must be cast, not passed through raw.
        """
        from decimal import Decimal

        from executor import _max_age_cutoff
        now = datetime(2026, 8, 27, 14, 30, 0, 123000, tzinfo=timezone.utc)
        assert _max_age_cutoff(Decimal(15), now) == "2026-08-27T14:15:00.123Z"

    def test_cutoff_format_matches_redis_created_at(self):
        from executor import _max_age_cutoff
        now = datetime(2026, 8, 27, 14, 30, 0, 123000, tzinfo=timezone.utc)
        assert _max_age_cutoff(15, now) == "2026-08-27T14:15:00.123Z"

    def test_lead_created_within_window_is_gte_cutoff(self):
        from executor import _max_age_cutoff
        cutoff = _max_age_cutoff(
            15, datetime(2026, 8, 27, 14, 30, 0, 0, tzinfo=timezone.utc)
        )
        assert "2026-08-27T14:20:00.000Z" >= cutoff

    def test_lead_older_than_window_is_not_gte_cutoff(self):
        from executor import _max_age_cutoff
        cutoff = _max_age_cutoff(
            15, datetime(2026, 8, 27, 14, 30, 0, 0, tzinfo=timezone.utc)
        )
        assert not ("2026-08-27T14:00:00.000Z" >= cutoff)

    def test_lead_exactly_at_cutoff_is_gte_cutoff(self):
        from executor import _max_age_cutoff
        cutoff = _max_age_cutoff(
            15, datetime(2026, 8, 27, 14, 30, 0, 0, tzinfo=timezone.utc)
        )
        assert "2026-08-27T14:15:00.000Z" >= cutoff


class TestRecordPlanEvent:
    """_record_plan_event: best-effort audit write for automatic executor transitions."""

    def test_writes_expected_audit_record(self):
        import executor

        mock_audit = MagicMock()
        run = {"planId": "plan-1", "runId": "run-1"}
        with patch("executor.build_audit", return_value=mock_audit):
            executor._record_plan_event(run, "bucket_started", {"bucketIndex": 2})
        mock_audit.record.assert_called_once_with(
            entity_type="plan_run",
            entity_id="plan-1/run-1",
            action="bucket_started",
            actor_sub="system",
            actor_email="system@api-plans-executor",
            extra={"bucketIndex": 2},
        )

    def test_extra_defaults_to_none(self):
        import executor

        mock_audit = MagicMock()
        run = {"planId": "plan-1", "runId": "run-1"}
        with patch("executor.build_audit", return_value=mock_audit):
            executor._record_plan_event(run, "window_closed")
        mock_audit.record.assert_called_once_with(
            entity_type="plan_run",
            entity_id="plan-1/run-1",
            action="window_closed",
            actor_sub="system",
            actor_email="system@api-plans-executor",
            extra=None,
        )

    def test_swallows_write_failure_without_raising(self):
        import executor

        mock_audit = MagicMock()
        mock_audit.record.side_effect = RuntimeError("dynamodb throttled")
        run = {"planId": "plan-1", "runId": "run-1"}
        with patch("executor.build_audit", return_value=mock_audit):
            executor._record_plan_event(run, "bucket_completed", {"bucketIndex": 0})
        # No exception raised — the call above completing is the assertion.
