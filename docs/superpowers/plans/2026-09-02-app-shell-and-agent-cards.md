# App Shell Redesign + Agent Availability Compact Cards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the app's top-nav header with a persistent, collapsible sidebar + top bar shell (per reference mockups), and redesign both existing "Agent availability" widgets into compact, routing-profile-grouped cards reusing the existing staffing-risk logic.

**Architecture:** Two independent halves that only share two small pieces of infrastructure (a `usePersistedState` hook and the new `allRoutingProfiles` API field). Half A (App Shell) touches `Layout.tsx`/`PlansLayout.tsx` and adds `Sidebar.tsx`/`TopBar.tsx`/`navConfig.ts`. Half B (Agent Cards) touches `BrandedMonitor.tsx`/`AgentAvailabilityPanel.tsx` and adds `AgentAvailabilityCard.tsx`, plus a backend field on the existing `/metrics/branded/agents` endpoint.

**Tech Stack:** React + TypeScript + Vite (frontend), Python 3.12 Lambda (backend, `services/api-metrics`), boto3 Connect client, TanStack Query, Tailwind CSS.

**Spec:** `docs/superpowers/specs/2026-09-02-app-shell-and-agent-cards-design.md`

## Global Constraints

- **`@/components/ui` collision** (real, repo-wide): `frontend/src/components/ui.tsx` (file) and `frontend/src/components/ui/` (directory) both exist; the bare `@/components/ui` path always resolves to the file. Any primitive from the directory (`StatusChip`, `StatTile`, etc.) must be imported from its specific file, never the bare path.
- **COT-only timezone.** Any new time display must use `fmtTime` from `@/lib/utils`, never `toLocaleTimeString`/`Date.prototype.getHours`/browser-local formatting. The new TopBar clock is COT (matching the rest of the app), not the reference mockup's literal Eastern-Time styling — confirmed with Sebastian.
- **No icon library, no obscure Unicode glyphs.** Plain, common-block Unicode (▶ ● ↺ ↑ ↓ ·) or raw inline SVG (`viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5"`, matching `PlansLayout.tsx`'s existing icons) only.
- **`BRANDED_MONITOR_TEAMS`/`teamForProfile`/`PANEL_TEAM`** (in `frontend/src/lib/routingProfileTeams.ts` and `AgentAvailabilityPanel.tsx`) are the existing, correct team-scoping mechanisms — reused as-is, never redefined or duplicated with ad-hoc string matching.
- **Presentational JSX is not unit-tested** by this repo's convention — only pure, exported functions get tests. Two tasks in this plan (Task 1, Task 3) add pure functions specifically so their logic has real test coverage; the surrounding JSX does not need its own test.
- **Branding stays "VIP Connect Admin".** Only the reference mockup's visual layout (icon + wordmark) is adopted — the text itself does not change to the mockup's "Medwork / Orchestrator".
- **No new IAM permissions.** The backend change (Task 1) reads data the Lambda already fetches via an existing `list_routing_profiles` call (`_routing_profile_ids()` in `branded.py`) — it does not add a new AWS API call or require a CDK/IAM change.
- **`services/api-metrics/src/handlers/branded.py`, `frontend/src/lib/agentRoster.ts`, `frontend/src/pages/BrandedMonitor.tsx`, `frontend/src/pages/AgentAvailabilityPanel.tsx`, `frontend/src/pages/PlansLayout.tsx`, `frontend/src/components/Layout.tsx`** are all existing, already-shipped files — every task below states an exact anchor (a line of existing code to find) rather than a line number, since line numbers shift as earlier tasks land.

---

### Task 1: Backend — expose the full routing-profile catalog on `/metrics/branded/agents`

**Files:**
- Modify: `services/api-metrics/src/handlers/branded.py`
- Modify: `services/api-metrics/tests/unit/test_branded_roster.py`
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/src/lib/mockApi.ts`

**Interfaces:**
- Produces: `allRoutingProfiles: { id: string; name: string }[]` on the `GET /metrics/branded/agents` response — the FULL routing-profile catalog for the Connect instance (not just profiles that currently have an agent logged in), populated only when the request has no `queueId` (both of this plan's frontend consumers call it without one). Empty array when a `queueId` is given, or when the instance has zero routing profiles.
- Consumes: nothing new — reuses the existing `_routing_profile_ids()` helper and its `_rp_name_cache` side effect, both already present in `branded.py`.

- [ ] **Step 1: Add a failing test proving `allRoutingProfiles` includes profiles with zero agents**

Add to `services/api-metrics/tests/unit/test_branded_roster.py`, in a new test class after `TestRoutingProfileBatching`:

```python
class TestAllRoutingProfilesCatalog:
    """allRoutingProfiles must be the full Connect catalog (from list_routing_profiles),
    not the routingProfiles field's agent-derived subset — a routing profile with zero
    agents currently logged in must still appear here, or the frontend has no way to
    know it exists at all.
    """

    def test_includes_profiles_with_zero_agents(self):
        from handlers import branded

        # 3 profiles registered in Connect; only rp-1 has an agent today.
        profiles = [
            {"Id": "rp-1", "Name": "Staffed Profile"},
            {"Id": "rp-2", "Name": "Empty Profile A"},
            {"Id": "rp-3", "Name": "Empty Profile B"},
        ]
        mock = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [{"RoutingProfileSummaryList": profiles}]
        mock.get_paginator.return_value = paginator
        mock.list_agent_statuses.return_value = {
            "AgentStatusSummaryList": [{"Id": "s-avail", "Name": "Available", "Type": "ROUTABLE"}],
        }
        mock.describe_user.return_value = {
            "User": {"IdentityInfo": {"FirstName": "A", "LastName": "B"}, "Username": "ab"},
        }
        mock.get_current_user_data.return_value = {
            "UserDataList": [_user_data(user_id="u-1", rp_id="rp-1", status_arn="arn:.../agent-status/s-avail")],
        }

        with patch("handlers.branded._connect", mock):
            resp = branded.get_agent_roster({"queryStringParameters": {}}, {})

        body = json.loads(resp["body"])
        # routingProfiles (agent-derived) only has rp-1 — the pre-existing behavior.
        assert {p["id"] for p in body["routingProfiles"]} == {"rp-1"}
        # allRoutingProfiles has all 3, including the two with zero agents today.
        assert {p["id"] for p in body["allRoutingProfiles"]} == {"rp-1", "rp-2", "rp-3"}
        assert {p["name"] for p in body["allRoutingProfiles"]} == {
            "Staffed Profile", "Empty Profile A", "Empty Profile B",
        }

    def test_queue_scoped_request_returns_empty_all_routing_profiles(self):
        """A queueId-scoped request never calls list_routing_profiles (the existing
        code path skips it entirely), so allRoutingProfiles must be [] rather than
        stale/partial — never silently reuse a previous request's cache contents."""
        from handlers import branded

        ud = [_user_data(status_arn="arn:.../agent-status/s-avail")]
        statuses = [{"Id": "s-avail", "Name": "Available", "Type": "ROUTABLE"}]

        with patch("handlers.branded._connect", _mock_connect(ud, statuses)):
            resp = branded.get_agent_roster({"queryStringParameters": {"queueId": "q-1"}}, {})

        body = json.loads(resp["body"])
        assert body["allRoutingProfiles"] == []
```

Update the existing empty-roster test in `TestEmptyRosterResponseShape` (it currently asserts only 3 of what will become 4 relevant keys):

Find:
```python
class TestEmptyRosterResponseShape:
    """The empty-roster early-return must carry the same 4 keys the TS type
    declares non-optional (routingProfiles, lastUpdated) — a prior version
    omitted them, which type-disagreed with the frontend contract."""

    def test_empty_roster_still_returns_routing_profiles_and_last_updated(self):
        from handlers import branded

        mock = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [{"RoutingProfileSummaryList": []}]
        mock.get_paginator.return_value = paginator

        with patch("handlers.branded._connect", mock):
            resp = branded.get_agent_roster({"queryStringParameters": {}}, {})

        body = json.loads(resp["body"])
        assert body["agents"] == []
        assert body["routingProfiles"] == []
        assert "lastUpdated" in body and body["lastUpdated"]
```

Change to:
```python
class TestEmptyRosterResponseShape:
    """The empty-roster early-return must carry the same 5 keys the TS type
    declares non-optional (routingProfiles, allRoutingProfiles, lastUpdated) —
    a prior version omitted some, which type-disagreed with the frontend contract."""

    def test_empty_roster_still_returns_routing_profiles_and_last_updated(self):
        from handlers import branded

        mock = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [{"RoutingProfileSummaryList": []}]
        mock.get_paginator.return_value = paginator

        with patch("handlers.branded._connect", mock):
            resp = branded.get_agent_roster({"queryStringParameters": {}}, {})

        body = json.loads(resp["body"])
        assert body["agents"] == []
        assert body["routingProfiles"] == []
        assert body["allRoutingProfiles"] == []
        assert "lastUpdated" in body and body["lastUpdated"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd services/api-metrics && python -m pytest tests/unit/test_branded_roster.py -v`
Expected: `TestAllRoutingProfilesCatalog::test_includes_profiles_with_zero_agents` and `test_queue_scoped_request_returns_empty_all_routing_profiles` FAIL with `KeyError: 'allRoutingProfiles'`. The updated `TestEmptyRosterResponseShape` test also FAILS on the new assertion.

- [ ] **Step 3: Implement the minimal change in `branded.py`**

Find, in `get_agent_roster`:
```python
    qs = event.get("queryStringParameters") or {}
    queue_id = qs.get("queueId", "")

    # GetCurrentUserData requires at least one non-empty filter field.
    # When a specific queue is requested, scope to that queue.
    # Otherwise, scope to all routing profiles (covers every agent in the instance).
    if queue_id:
        filter_batches: list[dict] = [{"Queues": [queue_id]}]
    else:
        profile_ids = _routing_profile_ids()
        if not profile_ids:
            return _ok({
                "agents": [], "queueId": queue_id, "routingProfiles": [],
                "lastUpdated": datetime.now(timezone.utc).isoformat(),
            })
        filter_batches = [{"RoutingProfiles": batch} for batch in _chunk(profile_ids, 100)]
```

Change to:
```python
    qs = event.get("queryStringParameters") or {}
    queue_id = qs.get("queueId", "")

    # GetCurrentUserData requires at least one non-empty filter field.
    # When a specific queue is requested, scope to that queue.
    # Otherwise, scope to all routing profiles (covers every agent in the instance).
    #
    # all_routing_profiles is the FULL Connect catalog (from list_routing_profiles,
    # via _routing_profile_ids()) — unlike routing_profiles below, which is derived
    # only from agents actually present in this response. A queue-scoped request
    # never calls _routing_profile_ids(), so it stays [] rather than reusing a
    # possibly-stale cache from a prior unscoped call.
    all_routing_profiles: list[dict] = []
    if queue_id:
        filter_batches: list[dict] = [{"Queues": [queue_id]}]
    else:
        profile_ids = _routing_profile_ids()
        all_routing_profiles = sorted(
            [{"id": pid, "name": _rp_name_cache.get(pid, pid)} for pid in profile_ids],
            key=lambda x: x["name"],
        )
        if not profile_ids:
            return _ok({
                "agents": [], "queueId": queue_id, "routingProfiles": [],
                "allRoutingProfiles": [],
                "lastUpdated": datetime.now(timezone.utc).isoformat(),
            })
        filter_batches = [{"RoutingProfiles": batch} for batch in _chunk(profile_ids, 100)]
```

Find, at the end of `get_agent_roster`:
```python
    return _ok({
        "agents": agents,
        "queueId": queue_id,
        "routingProfiles": routing_profiles,
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
    })
```

Change to:
```python
    return _ok({
        "agents": agents,
        "queueId": queue_id,
        "routingProfiles": routing_profiles,
        "allRoutingProfiles": all_routing_profiles,
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
    })
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd services/api-metrics && python -m pytest tests/unit/test_branded_roster.py -v`
Expected: all tests PASS, including the 2 new ones and the updated empty-roster test.

- [ ] **Step 5: Run the full backend test suite to confirm no regressions**

Run: `cd services/api-metrics && python -m pytest tests/unit/ -v`
Expected: same pass count as before this change, plus the 2 new tests — no other test's assertions reference `allRoutingProfiles`, so nothing else should change.

- [ ] **Step 6: Update the frontend type contract**

Find, in `frontend/src/lib/api.ts`:
```typescript
    getAgentRoster: (queueId?: string) =>
      request<{ agents: AgentRosterEntry[]; queueId: string; lastUpdated: string; routingProfiles: RoutingProfileSummary[] }>(
        `/metrics/branded/agents${queueId ? `?queueId=${encodeURIComponent(queueId)}` : ''}`,
      ),
```

Change to:
```typescript
    getAgentRoster: (queueId?: string) =>
      request<{
        agents: AgentRosterEntry[];
        queueId: string;
        lastUpdated: string;
        routingProfiles: RoutingProfileSummary[];
        allRoutingProfiles: RoutingProfileSummary[];
      }>(
        `/metrics/branded/agents${queueId ? `?queueId=${encodeURIComponent(queueId)}` : ''}`,
      ),
```

- [ ] **Step 7: Update the preview-mode mock stub**

Find, in `frontend/src/lib/mockApi.ts`:
```typescript
    getAgentRoster: async () => ({ agents: [], queueId: '', lastUpdated: new Date().toISOString(), routingProfiles: [] }),
```

Change to:
```typescript
    getAgentRoster: async () => ({
      agents: [], queueId: '', lastUpdated: new Date().toISOString(), routingProfiles: [], allRoutingProfiles: [],
    }),
```

- [ ] **Step 8: Run frontend typecheck**

Run: `cd frontend && npm run typecheck`
Expected: clean — no consumer of `getAgentRoster()` destructures `allRoutingProfiles` yet (that starts in Task 8/9), so adding an extra field to the type cannot break anything that already compiles.

- [ ] **Step 9: Commit**

```bash
git add services/api-metrics/src/handlers/branded.py services/api-metrics/tests/unit/test_branded_roster.py frontend/src/lib/api.ts frontend/src/lib/mockApi.ts
git commit -m "feat(api-metrics): expose full routing-profile catalog on agent-roster endpoint"
```

---

### Task 2: `usePersistedState` — a tiny localStorage-backed state hook

**Files:**
- Create: `frontend/src/hooks/usePersistedState.ts`

**Interfaces:**
- Produces: `usePersistedState<T>(key: string, defaultValue: T): [T, (value: T) => void]` — a `useState`-shaped hook whose value survives page reloads via `localStorage`, used by Task 4 (`Sidebar.tsx`) and Task 7 (`PlansLayout.tsx`) to persist sidebar collapse state.
- Consumes: nothing.

- [ ] **Step 1: Write the hook**

```typescript
import { useState } from 'react';

/**
 * A useState that also persists to localStorage under `key`. Reads/writes are
 * wrapped in try/catch — private browsing or a full storage quota must never
 * break the app, just silently fall back to in-memory-only behavior for that
 * session.
 */
export function usePersistedState<T>(key: string, defaultValue: T): [T, (value: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = window.localStorage.getItem(key);
      return raw !== null ? (JSON.parse(raw) as T) : defaultValue;
    } catch {
      return defaultValue;
    }
  });

  const setPersisted = (next: T): void => {
    setValue(next);
    try {
      window.localStorage.setItem(key, JSON.stringify(next));
    } catch {
      // Storage unavailable — in-memory state for this session still works.
    }
  };

  return [value, setPersisted];
}
```

This is a thin wrapper around browser APIs with no repo-testable pure logic of its own (matches the existing `useAuth`/`useIdleTimeout` hooks, neither of which has a dedicated test file) — no test step for this task.

- [ ] **Step 2: Run typecheck**

Run: `cd frontend && npm run typecheck`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/hooks/usePersistedState.ts
git commit -m "feat(frontend): add usePersistedState hook for sidebar collapse persistence"
```

