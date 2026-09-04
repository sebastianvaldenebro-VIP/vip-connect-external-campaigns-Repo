# UI Polish Round Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A batch of 8 UI polish items Sebastian requested after using the shipped Campaign Monitor / Agent Roster / Branded Monitor pages: full-width layout, a collapsible sidebar section, a z-order fix on the Campaign Monitor timeline bar, dropdown filters (replacing pill buttons) + a reordered/legended capacity block on the Agent Roster page, a scoped/horizontal/click-through agent-availability sidebar on Branded Monitor's Live Monitor tab, a matching scoped/horizontal/click-through compact widget on Campaign Monitor, and a Start/End time display on each campaign card.

**Architecture:** No new pages, no backend changes — this is entirely `frontend/`. Two small pieces of cross-component plumbing tie several items together: (1) `AgentRoster.tsx` gains two optional props (`initialTeamFilter`, `initialProfileFilter`) so a link that lands on the Agents tab can arrive pre-filtered; (2) `BrandedMonitor.tsx` reads `?tab=`/`?team=`/`?profile=` from the URL once on mount (for cross-page arrivals) and keeps a small local "preset" state (for same-page sidebar-row clicks) that feeds those two props.

**Tech Stack:** React 18 + TypeScript, react-router-dom v6, Tailwind, Vitest.

**Spec:** Confirmed directly with Sebastian in conversation (2026-09-02) — no separate design doc; each task below states the confirmed behavior inline.

## Global Constraints

- **`@/components/ui` collision** (real, repo-wide): `frontend/src/components/ui.tsx` (file) and `frontend/src/components/ui/` (directory) both exist; the bare `@/components/ui` path always resolves to the file. Any primitive from the directory (`StatTile`, `StatusChip`, etc.) must be imported from its specific file, never the bare path.
- **COT-only timezone.** Any new time display must use `fmtTime` from `@/lib/utils`, never `toLocaleTimeString`/`Date.prototype.getHours`/browser-local formatting.
- **No icon library, no obscure Unicode glyphs.** Plain, common-block Unicode (▶ ● ↺ ↑ ↓ · are all confirmed safe elsewhere in this codebase) or raw inline SVG (`viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5"`, matching `PlansLayout.tsx`'s existing icons) only.
- **`BRANDED_MONITOR_TEAMS = ['patient-success', 'appointment-services']`** (in `frontend/src/lib/routingProfileTeams.ts`) is the existing, correct scope for "branded teams" — reuse it, don't redefine which teams count as branded.
- **Presentational JSX is not unit-tested** by this repo's convention — only pure, exported functions get tests. Two tasks in this plan (Task 3, Task 4) extract small pure functions specifically so their logic has real test coverage; the surrounding JSX does not need its own test.
- **`AgentRoster.tsx`, `BrandedMonitor.tsx`, `AgentAvailabilityPanel.tsx`, `DayHeaderTimeline.tsx`, `PlansLayout.tsx`, `Layout.tsx`** are all existing, already-shipped files — every task below states an exact anchor (a line of existing code to find) rather than a line number, since line numbers shift as earlier tasks land.

---

### Task 1: Full-width layout

**Files:**
- Modify: `frontend/src/components/Layout.tsx`

**Interfaces:** None — purely a class-name change, no new props, no behavior change beyond width.

- [ ] **Step 1: Remove the `max-w-7xl` constraint from both the header and the main content area**

Find:
```tsx
        <div className="mx-auto flex h-14 max-w-7xl items-center justify-between px-6">
```
Change to:
```tsx
        <div className="flex h-14 items-center justify-between px-6">
```
(dropping both `mx-auto` and `max-w-7xl` — with no max-width, `mx-auto` is a no-op, so drop it too rather than leave dead code)

Find:
```tsx
      <main className="mx-auto w-full max-w-7xl flex-1 px-6 py-8">
```
Change to:
```tsx
      <main className="w-full flex-1 px-6 py-8">
```

- [ ] **Step 2: Verify**

Run: `cd frontend && npm run typecheck && npm run build`
Expected: both clean. No test file needed (pure class-name change, no logic).

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/Layout.tsx
git commit -m "fix(frontend): remove max-w-7xl constraint so the app uses full screen width"
```

---

### Task 2: Collapsible "Operations"/"Configuration" sidebar groups

**Files:**
- Modify: `frontend/src/pages/PlansLayout.tsx`

**Interfaces:** None — self-contained local state, no new exports.

**Context:** `PlansLayout.tsx`'s sidebar renders two groups (`GROUPS` array: "Operations", "Configuration"), each with a label and a list of `NavLink`s. Confirmed with Sebastian: the groups should be collapsible (click the label to expand/collapse). Default: both expanded (matches current always-visible behavior, so nothing looks different until a user actually clicks to collapse).

- [ ] **Step 1: Add collapse state and a clickable header with a chevron**

Find the top of the `PlansLayout` function:
```tsx
export function PlansLayout(): ReactNode {
  const navigate = useNavigate();

  return (
```
Change to:
```tsx
export function PlansLayout(): ReactNode {
  const navigate = useNavigate();
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  return (
```
Add `useState` to the existing React import — find:
```tsx
import type { ReactNode } from 'react';
```
Change to:
```tsx
import { useState, type ReactNode } from 'react';
```

- [ ] **Step 2: Make the group label clickable and conditionally render its items**

Find:
```tsx
          {GROUPS.map((group) => (
            <div key={group.label} className="flex flex-col gap-1">
              <p className="px-3 text-[11px] font-semibold uppercase tracking-widest text-muted-foreground">
                {group.label}
              </p>
              {group.items.map((item) => (
```
Change to:
```tsx
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
```
Then find the closing of that same `.map` block:
```tsx
                  {item.icon}
                  {item.label}
                </NavLink>
              ))}
            </div>
          ))}
```
Change to:
```tsx
                  {item.icon}
                  {item.label}
                </NavLink>
              ))}
            </div>
            );
          })}
