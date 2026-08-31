# Day Activity Feed — Executor Instrumentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `services/api-plans/src/executor.py`'s automatic state transitions (bucket started/completed, window closed, empty-segment retry, campaign creation failure) write to the existing `AdminAuditLog` audit trail, so the future Campaign Monitor UI's "day activity feed" panel has real data to read from `GET /audit/{entityId}` — which already exists and already returns exactly the reverse-chronological order the feed needs.

**Architecture:** Reuse the existing `AuditRecorder`/`build_from_env` audit-write path verbatim (`vip_shared.infrastructure.persistence.audit`) — no new table, no new endpoint, no IaC change (the Lambda hosting `executor.py` already has `AUDIT_TABLE` wired and `dynamodb:PutItem` granted, because `handlers/runs.py` runs in the same Lambda and already uses this exact path for manual operator actions). Add one small private helper, `_record_plan_event(run, action, extra)`, that wraps `build_audit().record(...)` with `actor_sub="system"` / `actor_email="system@api-plans-executor"` and swallows any write failure (telemetry must never abort plan execution) — then call it at 12 existing transition points across `executor.py`. Every call site is additive only: no existing return value, control flow, or side effect changes; only a new log-like call is inserted.

**Tech Stack:** Python 3.12, pytest + `unittest.mock` (this repo's existing test convention for `executor.py`: `patch("executor.<name>")` for module-level names, exactly as already done for `_notify_sns`).

**Spec:** No separate spec file — this plan's own Architecture section, plus the verified line-by-line findings recorded in the Global Constraints below, is the authority. `executor.py` runs in production today for 4 active plans; every fact below was verified by reading the live file on `main`, not inferred.

## Global Constraints

- `executor.py` is live production code. Every task in this plan is strictly additive (new function calls at existing points) — no task may change an existing return type, existing control flow, or existing business logic. If a task's diff touches anything beyond adding a `_record_plan_event(...)` call (and the one new helper + one new import in Task 1), stop and report it as a deviation, don't implement it.
- `_record_plan_event` must never raise. A `dynamodb:PutItem` failure (throttle, misconfigured table, anything) must be caught and logged, never propagated — this telemetry write must not be able to abort a plan run.
- `_advance_bucket` (Task 3) has a known, accepted, pre-existing race: on `ConcurrentWriteError` inside `_advance_bucket`, the function can re-execute for the same bucket on a later tick (this is intentional — see the existing comment at `executor.py:2286-2290` about `stale_tick` guard re-tryability). A `bucket_completed` audit row can duplicate in that rare case. This is acceptable — a duplicate cosmetic row in an activity feed is not a defect worth adding new locking for. Do not attempt to make this idempotent; do not add a new guard. State this acceptance in a one-line comment at the call site, nothing more.
- Test pattern: this repo's `test_executor_v2.py` already mocks module-level executor functions via `patch("executor.<name>")` (e.g. `patch("executor._notify_sns")`). Use the identical pattern for `patch("executor.build_audit")` and/or `patch("executor._record_plan_event")`.
- Reconcile expected/actual normalization (`_create_segment`'s return signature) is explicitly **out of scope** for this plan — it's a separate plan (different kind of change: a signature change across 4 call sites, not pure instrumentation).
- Run the full `services/api-plans` test suite before and after each task (`cd services/api-plans && python -m pytest tests/unit -q`) — baseline is 432 passed, 0 failed (confirmed on `main` before this plan). Any new failure is this plan's to fix; there is no known pre-existing failure in this package to worry about (unlike the frontend's unrelated `chainMap.test.ts`).

---

## File Structure

```
services/api-plans/src/executor.py                    # MODIFY — 1 import, 1 helper, 12 call sites
services/api-plans/tests/unit/test_executor_v2.py      # MODIFY — new tests for the helper + each call site
frontend/src/pages/Audit.tsx                            # MODIFY — new actions/entityType in filter dropdowns + actionTone(), export actionTone
frontend/src/pages/Audit.test.ts                         # NEW — tests for the now-exported actionTone()
```

---

### Task 1: `_record_plan_event` helper

**Files:**
- Modify: `services/api-plans/src/executor.py` (add import near line 49-50, add helper function near line 4887, right before `_notify_sns`)
- Test: `services/api-plans/tests/unit/test_executor_v2.py` (append near the end)

**Interfaces:**
- Consumes: `vip_shared.infrastructure.persistence.audit.build_from_env` (existing, verified signature: `build_from_env() -> AuditRecorder`; `AuditRecorder.record(*, entity_type, entity_id, action, actor_sub, actor_email, before=None, after=None, ip_address=None, user_agent=None, extra=None) -> None`).
- Produces: `_record_plan_event(run: dict, action: str, extra: dict | None = None) -> None`. Tasks 2-5 call this at 12 sites with `run` already in scope at each.

- [ ] **Step 1: Write the failing test**

Add to `services/api-plans/tests/unit/test_executor_v2.py` (near the end of the file, after the last test):

```python
class TestRecordPlanEvent:
    """_record_plan_event: best-effort audit write for automatic executor transitions."""

    def test_writes_expected_audit_record(self):
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
        mock_audit = MagicMock()
        mock_audit.record.side_effect = RuntimeError("dynamodb throttled")
        run = {"planId": "plan-1", "runId": "run-1"}
        with patch("executor.build_audit", return_value=mock_audit):
            executor._record_plan_event(run, "bucket_completed", {"bucketIndex": 0})
        # No exception raised — the call above completing is the assertion.
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd services/api-plans && python -m pytest tests/unit/test_executor_v2.py::TestRecordPlanEvent -v`
Expected: FAIL — `AttributeError: module 'executor' has no attribute '_record_plan_event'` (or `build_audit` not found for `patch`).

- [ ] **Step 3: Add the import**

In `services/api-plans/src/executor.py`, after the existing top-level imports (after line 50, `from botocore.exceptions import ClientError`, before the blank line and `from builders import (` block at line 52):

```python
from vip_shared.infrastructure.persistence.audit import build_from_env as build_audit
```

- [ ] **Step 4: Add the helper function**

In `services/api-plans/src/executor.py`, immediately before `def _notify_sns(subject: str, detail: str, attributes: dict | None = None) -> None:` (currently at line 4887):

```python
def _record_plan_event(run: dict, action: str, extra: dict | None = None) -> None:
    """Best-effort write to the day-activity-feed audit trail (AdminAuditLog).

    Telemetry only — a write failure here must never abort plan execution,
    so every exception is caught and logged, not raised.
    """
    try:
        build_audit().record(
            entity_type="plan_run",
            entity_id=f"{run['planId']}/{run['runId']}",
            action=action,
            actor_sub="system",
            actor_email="system@api-plans-executor",
            extra=extra,
        )
    except Exception as exc:
        logger.warning(
            "_record_plan_event(%s) failed: %s", action, type(exc).__name__
        )
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd services/api-plans && python -m pytest tests/unit/test_executor_v2.py::TestRecordPlanEvent -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Run the full package suite**

Run: `cd services/api-plans && python -m pytest tests/unit -q`
Expected: 435 passed (432 baseline + 3 new), 0 failed.

- [ ] **Step 7: Commit**

```bash
git add services/api-plans/src/executor.py services/api-plans/tests/unit/test_executor_v2.py
git commit -m "feat(executor): add _record_plan_event audit-write helper"
```

---

### Task 2: Instrument `bucket_started` (2 call sites)

**Files:**
- Modify: `services/api-plans/src/executor.py:1911-1918` (`_start_bucket`) and `:1958-1977` (`_activate_warming_bucket`)
- Test: `services/api-plans/tests/unit/test_executor_v2.py`

**Interfaces:**
- Consumes: `_record_plan_event` (Task 1).
- Produces: nothing new consumed by later tasks — this and Tasks 3-5 are independent of each other, all consuming only Task 1's helper.

- [ ] **Step 1: Write the failing tests**

Add to `services/api-plans/tests/unit/test_executor_v2.py`:

```python
class TestBucketStartedAuditEvent:
    def test_start_bucket_records_bucket_started(self, monkeypatch):
        # Reuses this file's existing _make_run/_make_plan-style fixtures if present;
        # otherwise build the minimal run/plan shape _start_bucket needs.
        run = {
            "planId": "plan-1",
            "runId": "run-1",
            "planSnapshot": {"buckets": [{"name": "1st attempt"}]},
            "bucketStates": [{"campaignStates": []}],
        }
        mock_audit = MagicMock()
        with (
            patch("executor.build_audit", return_value=mock_audit),
            patch("executor._schedule_tick", return_value="sched-1"),
        ):
            executor._start_bucket(run, 0)
        mock_audit.record.assert_called_once_with(
            entity_type="plan_run",
            entity_id="plan-1/run-1",
            action="bucket_started",
            actor_sub="system",
            actor_email="system@api-plans-executor",
            extra={"bucketIndex": 0, "bucketName": "1st attempt"},
        )

    def test_activate_warming_bucket_records_bucket_started(self, monkeypatch):
        run = {
            "planId": "plan-2",
            "runId": "run-2",
            "bucketStates": [
                {"campaignStates": []},
                {"campaignStates": [], "status": "warming"},
            ],
        }
        plan = {"buckets": [{}, {"name": "3rd attempt"}]}
        mock_audit = MagicMock()
        with (
            patch("executor.build_audit", return_value=mock_audit),
            patch("executor._schedule_tick", return_value="sched-2"),
        ):
            executor._activate_warming_bucket(run, plan, 1)
        mock_audit.record.assert_called_once_with(
            entity_type="plan_run",
            entity_id="plan-2/run-2",
            action="bucket_started",
            actor_sub="system",
            actor_email="system@api-plans-executor",
            extra={"bucketIndex": 1, "bucketName": "3rd attempt"},
        )
```

If either test's fixture shape doesn't match what `_start_bucket`/`_activate_warming_bucket` actually need at runtime (e.g. `_schedule_tick` isn't the right name to patch, or another field is required before reaching the instrumentation line), adjust the fixture to whatever minimal shape makes the function reach the `bucket_state["startedAt"] = now_iso` line without a `KeyError` — do not weaken the assertion on `mock_audit.record`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd services/api-plans && python -m pytest tests/unit/test_executor_v2.py::TestBucketStartedAuditEvent -v`
Expected: FAIL — `mock_audit.record` not called (0 calls), since no instrumentation exists yet.

- [ ] **Step 3: Instrument `_start_bucket`**

In `services/api-plans/src/executor.py`, immediately after line 1918 (`bucket_state["startedAt"] = now_iso`), insert:

```python
    _record_plan_event(
        run, "bucket_started", {"bucketIndex": index, "bucketName": plan["buckets"][index].get("name")}
    )
```

- [ ] **Step 4: Instrument `_activate_warming_bucket`**

In the same file, immediately after line 1977 (`bucket_state["startedAt"] = now_iso`, inside `_activate_warming_bucket`), insert:

```python
    _record_plan_event(
        run,
        "bucket_started",
        {"bucketIndex": bucket_index, "bucketName": plan["buckets"][bucket_index].get("name")},
    )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd services/api-plans && python -m pytest tests/unit/test_executor_v2.py::TestBucketStartedAuditEvent -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Run the full package suite**

Run: `cd services/api-plans && python -m pytest tests/unit -q`
Expected: all previously-passing tests still pass, plus the 2 new ones.

- [ ] **Step 7: Commit**

```bash
git add services/api-plans/src/executor.py services/api-plans/tests/unit/test_executor_v2.py
git commit -m "feat(executor): audit bucket_started at both start-bucket paths"
```

---

### Task 3: Instrument `bucket_completed` (1 call site)

**Files:**
- Modify: `services/api-plans/src/executor.py:2276-2303` (`_advance_bucket`)
- Test: `services/api-plans/tests/unit/test_executor_v2.py`

**Interfaces:**
- Consumes: `_record_plan_event` (Task 1).

- [ ] **Step 1: Write the failing test**

Add to `services/api-plans/tests/unit/test_executor_v2.py`:

```python
class TestBucketCompletedAuditEvent:
    def test_advance_bucket_records_bucket_completed(self):
        run = {
            "planId": "plan-3",
            "runId": "run-3",
            "bucketStates": [{"campaignStates": [], "status": "running"}],
        }
        plan = {"buckets": [{"name": "Cancellation/No Show", "cleanup": False}]}
        mock_audit = MagicMock()
        with (
            patch("executor.build_audit", return_value=mock_audit),
            patch("executor.save_run"),
            patch("executor._delete_bucket_schedule_safe"),
            patch("executor._fire_bucket_chains"),
        ):
            executor._advance_bucket(run, plan, 0, reason="time_expired")
        mock_audit.record.assert_called_once_with(
            entity_type="plan_run",
            entity_id="plan-3/run-3",
            action="bucket_completed",
            actor_sub="system",
            actor_email="system@api-plans-executor",
            extra={"bucketIndex": 0, "bucketName": "Cancellation/No Show", "reason": "time_expired"},
        )
```

If `_advance_bucket` needs additional patched dependencies beyond the four listed (e.g. it calls something else unconditionally before reaching line 2303 given the exact current code), add them — the goal is reaching the `bucket_state["status"] = "completed"` line with nothing raising, not testing those dependencies.

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd services/api-plans && python -m pytest tests/unit/test_executor_v2.py::TestBucketCompletedAuditEvent -v`
Expected: FAIL — `mock_audit.record` not called.

- [ ] **Step 3: Instrument `_advance_bucket`**

In `services/api-plans/src/executor.py`, immediately after line 2303 (`bucket_state["completedAt"] = now`), insert:

```python
        # Duplicate audit row possible if this function re-executes after a
        # ConcurrentWriteError below (see comment above on stale_tick re-tryability)
        # — accepted, a cosmetic duplicate in the activity feed, not worth a new lock.
        _record_plan_event(
            run, "bucket_completed", {"bucketIndex": bucket_index, "bucketName": bucket.get("name"), "reason": reason}
        )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd services/api-plans && python -m pytest tests/unit/test_executor_v2.py::TestBucketCompletedAuditEvent -v`
Expected: PASS.

- [ ] **Step 5: Run the full package suite**

Run: `cd services/api-plans && python -m pytest tests/unit -q`

- [ ] **Step 6: Commit**

```bash
git add services/api-plans/src/executor.py services/api-plans/tests/unit/test_executor_v2.py
git commit -m "feat(executor): audit bucket_completed in _advance_bucket"
```

---

### Task 4: Instrument `window_closed` (3 call sites in `tick()`)

**Files:**
- Modify: `services/api-plans/src/executor.py:984-1012` (inside `tick()`)
- Test: `services/api-plans/tests/unit/test_executor_v2.py`

**Interfaces:**
- Consumes: `_record_plan_event` (Task 1).
- Note: instrument the 3 call sites in `tick()` itself, NOT inside `_force_finish_internal` — `_force_finish_internal` is shared with the operator-manual `force_finish_run` path (`handlers/runs.py`), which already writes its own `action="force_finish"` audit record. Instrumenting inside `_force_finish_internal` would double-log every manual force-finish.

- [ ] **Step 1: Write the failing tests**

Add to `services/api-plans/tests/unit/test_executor_v2.py`. These test the three `tick()` early-return branches directly; adapt the run/plan fixture shape to whatever `tick()` needs to reach each specific cutoff check without a `KeyError` — the working-hours/loop/daily cutoff branches run very early in `tick()` (before any campaign polling), so a minimal `run`/`plan` should suffice:

```python
class TestWindowClosedAuditEvent:
    def test_working_hours_cutoff_records_window_closed(self):
        mock_audit = MagicMock()
        run = {"planId": "plan-4", "runId": "run-4", "bucketStates": [{}]}
        plan = {"workingHours": {"endTime": "00:00"}, "buckets": [{}]}  # already past cutoff
        with (
            patch("executor.store") as mock_store,
            patch("executor.build_audit", return_value=mock_audit),
            patch("executor._force_finish_internal"),
        ):
            mock_store.get_run.return_value = run
            mock_store.get_plan.return_value = plan
            result = executor.tick("plan-4", "run-4", 0)
        assert result == {"ok": True, "reason": "working_hours_cutoff"}
        mock_audit.record.assert_called_once_with(
            entity_type="plan_run",
            entity_id="plan-4/run-4",
            action="window_closed",
            actor_sub="system",
            actor_email="system@api-plans-executor",
            extra={"reason": "working_hours_cutoff"},
        )
```

Write the equivalent `test_loop_cutoff_records_window_closed` (set `plan["loop"] = {"endTime": "00:00"}`, no `workingHours`, expect `reason == "loop_cutoff"`) and `test_daily_cutoff_records_window_closed` (no `workingHours`/`loop` at all, and whatever fixture makes `_past_daily_cutoff(_now_utc())` return `True` — if that helper isn't easily forced true via fixture alone, `patch("executor._past_daily_cutoff", return_value=True)` instead, and assert `reason == "daily_cutoff"`).

Read the actual current top of `tick()` (`services/api-plans/src/executor.py:944` onward) before writing these — the exact fixture keys `tick()` reads before reaching each cutoff check (e.g. whether it calls `store.get_run`/`store.get_plan` or receives them differently) must match reality, not be guessed from this brief. If `tick()`'s actual data-loading differs from `mock_store.get_run`/`get_plan` above, adjust the patch target and fixture accordingly — the assertion on `mock_audit.record` and the returned `reason` are what must hold, not the exact mocking mechanics shown here.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd services/api-plans && python -m pytest tests/unit/test_executor_v2.py::TestWindowClosedAuditEvent -v`
Expected: FAIL — `mock_audit.record` not called on all three.

- [ ] **Step 3: Instrument the working-hours cutoff**

In `services/api-plans/src/executor.py`, immediately before line 992 (`_force_finish_internal(run, plan)`, inside the `if _wh_end:` / `if _now_hhmm >= ...` block), insert:

```python
            _record_plan_event(run, "window_closed", {"reason": "working_hours_cutoff"})
```

- [ ] **Step 4: Instrument the loop cutoff**

Immediately before line 1005 (the second `_force_finish_internal(run, plan)` call, inside the `if _loop_end:` block), insert:

```python
            _record_plan_event(run, "window_closed", {"reason": "loop_cutoff"})
```

- [ ] **Step 5: Instrument the daily cutoff**

Immediately before line 1011 (the third `_force_finish_internal(run, plan)` call, inside `if _past_daily_cutoff(_now_utc()):`), insert:

```python
        _record_plan_event(run, "window_closed", {"reason": "daily_cutoff"})
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd services/api-plans && python -m pytest tests/unit/test_executor_v2.py::TestWindowClosedAuditEvent -v`
Expected: PASS (3 tests).

- [ ] **Step 7: Run the full package suite**

Run: `cd services/api-plans && python -m pytest tests/unit -q`

- [ ] **Step 8: Commit**

```bash
git add services/api-plans/src/executor.py services/api-plans/tests/unit/test_executor_v2.py
git commit -m "feat(executor): audit window_closed at all 3 tick() cutoffs"
```

---

### Task 5: Instrument `reconcile_retry` and `creation_failed` (6 call sites)

**Files:**
- Modify: `services/api-plans/src/executor.py` at 6 sites: `:3735` (`_start_one_campaign`, empty-segment retry), `:2066` (`_activate_warming_bucket`), `:2250` (`_prestart_next_bucket`), `:3686`, `:3924`, `:3948` (all three in `_start_one_campaign`)
- Test: `services/api-plans/tests/unit/test_executor_v2.py`

**Interfaces:**
- Consumes: `_record_plan_event` (Task 1).

- [ ] **Step 1: Write the failing tests**

Add to `services/api-plans/tests/unit/test_executor_v2.py`. Six focused tests, one per call site — reuse or adapt this file's existing fixtures for exercising `_start_one_campaign`'s error paths (this file already has tests hitting the `_EmptySegmentError` retry branch and the campaign-creation-failure branches for its existing assertions — model these new tests on those, adding only the `mock_audit`/`build_audit` patch and the new assertion):

```python
class TestReconcileRetryAndCreationFailedAuditEvents:
    def test_empty_segment_retry_records_reconcile_retry(self):
        # Arrange whatever this file's existing empty-segment-retry test arranges
        # (same run/plan/bucket/campaign fixture, same patched _create_segment
        # raising executor._EmptySegmentError), plus patch executor.build_audit.
        # Assert executor._start_one_campaign(...) results in:
        #   mock_audit.record.assert_called_once_with(
        #       entity_type="plan_run",
        #       entity_id=f"{run['planId']}/{run['runId']}",
        #       action="reconcile_retry",
        #       actor_sub="system",
        #       actor_email="system@api-plans-executor",
        #       extra={"bucketIndex": bucket_index, "campaignIndex": campaign_index, "retry": 1, "retryLimit": reconcile_retry_limit},
        #   )
        ...

    def test_activate_warming_bucket_campaign_failure_records_creation_failed(self):
        # Model on this file's existing _activate_warming_bucket failure test.
        # Assert action="creation_failed", extra={"bucketIndex": ..., "campaignIndex": ci, "error": str(exc)}.
        ...

    def test_prestart_next_bucket_campaign_failure_records_creation_failed(self):
        # Model on this file's existing _prestart_next_bucket failure test.
        # Assert action="creation_failed", extra={"bucketIndex": next_index, "campaignIndex": ci, "error": str(exc)}.
        ...

    def test_start_warmed_campaign_failure_records_creation_failed(self):
        # Covers executor.py:3686 (start-warmed-campaign generic failure path).
        ...

    def test_campaign_creation_client_error_records_creation_failed(self):
        # Covers executor.py:3924 (ClientError branch, non-throttle).
        ...

    def test_campaign_creation_generic_exception_records_creation_failed(self):
        # Covers executor.py:3948 (bare Exception branch).
        ...
```

The bodies above are deliberately sketched, not fully written — this task's implementer must read the actual current fixture/mocking pattern this test file already uses for `_start_one_campaign`'s existing empty-segment-retry test and its existing campaign-creation-failure tests (search this file for `_EmptySegmentError` and for assertions on `cs["exitReason"]`/`REASON_CREATION_FAILED` to find them), and write each new test as a variant of an existing one plus the new `build_audit`/`mock_audit.record` assertion — copying real, existing, working fixture setup, not inventing new fixture shapes. Every test must be a complete, runnable pytest test with real assertions before moving to Step 2 — no test may be left as a sketch.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd services/api-plans && python -m pytest tests/unit/test_executor_v2.py::TestReconcileRetryAndCreationFailedAuditEvents -v`
Expected: FAIL — all 6 fail on `mock_audit.record` not called.

- [ ] **Step 3: Instrument the empty-segment retry**

In `services/api-plans/src/executor.py`, inside the `except _EmptySegmentError as exc:` block of `_start_one_campaign`'s retry loop, immediately after line 3735 (`cs["reconcileRetries"] = empty_retries + 1`) and before the `_slog.warn(...)` call at line 3736, insert:

```python
                    _record_plan_event(
                        run,
                        "reconcile_retry",
                        {
                            "bucketIndex": bucket_index,
                            "campaignIndex": campaign_index,
                            "retry": empty_retries + 1,
                            "retryLimit": reconcile_retry_limit,
                        },
                    )
```

- [ ] **Step 4: Instrument `_activate_warming_bucket`'s creation_failed (line 2066)**

Immediately after line 2068 (`cs["completedAt"] = now_iso`, the last line of that `except` block), insert:

```python
                    _record_plan_event(
                        run,
                        "creation_failed",
                        {"bucketIndex": bucket_index, "campaignIndex": ci, "error": str(exc)},
                    )
```

- [ ] **Step 5: Instrument `_prestart_next_bucket`'s creation_failed (line 2250)**

Immediately after line 2252 (`cs["completedAt"] = _now_iso()`, the last line of that `except` block), insert:

```python
                    _record_plan_event(
                        run,
                        "creation_failed",
                        {"bucketIndex": next_index, "campaignIndex": ci, "error": str(exc)},
                    )
```

- [ ] **Step 6: Instrument `_start_one_campaign`'s three creation_failed sites (lines 3686, 3924, 3948)**

Immediately after line 3688 (`cs["completedAt"] = now_iso`, in the "start warmed campaign failed" `else` branch), insert:

```python
                cs["completedAt"] = now_iso
                _record_plan_event(
                    run,
                    "creation_failed",
                    {"bucketIndex": bucket_index, "campaignIndex": campaign_index, "error": str(exc)},
                )
```

(Note: `cs["completedAt"] = now_iso` above is the existing line 3688 shown for anchoring — do not duplicate it, only add the `_record_plan_event(...)` call after it.)

Immediately after line 3926 (`cs["completedAt"] = now_iso`, in the `ClientError`/non-throttle branch), insert the same shape — note this branch is indented one level shallower (12 spaces) than the empty-segment-retry site in Step 3 and the warmed-campaign site above (both 16-20 spaces) — match the file's actual indentation at this exact point, not the other sites':

```python
            _record_plan_event(
                run,
                "creation_failed",
                {"bucketIndex": bucket_index, "campaignIndex": campaign_index, "error": str(exc)},
            )
```

Immediately after line 3950 (`cs["completedAt"] = now_iso`, in the bare `except Exception as exc:` branch), insert:

```python
        _record_plan_event(
            run,
            "creation_failed",
            {"bucketIndex": bucket_index, "campaignIndex": campaign_index, "error": str(exc)},
        )
```

Match each insertion's indentation to its surrounding block exactly (the three sites have different nesting depth — verify against the actual current file, not the brief's approximation, before inserting).

- [ ] **Step 7: Run the tests to verify they pass**

Run: `cd services/api-plans && python -m pytest tests/unit/test_executor_v2.py::TestReconcileRetryAndCreationFailedAuditEvents -v`
Expected: PASS (6 tests).

- [ ] **Step 8: Run the full package suite**

Run: `cd services/api-plans && python -m pytest tests/unit -q`
Expected: all previously-passing tests (432 baseline + this plan's prior tasks' new tests) still pass, plus these 6.

- [ ] **Step 9: Commit**

```bash
git add services/api-plans/src/executor.py services/api-plans/tests/unit/test_executor_v2.py
git commit -m "feat(executor): audit reconcile_retry and creation_failed at all 6 sites"
```

---

### Task 6: Frontend — surface the new actions in `Audit.tsx`

**Files:**
- Modify: `frontend/src/pages/Audit.tsx`
- Test: `frontend/src/pages/Audit.test.ts` (new)

**Interfaces:**
- Consumes: nothing from Tasks 1-5 (frontend reads via the existing `GET /audit` endpoint, unaffected by this plan's backend changes at the API contract level — new rows just have new `action`/`entityType` values already supported by the existing `AuditEntry` type's `extra` field).
- Produces: `actionTone` becomes exported (was previously module-private) so it's testable per this repo's established convention (pure logic gets a test, JSX doesn't).

- [ ] **Step 1: Write the failing test**

Create `frontend/src/pages/Audit.test.ts`:

```ts
import { describe, expect, it } from 'vitest';

import { actionTone } from './Audit';

describe('actionTone', () => {
  it('tones existing manual actions unchanged', () => {
    expect(actionTone('create')).toBe('success');
    expect(actionTone('delete')).toBe('danger');
    expect(actionTone('pause')).toBe('warning');
  });

  it('tones bucket_started and bucket_completed as success', () => {
    expect(actionTone('bucket_started')).toBe('success');
    expect(actionTone('bucket_completed')).toBe('success');
  });

  it('tones window_closed as warning', () => {
    expect(actionTone('window_closed')).toBe('warning');
  });

  it('tones reconcile_retry as warning', () => {
    expect(actionTone('reconcile_retry')).toBe('warning');
  });

  it('tones creation_failed as danger', () => {
    expect(actionTone('creation_failed')).toBe('danger');
  });

  it('falls back to default for an unrecognized action', () => {
    expect(actionTone('something_new')).toBe('default');
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd frontend && npx vitest run src/pages/Audit.test.ts`
Expected: FAIL — `actionTone` is not exported from `./Audit` (module has no exported member `actionTone`).

- [ ] **Step 3: Export `actionTone` and extend it**

In `frontend/src/pages/Audit.tsx`, change line 279 from:

```ts
function actionTone(action: string): 'success' | 'warning' | 'danger' | 'default' {
```

to:

```ts
export function actionTone(action: string): 'success' | 'warning' | 'danger' | 'default' {
```

And extend the `switch` (lines 280-294) by adding new `case` branches — the full updated function body:

```ts
export function actionTone(action: string): 'success' | 'warning' | 'danger' | 'default' {
  switch (action) {
    case 'create':
    case 'start':
    case 'resume':
    case 'bucket_started':
    case 'bucket_completed':
      return 'success';
    case 'pause':
    case 'estimate':
    case 'snapshot':
    case 'window_closed':
    case 'reconcile_retry':
      return 'warning';
    case 'delete':
    case 'stop':
    case 'creation_failed':
      return 'danger';
    default:
      return 'default';
  }
}
```

- [ ] **Step 4: Add the new actions and entity type to the filter dropdowns**

In the same file, change the `ACTIONS` array (lines 16-26) to:

```ts
const ACTIONS = [
  'create',
  'update',
  'delete',
  'estimate',
  'snapshot',
  'start',
  'stop',
  'pause',
  'resume',
  'bucket_started',
  'bucket_completed',
  'window_closed',
  'reconcile_retry',
  'creation_failed',
];
```

And change `ENTITY_TYPES` (line 28) to include the entity type these new automatic events use (already used by manual `plan_run` actions today, per `handlers/runs.py`, but never added to this dropdown):

```ts
const ENTITY_TYPES = ['segment', 'campaign', 'plan_run'];
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd frontend && npx vitest run src/pages/Audit.test.ts`
Expected: PASS (6 tests).

- [ ] **Step 6: Typecheck**

Run: `cd frontend && npm run typecheck`
Expected: no new errors.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/pages/Audit.tsx frontend/src/pages/Audit.test.ts
git commit -m "feat(frontend): surface automatic plan_run audit actions in Audit log filters"
```

---

### Task 7: Full verification

**Files:** none (verification only)

- [ ] **Step 1: Backend full suite**

Run: `cd services/api-plans && python -m pytest tests/unit -q`
Expected: 432 baseline + 3 (Task 1) + 2 (Task 2) + 1 (Task 3) + 3 (Task 4) + 6 (Task 5) = 447 passed, 0 failed.

- [ ] **Step 2: Frontend full suite + typecheck + build**

Run: `cd frontend && npx vitest run && npm run typecheck && npm run build`
Expected: tests pass except the 3 known pre-existing, unrelated `chainMap.test.ts` failures (not part of this plan); typecheck and build clean.

- [ ] **Step 3: Grep for accidental scope creep**

Run: `git diff main --stat` (or the equivalent range for this plan's branch) and confirm the only files touched are the 4 listed in File Structure above — no incidental edits to `_create_segment`'s signature, no changes to any of the 4 `_create_segment` call sites' return-value unpacking (that's the separate, out-of-scope reconcile-normalization plan), no changes to `handlers/runs.py` or IaC.

---

## Self-Review Notes

- **Spec coverage:** all 12 verified instrumentation points from the research (2 bucket_started, 1 bucket_completed, 3 window_closed, 1 reconcile_retry, 5 creation_failed) have a task and an exact insertion point. The one known gap from research — "window_opened" for automatic (non-manual) run starts — could not be verified with certainty (the automatic trigger's exact entry point was not confirmed) and is intentionally left out of this plan rather than guessed; a follow-up plan should re-investigate specifically that one event type.
- **Placeholder scan:** Task 5's test bodies are the one deliberate exception to "no placeholders" — they're explicitly scoped as "model on an existing test in this file plus one new assertion" because the exact existing fixture shape for `_start_one_campaign`'s error-path tests could not be transcribed here without reading the full ~250-line surrounding test class, which would have made this plan document unreviewable in length. The task text is explicit that no test may be left unfinished — the implementer must find and adapt the real fixture, not invent one.
- **Type consistency:** `_record_plan_event(run, action, extra=None)` signature is defined once in Task 1 and used identically (positional `run`, `action`, keyword-or-positional `extra`) at all 11 call sites across Tasks 2-5.
