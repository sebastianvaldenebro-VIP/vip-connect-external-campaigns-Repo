# Testing — VIP Connect Admin UI

---

## 1. Test suites

| Suite | Location | Runtime | What it covers |
| --- | --- | --- | --- |
| Python unit — executor v1 | `services/api-plans/tests/unit/test_executor.py` | pytest | Legacy single-campaign execution flow |
| Python unit — executor v2 | `services/api-plans/tests/unit/test_executor_v2.py` | pytest | DAG dispatch, pre-start, cascade-cancel, on_plan_complete |
| Python unit — store | `services/api-plans/tests/unit/test_store.py` | pytest | DynamoDB serialization / deserialization |
| Python unit — builders | `services/api-plans/tests/unit/test_builders.py` | pytest | Segment name construction, filter mapping |
| TypeScript type check | `frontend/` | `tsc --noEmit` | Static type correctness across all frontend files |

---

## 2. How to run

### Python unit tests

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-plans

# All suites
PYTHONPATH="src:../shared/python" python3 -m pytest tests/unit/ -v

# Specific file
PYTHONPATH="src:../shared/python" python3 -m pytest tests/unit/test_executor_v2.py -v

# With coverage report
PYTHONPATH="src:../shared/python" python3 -m pytest tests/unit/ --cov=src --cov-report=term-missing
```

`PYTHONPATH` must include both `src` (the Lambda source) and `../shared/python` (the `vip_shared` package).

### TypeScript type check

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/frontend
npx tsc --noEmit
```

---

## 3. test_executor_v2.py — coverage map

44 tests covering the full executor v2 path.

### Helper utilities

| Helper | Purpose |
| --- | --- |
| `_campaign_state(cid, status, connect_id)` | Build a `campaignState` dict for a run |
| `_bucket_state(campaigns, status, bucket_id)` | Build a `bucketState` dict |
| `_make_plan(buckets)` | Build a minimal plan dict (with `planSnapshot`-compatible shape) |
| `_make_run(plan, bucket_states, status, bucket_index)` | Build a full run dict |

### Test groups

**`_find_campaign_state`**
- Returns campaign when it exists in the current bucket
- Returns campaign when it exists in a different bucket (cross-bucket lookup)
- Returns `None` for unknown ID

**`_all_campaigns_terminal`**
- `True` when all campaigns have terminal status
- `False` when any campaign is still `running`
- `False` when any campaign is still `queued`

**`_dispatch_ready_campaigns` — DAG dispatch**
- Stage-1 campaign (no deps) starts immediately
- Stage-2 campaign waits for parent to complete
- Stage-2 starts once parent becomes `completed`
- Cascade-cancel: parent `error` → child `cancelled`
- Cascade-cancel: parent `cancelled` → child `cancelled`
- Cross-bucket dep: child in bucket 1 waits for parent in bucket 0
- Cross-bucket dep resolves once parent is `completed`
- No dispatch when parent is still `running`
- `dependsOn` with multiple parents: all must complete before child starts

**`tick` integration**
- Running campaign polled; transitions to `completed` when Connect reports done
- Running campaign error → cascade to dependent queued campaign
- All campaigns terminal in a tick → `_advance_bucket` called
- Stale tick (run already completed) → returns without mutation
- Pre-start window triggered inside tick when time remaining ≤ 5 min

**`_prestart_next_bucket`**
- Stage-1 campaigns in next bucket set to `warming` with a `connectCampaignId`
- Stage-2 campaigns (have deps) remain `queued` during pre-start
- Already-warming bucket is not double-warmed

**`_expire_bucket`**
- Running campaigns set to `expired`
- Queued campaigns set to `cancelled` with `exitReason: "bucket_expired"`
- `_advance_bucket` called after expiry

**`_advance_bucket`**
- Status-based bucket: advances when all campaigns terminal
- Last bucket: run status set to `completed`
- `start_run_chained` called after last bucket completes
- Next bucket in `warming` state → `_activate_warming_bucket` called instead of `_start_bucket`

**`abort_run`**
- Running campaigns stopped
- Queued campaigns set to `cancelled: aborted`
- Warming campaigns set to `cancelled: aborted`
- Run status set to `aborted`
- Schedule deleted

**`start_run_chained`**
- Finds all plans with `trigger.type = "on_plan_complete"` and matching `planId`
- Calls `start_run` for each downstream plan

**`_maybe_loop`**
- Loop plan: re-triggers the same plan after completion when `loop.enabled = true`
- Stops looping after `loop.stopAfter` ISO timestamp
- Does not loop when `loop.enabled = false`

---

## 4. Mocking strategy

All tests mock at the boundary of external I/O:

```python
# DynamoDB
mock_table = MagicMock()
with patch("store._table", return_value=mock_table):
    ...

# Connect Campaigns
with patch("executor._safe_stop_campaign") as mock_stop:
    ...

# EventBridge Scheduler
with patch("scheduler_manager.upsert_schedule") as mock_sched:
    ...

# store.save_run / store.get_run
with patch("executor.store") as mock_store:
    mock_store.get_run.return_value = run
    ...
```

No test hits real AWS APIs. No network calls. Tests run entirely in-process.

---

## 5. Adding a new test

1. Add a `def test_<description>():` function to the relevant test file.
2. Use the helper factories (`_make_run`, `_make_plan`, etc.) to build test data.
3. Patch any external calls the code under test would make.
4. Assert on the mutated state of the run dict — the executor mutates in place and returns.

Example:

```python
def test_dispatch_starts_stage1_immediately():
    plan = _make_plan([_bucket([_campaign("c1", depends_on=[])])])
    run = _make_run(plan, [_bucket_state([_campaign_state("c1", "queued")])])

    with patch("executor._start_one_campaign") as mock_start:
        changed = executor._dispatch_ready_campaigns(run, 0)

    mock_start.assert_called_once()
    assert changed is True
    assert run["bucketStates"][0]["campaignStates"][0]["status"] == "running"
```

---

## 6. CI integration `[planned]`

The test command to wire into GitHub Actions:

```yaml
- name: Run Python unit tests
  working-directory: services/api-plans
  env:
    PYTHONPATH: src:../shared/python
  run: python3 -m pytest tests/unit/ -v --tb=short --exit-zero-on-no-tests

- name: TypeScript type check
  working-directory: frontend
  run: npx tsc --noEmit
```

These are not yet part of the CI pipeline; tracked as a post-MVP hardening task.