---

### Task 3: `navConfig.ts` — shared sidebar nav data + a tested `alertsCount`/breadcrumb helper

**Files:**
- Create: `frontend/src/lib/navConfig.ts`
- Create: `frontend/src/lib/navConfig.test.ts`

**Interfaces:**
- Produces:
  - `NAV_GROUPS: NavGroup[]` — the sidebar's nav structure (route + label only, no icons — icons are JSX and live in `Sidebar.tsx`, Task 4), consumed by Task 4 (`Sidebar.tsx`).
  - `breadcrumbLabelForPath(pathname: string): string` — given the current route, returns the active top-level nav item's label (or `'Monitor'` as a fallback for unmatched paths, since `/dashboard` is the app's default landing route). Consumed by Task 5 (`TopBar.tsx`).
  - `type NavItem = { to: string; label: string }`
  - `type NavGroup = { label: string; items: NavItem[] }`
- Consumes: nothing.

- [ ] **Step 1: Write the failing test for `breadcrumbLabelForPath`**

```typescript
import { describe, expect, it } from 'vitest';
import { breadcrumbLabelForPath } from './navConfig';

describe('breadcrumbLabelForPath', () => {
  it('matches an exact top-level nav route', () => {
    expect(breadcrumbLabelForPath('/segments')).toBe('Segments');
  });

  it('matches a nested route by its longest matching prefix', () => {
    // /plans/history is its own nav item (History); /plans/anything-else
    // that isn't a specific nav item should still resolve to "Plans".
    expect(breadcrumbLabelForPath('/plans/history')).toBe('History');
    expect(breadcrumbLabelForPath('/plans/templates')).toBe('Templates');
    expect(breadcrumbLabelForPath('/plans/p1')).toBe('Plans');
  });

  it('falls back to Monitor for an unmatched path', () => {
    expect(breadcrumbLabelForPath('/some-unknown-route')).toBe('Monitor');
  });

  it('matches /dashboard to Monitor', () => {
    expect(breadcrumbLabelForPath('/dashboard')).toBe('Monitor');
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd frontend && npx vitest run src/lib/navConfig.test.ts`
Expected: FAIL — `navConfig.ts` doesn't exist yet.

