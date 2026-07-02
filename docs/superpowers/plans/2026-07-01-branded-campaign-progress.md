# Branded Campaign Progress UI — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a live progress panel to PlanDetail that shows PENDING / DIALED contact counts per branded campaign, auto-refreshing every 30 seconds while the run is active.

**Architecture:** A new backend endpoint reads the run's campaign states to find branded campaign IDs, queries `VipProgressiveCampaignQueue` for PENDING and DIALED counts keyed by plan-level `campaignId`, and returns them. The frontend polls this endpoint every 30 seconds (React Query `refetchInterval`) and renders the counts inside each branded `CampaignCard`.

**Tech Stack:** Python 3.12 (Lambda handler), TypeScript + React 18, @tanstack/react-query v5, Tailwind CSS.

## Global Constraints

- PHI must never appear in logs, error messages, stack traces, URLs, or query strings. Phone numbers are PHI. The endpoint must never return phone numbers — only counts.
- Never run `git commit`, `git push`, `git add` — Sebastian creates all commits.
- No speculative features — YAGNI. Only what is listed here.
- Touch only what you must. Do not refactor surrounding code in files you edit.
- After every backend code change: run `cd services/api-plans && python3 -m pytest tests/ -q`. After every frontend change: run `cd frontend && npx tsc --noEmit`.
- The response is keyed by plan-level `campaignId` (e.g. `"NY-NL_13"`), NOT by `brandedCampaignId` (the internal DDB UUID). The frontend already has `cs.campaignId`; it must not need to know the internal `brandedCampaignId`.

---

## File Map

| File | Change |
|---|---|
| `services/api-plans/src/executor.py` | Add `get_branded_queue_counts(campaign_id: str) -> tuple[int, int]` (public, not prefixed with `_`) |
| `services/api-plans/src/handlers/runs.py` | Add `branded_progress(event, path_params)` handler |
| `services/api-plans/src/router.py` | Add `GET /plans/{id}/runs/{runId}/branded-progress` route |
| `services/api-plans/tests/unit/test_branded_progress_handler.py` | New test file for the handler |
| `frontend/src/lib/api.ts` | Add `BrandedProgressResponse` type + `getBrandedProgressV2` function |
| `frontend/src/pages/PlanDetail.tsx` | Add `useQuery` for progress, `brandedCounts` prop on `CampaignCard`, progress UI |

---

## Task 1: Backend — queue count helper + handler + route

**Files:**
- Modify: `services/api-plans/src/executor.py` (add after `_count_branded_queue`, around line 215)
- Modify: `services/api-plans/src/handlers/runs.py` (append new function at end)
- Modify: `services/api-plans/src/router.py` (add one route entry)
- Create: `services/api-plans/tests/unit/test_branded_progress_handler.py`

**Interfaces:**
- Produces: `executor.get_branded_queue_counts(campaign_id: str) -> tuple[int, int]` — returns `(pending_count, dialed_count)`. Uses `_CAMPAIGN_QUEUE_TABLE_BRANDED` env var and `_get_ddb_client()` already defined in executor.py.
- Produces: `handlers.runs.branded_progress(event, path_params) -> dict` — HTTP handler returning `{"progress": {campaignId: {"pending": N, "dialed": N, "total": N}}}`.
- Produces: route `"GET /plans/{id}/runs/{runId}/branded-progress"` in router.py.

- [ ] **Step 1: Write failing tests**

Create `services/api-plans/tests/unit/test_branded_progress_handler.py`:

```python
"""Tests for branded_progress handler in handlers/runs.py."""
from __future__ import annotations

import sys
import os
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

_stub_modules = {
    "store": MagicMock(),
    "executor": MagicMock(),
    "scheduler_manager": MagicMock(),
    "vip_shared": MagicMock(),
    "vip_shared.application": MagicMock(),
    "vip_shared.application.http": MagicMock(),
    "vip_shared.infrastructure": MagicMock(),
    "vip_shared.infrastructure.persistence": MagicMock(),
    "vip_shared.infrastructure.persistence.audit": MagicMock(),
}


def _make_run(campaign_states_per_bucket):
    """Helper to build a minimal run dict."""
    return {
        "planId": "plan-1",
        "runId": "run-1",
        "status": "running",
        "bucketStates": [
            {"campaignStates": cs_list}
            for cs_list in campaign_states_per_bucket
        ],
    }


def test_returns_counts_for_branded_campaign():
    with patch.dict(sys.modules, _stub_modules):
        import importlib
        import handlers.runs as runs_mod
        importlib.reload(runs_mod)

        runs_mod.store.get_run.return_value = _make_run([
            [
                {"campaignId": "NY-NL_13", "brandedCampaignId": "abc-123", "status": "running"},
                {"campaignId": "NJ-NL_5", "status": "running"},  # no brandedCampaignId
            ]
        ])
        runs_mod.executor.get_branded_queue_counts.return_value = (20, 12)

        from vip_shared.application.http import json_response as _jr
        runs_mod.json_response = lambda code, body: {"statusCode": code, "body": body}

        event = {}
        result = runs_mod.branded_progress(event, {"id": "plan-1", "runId": "run-1"})

        assert result["statusCode"] == 200
        body = result["body"]
        assert "NY-NL_13" in body["progress"]
        assert body["progress"]["NY-NL_13"] == {"pending": 20, "dialed": 12, "total": 32}
        assert "NJ-NL_5" not in body["progress"]
        runs_mod.executor.get_branded_queue_counts.assert_called_once_with("abc-123")


def test_returns_404_when_run_not_found():
    with patch.dict(sys.modules, _stub_modules):
        import importlib
        import handlers.runs as runs_mod
        importlib.reload(runs_mod)

        runs_mod.store.get_run.return_value = None
        runs_mod.json_response = lambda code, body: {"statusCode": code, "body": body}

        result = runs_mod.branded_progress({}, {"id": "plan-1", "runId": "missing"})
        assert result["statusCode"] == 404


def test_returns_empty_progress_when_no_branded_campaigns():
    with patch.dict(sys.modules, _stub_modules):
        import importlib
        import handlers.runs as runs_mod
        importlib.reload(runs_mod)

        runs_mod.store.get_run.return_value = _make_run([
            [{"campaignId": "NJ-NL_5", "status": "running"}]
        ])
        runs_mod.json_response = lambda code, body: {"statusCode": code, "body": body}

        result = runs_mod.branded_progress({}, {"id": "plan-1", "runId": "run-1"})
        assert result["statusCode"] == 200
        assert result["body"]["progress"] == {}


def test_counts_error_treated_as_zero():
    """If get_branded_queue_counts raises, that campaign is skipped — no 500."""
    with patch.dict(sys.modules, _stub_modules):
        import importlib
        import handlers.runs as runs_mod
        importlib.reload(runs_mod)

        runs_mod.store.get_run.return_value = _make_run([
            [{"campaignId": "NY-NL_13", "brandedCampaignId": "abc-123", "status": "running"}]
        ])
        runs_mod.executor.get_branded_queue_counts.side_effect = Exception("DDB error")
        runs_mod.json_response = lambda code, body: {"statusCode": code, "body": body}

        result = runs_mod.branded_progress({}, {"id": "plan-1", "runId": "run-1"})
        assert result["statusCode"] == 200
        # Campaign skipped on error — not included in progress, no 500
        assert result["body"]["progress"] == {}
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-plans
python3 -m pytest tests/unit/test_branded_progress_handler.py -v 2>&1 | tail -20
```

Expected: `ImportError` or `AttributeError` on `branded_progress` — the function doesn't exist yet.

- [ ] **Step 3: Add `get_branded_queue_counts` to executor.py**

In `services/api-plans/src/executor.py`, locate `_count_branded_queue` (around line 189). Add the following function immediately after it (after its closing line, before `_expire_branded_queue_items`):

