# Plans Scheduler — Design Spec
**Date:** 2026-05-06  
**Status:** Approved

## Overview

Add a Scheduler feature to the Plans section that lets operators configure a daily auto-run for any non-template plan. A plan can be configured to run automatically at a specific time on selected days of the week, without operator intervention.

---

## Data Model

Add an optional `schedule` field to the plan item in DynamoDB (`VipAdminPlans`, SK=`META`):

```json
{
  "schedule": {
    "enabled": true,
    "hour": 8,
    "minute": 30,
    "timezone": "America/New_York",
    "days": ["MON", "TUE", "WED", "THU", "FRI"]
  }
}
```

- `hour` / `minute`: local time in `timezone`
- `timezone`: always `"America/New_York"` in this system (stored for clarity and future flexibility)
- `days`: subset of `["MON","TUE","WED","THU","FRI","SAT","SUN"]` — at least one required when enabled
- Field is absent (not null) on plans that have never had a schedule configured

**DST note:** EventBridge Rules only accept UTC cron expressions. The backend converts local→UTC at save time using `zoneinfo`. When DST changes, the schedule drifts 1 hour. The UI displays a warning and lets the user re-save to correct it.

---

## Backend

### New file: `services/api-plans/src/scheduler_manager.py`

Manages EventBridge Rules for plan schedules. Rule naming: `vip-sched-{planId.replace('-','')[:20]}`.

**`upsert_schedule(plan_id, schedule)`**
1. Compute UTC hour/minute from local time + timezone via `zoneinfo`
2. Build EventBridge cron: `cron({utc_min} {utc_hour} ? * {days_str} *)`  
   e.g. MON-FRI at 8:30 AM NY (EST) → `cron(30 13 ? * MON-FRI *)`
3. `events:PutRule` — creates or updates the rule
4. `events:PutTargets` — Lambda ARN as target, input `{"action":"scheduled_run","planId":"..."}`
5. `lambda:AddPermission` — grants `events.amazonaws.com` invoke rights (idempotent, ignores `ResourceConflictException`)

**`delete_schedule(plan_id)`**
1. `events:RemoveTargets`
2. `events:DeleteRule`
3. `lambda:RemovePermission`
All steps wrapped in try/except, `ResourceNotFoundException` silently ignored.

### Modify: `services/api-plans/src/store.py`

- `put_plan`: persist `schedule` field if present
- `_plan_from_item`: return `schedule` field (pass-through, no transformation)

### Modify: `services/api-plans/src/handlers/plans.py`

- `create_plan`: after `store.put_plan`, if `schedule.enabled == True` → call `scheduler_manager.upsert_schedule`
- `update_plan`: compare old vs new schedule — call `upsert_schedule` if enabled, `delete_schedule` if transitioning to disabled or removed
- `delete_plan`: call `scheduler_manager.delete_schedule` (always, idempotent)

### Modify: `services/api-plans/src/executor.py`

Add `scheduled_run(plan_id)`:
```python
def scheduled_run(plan_id: str) -> dict:
    latest = get_latest_run(plan_id)
    if latest and latest.get("status") == "running":
        logger.info("scheduled_run: plan %s already running, skipping", plan_id)
        return {"ok": True, "reason": "already_running"}
    return start_run(plan_id)
```

### Modify: `services/api-plans/src/handler.py`

Add branch in `lambda_handler`:
```python
elif event.get("action") == "scheduled_run":
    return executor.scheduled_run(event["planId"])
```

### Modify: `infra/lib/stacks/api-plans-stack.ts`

Extend `EventBridgeRules` policy resources to include schedule rules:
```typescript
resources: [
  `arn:aws:events:${this.region}:${this.account}:rule/vip-plan-*`,
  `arn:aws:events:${this.region}:${this.account}:rule/vip-sched-*`,
]
```

---

## Frontend

### New file: `frontend/src/pages/PlansScheduler.tsx`

Renders a table of all non-template plans. Each row is independently editable.

**Columns:**
| Plan | Enabled | Days | Time (ET) | Next run |
|---|---|---|---|---|
| NY Morning Wave | toggle | `L M X J V S D` pills | `08` : `30` | Tomorrow 8:30 AM |

**Row behavior:**
- Toggle switches `enabled` on/off — does not clear the time/days config
- Days: 7 pill buttons (`L M X J V S D` → MON–SUN). At least one must be selected when enabled
- Time: two controlled `<input type="number">` fields for HH (0–23) and MM (0–59)
- **Save button** appears only when the row has unsaved changes
- **Next run** column: computed client-side from `hour`, `minute`, `days`, and current ET datetime. Shows `—` when disabled
- Saving calls `api.plans.update(planId, { schedule: { ... } })`
- On save success: invalidates `['plans']` query, shows inline "Saved ✓" feedback for 2 seconds

**Loading/error states:**
- Full-page spinner while plans list loads
- Per-row save spinner while mutation is in flight
- Row disabled during save

**Empty state:** "No daily plans yet." card if no non-template plans exist.

**DST warning banner** (shown always at top of page):
> "Times are Eastern. Re-save after DST changes to keep schedules accurate."

### Modify: `frontend/src/pages/PlansLayout.tsx`

Add Scheduler item under CONFIGURATION group (after Templates):
```tsx
{ to: '/plans/scheduler', label: 'Scheduler', icon: <clock svg> }
```

### Modify: `frontend/src/App.tsx`

```tsx
import { PlansScheduler } from '@/pages/PlansScheduler';
// ...
<Route path="scheduler" element={<PlansScheduler />} />
```
(nested inside the `<Route path="/plans" element={<PlansLayout />}>` block)

### Modify: `frontend/src/lib/api.ts`

Add `schedule` to `PlanSummary`:
```typescript
export interface PlanSchedule {
  enabled: boolean;
  hour: number;
  minute: number;
  timezone: string;
  days: string[];
}

// In PlanSummary:
schedule?: PlanSchedule;
```

---

## IAM / Infrastructure

No new IAM permissions needed. The existing `EventBridgeRules` policy and `LambdaSelfPermission` policy already cover `events:PutRule`, `events:PutTargets`, `events:RemoveTargets`, `events:DeleteRule`, `lambda:AddPermission`, and `lambda:RemovePermission`. Only the resource ARN pattern needs to be extended to include `vip-sched-*`.

---

## Edge Cases

| Scenario | Behavior |
|---|---|
| Plan fires while already running | `scheduled_run` logs and skips silently |
| Template plan | Never shown in Scheduler page, `scheduled_run` would skip (isTemplate check) |
| Plan deleted with active schedule | `delete_plan` calls `delete_schedule` (idempotent) |
| Days array empty on save | Frontend validation blocks save — at least 1 day required |
| Invalid hour/minute | Frontend clamps: hour 0–23, minute 0–59 |
| Schedule disabled but time set | Rule is deleted; config preserved in DynamoDB for re-enable |

---

## Build Sequence

1. `scheduler_manager.py` (standalone, no deps on executor)
2. `store.py` — add schedule field passthrough
3. `handlers/plans.py` — wire scheduler_manager on create/update/delete
4. `executor.py` — add `scheduled_run`
5. `handler.py` — add `scheduled_run` action dispatch
6. CDK — extend EventBridgeRules resource ARNs, deploy
7. Frontend: `api.ts` types → `PlansScheduler.tsx` → `PlansLayout.tsx` + `App.tsx`
8. Build + deploy frontend