- [ ] **Step 3: Write `navConfig.ts`**

This is a `lib/` file — every other file in `frontend/src/lib/` is plain `.ts` with no JSX (confirmed: `agentRoster.ts`, `routingProfileTeams.ts`, `utils.ts`, etc. are all `.ts`). This file holds pure nav data (route + label) only; icons are JSX and belong in `Sidebar.tsx` (Task 4), keyed by `to` so the two stay in sync without this file needing to import React.

```typescript
export type NavItem = { to: string; label: string };
export type NavGroup = { label: string; items: NavItem[] };

export const NAV_GROUPS: NavGroup[] = [
  {
    label: 'Contact center',
    items: [
      { to: '/dashboard', label: 'Monitor' },
      { to: '/plans/history', label: 'History' },
      { to: '/plans', label: 'Plans' },
      { to: '/plans/templates', label: 'Templates' },
      { to: '/segments', label: 'Segments' },
    ],
  },
  {
    label: 'Admin',
    items: [
      { to: '/campaigns', label: 'Campaigns' },
      { to: '/profiles', label: 'Profiles' },
      { to: '/audit', label: 'Audit' },
      { to: '/contact-artifacts', label: 'Artifacts' },
    ],
  },
];

/**
 * The active top-level nav item's label for a given pathname — used by the
 * TopBar breadcrumb. Matches the item whose `to` is the longest prefix of
 * `pathname` (so `/plans/history` picks "History" over "Plans", but
 * `/plans/p1` — no specific item matches beyond `/plans` — picks "Plans").
 * Falls back to "Monitor" (the app's default landing item) if nothing matches.
 */
export function breadcrumbLabelForPath(pathname: string): string {
  const allItems = NAV_GROUPS.flatMap((g) => g.items);
  let best: NavItem | null = null;
  for (const item of allItems) {
    if (pathname === item.to || pathname.startsWith(`${item.to}/`)) {
      if (!best || item.to.length > best.to.length) best = item;
    }
  }
  return best?.label ?? 'Monitor';
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd frontend && npx vitest run src/lib/navConfig.test.ts`
Expected: PASS, all 4 assertions.

- [ ] **Step 5: Run typecheck**

Run: `cd frontend && npm run typecheck`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/lib/navConfig.ts frontend/src/lib/navConfig.test.ts
git commit -m "feat(frontend): add shared sidebar nav config + breadcrumb label helper"
```

---

### Task 4: `Sidebar.tsx` — the new global, collapsible nav rail

**Files:**
- Create: `frontend/src/components/Sidebar.tsx`

**Interfaces:**
- Consumes: `NAV_GROUPS` from `@/lib/navConfig` (Task 3), `usePersistedState` from `@/hooks/usePersistedState` (Task 2).
- Produces: `Sidebar(): ReactNode` — no props. Consumed by Task 6 (`Layout.tsx`).

- [ ] **Step 1: Write the component**

```tsx
import type { ReactNode } from 'react';
import { NavLink } from 'react-router-dom';

import { usePersistedState } from '@/hooks/usePersistedState';
import { NAV_GROUPS } from '@/lib/navConfig';
import { cn } from '@/lib/utils';

// Keyed by route (`to`), not by label — routes are guaranteed unique across
// NAV_GROUPS, labels are not enforced to be. navConfig.ts stays plain data
// (no JSX) so every other file in lib/ keeps its no-JSX convention; icons
// live here instead, next to the only place that renders them.
const NAV_ICONS: Record<string, ReactNode> = {
  '/dashboard': (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-4 w-4 shrink-0">
      <path d="M2 10h2l2-5 3 9 3-7 2 3h4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ),
  '/plans/history': (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-4 w-4 shrink-0">
      <circle cx="10" cy="10" r="8" />
      <path d="M10 6v4l3 2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ),
  '/plans': (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-4 w-4 shrink-0">
      <rect x="2" y="4" width="16" height="12" rx="2" />
      <path d="M6 14V10M10 14V8M14 14V6" strokeLinecap="round" />
    </svg>
  ),
  '/plans/templates': (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-4 w-4 shrink-0">
      <rect x="2" y="13" width="16" height="4" rx="1" />
      <rect x="2" y="7" width="16" height="4" rx="1" />
      <rect x="2" y="1" width="16" height="4" rx="1" />
    </svg>
  ),
  '/segments': (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-4 w-4 shrink-0">
      <circle cx="7" cy="7" r="3" />
      <circle cx="14" cy="14" r="3" />
      <path d="M9.5 9.5l3 3" strokeLinecap="round" />
    </svg>
  ),
  '/campaigns': (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-4 w-4 shrink-0">
      <path d="M3 8l14-4v12L3 12V8z" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ),
  '/profiles': (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-4 w-4 shrink-0">
      <circle cx="10" cy="7" r="3" />
      <path d="M4 17c0-3 2.5-5 6-5s6 2 6 5" strokeLinecap="round" />
    </svg>
  ),
  '/audit': (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-4 w-4 shrink-0">
      <rect x="4" y="2" width="12" height="16" rx="1" />
      <path d="M7 7h6M7 10h6M7 13h4" strokeLinecap="round" />
    </svg>
  ),
  '/contact-artifacts': (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-4 w-4 shrink-0">
      <path d="M4 4h8l4 4v10H4V4z" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M12 4v4h4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ),
};

export function Sidebar(): ReactNode {
  const [collapsed, setCollapsed] = usePersistedState('sidebar-collapsed', false);

  return (
    <aside className={cn('flex flex-col border-r border-border bg-card transition-all', collapsed ? 'w-16' : 'w-56')}>
      <div className="flex h-14 items-center gap-2 border-b border-border px-4">
        <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded bg-primary text-xs font-bold text-primary-foreground">
          +
        </span>
        {!collapsed && <span className="truncate text-sm font-semibold tracking-tight">VIP Connect Admin</span>}
      </div>

      <nav className="flex-1 space-y-5 overflow-y-auto px-2 py-4">
        {NAV_GROUPS.map((group) => (
          <div key={group.label} className="flex flex-col gap-1">
            {!collapsed && (
              <div className="px-2 pb-1 text-[11px] font-semibold uppercase tracking-widest text-muted-foreground">
                {group.label}
              </div>
            )}
            {group.items.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                title={collapsed ? item.label : undefined}
                className={({ isActive }) =>
                  cn(
                    'flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors',
                    isActive ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:bg-muted hover:text-foreground',
                  )
                }
              >
                {NAV_ICONS[item.to]}
                {!collapsed && <span className="truncate">{item.label}</span>}
              </NavLink>
            ))}
          </div>
        ))}
      </nav>

      <button
        type="button"
        onClick={() => setCollapsed(!collapsed)}
        className="flex items-center justify-center gap-2 border-t border-border px-3 py-3 text-xs font-medium text-muted-foreground hover:bg-muted hover:text-foreground"
        title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
      >
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" className={cn('h-4 w-4 shrink-0 transition-transform', collapsed && 'rotate-180')}>
          <path d="M12 4l-6 6 6 6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        {!collapsed && <span>Collapse</span>}
      </button>

      {!collapsed && (
        <div className="border-t border-border px-4 py-3">
          <div className="text-[10px] font-semibold uppercase tracking-widest text-muted-foreground">Context</div>
          <div className="text-sm font-medium text-foreground">Outbound scheduling</div>
        </div>
      )}
    </aside>
  );
}
```

This is presentational JSX — no unit test, per repo convention (its only data, `NAV_GROUPS`, is a plain constant with no branching logic; the one piece of real logic this plan adds for navigation, `breadcrumbLabelForPath`, lives in and is tested from Task 3's `navConfig.ts`, not here).

- [ ] **Step 2: Run typecheck**

Run: `cd frontend && npm run typecheck`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/Sidebar.tsx
git commit -m "feat(frontend): add collapsible global Sidebar component"
```