```

- [ ] **Step 3: Verify**

Run: `cd frontend && npm run typecheck && npm run build`
Expected: both clean.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/PlansLayout.tsx
git commit -m "feat(frontend): make Plans sidebar groups (Operations/Configuration) collapsible"
```

---

### Task 3: Fix z-order on the Campaign Monitor timeline bar

**Files:**
- Modify: `frontend/src/pages/DayHeaderTimeline.tsx`
- Modify: `frontend/src/pages/DayHeaderTimeline.test.ts`

**Interfaces:**
- Produces: `sortSegmentsForRender(segments: TimelineSegment[]): TimelineSegment[]` — new exported pure function.
- Consumes: `TimelineSegment` (already exported from this same file).

**Context:** `DayHeaderTimeline`'s bar renders each bucket's segment as an absolutely-positioned `<div>`, in `run.bucketStates` array order (= plan-defined bucket order). Since each segment is positioned by `left: startPct%`, later-in-array segments paint OVER earlier ones wherever they visually overlap (default CSS stacking: later DOM siblings on top). If a bucket completes but a later-indexed bucket's segment overlaps its position (e.g., a bucket ran long, or something was force-started out of order), the `completed` (green) segment can get visually covered by a lower-priority-looking `queued`/`warming`/`running` segment, making the bar look like progress reversed. Confirmed with Sebastian: completed (green) should always be visible — never covered — and the visual order should read as "progressing."

Fix: render segments in a fixed status-priority order (queued → warming → running → completed), so `completed` is always painted last (on top), regardless of array/index order. This does NOT change `computeTimelineSegments`'s own return order (existing tests index into it by position and must keep passing unmodified) — it's a separate, render-only sort applied just before mapping to JSX.

- [ ] **Step 1: Write the failing tests**

Add to `frontend/src/pages/DayHeaderTimeline.test.ts` (add `sortSegmentsForRender` to the existing import line from `./DayHeaderTimeline`, and append this new `describe` block at the end of the file):

```ts
describe('sortSegmentsForRender', () => {
  it('renders completed segments last (on top), regardless of input order', () => {
    const queued    = { bucketIndex: 0, startPct: 0,  endPct: 10, status: 'queued' as const };
    const running   = { bucketIndex: 1, startPct: 10, endPct: 20, status: 'running' as const };
    const completed = { bucketIndex: 2, startPct: 20, endPct: 30, status: 'completed' as const };
    const warming   = { bucketIndex: 3, startPct: 30, endPct: 40, status: 'warming' as const };

    const result = sortSegmentsForRender([completed, queued, running, warming]);
    expect(result[result.length - 1]).toBe(completed);
  });

  it('sorts strictly by status priority: queued, warming, running, completed', () => {
    const queued    = { bucketIndex: 0, startPct: 0,  endPct: 10, status: 'queued' as const };
    const warming   = { bucketIndex: 1, startPct: 10, endPct: 20, status: 'warming' as const };
    const running   = { bucketIndex: 2, startPct: 20, endPct: 30, status: 'running' as const };
    const completed = { bucketIndex: 3, startPct: 30, endPct: 40, status: 'completed' as const };

    const result = sortSegmentsForRender([completed, running, warming, queued]);
    expect(result.map((s) => s.status)).toEqual(['queued', 'warming', 'running', 'completed']);
  });

  it('does not mutate the input array', () => {
    const input = [
      { bucketIndex: 0, startPct: 0, endPct: 10, status: 'completed' as const },
      { bucketIndex: 1, startPct: 10, endPct: 20, status: 'queued' as const },
    ];
    const inputCopy = [...input];
    sortSegmentsForRender(input);
    expect(input).toEqual(inputCopy);
  });

  it('preserves relative order between segments of the same status', () => {
    const a = { bucketIndex: 0, startPct: 0,  endPct: 10, status: 'queued' as const };
    const b = { bucketIndex: 1, startPct: 10, endPct: 20, status: 'queued' as const };
    const result = sortSegmentsForRender([a, b]);
    expect(result).toEqual([a, b]);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/pages/DayHeaderTimeline.test.ts`
Expected: FAIL — `sortSegmentsForRender` is not exported yet.

- [ ] **Step 3: Implement `sortSegmentsForRender` and use it in the render**

In `frontend/src/pages/DayHeaderTimeline.tsx`, add this right after the `SEGMENT_COLOR` constant:

```ts
const RENDER_PRIORITY: Record<BucketStateV2['status'], number> = {
  queued: 0,
  warming: 1,
  running: 2,
  completed: 3,
};

/** Sorts segments so higher-priority statuses (completed last) paint on top —
 * absolutely-positioned overlapping segments otherwise stack by array order,
 * which can visually bury a completed (green) segment under a later-indexed
 * queued/running one. Stable sort: same-status segments keep their relative order. */
export function sortSegmentsForRender(segments: TimelineSegment[]): TimelineSegment[] {
  return [...segments].sort((a, b) => RENDER_PRIORITY[a.status] - RENDER_PRIORITY[b.status]);
}
```