```python
def get_branded_queue_counts(branded_campaign_id: str) -> tuple[int, int]:
    """Return (pending_count, dialed_count) for a branded campaign queue.

    Queries VipProgressiveCampaignQueue twice — once filtered to PENDING/DISPATCHING,
    once to DIALED — and returns both counts. Used by the branded-progress endpoint.
    Raises on DDB errors; callers decide whether to swallow or propagate.
    """
    if not _CAMPAIGN_QUEUE_TABLE_BRANDED:
        return (0, 0)
    ddb = _get_ddb_client()
    base_kwargs = dict(
        TableName=_CAMPAIGN_QUEUE_TABLE_BRANDED,
        KeyConditionExpression="campaignId = :c",
        ExpressionAttributeValues={":c": {"S": branded_campaign_id}},
        Select="COUNT",
    )

    def _count_with_filter(filter_expr: str, attr_values: dict) -> int:
        kwargs = {**base_kwargs, "FilterExpression": filter_expr,
                  "ExpressionAttributeNames": {"#s": "status"}}
        kwargs["ExpressionAttributeValues"] = {**base_kwargs["ExpressionAttributeValues"], **attr_values}
        total = 0
        while True:
            resp = ddb.query(**kwargs)
            total += resp.get("Count", 0)
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                return total
            kwargs["ExclusiveStartKey"] = lek

    pending = _count_with_filter(
        "#s IN (:p, :d)",
        {":p": {"S": "PENDING"}, ":d": {"S": "DISPATCHING"}},
    )
    dialed = _count_with_filter("#s = :dl", {":dl": {"S": "DIALED"}})
    return (pending, dialed)
```

- [ ] **Step 4: Add `branded_progress` handler to handlers/runs.py**

At the end of `services/api-plans/src/handlers/runs.py`, append:

```python
def branded_progress(event: dict, path_params: dict) -> dict:
    """Return PENDING/DIALED contact counts per branded campaign for a run.

    Response shape:
      { "progress": { "<campaignId>": { "pending": N, "dialed": N, "total": N } } }

    Keyed by plan-level campaignId (e.g. "NY-NL_13"), not by the internal
    brandedCampaignId UUID. Campaigns without a brandedCampaignId are omitted.
    Count errors per-campaign are swallowed — that campaign is omitted rather
    than failing the whole response.
    """
    plan_id = path_params["id"]
    run_id = path_params["runId"]

    run = store.get_run(plan_id, run_id)
    if not run:
        return json_response(
            404,
            {"error": {"code": "NOT_FOUND", "message": f"Run {run_id} not found"}},
        )

    progress: dict[str, dict] = {}
    for bs in run.get("bucketStates", []):
        for cs in bs.get("campaignStates", []):
            branded_id = cs.get("brandedCampaignId")
            if not branded_id:
                continue
            campaign_id = cs.get("campaignId", "")
            try:
                pending, dialed = executor.get_branded_queue_counts(branded_id)
                progress[campaign_id] = {
                    "pending": pending,
                    "dialed": dialed,
                    "total": pending + dialed,
                }
            except Exception:
                pass  # DDB transient error — omit this campaign, don't 500

    return json_response(200, {"progress": progress})
```

- [ ] **Step 5: Add route to router.py**

In `services/api-plans/src/router.py`, add after the `apply-snapshot` route (line 33):

```python
    "GET /plans/{id}/runs/{runId}/branded-progress": runs_handler.branded_progress,
```

The full ROUTES dict should now include:
```python
    "POST /plans/{id}/runs/{runId}/apply-snapshot": runs_handler.apply_plan_snapshot,
    "GET /plans/{id}/runs/{runId}/branded-progress": runs_handler.branded_progress,
```

- [ ] **Step 6: Run tests — verify they pass**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-plans
python3 -m pytest tests/unit/test_branded_progress_handler.py -v 2>&1 | tail -20
```

Expected: `4 passed`.

- [ ] **Step 7: Run full suite — no regressions**

```bash
python3 -m pytest tests/ -q 2>&1 | tail -10
```

Expected: all tests pass (≥200 passed, 0 failed).

---

## Task 2: Frontend — api.ts type and fetch function

**Files:**
- Modify: `frontend/src/lib/api.ts`

**Interfaces:**
- Consumes: route `GET /plans/{id}/runs/{runId}/branded-progress` from Task 1
- Produces: `BrandedProgressResponse` type (used in Task 3)
- Produces: `api.plans.getBrandedProgressV2(planId, runId)` function (called in Task 3)

- [ ] **Step 1: Add the response type**

In `frontend/src/lib/api.ts`, locate `export type CampaignState` (line ~427). Immediately before it, add:

```typescript
export type BrandedCampaignCounts = {
  pending: number;
  dialed: number;
  total: number;
};