---

### Task 5: `TopBar.tsx` — breadcrumb, live clock, alerts badge, avatar

**Files:**
- Create: `frontend/src/components/TopBar.tsx`
- Modify: `frontend/src/lib/agentRoster.ts`
- Create/modify: `frontend/src/lib/agentRoster.test.ts` (append to existing file)

**Interfaces:**
- Produces (in `agentRoster.ts`): `totalActiveAlerts(agents: AgentRosterEntry[], nowMs?: number): number` — a pure aggregator combining per-agent alerts and per-profile staffing risks into one count for the TopBar badge.
- Produces (in `TopBar.tsx`): `TopBar(): ReactNode` — no props. Consumed by Task 6 (`Layout.tsx`).
- Consumes: `breadcrumbLabelForPath` from `@/lib/navConfig` (Task 3), `useAuth` from `@/hooks/useAuth` (existing), `fmtTime` from `@/lib/utils` (existing), `signOut` from `@/lib/auth` (existing).

- [ ] **Step 1: Write the failing test for `totalActiveAlerts`**

Find, at the top of `frontend/src/lib/agentRoster.test.ts`:
```typescript
import {
  aggregateByRoutingProfile,
  agentAlert,
  agentStatusTone,
  classifyStaffing,
  DEFAULT_ALERT_THRESHOLDS,
  isBusinessHours,
  minAvailableFor,
} from './agentRoster';
```

Change to:
```typescript
import {
  aggregateByRoutingProfile,
  agentAlert,
  agentStatusTone,
  classifyStaffing,
  DEFAULT_ALERT_THRESHOLDS,
  isBusinessHours,
  minAvailableFor,
  totalActiveAlerts,
} from './agentRoster';
```

This file already has an `agent(overrides)` fixture builder and `BUSINESS_HOURS_MS`/`OFF_HOURS_MS` time constants (used by the existing `classifyStaffing`/`isBusinessHours` tests) — reuse them rather than building new fixtures. Find the end of the file (the last block is the `describe('agentStatusTone', ...)` suite):
```typescript
describe('agentStatusTone', () => {
  it('maps every effectiveStatus value to a distinct, correct tone', () => {
    expect(agentStatusTone('Available')).toBe('success');
    expect(agentStatusTone('On Call')).toBe('info');
    expect(agentStatusTone('ACW')).toBe('acw');
    expect(agentStatusTone('Unavailable')).toBe('warning');
    expect(agentStatusTone('Offline')).toBe('neutral');
  });
});
```

Append immediately after it:
```typescript

describe('totalActiveAlerts', () => {
  it('sums a per-agent idle alert with that same agent\'s profile-level risk', () => {
    const agents = [
      // 20 min idle > the 10-min idle threshold → 1 agent alert.
      agent({ effectiveStatus: 'Available', statusStartTimestamp: new Date(BUSINESS_HOURS_MS - 20 * 60_000).toISOString() }),
    ];
    // Same agent is also this profile's only one: available=1, default min=1 → 'at-minimum' → +1 profile risk.
    expect(totalActiveAlerts(agents, BUSINESS_HOURS_MS)).toBe(2);
  });

  it('counts a per-profile staffing risk even when no per-agent alert applies', () => {
    const agents = [
      // Only 1 min idle — well under the 10-min threshold, no agent alert.
      agent({ effectiveStatus: 'Available', statusStartTimestamp: new Date(BUSINESS_HOURS_MS - 60_000).toISOString() }),
    ];
    // available=1, default min=1 → 'at-minimum', which IS an active risk on its own.
    expect(totalActiveAlerts(agents, BUSINESS_HOURS_MS)).toBe(1);
  });

  it('returns 0 for a healthy, alert-free roster', () => {
    const agents = [
      agent({ agentId: 'a1', effectiveStatus: 'Available', statusStartTimestamp: new Date(BUSINESS_HOURS_MS - 60_000).toISOString() }),
      agent({ agentId: 'a2', effectiveStatus: 'Available', statusStartTimestamp: new Date(BUSINESS_HOURS_MS - 60_000).toISOString() }),
    ];
    // 2 available agents, default min=1 → available > min → 'healthy'. Neither agent is idle long enough to alert.
    expect(totalActiveAlerts(agents, BUSINESS_HOURS_MS)).toBe(0);
  });

  it('suppresses profile-risk counts outside business hours for the identical roster', () => {
    const agents = [
      // effectiveStatus 'On Call', not 'Available' — 0 available agents in this profile.
      agent({ effectiveStatus: 'On Call', statusStartTimestamp: new Date(OFF_HOURS_MS - 60_000).toISOString() }),
    ];
    // available=0 < min=1 → 'no-coverage' during business hours — a real, counted risk.
    expect(totalActiveAlerts(agents, BUSINESS_HOURS_MS)).toBeGreaterThan(0);
    // The exact same roster, evaluated off-hours, must not count that risk at all.
    expect(totalActiveAlerts(agents, OFF_HOURS_MS)).toBe(0);
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd frontend && npx vitest run src/lib/agentRoster.test.ts`
Expected: FAIL — `totalActiveAlerts` is not exported yet.

- [ ] **Step 3: Implement `totalActiveAlerts` in `agentRoster.ts`**

Find, at the end of the "Status → tone" section (after `agentStatusTone`):

```typescript
export function agentStatusTone(effectiveStatus: AgentRosterEntry['effectiveStatus']): StatusTone {
  return AGENT_STATUS_TONE[effectiveStatus];
}
```

Add immediately after:

```typescript
// ── Aggregate alert count (TopBar badge) ─────────────────────────────────────

/**
 * Total active-alert count across the whole roster: every agent with a
 * per-agent alert (idle/break/longCall/longAcw), plus every routing profile
 * whose staffing risk isn't healthy/off-hours. Two different alert families,
 * summed into one number for a single global badge.
 */
export function totalActiveAlerts(agents: AgentRosterEntry[], nowMs: number = Date.now()): number {
  const agentAlertCount = agents.filter((a) => agentAlert(a, DEFAULT_ALERT_THRESHOLDS, nowMs) !== null).length;
  const profileRiskCount = aggregateByRoutingProfile(agents).filter((row) => {
    const risk = classifyStaffing(row.available, minAvailableFor(row.routingProfileName), nowMs).risk;
    return risk !== 'healthy' && risk !== 'off-hours';
  }).length;
  return agentAlertCount + profileRiskCount;
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd frontend && npx vitest run src/lib/agentRoster.test.ts`
Expected: PASS, all 4 new assertions plus every pre-existing test in this file.

- [ ] **Step 5: Write `TopBar.tsx`**

