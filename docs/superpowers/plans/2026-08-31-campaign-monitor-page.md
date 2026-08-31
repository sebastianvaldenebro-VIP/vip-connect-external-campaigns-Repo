# Campaign Monitor Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn `PlanDetail.tsx` (already the page that models bucket→campaign hierarchy) into the "Campaign Monitor" experience from the reference design — a day header with a wall-clock timeline, a reconcile badge on every campaign card, a live day-activity feed, and an agent-availability panel correlated with the running campaigns — using the data Plans B1 (day activity feed) and B2 (reconcile counts) already shipped to production, and the shared UI primitives Plan A already shipped (`StatusChip`, `ProgressBar`, `StepStrip`, `StatTile`, `Avatar`, `ActivityFeed`).

**Architecture:** `PlanDetail.tsx` already renders `plan.buckets` in order via `buildChainMap` — there is no "Track 1 / Track 2" concept anywhere in the real data model (confirmed: `BucketDefV2` has no track/mode field beyond `run_mode`/`parallel`; `PlanSummaryV2.loop` is a day-level re-loop window, not a dedicated lane). This plan does **not** invent a Track abstraction — it keeps rendering buckets in plan order exactly as today, and adds 4 independent, additive pieces on top:
1. A `cs.reconcile` badge on `CampaignCard` (data already flows from Plan B2, just untyped today).
2. A `DayActivityFeed` panel reading the already-existing `api.audit.entityHistory(entityId)` client (zero new backend plumbing — Plan B1's events are already there).
3. An `AgentAvailabilityPanel` reading the already-existing `api.brandedMonitor.getAgentRoster()` client with no `queueId` (confirmed this already returns every agent in the instance, not just the 2 branded teams — the "branded" name is legacy, the data is generic), aggregated client-side by routing profile. This panel is intentionally a compact summary (counts per routing profile), not a full per-agent roster — the full roster is a separate future page (Agent Roster, out of this plan's scope).
4. A `DayHeaderTimeline` — a wall-clock bar showing where "now" sits inside the plan's operating window, with bucket segments positioned by their actual/planned start-end times.

The page's "Main content" section becomes a 2-column grid on wide screens (bucket sections in the wider left column; `AgentAvailabilityPanel` + `DayActivityFeed` stacked in a narrower right column) so campaign slowness and agent availability stay visually adjacent, per the reference design's explicit reasoning — everything single-column below a reasonable breakpoint, matching this app's existing responsive conventions (check `PlanNew.tsx`/`BrandedMonitor.tsx` for the breakpoint classes already in use, don't invent a new one).

**Tech Stack:** React 18 + TypeScript, TanStack Query (existing polling pattern: `refetchInterval` gated on `run.status === 'running'`), Vitest for pure-logic tests (this repo's established convention: test pure functions, not JSX).

**Spec:** No separate spec file. This plan's Architecture section, backed by the verified current-code citations in each task, is the authority. Design intent (why a day-activity-feed, why an agent panel adjacent to campaigns, why a wall-clock timeline) traces back to the reference design brief already used for Plans A/B1/B2 in this session, adapted to the real data model per the "generalize, don't invent" decision already made for this feature.

## Global Constraints

- Timezone: this app already displays everything in **COT (Colombia, UTC-5, no DST)** via the `fmtTime` helper, currently module-scoped inside `PlanDetail.tsx` (Task 4 relocates it to `frontend/src/lib/utils.ts` to avoid a circular import — see Task 4 Step 0) — do not introduce ET or any other timezone convention; every new component in this plan must reuse that one helper, not a second copy.
- `AgentRosterEntry.effectiveStatus` is the real, already-computed vocabulary: `'Available' | 'On Call' | 'ACW' | 'Unavailable' | 'Offline'` (confirmed in `services/api-metrics/src/handlers/branded.py`, mirrored in `frontend/src/lib/api.ts:568`). Do not invent or use any other agent-status vocabulary (not the reference mockup's `on_call`/`away`, not a fourth naming).
- The 5 day-activity-feed event types and their exact `extra` shapes (already live in production since Plan B1) are: `bucket_started {bucketIndex, bucketName}`, `bucket_completed {bucketIndex, bucketName, reason}`, `window_closed {reason}`, `reconcile_retry {bucketIndex, campaignIndex, retry, retryLimit}`, `creation_failed {bucketIndex, campaignIndex, error}`. Do not invent a 6th type or different fields.
- `AgentAvailabilityPanel` in this plan is a **compact summary only** (aggregate counts per routing profile) — do not render a per-agent list here; that's explicitly deferred to a future Agent Roster page.
- Do not touch `services/` (backend) at all in this plan — everything needed already shipped in Plans B1/B2.
- Do not restructure `CampaignCard`/`BucketSection`/`RunStatusBar` beyond what each task explicitly describes — these are large, already-working functions; add to them surgically.
- Run `cd frontend && npx vitest run && npm run typecheck && npm run build` before and after each task. Baseline: whatever `main` currently reports (verify fresh — this plan doesn't know the exact count going in, unlike the backend plans, since frontend test count drifts with every merged plan this session).
- **`@/components/ui` import-path collision (found during Task 1):** `frontend/src/components/ui.tsx` (old primitives: `Button`/`Card`/`Badge`/`Spinner`/etc.) and `frontend/src/components/ui/` (Plan A's new barrel: `StatusChip`/`ProgressBar`/`StepStrip`/`StatTile`/`Avatar`/`ActivityFeed`/`status`) both exist; TypeScript's module resolution silently picks the `.tsx` file over the directory for the bare `@/components/ui` path. Every task in this plan that needs a Plan A primitive imports it from its specific file (`@/components/ui/StatusChip`, `@/components/ui/StatTile`, `@/components/ui/ActivityFeed`, `@/components/ui/status`) — never from the bare `@/components/ui` path. Pre-existing imports of `Button`/`Spinner`/etc. from the bare path are correct as-is and must not be changed. This collision is a pre-existing repo issue (introduced when Plan A added `components/ui/` alongside the already-existing `components/ui.tsx`) — fixing the collision itself (e.g. merging or renaming) is out of this plan's scope.

---

## File Structure

```
frontend/src/lib/api.ts                       # MODIFY — add `reconcile` to CampaignState, type AuditEntry.extra
frontend/src/lib/utils.ts                     # MODIFY — Task 4 relocates fmtTime/COL_OFFSET_MS here from PlanDetail.tsx
frontend/src/pages/PlanDetail.tsx             # MODIFY — reconcile badge in CampaignCard, mount 3 new panels, 2-col grid, fmtTime now imported not defined
frontend/src/pages/DayActivityFeed.tsx        # NEW — day activity feed panel + its event-formatting logic
frontend/src/pages/DayActivityFeed.test.ts    # NEW
frontend/src/pages/AgentAvailabilityPanel.tsx # NEW — compact agent-by-routing-profile summary
frontend/src/pages/AgentAvailabilityPanel.test.ts # NEW
frontend/src/pages/DayHeaderTimeline.tsx      # NEW — wall-clock bucket timeline
frontend/src/pages/DayHeaderTimeline.test.ts  # NEW
```

---

### Task 1: Types — `CampaignState.reconcile`, `AuditEntry.extra`, reconcile tone helper

**Files:**
- Modify: `frontend/src/lib/api.ts` (`CampaignState` type currently at lines 579-590; `AuditEntry` type currently at lines 235-248)
- Create: `frontend/src/pages/reconcile.ts` (small, shared by Task 2 and any future consumer)
- Test: `frontend/src/pages/reconcile.test.ts`

**Interfaces:**
- Produces: `CampaignState.reconcile?: { expected: number; actual: number; retries: number }` (matches the exact shape `executor.py` writes — `expected`/`actual` are real numbers when present; the field is entirely absent, never `null`-valued, when no segment was created — same optionality pattern already used for `cs.segmentName`/`connectCampaignId` elsewhere in this type). Produces `AuditEventExtra` — a discriminated-by-`action`-adjacent set of types for the 5 known extras (not a true discriminated union since `AuditEntry.action` is a plain `string`, not a literal type — see Task 4 for how the feed component narrows on it). Produces `reconcileTone(reconcile: CampaignState['reconcile']): StatusTone` and `formatReconcile(reconcile: CampaignState['reconcile']): string`. Task 2 consumes both; Task 4 consumes the `AuditEventExtra` shapes.

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/pages/reconcile.test.ts`:

```ts
import { describe, expect, it } from 'vitest';

import { formatReconcile, reconcileTone } from './reconcile';

describe('reconcileTone', () => {
  it('returns success when actual equals expected', () => {
    expect(reconcileTone({ expected: 50, actual: 50, retries: 0 })).toBe('success');
  });

  it('returns warning when actual is less than expected (truncation or exclusions)', () => {
    expect(reconcileTone({ expected: 50, actual: 47, retries: 0 })).toBe('warning');
  });

  it('returns warning when retries were needed even if counts match', () => {
    expect(reconcileTone({ expected: 50, actual: 50, retries: 2 })).toBe('warning');
  });

  it('returns neutral when reconcile data is absent', () => {
    expect(reconcileTone(undefined)).toBe('neutral');
  });
});

describe('formatReconcile', () => {
  it('formats a clean match with no retries', () => {
    expect(formatReconcile({ expected: 50, actual: 50, retries: 0 })).toBe('reconcile: 50 → 50 · clean');
  });

  it('formats a mismatch', () => {
    expect(formatReconcile({ expected: 50, actual: 47, retries: 0 })).toBe('reconcile: 50 → 47 · clean');
  });

  it('formats retries, singular', () => {
    expect(formatReconcile({ expected: 50, actual: 50, retries: 1 })).toBe('reconcile: 50 → 50 · 1 retry');
  });

  it('formats retries, plural', () => {
    expect(formatReconcile({ expected: 50, actual: 50, retries: 3 })).toBe('reconcile: 50 → 50 · 3 retries');
  });

  it('returns empty string when reconcile data is absent', () => {
    expect(formatReconcile(undefined)).toBe('');
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd frontend && npx vitest run src/pages/reconcile.test.ts`
Expected: FAIL — `Cannot find module './reconcile'`.

- [ ] **Step 3: Add the type fields**

In `frontend/src/lib/api.ts`, inside the `CampaignState` type (after `errorDetail?: string;`, before the closing `};`, currently around line 589):

```ts
  reconcile?: { expected: number; actual: number; retries: number };
```

Change `AuditEntry.extra` (currently `extra?: unknown;`, line 247) to:

```ts
  extra?: AuditEventExtra | Record<string, unknown> | null;
```

Add the `AuditEventExtra` union near the top of the file, right after the `AuditEntry` type definition (after its closing `};`, currently line 248):

```ts
export type AuditEventExtra =
  | { bucketIndex: number; bucketName?: string | null }
  | { bucketIndex: number; bucketName?: string | null; reason: string }
  | { reason: string }
  | { bucketIndex: number; campaignIndex: number; retry: number; retryLimit: number }
  | { bucketIndex: number; campaignIndex: number; error: string };
```

- [ ] **Step 4: Write the implementation**

Create `frontend/src/pages/reconcile.ts`:

```ts
import type { CampaignState } from '@/lib/api';
import type { StatusTone } from '@/components/ui/status';

type Reconcile = CampaignState['reconcile'];

export function reconcileTone(reconcile: Reconcile): StatusTone {
  if (!reconcile) return 'neutral';
  if (reconcile.retries > 0) return 'warning';
  return reconcile.actual === reconcile.expected ? 'success' : 'warning';
}

export function formatReconcile(reconcile: Reconcile): string {
  if (!reconcile) return '';
  const retrySuffix =
    reconcile.retries === 0
      ? 'clean'
      : `${reconcile.retries} retry${reconcile.retries === 1 ? '' : 'ies'}`.replace('retryies', 'retries');
  return `reconcile: ${reconcile.expected} → ${reconcile.actual} · ${retrySuffix}`;
}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd frontend && npx vitest run src/pages/reconcile.test.ts`
Expected: PASS (9 tests). If the `.replace('retryies', 'retries')` hack looks wrong when you actually run it, fix `formatReconcile`'s pluralization directly instead (e.g. `` `${n} ${n === 1 ? 'retry' : 'retries'}` ``) — the brief's inline code is a rough draft of the string-building, not a mandate to keep an awkward `.replace` call; write it cleanly and re-verify against the exact test strings above.

- [ ] **Step 6: Typecheck**

Run: `cd frontend && npm run typecheck`
Expected: no new errors.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/lib/api.ts frontend/src/pages/reconcile.ts frontend/src/pages/reconcile.test.ts
git commit -m "feat(frontend): type cs.reconcile and audit extra shapes, add reconcile formatting"
```

---

### Task 2: Reconcile badge in `CampaignCard`

**Files:**
- Modify: `frontend/src/pages/PlanDetail.tsx` (`CampaignCard`, currently lines 153-412)

**Interfaces:**
- Consumes: `reconcileTone`, `formatReconcile` from `./reconcile` (Task 1); `StatusChip` from `@/components/ui/StatusChip` (Plan A, already merged).

**Import path warning (found during Task 1):** `@/components/ui` is ambiguous in this repo — both `frontend/src/components/ui.tsx` (a file, the old primitives: `Button`/`Card`/`Badge`/etc.) and `frontend/src/components/ui/` (a directory, Plan A's new barrel: `StatusChip`/`ProgressBar`/`StepStrip`/`StatTile`/`Avatar`/`ActivityFeed`/`status`) exist side by side, and TypeScript's module resolution picks the `.tsx` file over the directory's `index.ts`, silently. `import { StatusChip } from '@/components/ui'` will fail to compile (`StatusChip` isn't exported from `ui.tsx`). Import Plan A's primitives from their specific file instead: `@/components/ui/StatusChip`, `@/components/ui/StatTile`, `@/components/ui/ActivityFeed`, `@/components/ui/status`, etc. Keep `Button`/`Spinner`/other pre-existing primitives imported from the bare `@/components/ui` exactly as before — that part still resolves correctly to `ui.tsx`, don't change those import lines.

**Read this first:** `CampaignCard`'s current body, specifically where `TimingMeta` renders (currently the last line before the closing `</div>`, line 409) — the reconcile badge belongs right above it, after any delivery-type-specific block (branded/SMS), so it reads as "the last fact about this campaign before timing."

- [ ] **Step 1: Add the import**

In `frontend/src/pages/PlanDetail.tsx`, keep the existing `Button`/`Spinner` import from `@/components/ui` unchanged (line 5), and add a separate new import line for `StatusChip` from its specific file:

```tsx
import { StatusChip } from '@/components/ui/StatusChip';
```

And add a new import line after the `buildChainMap` import (line 22):

```tsx
import { formatReconcile, reconcileTone } from './reconcile';
```

- [ ] **Step 2: Render the badge**

In `CampaignCard`, immediately before `<TimingMeta items={timingItems} />` (line 409), insert:

```tsx
      {cs.reconcile && (
        <StatusChip tone={reconcileTone(cs.reconcile)} label={formatReconcile(cs.reconcile)} mono />
      )}
```

- [ ] **Step 3: Verify**

Run: `cd frontend && npm run typecheck`
Expected: no new errors. No new test file for this step — `CampaignCard` is presentational JSX with no new branching logic beyond what Task 1's already-tested `reconcileTone`/`formatReconcile` provide, matching this repo's established convention (pure logic tested, JSX not).

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/PlanDetail.tsx
git commit -m "feat(frontend): show reconcile badge on campaign cards"
```

---

### Task 3: `AgentAvailabilityPanel` — compact summary by routing profile

**Files:**
- Create: `frontend/src/pages/AgentAvailabilityPanel.tsx`
- Test: `frontend/src/pages/AgentAvailabilityPanel.test.ts`

**Interfaces:**
- Consumes: `api.brandedMonitor.getAgentRoster()` (existing client, `frontend/src/lib/api.ts:888-891` — called with NO `queueId` argument, which per verified backend behavior returns every agent in the Connect instance, not just branded teams); `AgentRosterEntry`, `RoutingProfileSummary` types (already exist, `api.ts:563-577`); `StatTile` from `@/components/ui`.
- Produces: `aggregateByRoutingProfile(agents: AgentRosterEntry[]): Array<{ routingProfileId: string; routingProfileName: string; available: number; onCall: number; acw: number; offline: number; unavailable: number; total: number }>` — pure, exported, tested. `AgentAvailabilityPanel` component with props `{ className?: string }` (fetches its own data, no props needed for data).

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/pages/AgentAvailabilityPanel.test.ts`:

```ts
import { describe, expect, it } from 'vitest';

import type { AgentRosterEntry } from '@/lib/api';

import { aggregateByRoutingProfile } from './AgentAvailabilityPanel';

function agent(overrides: Partial<AgentRosterEntry>): AgentRosterEntry {
  return {
    agentId: 'a1',
    agentName: 'Test Agent',
    status: 'Available',
    statusType: 'ROUTABLE',
    effectiveStatus: 'Available',
    isIntentionalAbsence: false,
    activeContactState: '',
    statusStartTimestamp: new Date().toISOString(),
    routingProfileId: 'rp1',
    routingProfileName: 'Outbound Dialer Agent',
    contactsCount: 0,
    ...overrides,
  };
}

describe('aggregateByRoutingProfile', () => {
  it('groups agents by routing profile and counts each effectiveStatus bucket', () => {
    const agents = [
      agent({ agentId: 'a1', routingProfileId: 'rp1', routingProfileName: 'Outbound', effectiveStatus: 'Available' }),
      agent({ agentId: 'a2', routingProfileId: 'rp1', routingProfileName: 'Outbound', effectiveStatus: 'On Call' }),
      agent({ agentId: 'a3', routingProfileId: 'rp2', routingProfileName: 'New Lead', effectiveStatus: 'Available' }),
    ];
    const result = aggregateByRoutingProfile(agents);
    expect(result).toHaveLength(2);
    const outbound = result.find((r) => r.routingProfileId === 'rp1')!;
    expect(outbound).toMatchObject({
      routingProfileName: 'Outbound',
      available: 1,
      onCall: 1,
      acw: 0,
      offline: 0,
      unavailable: 0,
      total: 2,
    });
  });

  it('counts every effectiveStatus value distinctly', () => {
    const agents = [
      agent({ agentId: 'a1', effectiveStatus: 'Available' }),
      agent({ agentId: 'a2', effectiveStatus: 'On Call' }),
      agent({ agentId: 'a3', effectiveStatus: 'ACW' }),
      agent({ agentId: 'a4', effectiveStatus: 'Offline' }),
      agent({ agentId: 'a5', effectiveStatus: 'Unavailable' }),
    ];
    const [result] = aggregateByRoutingProfile(agents);
    expect(result).toMatchObject({ available: 1, onCall: 1, acw: 1, offline: 1, unavailable: 1, total: 5 });
  });

  it('returns an empty array for no agents', () => {
    expect(aggregateByRoutingProfile([])).toEqual([]);
  });

  it('sorts profiles by available count ascending (most understaffed first)', () => {
    const agents = [
      agent({ agentId: 'a1', routingProfileId: 'rp1', routingProfileName: 'Well staffed', effectiveStatus: 'Available' }),
      agent({ agentId: 'a2', routingProfileId: 'rp1', routingProfileName: 'Well staffed', effectiveStatus: 'Available' }),
      agent({ agentId: 'a3', routingProfileId: 'rp2', routingProfileName: 'Understaffed', effectiveStatus: 'On Call' }),
    ];
    const result = aggregateByRoutingProfile(agents);
    expect(result[0].routingProfileName).toBe('Understaffed');
    expect(result[1].routingProfileName).toBe('Well staffed');
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd frontend && npx vitest run src/pages/AgentAvailabilityPanel.test.ts`
Expected: FAIL — `Cannot find module './AgentAvailabilityPanel'`.

- [ ] **Step 3: Write the implementation**

Create `frontend/src/pages/AgentAvailabilityPanel.tsx`:

```tsx
import type { ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';

import { StatTile } from '@/components/ui/StatTile';
import { api, type AgentRosterEntry } from '@/lib/api';

export type RoutingProfileAvailability = {
  routingProfileId: string;
  routingProfileName: string;
  available: number;
  onCall: number;
  acw: number;
  offline: number;
  unavailable: number;
  total: number;
};

export function aggregateByRoutingProfile(agents: AgentRosterEntry[]): RoutingProfileAvailability[] {
  const byProfile = new Map<string, RoutingProfileAvailability>();
  for (const agent of agents) {
    let row = byProfile.get(agent.routingProfileId);
    if (!row) {
      row = {
        routingProfileId: agent.routingProfileId,
        routingProfileName: agent.routingProfileName,
        available: 0,
        onCall: 0,
        acw: 0,
        offline: 0,
        unavailable: 0,
        total: 0,
      };
      byProfile.set(agent.routingProfileId, row);
    }
    row.total += 1;
    switch (agent.effectiveStatus) {
      case 'Available':
        row.available += 1;
        break;
      case 'On Call':
        row.onCall += 1;
        break;
      case 'ACW':
        row.acw += 1;
        break;
      case 'Offline':
        row.offline += 1;
        break;
      default:
        row.unavailable += 1;
    }
  }
  return [...byProfile.values()].sort((a, b) => a.available - b.available);
}

export function AgentAvailabilityPanel({ className }: { className?: string }): ReactNode {
  const query = useQuery({
    queryKey: ['agent-roster', 'all'],
    queryFn: () => api.brandedMonitor.getAgentRoster(),
    refetchInterval: 20_000,
  });

  const rows = aggregateByRoutingProfile(query.data?.agents ?? []);

  return (
    <div className={className}>
      <h3 className="text-sm font-semibold text-gray-700 mb-2">Agent availability</h3>
      {query.isPending ? (
        <p className="text-xs text-gray-400">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="text-xs text-gray-400">No agents online.</p>
      ) : (
        <div className="space-y-2">
          {rows.map((row) => (
            <div key={row.routingProfileId} className="rounded-lg border border-gray-200 p-2.5">
              <div className="text-xs font-medium text-gray-700 truncate mb-1.5">{row.routingProfileName}</div>
              <div className="grid grid-cols-4 gap-1.5">
                <StatTile
                  label="Avail"
                  value={row.available}
                  valueClassName={row.available === 0 ? 'text-red-600' : undefined}
                />
                <StatTile label="Call" value={row.onCall} />
                <StatTile label="ACW" value={row.acw} />
                <StatTile label="Off" value={row.offline + row.unavailable} />
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd frontend && npx vitest run src/pages/AgentAvailabilityPanel.test.ts`
Expected: PASS (4 tests).

- [ ] **Step 5: Typecheck**

Run: `cd frontend && npm run typecheck`
Expected: no new errors.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/AgentAvailabilityPanel.tsx frontend/src/pages/AgentAvailabilityPanel.test.ts
git commit -m "feat(frontend): add AgentAvailabilityPanel (compact, by routing profile)"
```

---

### Task 4: `DayActivityFeed` — reverse-chron event panel

**Files:**
- Create: `frontend/src/pages/DayActivityFeed.tsx`
- Test: `frontend/src/pages/DayActivityFeed.test.ts`
- Modify: `frontend/src/lib/utils.ts` (add `fmtTime`, Step 0), `frontend/src/pages/PlanDetail.tsx` (remove local `fmtTime`/`COL_OFFSET_MS`, import from `@/lib/utils` instead, Step 0)

**Interfaces:**
- Consumes: `api.audit.entityHistory(entityId)` (existing client, `frontend/src/lib/api.ts:916-919`, hits `GET /audit/{entityId}` which already sorts newest-first with `Limit=100`); `AuditEntry`, `AuditEventExtra` types (Task 1); `ActivityFeed`, `ActivityFeedItem` from `@/components/ui`; `actionTone` — **import it directly from `./Audit`** (already exported there per the Day Activity Feed plan's Task 6, do not redefine a second copy of the same tone map); `fmtTime` from `@/lib/utils` (moved there by this task — see Step 0 below).
- Produces: `formatActivityEntry(entry: AuditEntry): string` — pure, exported, tested, maps each of the 5 known `action` values to a human-readable sentence using `entry.extra`'s fields; falls back to `entry.action` verbatim for any unrecognized action (forward-compatible, doesn't crash on a 6th future event type). `DayActivityFeed` component with props `{ planId: string; runId: string; className?: string }`.

**Read this first:** `services/api-metrics/src/handlers/audit.py:78-93` if you need to re-confirm the exact response shape beyond what's already typed in `frontend/src/lib/api.ts:916-919` (`{ entityId: string; entries: AuditEntry[] }`) — the entityId path segment must be `plan_run/{planId}/{runId}` (URL-encoded by `request()` internally via `encodeURIComponent` on the whole path segment — check `api.audit.entityHistory`'s implementation to confirm whether you pass the raw `plan_run/${planId}/${runId}` string or something already-encoded, and match that convention exactly).

**Do not import from `PlanDetail.tsx` in this task or any other new file in this plan.** Task 6 makes `PlanDetail.tsx` import `DayActivityFeed.tsx` — if `DayActivityFeed.tsx` also imported something from `PlanDetail.tsx`, that would be a circular module dependency between the two files. `fmtTime`/`COL_OFFSET_MS` are currently module-scoped inside `PlanDetail.tsx` (lines 26-34) — this task moves them to `frontend/src/lib/utils.ts` instead (which nothing in this plan imports from, so no cycle), and updates `PlanDetail.tsx` to import `fmtTime` from there instead of defining it locally.

- [ ] **Step 0: Move `fmtTime`/`COL_OFFSET_MS` to `frontend/src/lib/utils.ts`**

In `frontend/src/lib/utils.ts`, add (matching the file's existing style — plain exported functions, no class):

```ts
const COL_OFFSET_MS = -5 * 60 * 60 * 1000;

/** Formats a UTC timestamp as "HH:MM" in Colombia time (UTC-5, no DST). */
export function fmtTime(d: Date | string | null | undefined): string {
  if (!d) return '—';
  const dt = typeof d === 'string' ? new Date(d) : d;
  if (isNaN(dt.getTime())) return '—';
  const col = new Date(dt.getTime() + COL_OFFSET_MS);
  return `${col.getUTCHours().toString().padStart(2, '0')}:${col.getUTCMinutes().toString().padStart(2, '0')}`;
}
```

In `frontend/src/pages/PlanDetail.tsx`, delete the local `COL_OFFSET_MS` constant and `fmtTime` function (currently lines 26-34), and add `fmtTime` to the existing `@/lib/utils` import if one exists, or add a new import line: `import { fmtTime } from '@/lib/utils';`. Run `cd frontend && npx vitest run && npm run typecheck` right after this step, before continuing — this is a pure relocation with zero behavior change, and any failure here means a call site was missed, not that something new is broken.

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/pages/DayActivityFeed.test.ts`:

```ts
import { describe, expect, it } from 'vitest';

import type { AuditEntry } from '@/lib/api';

import { formatActivityEntry } from './DayActivityFeed';

function entry(action: string, extra: unknown): AuditEntry {
  return { entityId: 'plan_run/p1/r1', action, timestamp: '2026-08-31T19:40:00.000Z', extra };
}

describe('formatActivityEntry', () => {
  it('formats bucket_started', () => {
    expect(formatActivityEntry(entry('bucket_started', { bucketIndex: 2, bucketName: 'NJ/CT' })))
      .toBe('Bucket "NJ/CT" started');
  });

  it('formats bucket_started with no bucketName', () => {
    expect(formatActivityEntry(entry('bucket_started', { bucketIndex: 2, bucketName: null })))
      .toBe('Bucket 3 started');
  });

  it('formats bucket_completed', () => {
    expect(formatActivityEntry(entry('bucket_completed', { bucketIndex: 1, bucketName: 'NJ/CT', reason: 'all_campaigns_done' })))
      .toBe('Bucket "NJ/CT" completed — all_campaigns_done');
  });

  it('formats window_closed', () => {
    expect(formatActivityEntry(entry('window_closed', { reason: 'working_hours_cutoff' })))
      .toBe('Operating window closed — working_hours_cutoff');
  });

  it('formats reconcile_retry', () => {
    expect(formatActivityEntry(entry('reconcile_retry', { bucketIndex: 0, campaignIndex: 2, retry: 1, retryLimit: 5 })))
      .toBe('Bucket 1 / campaign 3 — reconcile retry 1 of 5');
  });

  it('formats creation_failed', () => {
    expect(formatActivityEntry(entry('creation_failed', { bucketIndex: 0, campaignIndex: 1, error: 'ThrottlingException' })))
      .toBe('Bucket 1 / campaign 2 — creation failed: ThrottlingException');
  });

  it('falls back to the raw action for an unrecognized event type', () => {
    expect(formatActivityEntry(entry('some_future_action', { anything: true }))).toBe('some_future_action');
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd frontend && npx vitest run src/pages/DayActivityFeed.test.ts`
Expected: FAIL — `Cannot find module './DayActivityFeed'`.

- [ ] **Step 3: Write the implementation**

Create `frontend/src/pages/DayActivityFeed.tsx`:

```tsx
import type { ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';

import { ActivityFeed, type ActivityFeedItem } from '@/components/ui/ActivityFeed';
import { api, type AuditEntry } from '@/lib/api';

import { fmtTime } from '@/lib/utils';

import { actionTone } from './Audit';

export function formatActivityEntry(entry: AuditEntry): string {
  const extra = (entry.extra ?? {}) as Record<string, unknown>;
  switch (entry.action) {
    case 'bucket_started': {
      const name = extra.bucketName as string | null | undefined;
      return name ? `Bucket "${name}" started` : `Bucket ${(extra.bucketIndex as number) + 1} started`;
    }
    case 'bucket_completed': {
      const name = extra.bucketName as string | null | undefined;
      const label = name ? `Bucket "${name}"` : `Bucket ${(extra.bucketIndex as number) + 1}`;
      return `${label} completed — ${extra.reason as string}`;
    }
    case 'window_closed':
      return `Operating window closed — ${extra.reason as string}`;
    case 'reconcile_retry':
      return `Bucket ${(extra.bucketIndex as number) + 1} / campaign ${(extra.campaignIndex as number) + 1} — reconcile retry ${extra.retry as number} of ${extra.retryLimit as number}`;
    case 'creation_failed':
      return `Bucket ${(extra.bucketIndex as number) + 1} / campaign ${(extra.campaignIndex as number) + 1} — creation failed: ${extra.error as string}`;
    default:
      return entry.action;
  }
}

export function DayActivityFeed({
  planId,
  runId,
  className,
}: {
  planId: string;
  runId: string;
  className?: string;
}): ReactNode {
  const query = useQuery({
    queryKey: ['day-activity', planId, runId],
    queryFn: () => api.audit.entityHistory(`plan_run/${planId}/${runId}`),
    refetchInterval: 15_000,
  });

  const items: ActivityFeedItem[] = (query.data?.entries ?? []).map((entry) => ({
    id: `${entry.timestamp}-${entry.action}`,
    timestampLabel: fmtTime(entry.timestamp),
    text: formatActivityEntry(entry),
    tone: actionTone(entry.action),
  }));

  return (
    <div className={className}>
      <h3 className="text-sm font-semibold text-gray-700 mb-2">Day activity</h3>
      <ActivityFeed items={items} emptyLabel={query.isPending ? 'Loading…' : 'No activity yet.'} />
    </div>
  );
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd frontend && npx vitest run src/pages/DayActivityFeed.test.ts`
Expected: PASS (7 tests).

- [ ] **Step 5: Typecheck**

Run: `cd frontend && npm run typecheck`
Expected: no new errors — this step will catch it immediately if Step 0's relocation missed a `fmtTime` call site in `PlanDetail.tsx`.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/DayActivityFeed.tsx frontend/src/pages/DayActivityFeed.test.ts frontend/src/pages/PlanDetail.tsx frontend/src/lib/utils.ts
git commit -m "feat(frontend): add DayActivityFeed panel, relocate fmtTime to lib/utils to avoid a circular import"
```

---

### Task 5: `DayHeaderTimeline` — wall-clock bucket bar

**Files:**
- Create: `frontend/src/pages/DayHeaderTimeline.tsx`
- Test: `frontend/src/pages/DayHeaderTimeline.test.ts`

**Interfaces:**
- Consumes: `PlanSummaryV2`, `PlanRunV2`, `BucketDefV2` types; `computePlannedStart`, `campaignDurMin` — **read `PlanDetail.tsx` again before this task**: these are currently non-exported, module-scoped functions (lines 57-62 and 66-88) that compute a bucket's planned start; you need bucket-level start/end, not campaign-level, so check whether a bucket-level equivalent already exists inline in `BucketSection` (it does — `plannedStart` + `durMin` are computed directly inside `BucketSection`, not via a separately-named function) and either export a small new bucket-level helper from `PlanDetail.tsx` or duplicate the two-line computation here with a comment pointing at `BucketSection` as the reference — your call, but state which you chose in your report.
- Produces: `computeTimelineSegments(plan: PlanSummaryV2, run: PlanRunV2, windowStart: Date, windowEnd: Date): Array<{ bucketIndex: number; startPct: number; endPct: number; status: BucketStateV2['status'] }>` — pure, exported, tested, clamps segments to `[0, 100]` when a bucket's real start/end falls outside the window (don't let a bucket that started before `windowStart` or is still projected to end after `windowEnd` produce an out-of-bounds `startPct`/`endPct`). `DayHeaderTimeline` component with props `{ plan: PlanSummaryV2; run: PlanRunV2 }`.

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/pages/DayHeaderTimeline.test.ts`. Read `BucketSection`'s current `plannedStart`/`durMin` computation in `PlanDetail.tsx` first so these fixtures produce numbers you can hand-verify — the exact expected `startPct`/`endPct` values below assume an 8am-8pm (12-hour, 720-minute) window and a bucket that runs from minute 60 to minute 180 of that window (i.e. 9am-11am):

```ts
import { describe, expect, it } from 'vitest';

import type { PlanRunV2, PlanSummaryV2 } from '@/lib/api';

import { computeTimelineSegments } from './DayHeaderTimeline';

const windowStart = new Date('2026-08-31T13:00:00.000Z'); // 8:00 AM COT (UTC-5)
const windowEnd = new Date('2026-09-01T01:00:00.000Z');   // 8:00 PM COT

function plan(buckets: PlanSummaryV2['buckets']): PlanSummaryV2 {
  return {
    planId: 'p1', name: 'Test', trigger: { type: 'manual' }, isTemplate: false, is_template: false,
    isDefault: false, buckets, createdAt: windowStart.toISOString(),
  };
}

describe('computeTimelineSegments', () => {
  it('positions a single bucket at its actual start/end within the window', () => {
    const p = plan([{ id: 'b1', name: 'B1', run_mode: 'time_based', duration_minutes: 120, cleanup: true, prestart_next: false, campaignConfig: {} as never, campaigns: [] }]);
    const run: PlanRunV2 = {
      planId: 'p1', runId: 'r1', status: 'running', currentBucketIndex: 0,
      startedAt: windowStart.toISOString(),
      bucketStates: [{
        bucketId: 'b1', name: 'B1', status: 'completed', campaignStates: [],
        startedAt: new Date(windowStart.getTime() + 60 * 60_000).toISOString(),
        completedAt: new Date(windowStart.getTime() + 180 * 60_000).toISOString(),
      }],
    };
    const [seg] = computeTimelineSegments(p, run, windowStart, windowEnd);
    expect(seg.startPct).toBeCloseTo((60 / 720) * 100, 1);
    expect(seg.endPct).toBeCloseTo((180 / 720) * 100, 1);
    expect(seg.status).toBe('completed');
  });

  it('clamps a segment that starts before the window to 0%', () => {
    const p = plan([{ id: 'b1', name: 'B1', run_mode: 'time_based', duration_minutes: 60, cleanup: true, prestart_next: false, campaignConfig: {} as never, campaigns: [] }]);
    const run: PlanRunV2 = {
      planId: 'p1', runId: 'r1', status: 'running', currentBucketIndex: 0,
      startedAt: windowStart.toISOString(),
      bucketStates: [{
        bucketId: 'b1', name: 'B1', status: 'running', campaignStates: [],
        startedAt: new Date(windowStart.getTime() - 30 * 60_000).toISOString(),
      }],
    };
    const [seg] = computeTimelineSegments(p, run, windowStart, windowEnd);
    expect(seg.startPct).toBe(0);
  });

  it('clamps a still-running bucket projected past the window to 100%', () => {
    const p = plan([{ id: 'b1', name: 'B1', run_mode: 'time_based', duration_minutes: 10_000, cleanup: true, prestart_next: false, campaignConfig: {} as never, campaigns: [] }]);
    const run: PlanRunV2 = {
      planId: 'p1', runId: 'r1', status: 'running', currentBucketIndex: 0,
      startedAt: windowStart.toISOString(),
      bucketStates: [{ bucketId: 'b1', name: 'B1', status: 'running', campaignStates: [], startedAt: windowStart.toISOString() }],
    };
    const [seg] = computeTimelineSegments(p, run, windowStart, windowEnd);
    expect(seg.endPct).toBe(100);
  });

  it('returns one segment per bucket, in plan order', () => {
    const p = plan([
      { id: 'b1', name: 'B1', run_mode: 'status_based', cleanup: true, prestart_next: false, campaignConfig: {} as never, campaigns: [] },
      { id: 'b2', name: 'B2', run_mode: 'status_based', cleanup: true, prestart_next: false, campaignConfig: {} as never, campaigns: [] },
    ]);
    const run: PlanRunV2 = {
      planId: 'p1', runId: 'r1', status: 'running', currentBucketIndex: 1,
      startedAt: windowStart.toISOString(),
      bucketStates: [
        { bucketId: 'b1', name: 'B1', status: 'completed', campaignStates: [], startedAt: windowStart.toISOString(), completedAt: windowStart.toISOString() },
        { bucketId: 'b2', name: 'B2', status: 'running', campaignStates: [], startedAt: windowStart.toISOString() },
      ],
    };
    const segments = computeTimelineSegments(p, run, windowStart, windowEnd);
    expect(segments).toHaveLength(2);
    expect(segments[0].bucketIndex).toBe(0);
    expect(segments[1].bucketIndex).toBe(1);
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd frontend && npx vitest run src/pages/DayHeaderTimeline.test.ts`
Expected: FAIL — `Cannot find module './DayHeaderTimeline'`.

- [ ] **Step 3: Write the implementation**

Create `frontend/src/pages/DayHeaderTimeline.tsx`. Use whichever bucket-start-time source you decided on in this task's Interfaces note (either an exported helper from `PlanDetail.tsx`, or the two-line duplication — shown here as the duplication option; adjust the import if you export a helper instead):

```tsx
import type { ReactNode } from 'react';

import type { BucketStateV2, PlanRunV2, PlanSummaryV2 } from '@/lib/api';

export type TimelineSegment = {
  bucketIndex: number;
  startPct: number;
  endPct: number;
  status: BucketStateV2['status'];
};

function clampPct(minutesFromWindowStart: number, windowMinutes: number): number {
  return Math.min(100, Math.max(0, (minutesFromWindowStart / windowMinutes) * 100));
}

export function computeTimelineSegments(
  plan: PlanSummaryV2,
  run: PlanRunV2,
  windowStart: Date,
  windowEnd: Date,
): TimelineSegment[] {
  const windowMinutes = (windowEnd.getTime() - windowStart.getTime()) / 60_000;
  const now = Date.now();

  return run.bucketStates.map((bs, bucketIndex) => {
    const startedAt = bs.startedAt ? new Date(bs.startedAt).getTime() : windowStart.getTime();
    const endedAt = bs.completedAt
      ? new Date(bs.completedAt).getTime()
      : bs.status === 'running' || bs.status === 'warming'
        ? now
        : startedAt;

    const startMin = (startedAt - windowStart.getTime()) / 60_000;
    const endMin = (endedAt - windowStart.getTime()) / 60_000;

    return {
      bucketIndex,
      startPct: clampPct(startMin, windowMinutes),
      endPct: clampPct(endMin, windowMinutes),
      status: bs.status,
    };
  });
}

const SEGMENT_COLOR: Record<BucketStateV2['status'], string> = {
  queued: 'bg-gray-200',
  warming: 'bg-amber-300',
  running: 'bg-violet-500',
  completed: 'bg-green-500',
};

export function DayHeaderTimeline({ plan, run }: { plan: PlanSummaryV2; run: PlanRunV2 }): ReactNode {
  const now = new Date();
  const windowStart = new Date(now);
  windowStart.setHours(8, 0, 0, 0);
  const windowEnd = new Date(now);
  windowEnd.setHours(20, 0, 0, 0);

  const segments = computeTimelineSegments(plan, run, windowStart, windowEnd);
  const nowPct = Math.min(100, Math.max(0, ((now.getTime() - windowStart.getTime()) / (windowEnd.getTime() - windowStart.getTime())) * 100));

  return (
    <div className="relative h-6 w-full rounded-full bg-gray-100 overflow-hidden">
      {segments.map((seg) => (
        <div
          key={seg.bucketIndex}
          className={`absolute top-0 h-full ${SEGMENT_COLOR[seg.status]}`}
          style={{ left: `${seg.startPct}%`, width: `${Math.max(0.5, seg.endPct - seg.startPct)}%` }}
        />
      ))}
      <div className="absolute top-0 h-full w-0.5 bg-gray-900" style={{ left: `${nowPct}%` }} />
    </div>
  );
}
```

The 8am-8pm default window is a deliberate fallback since `PlanSummaryV2.workingHours` is optional and its `startTime`/`endTime` are `HH:MM` strings in COT, not `Date` objects — if `plan.workingHours` is present, use it to set `windowStart`/`windowEnd` instead of the hardcoded 8/20 (parse the `HH:MM` string, apply to `now`'s date, same pattern as the hardcoded version); if absent, the 8am-8pm fallback matches the reference design's own default and this app's typical operating hours.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd frontend && npx vitest run src/pages/DayHeaderTimeline.test.ts`
Expected: PASS (4 tests).

- [ ] **Step 5: Typecheck**

Run: `cd frontend && npm run typecheck`
Expected: no new errors.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/DayHeaderTimeline.tsx frontend/src/pages/DayHeaderTimeline.test.ts
git commit -m "feat(frontend): add DayHeaderTimeline wall-clock bucket bar"
```

---

### Task 6: Mount everything into `PlanDetail.tsx`

**Files:**
- Modify: `frontend/src/pages/PlanDetail.tsx`

**Interfaces:**
- Consumes: `DayHeaderTimeline` (Task 5), `AgentAvailabilityPanel` (Task 3), `DayActivityFeed` (Task 4).

**Read this first:** the "Main content" section (currently starting around line 989, `{/* ── Main content ─────... */} <div className="px-8 py-6 space-y-8">`) and the "Plan name + meta" block right above it (currently lines 951-973) — this task restructures the main-content div into a responsive 2-column grid and adds the timeline right after the plan meta block. Check `BrandedMonitor.tsx` or `PlanNew.tsx` for this app's existing responsive grid breakpoint convention (e.g. `lg:grid-cols-[...]`) before picking one — don't invent a new breakpoint scheme.

- [ ] **Step 1: Add the imports**

In `frontend/src/pages/PlanDetail.tsx`, add after the existing `buildChainMap`/local imports:

```tsx
import { AgentAvailabilityPanel } from './AgentAvailabilityPanel';
import { DayActivityFeed } from './DayActivityFeed';
import { DayHeaderTimeline } from './DayHeaderTimeline';
```

- [ ] **Step 2: Add the timeline after the plan meta block**

Immediately after the closing `</div>` of the "Plan name + meta" block (currently line 972, right before the outer header `</div>` at line 973), insert — only when there's an active or displayed run (the timeline needs a `run` to plot; render nothing without one):

```tsx
        {displayRun && (
          <div className="mt-3">
            <DayHeaderTimeline plan={plan} run={displayRun} />
          </div>
        )}
```

Confirm `displayRun` is already in scope at this point in the component (it's referenced later at line 992 — verify it's defined before line 972's block, not after; if it's defined later in the component via a `const` that hasn't been declared yet at this point in the file, move this insertion to after that `const displayRun = ...` line instead of forcing an out-of-order reference — read the component's full variable declaration order before finalizing where this insertion actually lands).

- [ ] **Step 3: Restructure "Main content" into a 2-column grid**

Change the main-content wrapper (currently `<div className="px-8 py-6 space-y-8">` at line 989) so the bucket-sections content and the new side panels sit in a responsive grid. The exact bucket-sections JSX (the `displayRun ? (...) : (...)` block currently starting at line 992) stays wrapped in the left column; the two new panels go in the right column, only rendered when there's a `displayRun` (they need `planId`/`runId`, and the agent panel is only meaningful during an active/recent run):

```tsx
      <div className="px-8 py-6">
        <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_320px]">
          <div className="space-y-6">
            {/* ...unchanged: the existing displayRun ? (...) : (...) block goes here... */}
          </div>
          {displayRun && (
            <div className="space-y-6">
              <AgentAvailabilityPanel />
              <DayActivityFeed planId={id!} runId={displayRun.runId} />
            </div>
          )}
        </div>
      </div>
```

Do not touch anything inside the existing `displayRun ? (...) : (...)` block itself — this step only changes the wrapping structure around it. If `id` is possibly `undefined` at this point per its type (`useParams<{ id: string }>()` — check whether this hook's return type already makes `id` definitely a string or `string | undefined`), use the same non-null-assertion or guard style already used elsewhere in this component for `id!`, don't introduce a new pattern.

- [ ] **Step 4: Verify visually**

Run the dev server (`cd frontend && npm run dev`) and navigate to a plan detail page for a currently-running plan (or any plan with a `latestRun`). Confirm: the timeline renders below the plan name without throwing, the right column shows the agent panel and activity feed side-by-side with the bucket list on wide viewports, and everything collapses to a single column below the `lg` breakpoint. Take this as a manual verification step — no automated test is expected for this layout-only change.

- [ ] **Step 5: Typecheck and build**

Run: `cd frontend && npm run typecheck && npm run build`
Expected: both succeed with no new errors.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/PlanDetail.tsx
git commit -m "feat(frontend): mount DayHeaderTimeline, AgentAvailabilityPanel, DayActivityFeed into PlanDetail"
```

---

### Task 7: Full verification

**Files:** none (verification only)

- [ ] **Step 1: Full frontend suite**

Run: `cd frontend && npx vitest run`
Expected: all tests pass except the 3 known pre-existing, unrelated `chainMap.test.ts` failures (confirmed present on `main` since before this session's work began — not this plan's to fix).

- [ ] **Step 2: Typecheck and build**

Run: `cd frontend && npm run typecheck && npm run build`
Expected: both clean.

- [ ] **Step 3: Manual smoke test**

Run: `cd frontend && npm run dev`, log in, navigate to `/plans/monitor`, click into a running plan's detail page. Confirm: reconcile badges appear on campaign cards that have `cs.reconcile` set (check a campaign known to have reconciled — cross-reference against the DynamoDB verification done earlier this session if needed), the day activity feed shows real events (not empty, given production already has `bucket_started`/`bucket_completed` events flowing), the agent panel shows real routing-profile counts, and the timeline bar renders without visual glitches (bars within bounds, "now" marker visible).

---

## Self-Review Notes

- **Spec coverage:** all 4 additive pieces (reconcile badge, day activity feed, agent panel, timeline) have a task with real, complete code and real tests for every pure-logic function. The layout restructuring (Task 6) is the one piece with no automated test, matching this repo's established convention that layout-only JSX changes aren't unit-tested — verified manually instead.
- **Placeholder scan:** none — every task has complete, runnable code. Task 5 explicitly asks the implementer to choose between two concrete options (export a helper vs. duplicate 2 lines) and state the choice, rather than leaving it unresolved — this is a real, bounded decision point, not a placeholder.
- **Type consistency:** `CampaignState.reconcile`'s shape (Task 1) is read identically by `reconcileTone`/`formatReconcile` (Task 1) and consumed identically by `CampaignCard` (Task 2). `AuditEntry`/`AuditEventExtra` (Task 1) is read identically by `formatActivityEntry` (Task 4). `AgentRosterEntry.effectiveStatus`'s 5 literal values are exhaustively switched in `aggregateByRoutingProfile` (Task 3) with a `default` case, so a future 6th status value degrades to "unavailable" rather than crashing.