Then find, inside the `DayHeaderTimeline` component:
```tsx
        {segments.map((seg) => (
          <div
            key={seg.bucketIndex}
            className={`absolute top-0 h-full ${SEGMENT_COLOR[seg.status]}`}
            style={{ left: `${seg.startPct}%`, width: `${Math.max(0.5, seg.endPct - seg.startPct)}%` }}
          />
        ))}
```
Change `segments.map` to `sortSegmentsForRender(segments).map`:
```tsx
        {sortSegmentsForRender(segments).map((seg) => (
          <div
            key={seg.bucketIndex}
            className={`absolute top-0 h-full ${SEGMENT_COLOR[seg.status]}`}
            style={{ left: `${seg.startPct}%`, width: `${Math.max(0.5, seg.endPct - seg.startPct)}%` }}
          />
        ))}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/pages/DayHeaderTimeline.test.ts`
Expected: all pass (4 new + the file's existing tests unaffected).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/DayHeaderTimeline.tsx frontend/src/pages/DayHeaderTimeline.test.ts
git commit -m "fix(frontend): completed timeline segments always paint on top of overlapping ones"
```

---

### Task 4: `AgentRoster.tsx` — dropdown filters, filter presets, reordered capacity block, color legend

**Files:**
- Modify: `frontend/src/pages/AgentRoster.tsx`

**Interfaces:**
- Produces: `AgentRoster` gains two new optional props: `initialTeamFilter?: string | null`, `initialProfileFilter?: string | null` — Task 5 and Task 7 pass these when linking here from elsewhere.
- Consumes: nothing new — everything used here already exists in this file (`teamFilter`, `rpFilters`, `visibleRps`, `activeTeams`, `STAFFING_RISK_ORDER`, `SEGMENT`-style tone classes already used by `StaffingBar`).

**Context — 4 independent changes to the same file, confirmed with Sebastian:**
1. The Status/Team/Profile/Alerts filter rows currently render as pill buttons (`FilterBtn`, toggled by clicking). Replace all 4 with `<select>` dropdowns.
2. `AgentRoster` should accept an optional initial team/profile filter (from a link elsewhere), so it can render already-filtered when someone arrives via a link.
3. Move the "Teams & routing profiles" block (`<CapacityTable .../>`) so it renders right after `<WorkforceSummary .../>`, before the control bar / needs-attention panel / agent list.
4. Add a color legend to that block (which colors in the stacked bar mean what) — same pattern as `DayHeaderTimeline`'s existing legend row.

- [ ] **Step 1: Accept `initialTeamFilter`/`initialProfileFilter` props**

Find:
```tsx
export function AgentRoster(): ReactNode {
  const nowMs = useNowTick();
```
Change to:
```tsx
export function AgentRoster({
  initialTeamFilter,
  initialProfileFilter,
}: {
  initialTeamFilter?: string | null;
  initialProfileFilter?: string | null;
} = {}): ReactNode {
  const nowMs = useNowTick();
```

Find:
```tsx
  const [teamFilter, setTeamFilter] = useState<string | null>(null);
  const [rpFilters, setRpFilters] = useState<Set<string>>(new Set());
```
Change to:
```tsx
  const [teamFilter, setTeamFilter] = useState<string | null>(initialTeamFilter ?? null);
  const [rpFilters, setRpFilters] = useState<Set<string>>(
    () => (initialProfileFilter ? new Set([initialProfileFilter]) : new Set()),
  );
```

- [ ] **Step 2: Move the capacity block up**

Find the `return (` block inside `AgentRoster` — it currently renders (in this order): `<WorkforceSummary .../>`, then `<ControlBar .../>`, then `<NeedsAttentionPanel .../>`, then `<CapacityTable agents={agents} />`. Move the `<CapacityTable agents={agents} />` line so it comes immediately after `<WorkforceSummary .../>` and before `<ControlBar .../>`. The result should read, in order: `WorkforceSummary` → `CapacityTable` → `ControlBar` → `NeedsAttentionPanel` → `AgentList`. Do not change any prop passed to any of these — only reorder the 4 JSX lines.

- [ ] **Step 3: Add a color legend to `CapacityTable`**

Find, inside `CapacityTable`'s returned JSX, the header row:
```tsx
      <div className="px-4 py-2.5 border-b border-gray-100">
        <h2 className="text-sm font-semibold text-gray-800">Teams &amp; routing profiles</h2>
      </div>
```
Change to:
```tsx
      <div className="px-4 py-2.5 border-b border-gray-100 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-800">Teams &amp; routing profiles</h2>
        <div className="flex items-center gap-3 text-[11px] text-gray-500">
          <span className="inline-flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full bg-status-success-bar" />Available</span>
          <span className="inline-flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full bg-status-info-bar" />On call</span>
          <span className="inline-flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full bg-status-acw-bar" />ACW</span>
          <span className="inline-flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full bg-status-warning-bar" />Away</span>
          <span className="inline-flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full bg-status-neutral-bar" />Offline</span>
        </div>
      </div>
```
(This matches `StaffingBar`'s own 5 segment colors exactly — `bg-status-success-bar`/`info-bar`/`acw-bar`/`warning-bar`/`neutral-bar` — read `StaffingBar`'s `segments` array in this same file to confirm the mapping before making this edit, in case it has drifted from what's shown here.)

- [ ] **Step 4: Replace the 4 pill-button filter rows with `<select>` dropdowns**

Find the whole `ControlBar` function. Its current body (after the search input + "Group by profile" button row, which stays UNCHANGED) has 4 pill-button rows: Status, Team, Profile, Alerts. Replace all 4 with dropdowns. The full replacement for the part of `ControlBar`'s JSX from the Status row through the Alerts row:

```tsx
      <div className="flex items-center gap-3 flex-wrap">
        <label className="flex items-center gap-1.5 text-xs text-gray-600">
          Status
          <select
            value={statusFilters.size === 1 ? [...statusFilters][0] : ''}
            onChange={(e) => onToggleStatus(e.target.value as AgentRosterEntry['effectiveStatus'])}
            className="rounded-lg border border-gray-200 px-2 py-1 text-xs"
          >
            <option value="">All ({statusCounts['Available'] !== undefined ? Object.values(statusCounts).reduce((a, b) => a + b, 0) : 0})</option>
            {EFFECTIVE_STATUSES.map((s) => (
              <option key={s} value={s}>{STATUS_LABELS[s]} ({statusCounts[s] ?? 0})</option>
            ))}
          </select>
        </label>
        {activeTeams.length > 0 && (
          <label className="flex items-center gap-1.5 text-xs text-gray-600">
            Team
            <select
              value={teamFilter ?? ''}
              onChange={(e) => onSelectTeam(e.target.value || null)}
              className="rounded-lg border border-gray-200 px-2 py-1 text-xs"
            >
              <option value="">All</option>
              {activeTeams.map((team) => (
                <option key={team} value={team}>{TEAM_LABELS[team] ?? team} ({teamCounts[team] ?? 0})</option>
              ))}
            </select>
          </label>
        )}
        {visibleRps.length > 0 && (
          <label className="flex items-center gap-1.5 text-xs text-gray-600">
            Profile
            <select
              value={rpFilters.size === 1 ? [...rpFilters][0] : ''}
              onChange={(e) => { if (e.target.value) { onClearRp(); onToggleRp(e.target.value); } else { onClearRp(); } }}
              className="rounded-lg border border-gray-200 px-2 py-1 text-xs max-w-[220px]"
            >
              <option value="">All profiles</option>
              {visibleRps.map((rp) => (
                <option key={rp.id} value={rp.id}>{rp.name} ({rpCounts[rp.id] ?? 0})</option>
              ))}
            </select>
          </label>
        )}
        <label className="flex items-center gap-1.5 text-xs text-gray-600">
          Alerts
          <select
            value={alertFilter}
            onChange={(e) => onAlertFilter(e.target.value as AlertFilter)}
            className="rounded-lg border border-gray-200 px-2 py-1 text-xs"
          >
            <option value="all">All agents</option>
            <option value="any">Needs attention ({flaggedCount})</option>
          </select>
        </label>
      </div>
```

Notes on this replacement:
- The Status `<select>` only supports selecting ONE status at a time (or "All") — this is a deliberate simplification of the previous multi-select pill row, matching "dropdown, not multi-select pills." `onToggleStatus` already exists and takes one status; calling it when exactly one is currently selected would toggle it OFF (back to empty/all) — that's fine, selecting "All" from the dropdown when one is active should go through `onChange` with `value=""`, which the branch above doesn't handle by calling `onToggleStatus` — **read `onToggleStatus`'s actual implementation in this file before wiring the "All" case**: if `e.target.value` is `''`, you need a way to clear `statusFilters` entirely (there's no existing `onClearStatus` prop — add one, named `onClearStatus: () => void`, wired at the call site in `AgentRoster` to `() => setStatusFilters(new Set())`, mirroring the existing `onClearRp` pattern exactly). Update the `<select>`'s `onChange` to call `onClearStatus()` when `e.target.value === ''`, and `onToggleStatus(...)` otherwise (first clearing any other selected status, since only one can be active at a time under a dropdown — clear via `onClearStatus()` then `onToggleStatus(newValue)`, or simplest: give `ControlBar` a new `onSelectStatus: (s: AgentRosterEntry['effectiveStatus'] | null) => void` prop instead of reusing `onToggleStatus`/`onClearStatus` — wire it in `AgentRoster` as `(s) => setStatusFilters(s ? new Set([s]) : new Set())`, and use that single prop for the dropdown's `onChange`. This is cleaner than composing two calls — implement it this way.
- Add `onSelectStatus` to `ControlBar`'s prop type and destructuring, alongside the existing `onToggleStatus` (keep `onToggleStatus` too — nothing else in this task removes it from the props list, since removing an unused prop cleanly is fine but not required; if TypeScript's `noUnusedLocals`/`noUnusedParameters` flags it as genuinely unused after this change, remove the now-dead `onToggleStatus` prop and its corresponding `toggleStatus` function/callsite in `AgentRoster` — check `frontend/tsconfig.json`'s `noUnusedParameters` setting, if it's `false` an unused destructured prop is not an error, but an unused top-level function IS caught by `noUnusedLocals` — verify with `npm run typecheck` and remove whatever it flags).
- The Profile `<select>`'s `onChange` handling (clearing then setting) is a bit awkward with the existing `onClearRp`/`onToggleRp` pair — if this doesn't type-check cleanly or behaves oddly (e.g. `onToggleRp` toggling instead of setting), replace it with a new single `onSelectProfile: (id: string | null) => void` prop instead, wired in `AgentRoster` as `(id) => setRpFilters(id ? new Set([id]) : new Set())` — same simplification reasoning as the status dropdown. Prefer this cleaner single-setter approach for BOTH Status and Profile dropdowns from the start rather than trying to compose the old toggle-based handlers.
- Update `ControlBar`'s prop type accordingly for whichever of `onSelectStatus`/`onSelectProfile` (replacing `onToggleStatus`/`onClearRp`+`onToggleRp` respectively) you end up implementing, and update the `<ControlBar .../>` call site in `AgentRoster` to pass the new prop(s) instead of the old ones.
- `EFFECTIVE_STATUSES`/`STATUS_LABELS`/`TEAM_LABELS` are already imported/defined in this file — no new imports needed for those. `AgentRosterEntry` is already imported from `@/lib/api`.
- The "Group by profile" toggle button and the search `<input>` (both above the row you're replacing) are UNCHANGED — leave them exactly as they are.
- The "Showing N of M agents" + "Clear all" row (below the row you're replacing) is UNCHANGED — leave it exactly as it is; it already reads `hasActiveFilters`/`filteredCount`/`totalCount`/`onClearFilters`, none of which this task touches.

- [ ] **Step 5: Verify**

Run: `cd frontend && npx vitest run && npm run typecheck && npm run build`
Expected: same test count as before this task (no new tests in this task — presentational-only changes to already-untested JSX; `initialTeamFilter`/`initialProfileFilter` are plain prop plumbing, not new logic), clean typecheck/build.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/AgentRoster.tsx
git commit -m "feat(frontend): dropdown filters, filter presets, reordered capacity block + legend on Agent Roster"
```

---

### Task 5: `BrandedMonitor.tsx` — scope the Live Monitor sidebar, horizontal cards, click-through to Agents tab

**Files:**
- Modify: `frontend/src/pages/BrandedMonitor.tsx`

**Interfaces:**
- Consumes: `AgentRoster`'s new `initialTeamFilter`/`initialProfileFilter` props (Task 4, already merged).
- Produces: nothing new for later tasks in this plan — Task 7 (AgentAvailabilityPanel) links to this page's URL contract (`?tab=agents&team=<key>`) but doesn't import anything from this file directly.

**Context — 3 changes, confirmed with Sebastian:**
1. `AgentAvailabilitySidebar` currently derives its `profiles` list from whatever routing profiles appear in the (unfiltered) `agents` prop — it can show profiles from ANY team, not just the 2 branded ones. Scope it to only routing profiles belonging to `BRANDED_MONITOR_TEAMS` (still one row per individual routing profile — do NOT collapse to team-level rows).
2. Its cards currently stack vertically (`space-y-3`, one per line). Lay them out horizontally (wrap into a row) instead.
3. Each card becomes clickable: click → switch this page's own tab state to `'agents'`, with that specific routing profile pre-filtered in `AgentRoster`.

Also: `BrandedMonitor`'s `tab` state must be initializable from the URL (`?tab=agents`) plus an optional `?team=`/`?profile=`, since Task 7's `AgentAvailabilityPanel` (on a different page/route) will link here with those query params.

- [ ] **Step 1: Read initial tab + filter preset from the URL on mount**

Find:
```tsx
export function BrandedMonitor(): ReactNode {
  const [tab,  setTab]  = useState<Tab>('live');
  const [date, setDate] = useState(todayISO());
```
Change to:
```tsx
export function BrandedMonitor(): ReactNode {
  const [tab, setTab] = useState<Tab>(() => {
    const sp = new URLSearchParams(window.location.search);
    const t = sp.get('tab');
    return t === 'agents' || t === 'history' || t === 'live' ? t : 'live';
  });
  const [agentsPreset, setAgentsPreset] = useState<{ team?: string; profileId?: string }>(() => {
    const sp = new URLSearchParams(window.location.search);
    return { team: sp.get('team') ?? undefined, profileId: sp.get('profile') ?? undefined };
  });
  const [date, setDate] = useState(todayISO());
```

- [ ] **Step 2: Pass the preset into `AgentRoster`, and clear it once consumed so re-clicking a different card overrides cleanly**

Find:
```tsx
      {tab === 'agents'  && <AgentRoster />}
```
Change to:
```tsx
      {tab === 'agents'  && (
        <AgentRoster initialTeamFilter={agentsPreset.team} initialProfileFilter={agentsPreset.profileId} />
      )}
```
(`AgentRoster` only reads these as its INITIAL state — per Task 4's implementation, they seed `useState`'s initializer — so no further action is needed to "clear" them; each time `AgentRoster` mounts fresh (i.e., each time the user switches away from and back to the Agents tab), it re-reads whatever `agentsPreset` currently holds. Do not add any clearing logic — leaving `agentsPreset` as-is between tab switches is correct, since the next click that sets a new preset will overwrite it via `setAgentsPreset` in Step 4 below.)

- [ ] **Step 3: Scope `AgentAvailabilitySidebar` to branded-team profiles only, and lay cards out horizontally**

Find the `AgentAvailabilitySidebar` function signature and its `profiles` derivation:
```tsx
function AgentAvailabilitySidebar({ agents, isLoading, lastUpdated }: {
  agents: AgentRosterEntry[];
  isLoading?: boolean;
  lastUpdated?: string;
}): ReactNode {
  const profiles    = [...new Map(agents.map(a => [a.routingProfileId, a.routingProfileName]))].sort((a, b) => a[1].localeCompare(b[1]));
  const alertCount  = agents.filter(a => agentIdleAlert(a) !== null).length;

  return (
    <div className="space-y-3">
```
Change to:
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
```
(`BRANDED_MONITOR_TEAMS`/`teamForProfile` are already imported in this file — confirm via `grep -n "BRANDED_MONITOR_TEAMS\|teamForProfile" frontend/src/pages/BrandedMonitor.tsx` before assuming; if for any reason they're not imported at the top of this file, add `import { BRANDED_MONITOR_TEAMS, teamForProfile } from '@/lib/routingProfileTeams';`.)

Now find the `.map` over `profiles` that renders each card, and its inner `agents.filter`/`pa` derivation — these currently read from the OUTER `agents` (unfiltered) prop:
```tsx
      {profiles.map(([profileId, profileName]) => {
        const pa        = agents.filter(a => a.routingProfileId === profileId);
```
Change `agents.filter` to `brandedAgents.filter` (so the per-card counts are also scoped, not just the profile list):
```tsx
      {profiles.map(([profileId, profileName]) => {
        const pa        = brandedAgents.filter(a => a.routingProfileId === profileId);
```

Find the wrapping `<div>` for the whole cards section — currently it's a bare `.map` returning `<div key={profileId} className="rounded-xl border p-4 space-y-3 ...">` directly as siblings of each other under the outer `space-y-3`/now `flex flex-col gap-3` container. Wrap the `.map` output in a horizontal flex container so cards lay out in a row instead of inheriting the outer vertical stacking. Find:
```tsx
      {profiles.map(([profileId, profileName]) => {
```
and its corresponding closing `})}`. Wrap that whole block:
```tsx
      <div className="flex flex-wrap gap-3">
        {profiles.map(([profileId, profileName]) => {
```
...
```tsx
        })}
      </div>
```
(Read the actual current indentation/closing structure in the file before editing — this is describing what to wrap, not a literal diff, since the block spans the `pa`/`available`/`onCall`/`acw`/`online`/`lowAgents` derivations plus the returned JSX card. Everything inside stays as-is except: (a) the two `agents.filter` → `brandedAgents.filter` changes above, (b) making the returned card `<div>` clickable per Step 4 below, and (c) giving each card a `min-w-[200px] flex-1 basis-[220px]` sizing class so they wrap sensibly in the new horizontal container rather than stretching to fill it or collapsing too narrow — add these classes to the card's existing `className` string, e.g. change `` `rounded-xl border p-4 space-y-3 ${lowAgents ? ... }` `` to `` `rounded-xl border p-4 space-y-3 min-w-[200px] flex-1 basis-[220px] ${lowAgents ? ... }` ``.)

- [ ] **Step 4: Make each card clickable — selects that profile and switches to the Agents tab**

Find the returned card `<div>` for each profile (the one with `className={`rounded-xl border p-4 space-y-3 ${lowAgents ...}`}`. Add `onClick`, `role="button"`, `tabIndex={0}`, and a hover affordance:
```tsx
          <div
            key={profileId}
            onClick={() => onSelectProfile(profileId)}
            role="button"
            tabIndex={0}
            className={`rounded-xl border p-4 space-y-3 min-w-[200px] flex-1 basis-[220px] cursor-pointer transition-shadow hover:shadow-md ${lowAgents ? 'border-red-200 bg-red-50' : 'border-gray-200 bg-white'}`}
          >
```
(Keep whatever the actual current conditional class body is for the `lowAgents ? ... : ...` ternary — the snippet above shows the STRUCTURE to apply `onClick`/`role`/`tabIndex`/`cursor-pointer`/`hover:shadow-md` to, not a literal replacement of content you haven't read. Read the real current ternary before editing.)

- [ ] **Step 5: Wire the click handler and pass it down from the call site**

Find where `<AgentAvailabilitySidebar ... />` is rendered (inside `LiveView`):
```tsx
          <AgentAvailabilitySidebar
            agents={agentQuery.data?.agents ?? []}
            isLoading={agentQuery.isLoading}
            lastUpdated={agentQuery.data?.lastUpdated}
          />
```
This is inside `LiveView`, a different component from `BrandedMonitor` (the top-level component that owns `tab`/`setTab`/`agentsPreset`/`setAgentsPreset` from Steps 1-2). `LiveView` needs a new prop to bubble the click up. Find `LiveView`'s function signature:
```tsx
function LiveView({ date, onDateChange }: { date: string; onDateChange: (d: string) => void }): ReactNode {
```
Change to:
```tsx
function LiveView({ date, onDateChange, onSelectAgentProfile }: { date: string; onDateChange: (d: string) => void; onSelectAgentProfile: (profileId: string) => void }): ReactNode {
```
Then update the `<AgentAvailabilitySidebar .../>` call site inside `LiveView` to pass it through:
```tsx
          <AgentAvailabilitySidebar
            agents={agentQuery.data?.agents ?? []}
            isLoading={agentQuery.isLoading}
            lastUpdated={agentQuery.data?.lastUpdated}
            onSelectProfile={onSelectAgentProfile}
          />
```
Finally, find where `<LiveView .../>` itself is rendered inside the top-level `BrandedMonitor` component:
```tsx
      {tab === 'live'    && <LiveView    date={date} onDateChange={setDate} />}
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
        />
      )}
```

- [ ] **Step 6: Verify**

Run: `cd frontend && npx vitest run && npm run typecheck && npm run build`
Expected: same test count as before (no new pure functions in this task — all changes are presentational/wiring in already-untested JSX), clean typecheck/build.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/pages/BrandedMonitor.tsx
git commit -m "feat(frontend): scope Live Monitor's agent sidebar to branded teams, lay out horizontally, click-through to Agents tab"
```

---

### Task 6: `BrandedMonitor.tsx` — show campaign end time next to start time

**Files:**
- Modify: `frontend/src/pages/BrandedMonitor.tsx`

**Interfaces:** None — presentational only, reads an existing optional field (`completedAt`) already on `BrandedCampaignRecord`.

**Context:** `CampaignCard` currently shows a "Runtime" (elapsed duration) + "Contacts" 2-column row, but no absolute Start/End clock time anywhere on the card. `BrandedCampaignRecord.completedAt?: string` already exists (populated for completed/aborted/errored campaigns, absent while `RUNNING`). Confirmed with Sebastian: show Start time and End time (or "—" / "Running" while not yet completed) on each campaign's card.

- [ ] **Step 1: Add a Start/End row to `CampaignCard`**

Find, inside `CampaignCard`'s returned JSX, the "Runtime + contacts" block:
```tsx
      {/* Runtime + contacts */}
      {!compact && (isRunning || runtimeSec > 0) && (
        <div className="grid grid-cols-2 gap-3">
          <div>
            <div className="text-[10px] text-gray-400 uppercase font-semibold">Runtime</div>
            <div className="text-lg font-bold tabular-nums text-gray-900">{formatRuntime(runtimeSec)}</div>
          </div>
          <div>
            <div className="text-[10px] text-gray-400 uppercase font-semibold">Contacts</div>
            <div className="text-lg font-bold tabular-nums text-gray-900">{metric?.contactsPlaced ?? dialed}</div>
          </div>
        </div>
      )}
```
Add a new Start/End row immediately after it (still inside the `{!compact && ...}` guard's sibling content — i.e., add this as a new block right after the closing `)}` of the one above, at the same indentation level):
```tsx
      {!compact && (
        <div className="flex items-center gap-3 text-[11px] text-gray-500">
          <span>Start <span className="font-medium text-gray-700">{fmtTime(campaign.startedAt)}</span></span>
          <span>·</span>
          <span>End <span className="font-medium text-gray-700">{campaign.completedAt ? fmtTime(campaign.completedAt) : (isRunning ? 'Running' : '—')}</span></span>
        </div>
      )}
```
Add `fmtTime` to this file's imports from `@/lib/utils` — find the existing import line for `@/lib/utils` in this file (if one exists) and add `fmtTime` to it; if no such import exists yet, add a new line: `import { fmtTime } from '@/lib/utils';`. (This file already has its own local `formatTime`/`elapsedSeconds`/etc. per earlier work this session that migrated most of its OWN time helpers to `fmtTime` already — check whether `fmtTime` is already imported before adding a duplicate import line.)

- [ ] **Step 2: Verify**

Run: `cd frontend && npx vitest run && npm run typecheck && npm run build`
Expected: same test count as before, clean typecheck/build.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/pages/BrandedMonitor.tsx
git commit -m "feat(frontend): show campaign end time alongside start time on each campaign card"
```

---

### Task 7: `AgentAvailabilityPanel.tsx` — scope to Patient Access only, horizontal cards, click-through to Agent Roster

**Files:**
- Modify: `frontend/src/pages/AgentAvailabilityPanel.tsx`

**Interfaces:** None produced for later tasks — this is the last content-changing task in the plan.

**Context — 3 changes, confirmed with Sebastian:**
1. This compact widget (embedded in Campaign Monitor / `PlanDetail.tsx`) currently calls `getAgentRoster()` with no scope at all — it aggregates and shows EVERY routing profile in the whole Connect instance, across every team. Narrow it to show ONLY Patient Access (`patient-success` team) profiles — drop Appointment Services and every other team entirely.
2. Its cards currently stack vertically (`space-y-2`). Lay them out horizontally instead — same treatment as Task 5's sidebar.
3. Add a click-through: clicking anywhere on this panel navigates to Branded Monitor's Agents tab, pre-filtered to the Patient Access team (`/plans/branded-monitor?tab=agents&team=patient-success`).

- [ ] **Step 1: Filter to Patient Access only**

Find:
```tsx
import { StatTile } from '@/components/ui/StatTile';
import { api } from '@/lib/api';
import { aggregateByRoutingProfile } from '@/lib/agentRoster';
```
Change to:
```tsx
import { useNavigate } from 'react-router-dom';

import { StatTile } from '@/components/ui/StatTile';
import { api } from '@/lib/api';
import { aggregateByRoutingProfile } from '@/lib/agentRoster';
import { teamForProfile } from '@/lib/routingProfileTeams';
```

Find:
```tsx
  const rows = aggregateByRoutingProfile(query.data?.agents ?? []);
```
Change to:
```tsx
  const patientAccessAgents = (query.data?.agents ?? []).filter(
    (a) => teamForProfile(a.routingProfileName) === 'patient-success',
  );
  const rows = aggregateByRoutingProfile(patientAccessAgents);
```

- [ ] **Step 2: Horizontal card layout + click-through navigation**

Find:
```tsx
export function AgentAvailabilityPanel({
  active,
  className,
}: {
  active: boolean;
  className?: string;
}): ReactNode {
  const query = useQuery({
```
Change to:
```tsx
export function AgentAvailabilityPanel({
  active,
  className,
}: {
  active: boolean;
  className?: string;
}): ReactNode {
  const navigate = useNavigate();
  const query = useQuery({
```

Find:
```tsx
      {query.isError ? (
        <p className="text-xs text-red-500">Failed to load agent roster.</p>
      ) : query.isPending ? (
        <p className="text-xs text-gray-400">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="text-xs text-gray-400">No agents online.</p>
      ) : (
        <div className="space-y-2">
          {rows.map((row) => (
            <div key={row.routingProfileId} className="rounded-lg border border-gray-200 p-2.5">
```
Change to:
```tsx
      {query.isError ? (
        <p className="text-xs text-red-500">Failed to load agent roster.</p>
      ) : query.isPending ? (
        <p className="text-xs text-gray-400">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="text-xs text-gray-400">No agents online.</p>
      ) : (
        <div
          className="flex flex-wrap gap-2 cursor-pointer"
          role="button"
          tabIndex={0}
          onClick={() => navigate('/plans/branded-monitor?tab=agents&team=patient-success')}
          title="View in Agent Roster"
        >
          {rows.map((row) => (
            <div key={row.routingProfileId} className="rounded-lg border border-gray-200 p-2.5 min-w-[160px] flex-1 basis-[180px] transition-shadow hover:shadow-md">
```
(The closing `</div>` for the outer `space-y-2`-now-`flex flex-wrap` container and the per-row `</div>` are unchanged — only the two opening tags shown above changed; find and confirm the corresponding closing tags still balance correctly after this edit.)

- [ ] **Step 3: Verify**

Run: `cd frontend && npx vitest run && npm run typecheck && npm run build`
Expected: same test count as before (this file's only pure logic, `aggregateByRoutingProfile`, is untouched and already tested in `frontend/src/lib/agentRoster.test.ts` — the new `teamForProfile` filter is a one-line composition of two already-tested functions, not new logic requiring its own test), clean typecheck/build.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/AgentAvailabilityPanel.tsx
git commit -m "feat(frontend): scope Campaign Monitor's compact agent panel to Patient Access, horizontal layout, click-through to Agent Roster"
```

---

### Task 8: Full verification

**Files:** none (verification only)

- [ ] **Step 1: Full frontend suite**

Run: `cd frontend && npx vitest run`
Expected: all tests pass except the 3 known pre-existing, unrelated `chainMap.test.ts` failures (confirmed present since before this session's work began).

- [ ] **Step 2: Typecheck and build**

Run: `cd frontend && npm run typecheck && npm run build`
Expected: both clean.

- [ ] **Step 3: Manual smoke test**

Since `mockApi.ts`'s `brandedMonitor` stubs are empty (cannot exercise `getAgentRoster`/`getTodaySummary` with real data), use `VITE_PREVIEW_MODE=true` with a temporary, uncommitted patch to `frontend/src/lib/mockApi.ts`'s `brandedMonitor.getAgentRoster`/`getTodaySummary` (a realistic fixture: a few agents across 2-3 real Patient Access + Appointment Services routing profile names, a couple of running/completed branded campaigns) purely to render real data through the changed components — revert this patch (`git checkout -- frontend/src/lib/mockApi.ts`) immediately after the check, confirmed via `git status`. Run `cd frontend && npm run dev`, and via a browser (or Playwright), confirm:
- The app uses the full screen width on a wide viewport (Task 1).
- Clicking "Operations" or "Configuration" in the Plans sidebar collapses/expands that group (Task 2).
- On a Campaign Monitor plan with an active run, the timeline bar renders with completed segments visibly on top of any overlapping later segment (Task 3) — construct a fixture where a `time_based` bucket's projected end overlaps the next bucket's start to exercise this, since real day-to-day data may not naturally overlap.
- On Branded Monitor's Agents tab: Status/Team/Profile/Alerts are `<select>` dropdowns, not pill buttons; "Teams & routing profiles" appears right after the workforce summary tiles; it has a 5-color legend matching `StaffingBar`'s colors (Task 4).
- On Branded Monitor's Live Monitor tab: the "Agent availability" sidebar only shows Patient Access + Appointment Services routing profiles (not any other team, if your fixture includes one), cards lay out horizontally, and clicking a card switches to the Agents tab with that specific profile pre-filtered in the Profile dropdown (Task 5).
- Each campaign card on Live Monitor shows both a Start and an End (or "Running"/"—") time (Task 6).
- On Campaign Monitor's `PlanDetail` page, the compact "Agent availability" widget only shows Patient Access profiles, lays out horizontally, and clicking it navigates to `/plans/branded-monitor?tab=agents&team=patient-success` with the Agents tab's Team dropdown pre-set to Patient Access (Task 7).

## Self-Review Notes

- **Spec coverage:** all 8 items from Sebastian's confirmed list map 1:1 to a task (layout width → Task 1; collapsible menu → Task 2; timeline bar order → Task 3; dropdown filters + block reorder + legend → Task 4; sidebar scoping + horizontal + click-through → Task 5; campaign end time → Task 6; compact widget scoping + horizontal + click-through → Task 7).
- **Placeholder scan:** none found — every task has complete, runnable code or a precisely-described edit anchored to real existing code, with the one exception (Task 4's dropdown-wiring notes) being an explicit, bounded design decision left to the implementer ("prefer a single setter prop over composing two existing handlers") rather than an unresolved TBD — the decision criteria and fallback are both stated.
- **Type consistency:** `AgentRoster`'s new `initialTeamFilter`/`initialProfileFilter` props (Task 4) are consumed identically by Task 5 (`BrandedMonitor`'s `agentsPreset` state, sourced from URL params or a sidebar-card click) — both pass `string | undefined`, matching the prop types exactly. The `/plans/branded-monitor?tab=agents&team=<key>` URL contract is produced by Task 7 and consumed by Task 5's Step 1 — both use the literal query param names `tab`/`team`/`profile` and the literal team key `patient-success` (matching `TEAM_ROUTING_PROFILES`'s actual key in `routingProfileTeams.ts`, not a display label).