```tsx
import { type ReactNode, useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useLocation } from 'react-router-dom';

import { signOut } from '@/lib/auth';
import { api } from '@/lib/api';
import { totalActiveAlerts } from '@/lib/agentRoster';
import { breadcrumbLabelForPath } from '@/lib/navConfig';
import { fmtTime } from '@/lib/utils';
import { useAuth } from '@/hooks/useAuth';

/** First-letter-of-first-two-segments initials from a username/email, e.g.
 * "sebastian.valdenebro@medwork.io" → "SV", "preview@local" → "PL". */
function initialsFor(username: string): string {
  const local = username.split('@')[0] ?? username;
  const parts = local.split(/[.\s_-]+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0]![0] + parts[1]![0]).toUpperCase();
  return local.slice(0, 2).toUpperCase();
}

export function TopBar(): ReactNode {
  const location = useLocation();
  const { user } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 30_000);
    return () => clearInterval(id);
  }, []);

  // Same query key AgentAvailabilityPanel.tsx already uses — React Query
  // dedupes the network call when both are mounted on the same page.
  const agentQuery = useQuery({
    queryKey: ['agent-roster', 'all'],
    queryFn: () => api.brandedMonitor.getAgentRoster(),
    refetchInterval: 20_000,
  });
  const alertCount = totalActiveAlerts(agentQuery.data?.agents ?? []);

  return (
    <header className="flex h-14 items-center justify-between border-b border-border bg-card px-6">
      <div className="text-sm text-muted-foreground">
        Contact center <span className="mx-1.5 text-border">/</span>
        <span className="font-medium text-foreground">{breadcrumbLabelForPath(location.pathname)}</span>
      </div>
      <div className="flex items-center gap-4">
        <span className="rounded-md bg-muted px-2.5 py-1 text-xs font-medium tabular-nums text-muted-foreground">
          {fmtTime(now)}
        </span>
        <div className="relative flex items-center gap-1 text-muted-foreground" title={`${alertCount} active alert${alertCount === 1 ? '' : 's'}`}>
          <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-5 w-5">
            <path d="M10 3a5 5 0 00-5 5v2.5L3.5 13h13L15 10.5V8a5 5 0 00-5-5z" strokeLinecap="round" strokeLinejoin="round" />
            <path d="M8 15.5a2 2 0 004 0" strokeLinecap="round" />
          </svg>
          {alertCount > 0 && (
            <span className="absolute -right-1.5 -top-1.5 flex h-4 min-w-[16px] items-center justify-center rounded-full bg-status-danger-bar px-1 text-[10px] font-bold leading-none text-white">
              {alertCount}
            </span>
          )}
        </div>
        <div className="relative">
          <button
            type="button"
            onClick={() => setMenuOpen((v) => !v)}
            className="flex h-8 w-8 items-center justify-center rounded-full bg-primary text-xs font-bold text-primary-foreground"
          >
            {initialsFor(user?.username ?? '?')}
          </button>
          {menuOpen && (
            <div className="absolute right-0 top-10 z-10 w-44 rounded-md border border-border bg-card py-1 shadow-md">
              <div className="truncate border-b border-border px-3 py-2 text-xs text-muted-foreground">{user?.username}</div>
              <button
                type="button"
                onClick={() => void signOut()}
                className="w-full px-3 py-2 text-left text-sm text-muted-foreground hover:bg-muted hover:text-foreground"
              >
                Sign out
              </button>
            </div>
          )}
        </div>
      </div>
    </header>
  );
}
```

This is presentational JSX (its only extracted pure logic, `initialsFor`, is a 4-line, fully-deterministic string transform inlined here rather than in a shared lib — matching the size threshold this repo already uses for not extracting trivial one-off string helpers elsewhere, e.g. `queueLabel` in `BrandedMonitor.tsx`). No unit test for this file; `totalActiveAlerts` (the logic with real branching) is already tested from Step 1-4.

- [ ] **Step 6: Run typecheck**

Run: `cd frontend && npm run typecheck`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/TopBar.tsx frontend/src/lib/agentRoster.ts frontend/src/lib/agentRoster.test.ts
git commit -m "feat(frontend): add TopBar with breadcrumb, live clock, alerts badge, avatar menu"
```

---

### Task 6: Wire `Sidebar` + `TopBar` into `Layout.tsx`

**Files:**
- Modify: `frontend/src/components/Layout.tsx`

**Interfaces:**
- Consumes: `Sidebar` (Task 4), `TopBar` (Task 5).

- [ ] **Step 1: Replace the header-based layout with the sidebar+topbar shell**

Find the entire current file content:

```tsx
import type { ReactNode } from 'react';
import { NavLink, Outlet } from 'react-router-dom';

import { signOut } from '@/lib/auth';
import { useAuth } from '@/hooks/useAuth';
import { useIdleTimeout } from '@/hooks/useIdleTimeout';
import { cn } from '@/lib/utils';

const NAV = [
  { to: '/dashboard', label: 'Dashboard' },
  { to: '/segments', label: 'Segments' },
  { to: '/campaigns', label: 'Campaigns' },
  { to: '/plans', label: 'Plans' },
  { to: '/profiles', label: 'Profiles' },
  { to: '/audit', label: 'Audit' },
  { to: '/contact-artifacts', label: 'Artifacts' },
] as const;

export function Layout(): ReactNode {
  const { user } = useAuth();
  useIdleTimeout(Boolean(user));

  return (
    <div className="flex min-h-screen flex-col bg-background text-foreground">
      <header className="border-b border-border bg-card">
        <div className="flex h-14 items-center justify-between px-6">
          <div className="flex items-center gap-6">
            <span className="text-sm font-semibold tracking-tight">VIP Connect Admin</span>
            <nav className="flex items-center gap-1">
              {NAV.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  className={({ isActive }) =>
                    cn(
                      'rounded-md px-3 py-1.5 text-sm transition-colors',
                      isActive
                        ? 'bg-muted font-medium text-foreground'
                        : 'text-muted-foreground hover:text-foreground',
                    )
                  }
                >
                  {item.label}
                </NavLink>
              ))}
            </nav>
          </div>
          <div className="flex items-center gap-3 text-sm">
            <span className="text-muted-foreground">{user?.username}</span>
            <button
              onClick={() => void signOut()}
              className="rounded-md px-3 py-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground"
              type="button"
            >
              Sign out
            </button>
          </div>
        </div>
      </header>
      <main className="w-full flex-1 px-6 py-8">
        <Outlet />
      </main>
    </div>
  );
}
```

Replace it entirely with:

```tsx
import type { ReactNode } from 'react';
import { Outlet } from 'react-router-dom';

import { useAuth } from '@/hooks/useAuth';
import { useIdleTimeout } from '@/hooks/useIdleTimeout';
import { Sidebar } from './Sidebar';
import { TopBar } from './TopBar';

export function Layout(): ReactNode {
  const { user } = useAuth();
  useIdleTimeout(Boolean(user));

  return (
    <div className="flex min-h-screen bg-background text-foreground">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar />
        <main className="min-w-0 flex-1 px-6 py-8">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Run typecheck**

Run: `cd frontend && npm run typecheck`
Expected: clean.

- [ ] **Step 3: Run the full test suite**

Run: `cd frontend && npx vitest run`
Expected: same 128 total / 125 passed / 3 pre-existing `chainMap.test.ts` failures as every prior baseline this session, plus this plan's own new tests (Task 3's 4, Task 5's 4) all passing — no existing test imports or renders `Layout.tsx` directly (its own logic — `useAuth`/`useIdleTimeout` wiring — is unchanged), so nothing here should regress.

- [ ] **Step 4: Run the build**

Run: `cd frontend && npm run build`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/Layout.tsx
git commit -m "feat(frontend): replace top-nav header with Sidebar + TopBar shell"
```

---

### Task 7: `PlansLayout.tsx` — whole-sidebar collapse with reflow

**Files:**
- Modify: `frontend/src/pages/PlansLayout.tsx`

**Interfaces:**
- Consumes: `usePersistedState` from `@/hooks/usePersistedState` (Task 2).

- [ ] **Step 1: Replace per-group collapse state with one whole-sidebar collapse flag**

Find:
```tsx
export function PlansLayout(): ReactNode {
  const navigate = useNavigate();
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  return (
    <div className="flex gap-8">
      {/* ── Sidebar ─────────────────────────────────────────────────── */}
      <aside className="w-52 shrink-0">
        <div className="flex flex-col gap-5">
          {GROUPS.map((group) => {
            const isCollapsed = collapsed[group.label] ?? false;
            return (
            <div key={group.label} className="flex flex-col gap-1">
              <button
                type="button"
                onClick={() => setCollapsed((prev) => ({ ...prev, [group.label]: !isCollapsed }))}
                className="flex items-center justify-between px-3 text-[11px] font-semibold uppercase tracking-widest text-muted-foreground hover:text-foreground"
              >
                {group.label}
                <svg
                  viewBox="0 0 20 20"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  className={`h-3 w-3 shrink-0 transition-transform ${isCollapsed ? '' : 'rotate-90'}`}
                >
                  <path d="M7 5l6 5-6 5" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              </button>
              {!isCollapsed && group.items.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  className={({ isActive }) =>
                    cn(
                      'flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors',
                      isActive
                        ? 'bg-primary/10 text-primary'
                        : 'text-muted-foreground hover:bg-muted hover:text-foreground',
                    )
                  }
                >
                  {item.icon}
                  {item.label}
                </NavLink>
              ))}
            </div>
            );
          })}

          <div className="mt-2 px-3">
            <Button size="sm" className="w-full" onClick={() => navigate('/plans/new')}>
              New plan
            </Button>
          </div>
        </div>
      </aside>

      {/* ── Content ─────────────────────────────────────────────────── */}
      <div className="flex-1 min-w-0">
        <Outlet />
      </div>
    </div>
  );
}
```

Change to:
```tsx
export function PlansLayout(): ReactNode {
  const navigate = useNavigate();
  const [collapsed, setCollapsed] = usePersistedState('plans-sidebar-collapsed', false);

  return (
    <div className="flex gap-8">
      {/* ── Sidebar ─────────────────────────────────────────────────── */}
      <aside className={cn('shrink-0 transition-all', collapsed ? 'w-12' : 'w-52')}>
        <div className="flex flex-col gap-5">
          <button
            type="button"
            onClick={() => setCollapsed(!collapsed)}
            className="flex items-center gap-1.5 px-3 text-[11px] font-semibold uppercase tracking-widest text-muted-foreground hover:text-foreground"
            title={collapsed ? 'Expand' : 'Collapse'}
          >
            <svg
              viewBox="0 0 20 20"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              className={`h-3 w-3 shrink-0 transition-transform ${collapsed ? 'rotate-180' : ''}`}
            >
              <path d="M12 4l-6 6 6 6" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            {!collapsed && 'Collapse'}
          </button>

          {GROUPS.map((group) => (
            <div key={group.label} className="flex flex-col gap-1">
              {!collapsed && (
                <div className="px-3 text-[11px] font-semibold uppercase tracking-widest text-muted-foreground">
                  {group.label}
                </div>
              )}
              {group.items.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  title={collapsed ? item.label : undefined}
                  className={({ isActive }) =>
                    cn(
                      'flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors',
                      isActive
                        ? 'bg-primary/10 text-primary'
                        : 'text-muted-foreground hover:bg-muted hover:text-foreground',
                    )
                  }
                >
                  {item.icon}
                  {!collapsed && item.label}
                </NavLink>
              ))}
            </div>
          ))}

          {!collapsed && (
            <div className="mt-2 px-3">
              <Button size="sm" className="w-full" onClick={() => navigate('/plans/new')}>
                New plan
              </Button>
            </div>
          )}
        </div>
      </aside>

      {/* ── Content ─────────────────────────────────────────────────── */}
      <div className="flex-1 min-w-0">
        <Outlet />
      </div>
    </div>
  );
}
```

Find, at the top of the file:
```tsx
import { useState, type ReactNode } from 'react';
import { NavLink, Outlet, useNavigate } from 'react-router-dom';