export type BrandedProgressResponse = {
  progress: Record<string, BrandedCampaignCounts>;
};
```

- [ ] **Step 2: Add the fetch function**

In `frontend/src/lib/api.ts`, locate `applySnapshotV2` in the `plans` object (last entry before the closing `}`). After it, add:

```typescript
    getBrandedProgressV2: (planId: string, runId: string) =>
      request<BrandedProgressResponse>(
        `/plans/${encodeURIComponent(planId)}/runs/${encodeURIComponent(runId)}/branded-progress`,
      ),
```

- [ ] **Step 3: TypeScript check**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/frontend
npx tsc --noEmit 2>&1 | head -20
```

Expected: zero errors introduced by this task.

---

## Task 3: Frontend — PlanDetail polling + CampaignCard progress display

**Files:**
- Modify: `frontend/src/pages/PlanDetail.tsx`

**Interfaces:**
- Consumes: `BrandedProgressResponse`, `BrandedCampaignCounts`, `api.plans.getBrandedProgressV2` from Task 2
- Consumes: `campaignDef?.deliveryType === 'branded'` (already present)
- Consumes: `cs.campaignId` (already present in `CampaignCard`)

**What to build:**
1. A `useQuery` for branded progress in the `PlanDetail` component body — polls every 30s when run is active.
2. A `brandedCounts?: BrandedCampaignCounts` optional prop on `CampaignCard`.
3. A progress display inside `CampaignCard` shown only when branded and counts are available.

- [ ] **Step 1: Add import for BrandedProgressResponse**

At the top of `frontend/src/pages/PlanDetail.tsx`, the import from `'../lib/api'` currently imports several types. Add `BrandedCampaignCounts` to that import:

Find the existing import line (around line 3–5):
```typescript
import { type ... } from '../lib/api';
```

Add `BrandedCampaignCounts` to it. The exact current import will look like:
```typescript
import {
  type BucketDefV2,
  type CampaignDef,
  ...
  api,
} from '../lib/api';
```

Add `type BrandedCampaignCounts` to that list.

- [ ] **Step 2: Add brandedCounts prop to CampaignCard**

Locate the `CampaignCard` props destructuring (around line 148):

```typescript
function CampaignCard({
  cs,
  campaignDef,
  bucketDef,
  plannedStart,
  parentNames,
  isMerge,
  onForceStart,
  onForceStop,
  onSkip,
}: {
  cs: CampaignState;
  campaignDef?: CampaignDef | null;
  bucketDef?: BucketDefV2 | null;
  plannedStart: Date;
  parentNames?: string[];
  isMerge?: boolean;
  onForceStart?: () => void;
  onForceStop?: () => void;
  onSkip?: () => void;
})
```

Replace with (add `brandedCounts` to both destructuring and type):

```typescript
function CampaignCard({
  cs,
  campaignDef,
  bucketDef,
  plannedStart,
  parentNames,
  isMerge,
  onForceStart,
  onForceStop,
  onSkip,
  brandedCounts,
}: {
  cs: CampaignState;
  campaignDef?: CampaignDef | null;
  bucketDef?: BucketDefV2 | null;
  plannedStart: Date;
  parentNames?: string[];
  isMerge?: boolean;
  onForceStart?: () => void;
  onForceStop?: () => void;
  onSkip?: () => void;
  brandedCounts?: BrandedCampaignCounts;
})
```

- [ ] **Step 3: Render progress inside CampaignCard**

Inside `CampaignCard`, find the block that renders `cs.leadCount` (around line 263):

```tsx
      {cs.leadCount != null && (
        <div className="text-xs text-gray-400">{cs.leadCount.toLocaleString()} leads</div>
      )}
```

Immediately after it, add the branded progress display:

```tsx
      {campaignDef?.deliveryType === 'branded' && brandedCounts != null && (
        <div className="space-y-1">
          <div className="flex items-center justify-between text-[10px] text-gray-500">
            <span>
              <span className="font-medium text-green-600">{brandedCounts.dialed}</span>
              {' dialed · '}
              <span className="font-medium text-amber-600">{brandedCounts.pending}</span>
              {' pending'}
            </span>
            {brandedCounts.total > 0 && (
              <span className="text-gray-400">
                {Math.round((brandedCounts.dialed / brandedCounts.total) * 100)}%
              </span>
            )}
          </div>
          {brandedCounts.total > 0 && (
            <div className="w-full h-1 bg-gray-100 rounded-full overflow-hidden">
              <div
                className="h-full bg-green-400 rounded-full transition-all duration-500"
                style={{ width: `${(brandedCounts.dialed / brandedCounts.total) * 100}%` }}
              />
            </div>
          )}
        </div>
      )}
```

- [ ] **Step 4: Add the branded progress useQuery in PlanDetail**

In the `PlanDetail` component body, locate the existing queries (around line 517–535):

```typescript
  const planQuery = useQuery({ ... });
  const runsQuery = useQuery({ ... });
```

After `runsQuery`, add:

```typescript
  const latestRun = planQuery.data?.latestRun;
  const isRunActive = latestRun?.status === 'running';

  const brandedProgressQuery = useQuery({
    queryKey: ['branded-progress', id, latestRun?.runId],
    queryFn: () => api.plans.getBrandedProgressV2(id!, latestRun!.runId),
    enabled: !!id && !!latestRun?.runId && isRunActive,
    refetchInterval: isRunActive ? 30_000 : false,
  });
```

Note: if `latestRun` is already destructured from `planQuery.data` elsewhere in the component, remove the `const latestRun = ...` line above and use the existing binding instead. Search for `planQuery.data?.latestRun` in the file to check.

- [ ] **Step 5: Pass brandedCounts to CampaignCard**

Find where `CampaignCard` is rendered in the `BucketSection` or in the main render tree. It is called around line 402:

```tsx
<div key={cs.campaignId} className={spanClass}>
  <CampaignCard
    cs={cs}
    campaignDef={campaignDef}
    ...
  />
```

Add `brandedCounts` to the call. The `brandedProgressQuery.data?.progress` is a `Record<string, BrandedCampaignCounts>` keyed by `campaignId`. The `CampaignCard` already has `cs.campaignId`.

Locate every `<CampaignCard` usage and add the prop. There should be one or two call sites. For each, add:

```tsx
    brandedCounts={brandedProgressQuery.data?.progress[cs.campaignId]}
```

To pass `brandedProgressQuery` to `BucketSection`, you need to thread it. Check whether `CampaignCard` is called directly inside the main `PlanDetail` render or inside a `BucketSection` sub-component.

Read `BucketSection`'s props (around line 292) to see what it currently receives. Then:

If `CampaignCard` is inside `BucketSection`:
- Add `brandedProgress?: Record<string, BrandedCampaignCounts>` to `BucketSection`'s props type.
- Pass `brandedProgress={brandedProgressQuery.data?.progress}` at the `BucketSection` call site.
- Inside `BucketSection`, pass `brandedCounts={brandedProgress?.[cs.campaignId]}` to each `CampaignCard`.

If `CampaignCard` is called directly from `PlanDetail`, pass `brandedCounts={brandedProgressQuery.data?.progress[cs.campaignId]}` directly.

- [ ] **Step 6: TypeScript check**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/frontend
npx tsc --noEmit 2>&1 | head -30
```

Expected: zero errors.

- [ ] **Step 7: Lint check**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/frontend
npx eslint src/pages/PlanDetail.tsx src/lib/api.ts 2>&1 | head -20
```

Expected: zero new lint errors.

---

## Verification

1. Start a plan with at least one branded campaign running.
2. Open PlanDetail — the branded campaign card should show a green/amber count line + thin progress bar.
3. Wait 30 seconds — counts should update without a page reload.
4. When the run is not active (`status !== 'running'`), open the browser network tab — no polling requests to `/branded-progress` should fire.
5. When all leads are DIALED: progress bar shows 100%, counter shows `N dialed · 0 pending`.
6. If the endpoint returns an empty `progress` map (no branded campaigns), the existing campaign cards are unchanged.

## What is NOT in scope

- Showing individual lead details or phone numbers in the UI (PHI rule).
- Pause / resume controls for the branded campaign queue from the UI (separate feature).
- Historical progress charts or trends.
- Progress display for non-branded (`campaign` or `journey`) delivery types.