import { Button } from '@/components/ui';
import { cn } from '@/lib/utils';
```

Change to:
```tsx
import type { ReactNode } from 'react';
import { NavLink, Outlet, useNavigate } from 'react-router-dom';

import { Button } from '@/components/ui';
import { usePersistedState } from '@/hooks/usePersistedState';
import { cn } from '@/lib/utils';
```

(`useState` is no longer used directly in this file — `usePersistedState` replaces it — so the import must drop, not just add alongside it, or `noUnusedLocals` fails the build.)

- [ ] **Step 2: Run typecheck**

Run: `cd frontend && npm run typecheck`
Expected: clean.

- [ ] **Step 3: Run the full test suite**

Run: `cd frontend && npx vitest run`
Expected: same baseline as Task 6's Step 3 — this file has no dedicated test (presentational JSX, per convention) and nothing else imports its internals.

- [ ] **Step 4: Run the build**

Run: `cd frontend && npm run build`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/PlansLayout.tsx
git commit -m "feat(frontend): collapse the whole Plans sidebar with reflow, not just per-group"
```

---

### Task 8: `AgentAvailabilityCard.tsx` — the shared compact per-profile card

**Files:**
- Create: `frontend/src/components/AgentAvailabilityCard.tsx`

**Interfaces:**
- Produces: `AgentAvailabilityCard({ row, onClick }): ReactNode` where `row: RoutingProfileAvailability` (from `@/lib/agentRoster`) and `onClick?: () => void`. Consumed by Task 9 (`BrandedMonitor.tsx`) and Task 10 (`AgentAvailabilityPanel.tsx`).
- Consumes: `classifyStaffing`, `minAvailableFor`, `type RoutingProfileAvailability` from `@/lib/agentRoster` (all pre-existing); `StatusChip` from `@/components/ui/StatusChip`; `STATUS_TONE_CLASSES` from `@/components/ui/status`.

- [ ] **Step 1: Write the component**

```tsx
import type { ReactNode } from 'react';

import { StatusChip } from '@/components/ui/StatusChip';
import { STATUS_TONE_CLASSES } from '@/components/ui/status';
import { classifyStaffing, minAvailableFor, type RoutingProfileAvailability } from '@/lib/agentRoster';

/**
 * One routing profile's availability, compact enough to tile several per
 * row. Shared by BrandedMonitor's Live Monitor sidebar and Campaign
 * Monitor's compact widget — both scope `row` to their own team(s) before
 * calling this, so this component has no team-scoping logic of its own.
 */
export function AgentAvailabilityCard({
  row,
  onClick,
}: {
  row: RoutingProfileAvailability;
  onClick?: () => void;
}): ReactNode {
  const min = minAvailableFor(row.routingProfileName);
  const staffing = classifyStaffing(row.available, min);
  const tone = STATUS_TONE_CLASSES[staffing.tone];
  const atRisk = staffing.risk !== 'healthy' && staffing.risk !== 'off-hours';

  return (
    <div
      onClick={onClick}
      role={onClick ? 'button' : undefined}
      tabIndex={onClick ? 0 : undefined}
      className={`min-w-[200px] flex-1 basis-[220px] space-y-2 rounded-xl border p-3 transition-shadow ${
        onClick ? 'cursor-pointer hover:shadow-md' : ''
      } ${atRisk ? 'border-red-200 bg-red-50/60' : 'border-gray-200 bg-white'}`}
    >
      <div className="flex items-start justify-between gap-2">
        <span className="truncate text-sm font-semibold text-gray-800">{row.routingProfileName}</span>
        <div className="shrink-0 text-right">
          <div className={`text-2xl font-bold tabular-nums ${tone.fg}`}>{row.available}</div>
          <div className="-mt-1 text-[10px] uppercase text-gray-400">Available</div>
        </div>
      </div>
      <div className="flex gap-3 text-xs text-gray-500">
        <span>{row.onCall} CALL</span>
        <span>{row.acw} ACW</span>
        <span>{row.offline + row.unavailable} OFF</span>
      </div>
      {atRisk && (
        <div className="flex items-center justify-between">
          <StatusChip tone={staffing.tone} label={staffing.label} />
          <span className="text-[10px] text-gray-400">min {min} available</span>
        </div>
      )}
    </div>
  );
}
```

Presentational JSX — no unit test (all the logic it calls — `classifyStaffing`, `minAvailableFor` — is already tested where it's defined, in `agentRoster.test.ts`).

- [ ] **Step 2: Run typecheck**

Run: `cd frontend && npm run typecheck`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/AgentAvailabilityCard.tsx
git commit -m "feat(frontend): add shared AgentAvailabilityCard (compact, by routing profile)"
```

---

### Task 9: `BrandedMonitor.tsx` — Live Monitor sidebar uses the compact card

**Files:**
- Modify: `frontend/src/pages/BrandedMonitor.tsx`

**Interfaces:**
- Consumes: `AgentAvailabilityCard` (Task 8); `allRoutingProfiles` field (Task 1) via `agentQuery.data`.

- [ ] **Step 1: Add the new imports**

Find:
```tsx
import { elapsedSeconds, elapsedMinutes, formatRuntime, fmtTime } from '@/lib/utils';
import { BRANDED_MONITOR_TEAMS, teamForProfile } from '@/lib/routingProfileTeams';
import { AgentRoster } from './AgentRoster';
```

Change to:
```tsx
import { elapsedSeconds, elapsedMinutes, formatRuntime, fmtTime } from '@/lib/utils';
import { BRANDED_MONITOR_TEAMS, teamForProfile } from '@/lib/routingProfileTeams';
import { AgentAvailabilityCard } from '@/components/AgentAvailabilityCard';
import { aggregateByRoutingProfile, classifyStaffing, minAvailableFor, STAFFING_RISK_ORDER } from '@/lib/agentRoster';
import { AgentRoster } from './AgentRoster';
```

- [ ] **Step 2: Rewrite `AgentAvailabilitySidebar` to use the compact card, add the "All profiles" link and the zero-agent footer**

Find the entire function:
```tsx
function AgentAvailabilitySidebar({ agents, isLoading, lastUpdated, onSelectProfile }: {
  agents: AgentRosterEntry[];
  isLoading?: boolean;
  lastUpdated?: string;
  onSelectProfile: (profileId: string) => void;
}): ReactNode {
  const brandedAgents = agents.filter(
    (a) => (BRANDED_MONITOR_TEAMS as readonly string[]).includes(teamForProfile(a.routingProfileName) ?? ''),
  );
  const profiles    = [...new Map(brandedAgents.map(a => [a.routingProfileId, a.routingProfileName]))].sort((a, b) => a[1].localeCompare(b[1]));
  const alertCount  = brandedAgents.filter(a => agentIdleAlert(a) !== null).length;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-800">Agent availability — Branded teams</h2>
        {lastUpdated && !isLoading && (
          <span className="text-[10px] text-gray-400">
            {elapsedMinutes(lastUpdated) === 0 ? 'updated just now' : `updated ${elapsedMinutes(lastUpdated)}m ago`}
          </span>
        )}
        {isLoading && <span className="text-[10px] text-gray-400 animate-pulse">Loading…</span>}
      </div>

      <div className="flex flex-wrap gap-3">
        {profiles.map(([profileId, profileName]) => {
          const pa        = brandedAgents.filter(a => a.routingProfileId === profileId);
          const available = pa.filter(a => a.effectiveStatus === 'Available').length;
          const onCall    = pa.filter(a => a.effectiveStatus === 'On Call').length;
          const acw       = pa.filter(a => a.effectiveStatus === 'ACW').length;
          const online    = pa.filter(a => a.effectiveStatus !== 'Unavailable' && a.effectiveStatus !== 'Offline').length;
          const lowAgents = available < 2;
          return (
            <div
              key={profileId}
              onClick={() => onSelectProfile(profileId)}
              role="button"
              tabIndex={0}
              className={`rounded-xl border p-4 space-y-3 min-w-[200px] flex-1 basis-[220px] cursor-pointer transition-shadow hover:shadow-md ${lowAgents ? 'border-red-200 bg-red-50' : 'border-gray-200 bg-white'}`}
            >
              <div className="flex items-center gap-2">
                <div className={`w-2 h-2 rounded-full ${lowAgents ? 'bg-red-500' : 'bg-green-500'}`} />
                <span className="text-xs font-semibold text-gray-700">{profileName}</span>
              </div>
              <div className="grid grid-cols-2 gap-2 text-center">
                <div className={`rounded-lg py-2 ${lowAgents ? 'bg-red-100' : 'bg-gray-50'}`}>
                  <div className={`text-2xl font-bold tabular-nums ${lowAgents ? 'text-red-600' : 'text-green-600'}`}>{available}</div>
                  <div className="text-[10px] text-gray-500 uppercase">Available</div>
                </div>
                <div className="rounded-lg py-2 bg-gray-50">
                  <div className="text-2xl font-bold tabular-nums text-blue-600">{onCall}</div>
                  <div className="text-[10px] text-gray-500 uppercase">On contact</div>
                </div>
              </div>
              <div className="text-xs text-gray-500 flex gap-4">
                <span>ACW <strong className="text-gray-700">{acw}</strong></span>
                <span>Online <strong className="text-gray-700">{online}</strong></span>
              </div>
              {lowAgents && <div className="text-[11px] text-red-600 font-medium">⚠ Low available agents</div>}
            </div>
          );
        })}
      </div>

      {alertCount > 0 && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 p-3">
          <div className="text-xs text-amber-700 font-medium">
            {alertCount} agent{alertCount > 1 ? 's' : ''} need attention
          </div>
        </div>
      )}

      {profiles.length === 0 && !isLoading && (
        <div className="text-sm text-gray-400 text-center py-6 rounded-xl border border-gray-200 bg-white">No agents online</div>
      )}
    </div>
  );
}
```

Replace it entirely with:
```tsx
function AgentAvailabilitySidebar({ agents, allRoutingProfiles, isLoading, lastUpdated, onSelectProfile, onViewAllProfiles }: {
  agents: AgentRosterEntry[];
  allRoutingProfiles: RoutingProfileSummary[];
  isLoading?: boolean;
  lastUpdated?: string;
  onSelectProfile: (profileId: string) => void;
  onViewAllProfiles: () => void;
}): ReactNode {
  const isBranded = (name: string) => (BRANDED_MONITOR_TEAMS as readonly string[]).includes(teamForProfile(name) ?? '');
  const brandedAgents = agents.filter((a) => isBranded(a.routingProfileName));
  const alertCount  = brandedAgents.filter(a => agentIdleAlert(a) !== null).length;

  const rows = aggregateByRoutingProfile(brandedAgents)
    .sort((a, b) =>
      STAFFING_RISK_ORDER[classifyStaffing(a.available, minAvailableFor(a.routingProfileName)).risk]
      - STAFFING_RISK_ORDER[classifyStaffing(b.available, minAvailableFor(b.routingProfileName)).risk],
    );
  const shownIds = new Set(rows.map((r) => r.routingProfileId));
  const zeroAgentCount = allRoutingProfiles.filter((p) => isBranded(p.name) && !shownIds.has(p.id)).length;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-800">Agent availability — Branded teams</h2>
        <div className="flex items-center gap-3">
          <button type="button" onClick={onViewAllProfiles} className="text-[10px] font-medium text-amber-600 hover:text-amber-700">
            All profiles →
          </button>
          {lastUpdated && !isLoading && (
            <span className="text-[10px] text-gray-400">
              {elapsedMinutes(lastUpdated) === 0 ? 'updated just now' : `updated ${elapsedMinutes(lastUpdated)}m ago`}
            </span>
          )}
          {isLoading && <span className="text-[10px] text-gray-400 animate-pulse">Loading…</span>}
        </div>
      </div>

      <div className="flex flex-wrap gap-3">
        {rows.map((row) => (
          <AgentAvailabilityCard key={row.routingProfileId} row={row} onClick={() => onSelectProfile(row.routingProfileId)} />
        ))}
      </div>

      {alertCount > 0 && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 p-3">
          <div className="text-xs text-amber-700 font-medium">
            {alertCount} agent{alertCount > 1 ? 's' : ''} need attention
          </div>
        </div>
      )}

      {zeroAgentCount > 0 && (
        <div className="text-center text-xs text-gray-400">+{zeroAgentCount} profile{zeroAgentCount > 1 ? 's' : ''}</div>
      )}

      {rows.length === 0 && !isLoading && (
        <div className="text-sm text-gray-400 text-center py-6 rounded-xl border border-gray-200 bg-white">No agents online</div>
      )}
    </div>
  );
}
```

- [ ] **Step 3: Import `RoutingProfileSummary`**

Find:
```tsx
import {
  api,
  type AgentRosterEntry,
  type BrandedCampaignRecord,
  type BrandedMetricSnapshot,
  type BrandedTodaySummary,
} from '@/lib/api';
```

Change to:
```tsx
import {
  api,
  type AgentRosterEntry,
  type BrandedCampaignRecord,
  type BrandedMetricSnapshot,
  type BrandedTodaySummary,
  type RoutingProfileSummary,
} from '@/lib/api';
```

- [ ] **Step 4: Thread `allRoutingProfiles` and `onViewAllProfiles` through the call sites**

Find, in `LiveView`:
```tsx
function LiveView({ date, onDateChange, onSelectAgentProfile }: { date: string; onDateChange: (d: string) => void; onSelectAgentProfile: (profileId: string) => void }): ReactNode {
```

Change to:
```tsx
function LiveView({ date, onDateChange, onSelectAgentProfile, onViewAllProfiles }: {
  date: string;
  onDateChange: (d: string) => void;
  onSelectAgentProfile: (profileId: string) => void;
  onViewAllProfiles: () => void;
}): ReactNode {
```

Find:
```tsx
          <AgentAvailabilitySidebar
            agents={agentQuery.data?.agents ?? []}
            isLoading={agentQuery.isLoading}
            lastUpdated={agentQuery.data?.lastUpdated}
            onSelectProfile={onSelectAgentProfile}
          />
```

Change to:
```tsx
          <AgentAvailabilitySidebar
            agents={agentQuery.data?.agents ?? []}
            allRoutingProfiles={agentQuery.data?.allRoutingProfiles ?? []}
            isLoading={agentQuery.isLoading}
            lastUpdated={agentQuery.data?.lastUpdated}
            onSelectProfile={onSelectAgentProfile}
            onViewAllProfiles={onViewAllProfiles}
          />
```

Find, in the top-level `BrandedMonitor` component:
```tsx
      {tab === 'live'    && (
        <LiveView
          date={date}
          onDateChange={setDate}
          onSelectAgentProfile={(profileId) => {
            setAgentsPreset({ profileId });
            setTab('agents');
          }}
        />
      )}
```

Change to:
```tsx
      {tab === 'live'    && (
        <LiveView
          date={date}
          onDateChange={setDate}
          onSelectAgentProfile={(profileId) => {
            setAgentsPreset({ profileId });
            setTab('agents');
          }}
          onViewAllProfiles={() => {
            setAgentsPreset({});
            setTab('agents');
          }}
        />
      )}
```

- [ ] **Step 5: Run typecheck**

Run: `cd frontend && npm run typecheck`
Expected: clean.

- [ ] **Step 6: Run the full test suite**

Run: `cd frontend && npx vitest run`
Expected: same baseline as Task 6's Step 3 — this file has no dedicated test.

- [ ] **Step 7: Run the build**

Run: `cd frontend && npm run build`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/pages/BrandedMonitor.tsx
git commit -m "feat(frontend): use compact AgentAvailabilityCard in Live Monitor sidebar"
```

---

### Task 10: `AgentAvailabilityPanel.tsx` — compact widget uses the shared card, per-card profile filtering

**Files:**
- Modify: `frontend/src/pages/AgentAvailabilityPanel.tsx`
- Verify unchanged: `frontend/src/pages/AgentAvailabilityPanel.test.ts` (confirmed in Step 4 below — no edits needed)

**Interfaces:**
- Consumes: `AgentAvailabilityCard` (Task 8); `allRoutingProfiles` field (Task 1) via `query.data`.
- Behavior change from today: individual cards now navigate with BOTH `team` and `profile` query params (specific-profile filtering), where today the whole panel navigated with only `team` (no profile). The header's new "All profiles →" link takes over today's team-only, no-profile behavior. This is a strictly more precise version of the existing click-through — every URL this panel can produce today remains producible (via "All profiles"), plus a new, more specific one per card.

- [ ] **Step 1: Rewrite the file**

Find the entire current file content:
```tsx
import type { ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';

import { StatTile } from '@/components/ui/StatTile';
import { api } from '@/lib/api';
import { aggregateByRoutingProfile } from '@/lib/agentRoster';
import { teamForProfile } from '@/lib/routingProfileTeams';

// Deliberately narrower than BRANDED_MONITOR_TEAMS: this compact panel is scoped to
// Patient Access only by design, and its click-through must always land on this same
// team — kept in one constant so the two can't drift apart.
const PANEL_TEAM = 'patient-success';

export function AgentAvailabilityPanel({
  active,
  className,
}: {
  active: boolean;
  className?: string;
}): ReactNode {
  const navigate = useNavigate();
  const query = useQuery({
    queryKey: ['agent-roster', 'all'],
    queryFn: () => api.brandedMonitor.getAgentRoster(),
    refetchInterval: active ? 20_000 : false,
  });

  const patientAccessAgents = (query.data?.agents ?? []).filter(
    (a) => teamForProfile(a.routingProfileName) === PANEL_TEAM,
  );
  const rows = aggregateByRoutingProfile(patientAccessAgents);

  return (
    <div className={className}>
      <h3 className="text-sm font-semibold text-gray-700 mb-2">Agent availability — Patient Access</h3>
      {query.isError ? (
        <p className="text-xs text-red-500">Failed to load agent roster.</p>
      ) : query.isPending ? (
        <p className="text-xs text-gray-400">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="text-xs text-gray-400">No Patient Access agents online.</p>
      ) : (
        <div
          className="flex flex-wrap gap-2 cursor-pointer"
          role="button"
          tabIndex={0}
          onClick={() => navigate(`/plans/branded-monitor?tab=agents&team=${PANEL_TEAM}`)}
          title="View in Agent Roster"
        >
          {rows.map((row) => (
            <div key={row.routingProfileId} className="rounded-lg border border-gray-200 p-2.5 min-w-[160px] flex-1 basis-[180px] transition-shadow hover:shadow-md">
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

Replace it entirely with:
```tsx
import type { ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';

import { AgentAvailabilityCard } from '@/components/AgentAvailabilityCard';
import { api } from '@/lib/api';
import { aggregateByRoutingProfile, classifyStaffing, minAvailableFor, STAFFING_RISK_ORDER } from '@/lib/agentRoster';
import { teamForProfile } from '@/lib/routingProfileTeams';

// Deliberately narrower than BRANDED_MONITOR_TEAMS: this compact panel is scoped to
// Patient Access only by design, and its click-through must always land on this same
// team — kept in one constant so the two can't drift apart.
const PANEL_TEAM = 'patient-success';

export function AgentAvailabilityPanel({
  active,
  className,
}: {
  active: boolean;
  className?: string;
}): ReactNode {
  const navigate = useNavigate();
  const query = useQuery({
    queryKey: ['agent-roster', 'all'],
    queryFn: () => api.brandedMonitor.getAgentRoster(),
    refetchInterval: active ? 20_000 : false,
  });

  const patientAccessAgents = (query.data?.agents ?? []).filter(
    (a) => teamForProfile(a.routingProfileName) === PANEL_TEAM,
  );
  const rows = aggregateByRoutingProfile(patientAccessAgents)
    .sort((a, b) =>
      STAFFING_RISK_ORDER[classifyStaffing(a.available, minAvailableFor(a.routingProfileName)).risk]
      - STAFFING_RISK_ORDER[classifyStaffing(b.available, minAvailableFor(b.routingProfileName)).risk],
    );
  const shownIds = new Set(rows.map((r) => r.routingProfileId));
  const zeroAgentCount = (query.data?.allRoutingProfiles ?? []).filter(
    (p) => teamForProfile(p.name) === PANEL_TEAM && !shownIds.has(p.id),
  ).length;

  return (
    <div className={className}>
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-700">Agent availability — Patient Access</h3>
        <button
          type="button"
          onClick={() => navigate(`/plans/branded-monitor?tab=agents&team=${PANEL_TEAM}`)}
          className="text-[10px] font-medium text-amber-600 hover:text-amber-700"
        >
          All profiles →
        </button>
      </div>
      {query.isError ? (
        <p className="text-xs text-red-500">Failed to load agent roster.</p>
      ) : query.isPending ? (
        <p className="text-xs text-gray-400">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="text-xs text-gray-400">No Patient Access agents online.</p>
      ) : (
        <div className="flex flex-wrap gap-2">
          {rows.map((row) => (
            <AgentAvailabilityCard
              key={row.routingProfileId}
              row={row}
              onClick={() => navigate(`/plans/branded-monitor?tab=agents&team=${PANEL_TEAM}&profile=${row.routingProfileId}`)}
            />
          ))}
        </div>
      )}
      {zeroAgentCount > 0 && (
        <div className="mt-2 text-center text-xs text-gray-400">+{zeroAgentCount} profile{zeroAgentCount > 1 ? 's' : ''}</div>
      )}
    </div>
  );
}
```

(`StatTile` is no longer used in this file — the drop of its import is deliberate, not an oversight; `AgentAvailabilityCard` replaces its role entirely.)

- [ ] **Step 2: Run typecheck**

Run: `cd frontend && npm run typecheck`
Expected: clean.

- [ ] **Step 3: Run the full test suite**

Run: `cd frontend && npx vitest run`
Expected: same baseline as Task 6's Step 3. This file's only existing test, `src/pages/AgentAvailabilityPanel.test.ts`, must be checked (Step 4) — if it renders/asserts on this component's DOM structure, it needs updating.

- [ ] **Step 4: Confirm the existing test file needs no changes**

`frontend/src/pages/AgentAvailabilityPanel.test.ts` only asserts that `aggregateByRoutingProfile` is importable from `@/lib/agentRoster` and returns `[]` for empty input — it does not render the component or assert on any DOM structure (`StatTile`, click targets, etc.). This rewrite keeps that same import and usage unchanged, so this file needs no edits.

Run: `cd frontend && npx vitest run src/pages/AgentAvailabilityPanel.test.ts`
Expected: PASS, unchanged.

- [ ] **Step 5: Run the build**

Run: `cd frontend && npm run build`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/AgentAvailabilityPanel.tsx
git commit -m "feat(frontend): use compact AgentAvailabilityCard in Campaign Monitor widget, per-card profile filtering"
```
