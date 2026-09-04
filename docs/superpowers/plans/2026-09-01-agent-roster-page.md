# Agent Roster Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `BrandedMonitor.tsx`'s existing `AgentsView` (a plain sortable table) with a real Agent Roster page matching the `Agent Roster.dc.html` mockup: a workforce summary, a "needs attention" panel, a per-routing-profile capacity table, and a searchable/filterable, groupable agent list with live per-second timers.

**Architecture:** Extract the pure aggregation/alert/staffing logic that already exists (informally, scattered across `AgentAvailabilityPanel.tsx` and `BrandedMonitor.tsx`) into a new shared module `frontend/src/lib/agentRoster.ts`, generalize it to cover 4 alert types and a staffing-risk classification, then build the new page (`frontend/src/pages/AgentRoster.tsx`) on top of it. Swap it into `BrandedMonitor.tsx`'s existing `agents` tab — no new route, no new nav entry. A pre-existing backend pagination bug (silent 100-agent cap) is fixed first and independently, since any roster page built on top of it would report a wrong headcount.

**Tech Stack:** React 18 + TypeScript, TanStack Query, Tailwind (existing `components/ui/status.ts` tone system), Vitest, Python 3.12 Lambda (`services/api-metrics`), pytest.

**Spec:** `/mnt/c/Users/juansebastian/Downloads/Campaign Performance Dashboard (1)/Agent Roster.dc.html` (static mockup — read for structure/copy, not for its fictional per-profile numbers; see Global Constraints).

## Global Constraints

- **Scope: branded-dialer teams only.** Keep the existing `BRANDED_MONITOR_TEAMS = ['patient-success', 'appointment-services']` scoping from `frontend/src/lib/routingProfileTeams.ts:107`. Do not add the other 6 teams. (Confirmed with Sebastian 2026-09-01.)
- **`PS - *` routing profiles stay unclassified.** Do not add a new team for them and do not change `teamForProfile`'s exclusion of `PS - *` from `patient-success`, even though the mockup groups them there. Do not touch `frontend/src/lib/routingProfileTeams.ts` or its test at all in this plan. (Confirmed with Sebastian 2026-09-01.)
- **No "track" concept.** This page has no relationship to plan buckets/campaigns/DAGs — don't introduce one.
- **`@/components/ui` collision.** `frontend/src/components/ui.tsx` (file) and `frontend/src/components/ui/` (directory) both exist; the bare `@/components/ui` path always resolves to the file. Import every primitive used in this plan (`StatTile`, `StatusChip`, `Avatar`, `clampPercent`, `StatusTone`, `STATUS_TONE_CLASSES`) from its specific file (`@/components/ui/StatTile`, `@/components/ui/StatusChip`, `@/components/ui/Avatar`, `@/components/ui/status`), never from the bare path.
- **COT-only timezone.** No absolute clock times are shown on this page (only durations and "updated Ns ago" — timezone-independent). If any task is tempted to add an absolute time display, it must use `fmtTime` from `@/lib/utils`, never `toLocaleTimeString`/`Date.prototype.getHours`.
- **No icon library.** Plain Unicode (from common blocks only — Basic Latin, Latin-1 Supplement, Geometric Shapes, Dingbats `✓`/`↑`/`↓` are all confirmed safe) or raw inline SVG (`viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5"`, matching `frontend/src/pages/PlansLayout.tsx`'s existing icons). Never U+23xx "Miscellaneous Technical" glyphs (⏹⏭ already caused a real rendering bug this session) and never emoji-presentation characters (📊⚠️).
- **Placeholder staffing thresholds — not real data.** `MIN_AVAILABLE_BY_PROFILE` (Task 2) ships as an empty map with a `DEFAULT_MIN_AVAILABLE = 1` fallback. The mockup's per-profile numbers (rp1=2, rp2=2, etc.) are invented example data with no correspondence to this repo's real profile names — do not port them. Flag this to Sebastian as a follow-up once the page ships.
- **Presentational JSX is not unit-tested**, matching this repo's established convention (`PlanDetail.tsx`, `BrandedMonitor.tsx`, etc.) — only pure, exported functions get tests.

---

### Task 1: Backend — paginate `get_current_user_data` in `get_agent_roster`

**Files:**
- Modify: `services/api-metrics/src/handlers/branded.py:210-215`
- Test: `services/api-metrics/tests/unit/test_branded_roster.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `get_agent_roster` now returns every matching agent, not just the first 100. No response shape change — later tasks don't depend on anything new here beyond correctness.

**Context:** `get_agent_roster` (`branded.py:190-287`) calls `_connect.get_current_user_data(InstanceId=..., Filters=filters, MaxResults=100)` once and reads only `resp.get("UserDataList", [])` — `GetCurrentUserData` is a paginated Connect API that returns a `NextToken` when more results exist, and nothing here follows it. Any team with more than 100 matching agents silently loses the rest, with no error and no indication in the response. This bug predates this plan and affects the *existing* `AgentsView` too — fix it before building anything on top of it.

- [ ] **Step 1: Write the failing test**

Add this test class to `services/api-metrics/tests/unit/test_branded_roster.py` (append after `TestIntentionalAbsenceFlag`, using the same `_user_data`/`_mock_connect` helpers already in the file):

```python
class TestPagination:
    """GetCurrentUserData paginates via NextToken — a single unpaginated call
    silently drops agents past the first page.
    """

    def test_follows_next_token_across_pages(self):
        from handlers import branded

        page1 = {
            "UserDataList": [_user_data(user_id="u-1", status_arn="arn:.../agent-status/s-avail")],
            "NextToken": "token-2",
        }
        page2 = {
            "UserDataList": [_user_data(user_id="u-2", status_arn="arn:.../agent-status/s-avail")],
        }
        statuses = [{"Id": "s-avail", "Name": "Available", "Type": "ROUTABLE"}]

        mock = _mock_connect([], statuses)
        mock.get_current_user_data.side_effect = [page1, page2]

        with patch("handlers.branded._connect", mock):
            resp = branded.get_agent_roster({"queryStringParameters": {"queueId": "q-1"}}, {})

        body = json.loads(resp["body"])
        assert {a["agentId"] for a in body["agents"]} == {"u-1", "u-2"}
        assert mock.get_current_user_data.call_count == 2

    def test_second_call_passes_the_next_token(self):
        from handlers import branded

        page1 = {"UserDataList": [], "NextToken": "token-2"}
        page2 = {"UserDataList": []}
        statuses: list = []

        mock = _mock_connect([], statuses)
        mock.get_current_user_data.side_effect = [page1, page2]

        with patch("handlers.branded._connect", mock):
            branded.get_agent_roster({"queryStringParameters": {"queueId": "q-1"}}, {})

        second_call_kwargs = mock.get_current_user_data.call_args_list[1].kwargs
        assert second_call_kwargs["NextToken"] == "token-2"

    def test_single_page_response_still_works(self):
        """No NextToken in the response — the existing single-page behavior
        (every other test in this file) must not regress.
        """
        from handlers import branded

        ud = [_user_data(status_arn="arn:.../agent-status/s-avail")]
        statuses = [{"Id": "s-avail", "Name": "Available", "Type": "ROUTABLE"}]

        with patch("handlers.branded._connect", _mock_connect(ud, statuses)):
            resp = branded.get_agent_roster({"queryStringParameters": {"queueId": "q-1"}}, {})

        body = json.loads(resp["body"])
        assert len(body["agents"]) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd services/api-metrics && python3 -m pytest tests/unit/test_branded_roster.py -v`
Expected: the 3 new `TestPagination` tests FAIL — `test_follows_next_token_across_pages` and `test_second_call_passes_the_next_token` fail because `get_current_user_data` is currently only called once (asserting `call_count == 2` or checking `call_args_list[1]` fails with an `IndexError`); `test_single_page_response_still_works` passes already (that's fine — it exists to guard the fix, not to currently fail).

- [ ] **Step 3: Implement the pagination loop**

In `services/api-metrics/src/handlers/branded.py`, replace lines 209-266 (from `agents: list[dict] = []` through the end of the `try` block's `for ud in resp.get("UserDataList", []):` loop) with:

```python
    agents: list[dict] = []
    try:
        _CONNECTED = {"CONNECTED", "CONNECTED_ONHOLD", "INCOMING", "CONNECTING"}
        _ACW = {"ENDED"}
        now = _now()

        next_token: str | None = None
        while True:
            kwargs: dict = {
                "InstanceId": _CONNECT_INSTANCE_ID,
                "Filters": filters,
                "MaxResults": 100,
            }
            if next_token:
                kwargs["NextToken"] = next_token
            resp = _connect.get_current_user_data(**kwargs)

            for ud in resp.get("UserDataList", []):
                status = ud.get("Status", {})
                contacts = ud.get("Contacts", [])
                status_name = status.get("StatusName", "")
                status_type = _status_type_for_arn(status.get("StatusArn", ""))

                active = next((c for c in contacts if c.get("AgentContactState") in _CONNECTED), None)
                acw_candidate = next((c for c in contacts if c.get("AgentContactState") in _ACW), None)
                acw = None
                if acw_candidate:
                    started = _parse_ts(acw_candidate.get("StateStartTimestamp"))
                    if started and (now - started).total_seconds() <= _ACW_MAX_AGE_SECONDS:
                        acw = acw_candidate

                if active:
                    effective = "On Call"
                    effective_ts = active.get("StateStartTimestamp", status.get("StatusStartTimestamp", ""))
                elif acw:
                    effective = "ACW"
                    effective_ts = acw.get("StateStartTimestamp", status.get("StatusStartTimestamp", ""))
                elif status_type == "ROUTABLE":
                    effective = "Available"
                    effective_ts = status.get("StatusStartTimestamp", "")
                elif status_type == "OFFLINE":
                    effective = "Offline"
                    effective_ts = status.get("StatusStartTimestamp", "")
                else:
                    effective = "Unavailable"
                    effective_ts = status.get("StatusStartTimestamp", "")

                user_id = ud.get("User", {}).get("Id", "")
                rp_id = ud.get("RoutingProfile", {}).get("Id", "")
                agents.append({
                    "agentId": user_id,
                    "agentName": _agent_display_name(user_id),
                    "status": status_name,
                    "statusType": status_type,
                    "effectiveStatus": effective,
                    "statusStartTimestamp": str(effective_ts) if effective_ts else "",
                    "isIntentionalAbsence": status_name in _INTENTIONAL_ABSENCE_STATUSES,
                    "routingProfileId": rp_id,
                    "routingProfileName": _rp_name_cache.get(rp_id, rp_id),
                    "contactsCount": len(contacts),
                    "activeContactState": (active or acw or {}).get("AgentContactState", ""),
                })

            next_token = resp.get("NextToken")
            if not next_token:
                break
    except Exception as exc:
        logger.error("get_agent_roster: %s", type(exc).__name__)
        return _err(502, "Failed to fetch agent roster from Connect")
```

This is a pure control-flow change (the per-agent field derivation logic inside the `for` loop is byte-identical to what's there today) — only the outer call is now a `while True` / `NextToken` loop instead of a single call.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd services/api-metrics && python3 -m pytest tests/unit -v`
Expected: all tests in `test_branded_roster.py` pass (28 pre-existing + 3 new = 31), and the full `tests/unit` suite still shows 0 failures overall (28 passed before this task, per the 2026-08-31 baseline — confirm the new total).

- [ ] **Step 5: Commit**

```bash
git add services/api-metrics/src/handlers/branded.py services/api-metrics/tests/unit/test_branded_roster.py
git commit -m "fix(api-metrics): paginate GetCurrentUserData in get_agent_roster

Fixes a silent 100-agent cap — any team with more agents than that
lost the rest with no error. Affects the existing AgentsView too."
```

---

### Task 2: `lib/agentRoster.ts` — shared aggregation, alert, and staffing logic

**Files:**
- Create: `frontend/src/lib/agentRoster.ts`
- Create: `frontend/src/lib/agentRoster.test.ts`
- Modify: `frontend/src/lib/utils.ts` (add `elapsedSeconds`, `elapsedMinutes`, `formatRuntime`, `formatElapsed`)
- Modify: `frontend/src/pages/AgentAvailabilityPanel.tsx` (import `aggregateByRoutingProfile`/`RoutingProfileAvailability` from `@/lib/agentRoster` instead of defining locally)
- Modify: `frontend/src/pages/AgentAvailabilityPanel.test.ts` (replace with a minimal re-export sanity check; the real behavior tests move to `agentRoster.test.ts`)
- Modify: `frontend/src/pages/BrandedMonitor.tsx` (import the 4 relocated time helpers from `@/lib/utils` instead of the local copies at lines 20-45; delete the local copies)

**Interfaces:**
- Consumes: `AgentRosterEntry` from `@/lib/api` (unchanged).
- Produces (for Tasks 3-6): everything below, all from `frontend/src/lib/agentRoster.ts` unless noted.
  - `aggregateByRoutingProfile(agents: AgentRosterEntry[]): RoutingProfileAvailability[]`
  - `type AlertThresholds = { idleAlertMin: number; breakAlertMin: number; longCallMin: number; acwAlertMin: number }`
  - `DEFAULT_ALERT_THRESHOLDS: AlertThresholds`
  - `type AgentAlertKey = 'idle' | 'break' | 'longCall' | 'longAcw'`
  - `type AgentAlert = { key: AgentAlertKey; label: string; why: string; sev: 'warn' | 'error' }`
  - `agentAlert(agent: AgentRosterEntry, thresholds?: AlertThresholds, nowMs?: number): AgentAlert | null`
  - `type StaffingRisk = 'no-coverage' | 'understaffed' | 'at-minimum' | 'healthy'`
  - `type StaffingStatus = { risk: StaffingRisk; label: string; tone: StatusTone }`
  - `STAFFING_RISK_ORDER: Record<StaffingRisk, number>`
  - `classifyStaffing(available: number, min: number): StaffingStatus`
  - `DEFAULT_MIN_AVAILABLE: number`, `MIN_AVAILABLE_BY_PROFILE: Record<string, number>`, `minAvailableFor(routingProfileName: string): number`
  - `agentStatusTone(effectiveStatus: AgentRosterEntry['effectiveStatus']): StatusTone`
  - From `@/lib/utils`: `elapsedSeconds(iso: string, nowMs?: number): number`, `elapsedMinutes(iso: string, nowMs?: number): number`, `formatRuntime(seconds: number): string`, `formatElapsed(minutes: number): string`

- [ ] **Step 1: Move the 4 time helpers into `lib/utils.ts`, with an added optional `nowMs` param**

Open `frontend/src/lib/utils.ts` and add these 4 functions (place them after `startTimeIso`, before `today9pmNYIso` if that function exists, otherwise at the end of the file):

```ts
/** Seconds elapsed since `iso`, relative to `nowMs` (defaults to the real clock). */
export function elapsedSeconds(iso: string, nowMs: number = Date.now()): number {
  return Math.max(0, Math.floor((nowMs - new Date(iso).getTime()) / 1000));
}

/** Whole minutes elapsed since `iso`, relative to `nowMs` (defaults to the real clock). */
export function elapsedMinutes(iso: string, nowMs: number = Date.now()): number {
  return Math.floor(elapsedSeconds(iso, nowMs) / 60);
}

/** `seconds` as "M:SS", or "H:MM:SS" once past an hour. */
export function formatRuntime(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  return `${m}:${String(s).padStart(2, '0')}`;
}

/** `minutes` as "Nm", or "Hh" / "Hh Mm" once past an hour. */
export function formatElapsed(minutes: number): string {
  if (minutes < 60) return `${minutes}m`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m === 0 ? `${h}h` : `${h}h ${m}m`;
}
```

Now open `frontend/src/pages/BrandedMonitor.tsx` and:
1. Delete the local `elapsedSeconds` (lines 20-22), `elapsedMinutes` (24-26), `formatRuntime` (28-34), `formatElapsed` (40-45) function definitions.
2. Add `elapsedSeconds, elapsedMinutes, formatRuntime, formatElapsed, fmtTime` to an import from `@/lib/utils` near the top of the file (there may not be an existing `@/lib/utils` import in this file — add one: `import { elapsedSeconds, elapsedMinutes, formatRuntime, formatElapsed, fmtTime } from '@/lib/utils';`).
3. Delete the local `formatTime` function (lines 36-38) and replace its 3 call sites (`formatTime(group.startedAt)` ×2, `formatTime(c.startedAt)` ×1 — confirm exact locations with `grep -n "formatTime(" frontend/src/pages/BrandedMonitor.tsx` before editing, since line numbers shift once the earlier deletions land) with `fmtTime(group.startedAt)` / `fmtTime(c.startedAt)`.

This step touches `BrandedMonitor.tsx` but changes no behavior except fixing the `formatTime`→`fmtTime` COT bug (previously `toLocaleTimeString('es-CO', ...)`, browser-timezone-dependent; now correctly COT-fixed via `fmtTime`). Every other call site keeps its exact existing behavior — `elapsedSeconds`/`elapsedMinutes` gain an optional second parameter that every existing call site simply doesn't pass (defaults to `Date.now()`, unchanged behavior).

- [ ] **Step 2: Run the frontend suite to confirm this relocation alone is safe**

Run: `cd frontend && npx vitest run && npm run typecheck`
Expected: same 97 passed / 3 pre-existing unrelated `chainMap.test.ts` failures as the 2026-08-31 baseline; typecheck clean. (`AgentAvailabilityPanel.tsx` still defines `aggregateByRoutingProfile` locally at this point — Step 1 only touched `BrandedMonitor.tsx`/`lib/utils.ts`.)

- [ ] **Step 3: Write the failing tests for the new pure functions**

Create `frontend/src/lib/agentRoster.test.ts`:

```ts
import { describe, expect, it } from 'vitest';

import type { AgentRosterEntry } from './api';
import {
  aggregateByRoutingProfile,
  agentAlert,
  agentStatusTone,
  classifyStaffing,
  DEFAULT_ALERT_THRESHOLDS,
  minAvailableFor,
} from './agentRoster';

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

const T0 = new Date('2026-09-01T12:00:00.000Z').getTime();

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
      routingProfileName: 'Outbound', available: 1, onCall: 1, acw: 0, offline: 0, unavailable: 0, total: 2,
    });
  });

  it('sorts profiles by available count ascending (most understaffed first)', () => {
    const agents = [
      agent({ agentId: 'a1', routingProfileId: 'rp1', routingProfileName: 'Well staffed', effectiveStatus: 'Available' }),
      agent({ agentId: 'a2', routingProfileId: 'rp1', routingProfileName: 'Well staffed', effectiveStatus: 'Available' }),
      agent({ agentId: 'a3', routingProfileId: 'rp2', routingProfileName: 'Understaffed', effectiveStatus: 'On Call' }),
    ];
    const result = aggregateByRoutingProfile(agents);
    expect(result[0]!.routingProfileName).toBe('Understaffed');
  });

  it('returns an empty array for no agents', () => {
    expect(aggregateByRoutingProfile([])).toEqual([]);
  });
});

describe('agentAlert', () => {
  it('flags an Available agent past the idle threshold as idle/warn', () => {
    const a = agent({ effectiveStatus: 'Available', statusStartTimestamp: new Date(T0 - 11 * 60_000).toISOString() });
    expect(agentAlert(a, DEFAULT_ALERT_THRESHOLDS, T0)).toEqual({ key: 'idle', label: 'Idle', why: 'No calls routed', sev: 'warn' });
  });

  it('does not flag an Available agent under the idle threshold', () => {
    const a = agent({ effectiveStatus: 'Available', statusStartTimestamp: new Date(T0 - 5 * 60_000).toISOString() });
    expect(agentAlert(a, DEFAULT_ALERT_THRESHOLDS, T0)).toBeNull();
  });

  it('flags an Unavailable (non-intentional) agent past the break threshold as break/error', () => {
    const a = agent({
      effectiveStatus: 'Unavailable', isIntentionalAbsence: false,
      statusStartTimestamp: new Date(T0 - 21 * 60_000).toISOString(),
    });
    expect(agentAlert(a, DEFAULT_ALERT_THRESHOLDS, T0)).toMatchObject({ key: 'break', sev: 'error' });
  });

  it('never flags an intentional absence, no matter how long', () => {
    const a = agent({
      effectiveStatus: 'Unavailable', isIntentionalAbsence: true,
      statusStartTimestamp: new Date(T0 - 999 * 60_000).toISOString(),
    });
    expect(agentAlert(a, DEFAULT_ALERT_THRESHOLDS, T0)).toBeNull();
  });

  it('flags an On Call agent past the long-call threshold as longCall/warn', () => {
    const a = agent({ effectiveStatus: 'On Call', statusStartTimestamp: new Date(T0 - 13 * 60_000).toISOString() });
    expect(agentAlert(a, DEFAULT_ALERT_THRESHOLDS, T0)).toMatchObject({ key: 'longCall', sev: 'warn' });
  });

  it('flags an ACW agent past the long-wrap-up threshold as longAcw/warn', () => {
    const a = agent({ effectiveStatus: 'ACW', statusStartTimestamp: new Date(T0 - 4 * 60_000).toISOString() });
    expect(agentAlert(a, DEFAULT_ALERT_THRESHOLDS, T0)).toMatchObject({ key: 'longAcw', sev: 'warn' });
  });

  it('never flags Offline agents', () => {
    const a = agent({ effectiveStatus: 'Offline', statusStartTimestamp: new Date(T0 - 999 * 60_000).toISOString() });
    expect(agentAlert(a, DEFAULT_ALERT_THRESHOLDS, T0)).toBeNull();
  });

  it('respects custom thresholds', () => {
    const a = agent({ effectiveStatus: 'Available', statusStartTimestamp: new Date(T0 - 3 * 60_000).toISOString() });
    expect(agentAlert(a, { ...DEFAULT_ALERT_THRESHOLDS, idleAlertMin: 2 }, T0)).toMatchObject({ key: 'idle' });
  });
});

describe('classifyStaffing', () => {
  it('is "no-coverage"/danger when 0 are available, regardless of min', () => {
    expect(classifyStaffing(0, 3)).toEqual({ risk: 'no-coverage', label: 'No coverage', tone: 'danger' });
  });

  it('is "understaffed"/danger when available is below min', () => {
    expect(classifyStaffing(1, 3)).toEqual({ risk: 'understaffed', label: 'Understaffed', tone: 'danger' });
  });

  it('is "at-minimum"/warning when available equals min exactly', () => {
    expect(classifyStaffing(2, 2)).toEqual({ risk: 'at-minimum', label: 'At minimum', tone: 'warning' });
  });

  it('is "healthy"/success when available exceeds min', () => {
    expect(classifyStaffing(5, 2)).toEqual({ risk: 'healthy', label: 'Healthy', tone: 'success' });
  });
});

describe('minAvailableFor', () => {
  it('falls back to DEFAULT_MIN_AVAILABLE for any profile not in the map', () => {
    expect(minAvailableFor('Some Profile Nobody Configured')).toBe(1);
  });
});

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

- [ ] **Step 4: Run the tests to verify they fail**

Run: `cd frontend && npx vitest run src/lib/agentRoster.test.ts`
Expected: FAIL — `agentRoster.ts` does not exist yet ("Failed to resolve import").

- [ ] **Step 5: Implement `frontend/src/lib/agentRoster.ts`**

```ts
import type { AgentRosterEntry } from './api';
import { elapsedMinutes } from './utils';
import type { StatusTone } from '@/components/ui/status';

// ── Per-routing-profile aggregation ─────────────────────────────────────────

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

// ── Per-agent alerts ─────────────────────────────────────────────────────────

export type AlertThresholds = {
  idleAlertMin: number;
  breakAlertMin: number;
  longCallMin: number;
  acwAlertMin: number;
};

export const DEFAULT_ALERT_THRESHOLDS: AlertThresholds = {
  idleAlertMin: 10,
  breakAlertMin: 20,
  longCallMin: 12,
  acwAlertMin: 3,
};

export type AgentAlertKey = 'idle' | 'break' | 'longCall' | 'longAcw';

export type AgentAlert = {
  key: AgentAlertKey;
  label: string;
  why: string;
  sev: 'warn' | 'error';
};

/** Returns the single highest-priority alert for this agent's current status, or null. */
export function agentAlert(
  agent: AgentRosterEntry,
  thresholds: AlertThresholds = DEFAULT_ALERT_THRESHOLDS,
  nowMs: number = Date.now(),
): AgentAlert | null {
  const elapsed = elapsedMinutes(agent.statusStartTimestamp, nowMs);
  if (agent.effectiveStatus === 'Available' && elapsed > thresholds.idleAlertMin) {
    return { key: 'idle', label: 'Idle', why: 'No calls routed', sev: 'warn' };
  }
  if (agent.effectiveStatus === 'Unavailable' && !agent.isIntentionalAbsence && elapsed > thresholds.breakAlertMin) {
    return { key: 'break', label: 'Extended break', why: 'Extended break', sev: 'error' };
  }
  if (agent.effectiveStatus === 'On Call' && elapsed > thresholds.longCallMin) {
    return { key: 'longCall', label: 'Long call', why: 'Long call', sev: 'warn' };
  }
  if (agent.effectiveStatus === 'ACW' && elapsed > thresholds.acwAlertMin) {
    return { key: 'longAcw', label: 'Long wrap-up', why: 'Long wrap-up', sev: 'warn' };
  }
  return null;
}

// ── Staffing risk ────────────────────────────────────────────────────────────

export type StaffingRisk = 'no-coverage' | 'understaffed' | 'at-minimum' | 'healthy';

export type StaffingStatus = {
  risk: StaffingRisk;
  label: string;
  tone: StatusTone;
};

export const STAFFING_RISK_ORDER: Record<StaffingRisk, number> = {
  'no-coverage': 0,
  understaffed: 1,
  'at-minimum': 2,
  healthy: 3,
};

export function classifyStaffing(available: number, min: number): StaffingStatus {
  if (available === 0) return { risk: 'no-coverage', label: 'No coverage', tone: 'danger' };
  if (available < min) return { risk: 'understaffed', label: 'Understaffed', tone: 'danger' };
  if (available === min) return { risk: 'at-minimum', label: 'At minimum', tone: 'warning' };
  return { risk: 'healthy', label: 'Healthy', tone: 'success' };
}

/**
 * Per-routing-profile minimum staffing level. Empty by default — no real
 * thresholds have been set for any profile yet. Every profile falls back to
 * DEFAULT_MIN_AVAILABLE until these are tuned against real staffing needs.
 */
export const MIN_AVAILABLE_BY_PROFILE: Record<string, number> = {};
export const DEFAULT_MIN_AVAILABLE = 1;

export function minAvailableFor(routingProfileName: string): number {
  return MIN_AVAILABLE_BY_PROFILE[routingProfileName] ?? DEFAULT_MIN_AVAILABLE;
}

// ── Status → tone ────────────────────────────────────────────────────────────

const AGENT_STATUS_TONE: Record<AgentRosterEntry['effectiveStatus'], StatusTone> = {
  Available: 'success',
  'On Call': 'info',
  ACW: 'acw',
  Unavailable: 'warning',
  Offline: 'neutral',
};

export function agentStatusTone(effectiveStatus: AgentRosterEntry['effectiveStatus']): StatusTone {
  return AGENT_STATUS_TONE[effectiveStatus];
}
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd frontend && npx vitest run src/lib/agentRoster.test.ts`
Expected: all pass.

- [ ] **Step 7: Update `AgentAvailabilityPanel.tsx` to import from the new shared module**

In `frontend/src/pages/AgentAvailabilityPanel.tsx`, delete the local `RoutingProfileAvailability` type and `aggregateByRoutingProfile` function (lines 7-54), and add an import:

```ts
import { aggregateByRoutingProfile } from '@/lib/agentRoster';
```

The rest of the file (the `AgentAvailabilityPanel` component itself) is unchanged — it already just calls `aggregateByRoutingProfile(query.data?.agents ?? [])`.

- [ ] **Step 8: Replace `AgentAvailabilityPanel.test.ts` with a minimal re-export check**

Replace the entire contents of `frontend/src/pages/AgentAvailabilityPanel.test.ts` with:

```ts
import { describe, expect, it } from 'vitest';

import { aggregateByRoutingProfile } from '@/lib/agentRoster';

// Full behavior coverage for aggregateByRoutingProfile now lives in
// src/lib/agentRoster.test.ts (it moved there in the same commit this
// component started importing it from lib/agentRoster instead of defining
// it locally). This file keeps one sanity check that the import path
// AgentAvailabilityPanel.tsx actually uses is wired correctly.
describe('AgentAvailabilityPanel — aggregateByRoutingProfile import', () => {
  it('is importable from @/lib/agentRoster and behaves correctly', () => {
    expect(aggregateByRoutingProfile([])).toEqual([]);
  });
});
```

- [ ] **Step 9: Run the full frontend suite**

Run: `cd frontend && npx vitest run && npm run typecheck && npm run build`
Expected: typecheck/build clean. Test count: the 4 tests removed from `AgentAvailabilityPanel.test.ts` (net: -3, since 1 remains) are replaced by ~16 new tests in `agentRoster.test.ts` — same 3 pre-existing unrelated `chainMap.test.ts` failures, net test count up by roughly 12-13 versus the 2026-08-31 baseline of 97 passed. Confirm the exact number in your report rather than assuming.

- [ ] **Step 10: Commit**

```bash
git add frontend/src/lib/agentRoster.ts frontend/src/lib/agentRoster.test.ts frontend/src/lib/utils.ts frontend/src/pages/AgentAvailabilityPanel.tsx frontend/src/pages/AgentAvailabilityPanel.test.ts frontend/src/pages/BrandedMonitor.tsx
git commit -m "refactor(frontend): extract shared agent-roster logic to lib/agentRoster.ts

Generalizes the existing idle/break-only alert check to 4 alert types
and adds staffing-risk classification, in prep for a full Agent Roster
page. Also relocates elapsedSeconds/elapsedMinutes/formatRuntime/
formatElapsed to lib/utils.ts and fixes BrandedMonitor's formatTime
COT-timezone bug (toLocaleTimeString -> fmtTime) along the way."
```

---

### Task 3: `AgentRoster.tsx` Part A — page shell, workforce summary, capacity table

**Files:**
- Create: `frontend/src/pages/AgentRoster.tsx`

**Interfaces:**
- Consumes: `aggregateByRoutingProfile`, `classifyStaffing`, `minAvailableFor`, `STAFFING_RISK_ORDER`, `agentAlert`, `DEFAULT_ALERT_THRESHOLDS`, `agentStatusTone` (all from `@/lib/agentRoster`, Task 2); `elapsedMinutes`, `elapsedSeconds` (from `@/lib/utils`, Task 2); `api.brandedMonitor.getAgentRoster` (existing, `@/lib/api`); `BRANDED_MONITOR_TEAMS`, `TEAM_LABELS`, `teamForProfile` (existing, `@/lib/routingProfileTeams`); `StatTile` (`@/components/ui/StatTile`), `StatusChip` (`@/components/ui/StatusChip`).
- Produces (for Task 4/5): the exported `AgentRoster` component this task starts, plus a page-local `useNowTick()` hook and `nowMs` value that Tasks 4-5 read for elapsed-time display; a page-local `filteredAgents` list computed from the raw query data that Task 4's controls narrow and Task 5 renders.

**Context:** This task builds the top of the page — the query, the live "ticking clock" (independent of data refetch, per the mockup's 1-second timer requirement), the 6-tile workforce summary, and the "Teams & routing profiles" capacity table. Tasks 4 and 5 build the rest of the same file (needs-attention panel + controls, then the agent list) — all three tasks touch the same new file, in sequence, so each one's diff is additive to the previous.

- [ ] **Step 1: Create the file with the query, ticking clock, and page shell**

```tsx
import { type ReactNode, useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import { StatTile } from '@/components/ui/StatTile';
import { StatusChip } from '@/components/ui/StatusChip';
import { api, type AgentRosterEntry } from '@/lib/api';
import {
  aggregateByRoutingProfile,
  classifyStaffing,
  minAvailableFor,
  STAFFING_RISK_ORDER,
  type RoutingProfileAvailability,
} from '@/lib/agentRoster';
import { elapsedMinutes } from '@/lib/utils';
import { BRANDED_MONITOR_TEAMS, teamForProfile } from '@/lib/routingProfileTeams';

/** Ticks every second so elapsed-time displays (M:SS timers, alert thresholds)
 * update live without waiting for the next data refetch. */
function useNowTick(): number {
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  return nowMs;
}

export function AgentRoster(): ReactNode {
  const nowMs = useNowTick();

  const query = useQuery({
    queryKey: ['agent-roster', 'all'],
    queryFn: () => api.brandedMonitor.getAgentRoster(),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  const allAgents: AgentRosterEntry[] = query.data?.agents ?? [];
  const agents = allAgents.filter(
    (a) => (BRANDED_MONITOR_TEAMS as readonly string[]).includes(teamForProfile(a.routingProfileName) ?? ''),
  );
  const lastUpdated = query.data?.lastUpdated;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold text-gray-900">Agent roster</h1>
        <p className="text-xs text-gray-500 mt-0.5">
          Live status by team and routing profile — routing profile determines which calls an agent answers.
        </p>
      </div>

      {query.isError && (
        <p className="text-sm text-red-500">Failed to load agent roster.</p>
      )}

      {!query.isError && (
        <>
          <WorkforceSummary agents={agents} nowMs={nowMs} />
          <CapacityTable agents={agents} />
        </>
      )}

      {lastUpdated && !query.isLoading && (
        <div className="text-[11px] text-gray-400">
          updated {elapsedMinutes(lastUpdated, nowMs) === 0 ? 'just now' : `${elapsedMinutes(lastUpdated, nowMs)}m ago`}
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Add `WorkforceSummary`**

Append to the same file:

```tsx
function WorkforceSummary({ agents, nowMs }: { agents: AgentRosterEntry[]; nowMs: number }): ReactNode {
  const total       = agents.length;
  const offline     = agents.filter((a) => a.effectiveStatus === 'Offline').length;
  const unavailable = agents.filter((a) => a.effectiveStatus === 'Unavailable').length;
  const online      = total - offline;
  const available   = agents.filter((a) => a.effectiveStatus === 'Available').length;
  const onCall      = agents.filter((a) => a.effectiveStatus === 'On Call').length;
  const acw         = agents.filter((a) => a.effectiveStatus === 'ACW').length;
  const flagged     = agents.filter((a) => agentAlert(a, DEFAULT_ALERT_THRESHOLDS, nowMs) !== null).length;

  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
      <StatTile label="Agents online" value={online} valueClassName="text-gray-900" />
      <StatTile
        label="Available"
        value={available}
        valueClassName={available === 0 ? 'text-status-danger-fg' : 'text-status-success-fg'}
      />
      <StatTile label="On call" value={onCall} valueClassName="text-status-info-fg" />
      <StatTile label="After-call work" value={acw} valueClassName="text-status-acw-fg" />
      <StatTile label="Away / offline" value={offline + unavailable} valueClassName="text-gray-900" />
      <StatTile
        label="Needs attention"
        value={flagged}
        valueClassName={flagged > 0 ? 'text-status-warning-fg' : 'text-status-success-fg'}
      />
    </div>
  );
}
```

Add the missing import at the top of the file (edit the existing `@/lib/agentRoster` import line from Step 1 to include these two names):

```ts
  agentAlert,
  DEFAULT_ALERT_THRESHOLDS,
```

- [ ] **Step 3: Add `CapacityTable`**

Append to the same file:

```tsx
function CapacityTable({ agents }: { agents: AgentRosterEntry[] }): ReactNode {
  const rows: RoutingProfileAvailability[] = aggregateByRoutingProfile(agents);
  const withStaffing = rows
    .map((row) => ({ row, staffing: classifyStaffing(row.available, minAvailableFor(row.routingProfileName)) }))
    .sort((a, b) => STAFFING_RISK_ORDER[a.staffing.risk] - STAFFING_RISK_ORDER[b.staffing.risk]);

  if (withStaffing.length === 0) {
    return null;
  }

  return (
    <div className="rounded-xl border border-gray-200 bg-white overflow-hidden">
      <div className="px-4 py-2.5 border-b border-gray-100">
        <h2 className="text-sm font-semibold text-gray-800">Teams &amp; routing profiles</h2>
      </div>
      <div className="divide-y divide-gray-100">
        {withStaffing.map(({ row, staffing }) => {
          const min = minAvailableFor(row.routingProfileName);
          const team = teamForProfile(row.routingProfileName);
          return (
            <div
              key={row.routingProfileId}
              className={`px-4 py-3 flex items-center gap-4 ${
                staffing.risk === 'no-coverage' || staffing.risk === 'understaffed' ? 'bg-red-50/40' : ''
              }`}
            >
              <div className="min-w-[200px] flex-1">
                <div className="text-sm font-medium text-gray-800">{row.routingProfileName}</div>
                {team && <div className="text-[11px] text-gray-400">{TEAM_LABELS[team] ?? team}</div>}
              </div>
              <div className="w-14 text-center text-sm tabular-nums text-gray-600">{row.total}</div>
              <div className="flex-1 min-w-[140px]">
                <StaffingBar row={row} />
                <div className="text-[10px] text-gray-400 mt-0.5">min {min} available</div>
              </div>
              <div className="w-12 text-center text-sm tabular-nums text-status-success-fg">{row.available}</div>
              <div className="w-12 text-center text-sm tabular-nums text-status-info-fg">{row.onCall}</div>
              <div className="w-12 text-center text-sm tabular-nums text-status-acw-fg">{row.acw}</div>
              <div className="w-12 text-center text-sm tabular-nums text-gray-500">{row.offline + row.unavailable}</div>
              <div className="w-[116px] flex justify-end">
                <StatusChip tone={staffing.tone} label={staffing.label} />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** 5-segment stacked bar (available/onCall/acw/unavailable/offline), proportional to `row.total`. */
function StaffingBar({ row }: { row: RoutingProfileAvailability }): ReactNode {
  const segments: { count: number; className: string }[] = [
    { count: row.available,   className: 'bg-status-success-bar' },
    { count: row.onCall,      className: 'bg-status-info-bar' },
    { count: row.acw,         className: 'bg-status-acw-bar' },
    { count: row.unavailable, className: 'bg-status-warning-bar' },
    { count: row.offline,     className: 'bg-status-neutral-bar' },
  ];
  return (
    <div className="h-2 w-full rounded-full bg-gray-100 overflow-hidden flex">
      {segments.map((seg, i) =>
        seg.count > 0 ? (
          <div
            key={i}
            className={seg.className}
            style={{ width: `${row.total > 0 ? (seg.count / row.total) * 100 : 0}%` }}
          />
        ) : null,
      )}
    </div>
  );
}
```

Add `TEAM_LABELS` to the existing `@/lib/routingProfileTeams` import from Step 1.

Import `AgentRosterEntry` is already present from Step 1; no other new imports needed here.

- [ ] **Step 4: Verify it typechecks and renders without crashing**

Run: `cd frontend && npm run typecheck`
Expected: clean. (No test file for this task yet — `AgentRoster` isn't wired into any route until Task 6, and it has no pure exported functions yet; Task 5 adds the pure, tested functions.)

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/AgentRoster.tsx
git commit -m "feat(frontend): add AgentRoster page shell, workforce summary, capacity table"
```

---

### Task 4: `AgentRoster.tsx` Part B — needs-attention panel and controls

**Files:**
- Modify: `frontend/src/pages/AgentRoster.tsx` (same file Task 3 created)

**Interfaces:**
- Consumes: everything Task 3 produced in this file, plus `Avatar`/`initialsFromName` from `@/components/ui/Avatar`, `agentStatusTone` from `@/lib/agentRoster`.
- Produces (for Task 5): an `AgentRosterFilters` state shape (search text, status filter set, team filter, routing-profile filter set, alert filter, group-by-profile toggle) that Task 5's agent list reads to decide what to render. Exact shape:
  ```ts
  type AlertFilter = 'all' | 'any' | AgentAlertKey;
  ```
  (state variables themselves — `search`, `statusFilters`, `teamFilter`, `rpFilters`, `alertFilter`, `groupByProfile` — declared inside `AgentRoster` in this task, read by Task 5's rendering, which lives in the same component function).

**Context:** This is the "Needs attention" card grid (mockup section 4) plus the filter/search control bar (mockup section 3, simplified: this repo already has a working row-of-buttons multi-select pattern for team/routing-profile filtering in the `AgentsView` being replaced — reuse that pattern instead of building the mockup's popover-with-scrim UI, which is materially more code for the same practical effect at this page's 2-team scope). Task 5 wires the resulting filter state into the actual agent list.

- [ ] **Step 1: Add filter state and the control bar to `AgentRoster`**

In `frontend/src/pages/AgentRoster.tsx`, inside the `AgentRoster` component function (from Task 3), add state right after the `useNowTick()`/`useQuery` lines:

```tsx
  const [search, setSearch] = useState('');
  const [statusFilters, setStatusFilters] = useState<Set<AgentRosterEntry['effectiveStatus']>>(new Set());
  const [teamFilter, setTeamFilter] = useState<string | null>(null);
  const [rpFilters, setRpFilters] = useState<Set<string>>(new Set());
  const [alertFilter, setAlertFilter] = useState<AlertFilter>('all');
  const [groupByProfile, setGroupByProfile] = useState(true);
```

Add the `AlertFilter` type near the top of the file, after the imports:

```ts
type AlertFilter = 'all' | 'any' | AgentAlertKey;
```

Add `AgentAlertKey` to the `@/lib/agentRoster` import (Step 2 of Task 3 already added `agentAlert`/`DEFAULT_ALERT_THRESHOLDS` there — extend the same import line with the type).

Then, still inside `AgentRoster`, right before the `return (`, add the filtering logic (this reads `agents`, the team/branded-scoped array already computed in Task 3's Step 1):

```tsx
  function toggleStatus(status: AgentRosterEntry['effectiveStatus']) {
    setStatusFilters((prev) => {
      const next = new Set(prev);
      if (next.has(status)) next.delete(status); else next.add(status);
      return next;
    });
  }

  function toggleRp(id: string) {
    setRpFilters((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  function clearFilters() {
    setSearch('');
    setStatusFilters(new Set());
    setTeamFilter(null);
    setRpFilters(new Set());
    setAlertFilter('all');
  }

  const searchLower = search.trim().toLowerCase();
  const filteredAgents = agents.filter((a) => {
    if (statusFilters.size > 0 && !statusFilters.has(a.effectiveStatus)) return false;
    const team = teamForProfile(a.routingProfileName);
    if (teamFilter !== null && team !== teamFilter) return false;
    if (rpFilters.size > 0 && !rpFilters.has(a.routingProfileId)) return false;
    if (alertFilter !== 'all') {
      const alert = agentAlert(a, DEFAULT_ALERT_THRESHOLDS, nowMs);
      if (alertFilter === 'any' && alert === null) return false;
      if (alertFilter !== 'any' && alert?.key !== alertFilter) return false;
    }
    if (searchLower) {
      const teamLabel = team ? TEAM_LABELS[team] ?? '' : '';
      const haystack = `${a.agentName} ${a.routingProfileName} ${teamLabel}`.toLowerCase();
      if (!haystack.includes(searchLower)) return false;
    }
    return true;
  });

  const hasActiveFilters = search !== '' || statusFilters.size > 0 || teamFilter !== null || rpFilters.size > 0 || alertFilter !== 'all';
  const flaggedInScope = agents.filter((a) => agentAlert(a, DEFAULT_ALERT_THRESHOLDS, nowMs) !== null);
```

- [ ] **Step 2: Render the control bar**

Insert this JSX in the `return (...)` block from Task 3, right after the `<WorkforceSummary .../>` line (before `<CapacityTable .../>`):

```tsx
          <ControlBar
            search={search}
            onSearch={setSearch}
            statusFilters={statusFilters}
            onToggleStatus={toggleStatus}
            statusCounts={agents.reduce<Record<string, number>>((acc, a) => {
              acc[a.effectiveStatus] = (acc[a.effectiveStatus] ?? 0) + 1;
              return acc;
            }, {})}
            alertFilter={alertFilter}
            onAlertFilter={setAlertFilter}
            flaggedCount={flaggedInScope.length}
            groupByProfile={groupByProfile}
            onToggleGroup={() => setGroupByProfile((v) => !v)}
            filteredCount={filteredAgents.length}
            totalCount={agents.length}
            hasActiveFilters={hasActiveFilters}
            onClearFilters={clearFilters}
          />
          <NeedsAttentionPanel agents={flaggedInScope} nowMs={nowMs} onAlertKeyClick={setAlertFilter} />
```

Append the two new components to the end of the file:

```tsx
const EFFECTIVE_STATUSES: AgentRosterEntry['effectiveStatus'][] = ['Available', 'On Call', 'ACW', 'Unavailable', 'Offline'];
const STATUS_LABELS: Record<AgentRosterEntry['effectiveStatus'], string> = {
  Available: 'Available',
  'On Call': 'On Call',
  ACW: 'ACW',
  Unavailable: 'Away',
  Offline: 'Offline',
};

function FilterBtn({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }): ReactNode {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`px-3 py-1 rounded-lg text-xs font-medium transition-colors ${
        active ? 'bg-amber-100 text-amber-800' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
      }`}
    >
      {children}
    </button>
  );
}

function ControlBar({
  search, onSearch,
  statusFilters, onToggleStatus, statusCounts,
  alertFilter, onAlertFilter, flaggedCount,
  groupByProfile, onToggleGroup,
  filteredCount, totalCount, hasActiveFilters, onClearFilters,
}: {
  search: string; onSearch: (v: string) => void;
  statusFilters: Set<AgentRosterEntry['effectiveStatus']>; onToggleStatus: (s: AgentRosterEntry['effectiveStatus']) => void;
  statusCounts: Record<string, number>;
  alertFilter: AlertFilter; onAlertFilter: (f: AlertFilter) => void; flaggedCount: number;
  groupByProfile: boolean; onToggleGroup: () => void;
  filteredCount: number; totalCount: number; hasActiveFilters: boolean; onClearFilters: () => void;
}): ReactNode {
  return (
    <div className="bg-white rounded-xl border border-gray-200 p-4 space-y-3">
      <div className="flex items-center gap-3 flex-wrap">
        <input
          type="text"
          value={search}
          onChange={(e) => onSearch(e.target.value)}
          placeholder="Search name, profile, team..."
          className="flex-1 min-w-[220px] rounded-lg border border-gray-200 px-3 py-1.5 text-sm placeholder:text-gray-400"
        />
        <FilterBtn active={groupByProfile} onClick={onToggleGroup}>Group by profile</FilterBtn>
      </div>
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[11px] text-gray-400 font-medium w-14 shrink-0">Status</span>
        {EFFECTIVE_STATUSES.map((s) => (
          <FilterBtn key={s} active={statusFilters.has(s)} onClick={() => onToggleStatus(s)}>
            {STATUS_LABELS[s]} <span className="opacity-60">{statusCounts[s] ?? 0}</span>
          </FilterBtn>
        ))}
      </div>
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[11px] text-gray-400 font-medium w-14 shrink-0">Alerts</span>
        <FilterBtn active={alertFilter === 'all'} onClick={() => onAlertFilter('all')}>All agents</FilterBtn>
        <FilterBtn active={alertFilter === 'any'} onClick={() => onAlertFilter('any')}>Needs attention ({flaggedCount})</FilterBtn>
      </div>
      {hasActiveFilters && (
        <div className="flex items-center gap-3 pt-1 border-t border-gray-100">
          <span className="text-xs text-gray-500">Showing {filteredCount} of {totalCount} agents</span>
          <button type="button" onClick={onClearFilters} className="text-xs text-amber-600 hover:text-amber-700 font-medium">
            Clear all
          </button>
        </div>
      )}
    </div>
  );
}

function NeedsAttentionPanel({
  agents, nowMs, onAlertKeyClick,
}: {
  agents: AgentRosterEntry[]; nowMs: number; onAlertKeyClick: (key: AlertFilter) => void;
}): ReactNode {
  if (agents.length === 0) return null;

  const withAlerts = agents
    .map((a) => ({ agent: a, alert: agentAlert(a, DEFAULT_ALERT_THRESHOLDS, nowMs) }))
    .filter((x): x is { agent: AgentRosterEntry; alert: NonNullable<ReturnType<typeof agentAlert>> } => x.alert !== null)
    .sort((a, b) => {
      if (a.alert.sev !== b.alert.sev) return a.alert.sev === 'error' ? -1 : 1;
      return elapsedMinutes(b.agent.statusStartTimestamp, nowMs) - elapsedMinutes(a.agent.statusStartTimestamp, nowMs);
    });

  return (
    <div className="rounded-xl border border-amber-200 bg-white overflow-hidden">
      <div className="px-4 py-2.5 bg-amber-50 border-b border-amber-100">
        <h2 className="text-sm font-semibold text-amber-800">Needs attention</h2>
      </div>
      <div className="p-3 grid gap-2" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(310px, 1fr))' }}>
        {withAlerts.map(({ agent, alert }) => (
          <div key={agent.agentId} className="flex items-center gap-2.5 rounded-lg border border-gray-200 p-2.5">
            <Avatar name={agent.agentName || agent.agentId} tone={agentStatusTone(agent.effectiveStatus)} />
            <div className="flex-1 min-w-0">
              <div className="text-xs font-medium text-gray-800 truncate">{agent.agentName || agent.agentId}</div>
              <div className="text-[11px] text-gray-400 truncate">{agent.routingProfileName}</div>
            </div>
            <button
              type="button"
              onClick={() => onAlertKeyClick(alert.key)}
              className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium shrink-0 ${
                alert.sev === 'error' ? 'bg-red-100 text-red-700' : 'bg-amber-100 text-amber-700'
              }`}
            >
              {alert.label}
            </button>
            <span className="text-[11px] text-gray-400 tabular-nums shrink-0">
              {formatElapsed(elapsedMinutes(agent.statusStartTimestamp, nowMs))}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
```

Add these imports at the top of the file: `Avatar` from `@/components/ui/Avatar`, `agentStatusTone` and `formatElapsed` — `agentStatusTone` from `@/lib/agentRoster` (extend that import line), `formatElapsed` from `@/lib/utils` (extend that import line).

- [ ] **Step 3: Verify it typechecks**

Run: `cd frontend && npm run typecheck`
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/AgentRoster.tsx
git commit -m "feat(frontend): add AgentRoster search/filter controls and needs-attention panel"
```

---

### Task 5: `AgentRoster.tsx` Part C — agent list (grouped/flat), pure helpers, tests

**Files:**
- Modify: `frontend/src/pages/AgentRoster.tsx`
- Create: `frontend/src/pages/AgentRoster.test.ts`

**Interfaces:**
- Consumes: `filteredAgents`, `groupByProfile`, `nowMs` from the `AgentRoster` component (Tasks 3-4, same file).
- Produces: two new exported pure functions this task's tests cover — `sortAgentsForDisplay(agents, nowMs?)` and `groupAgentsByProfile(agents, nowMs?)` — plus the visible agent list UI (untested, presentational, per this repo's convention).

- [ ] **Step 1: Write the failing tests for the two pure functions**

Create `frontend/src/pages/AgentRoster.test.ts`:

```ts
import { describe, expect, it } from 'vitest';

import type { AgentRosterEntry } from '@/lib/api';

import { groupAgentsByProfile, sortAgentsForDisplay } from './AgentRoster';

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

const T0 = new Date('2026-09-01T12:00:00.000Z').getTime();

describe('sortAgentsForDisplay', () => {
  it('puts flagged agents before unflagged ones', () => {
    const idle = agent({ agentId: 'idle', effectiveStatus: 'Available', statusStartTimestamp: new Date(T0 - 15 * 60_000).toISOString() });
    const fresh = agent({ agentId: 'fresh', effectiveStatus: 'Available', statusStartTimestamp: new Date(T0 - 1 * 60_000).toISOString() });
    const result = sortAgentsForDisplay([fresh, idle], T0);
    expect(result.map((a) => a.agentId)).toEqual(['idle', 'fresh']);
  });

  it('within the same flagged state, sorts by longest time in status first', () => {
    const longer  = agent({ agentId: 'longer',  statusStartTimestamp: new Date(T0 - 30 * 60_000).toISOString() });
    const shorter = agent({ agentId: 'shorter', statusStartTimestamp: new Date(T0 - 5 * 60_000).toISOString() });
    const result = sortAgentsForDisplay([shorter, longer], T0);
    expect(result.map((a) => a.agentId)).toEqual(['longer', 'shorter']);
  });
});

describe('groupAgentsByProfile', () => {
  it('groups by routingProfileId and counts flagged agents per group', () => {
    const groups = groupAgentsByProfile(
      [
        agent({ agentId: 'a1', routingProfileId: 'rp1', routingProfileName: 'RP One', effectiveStatus: 'Available', statusStartTimestamp: new Date(T0 - 15 * 60_000).toISOString() }),
        agent({ agentId: 'a2', routingProfileId: 'rp1', routingProfileName: 'RP One', effectiveStatus: 'Available', statusStartTimestamp: new Date(T0 - 1 * 60_000).toISOString() }),
        agent({ agentId: 'a3', routingProfileId: 'rp2', routingProfileName: 'RP Two', effectiveStatus: 'Offline' }),
      ],
      T0,
    );
    expect(groups).toHaveLength(2);
    const rpOne = groups.find((g) => g.routingProfileId === 'rp1')!;
    expect(rpOne.agents).toHaveLength(2);
    expect(rpOne.flaggedCount).toBe(1);
  });

  it('sorts groups with any flagged agent before groups with none', () => {
    const groups = groupAgentsByProfile(
      [
        agent({ agentId: 'a1', routingProfileId: 'calm', routingProfileName: 'Calm RP', effectiveStatus: 'Available', statusStartTimestamp: new Date(T0 - 1 * 60_000).toISOString() }),
        agent({ agentId: 'a2', routingProfileId: 'busy', routingProfileName: 'Busy RP', effectiveStatus: 'Available', statusStartTimestamp: new Date(T0 - 15 * 60_000).toISOString() }),
      ],
      T0,
    );
    expect(groups[0]!.routingProfileId).toBe('busy');
  });

  it('returns an empty array for no agents', () => {
    expect(groupAgentsByProfile([], T0)).toEqual([]);
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd frontend && npx vitest run src/pages/AgentRoster.test.ts`
Expected: FAIL — `sortAgentsForDisplay`/`groupAgentsByProfile` aren't exported from `AgentRoster.tsx` yet.

- [ ] **Step 3: Implement the two pure functions and the agent list UI**

Add near the top of `frontend/src/pages/AgentRoster.tsx`, after the type/import block (before the `useNowTick` function):

```ts
export function sortAgentsForDisplay(agents: AgentRosterEntry[], nowMs: number = Date.now()): AgentRosterEntry[] {
  return [...agents].sort((a, b) => {
    const aFlagged = agentAlert(a, DEFAULT_ALERT_THRESHOLDS, nowMs) !== null;
    const bFlagged = agentAlert(b, DEFAULT_ALERT_THRESHOLDS, nowMs) !== null;
    if (aFlagged !== bFlagged) return aFlagged ? -1 : 1;
    return elapsedMinutes(b.statusStartTimestamp, nowMs) - elapsedMinutes(a.statusStartTimestamp, nowMs);
  });
}

export type AgentRosterGroup = {
  routingProfileId: string;
  routingProfileName: string;
  agents: AgentRosterEntry[];
  flaggedCount: number;
  staffing: ReturnType<typeof classifyStaffing>;
};

export function groupAgentsByProfile(agents: AgentRosterEntry[], nowMs: number = Date.now()): AgentRosterGroup[] {
  const byProfile = new Map<string, AgentRosterEntry[]>();
  for (const agent of agents) {
    const list = byProfile.get(agent.routingProfileId) ?? [];
    list.push(agent);
    byProfile.set(agent.routingProfileId, list);
  }
  const groups: AgentRosterGroup[] = [...byProfile.entries()].map(([routingProfileId, list]) => {
    const available = list.filter((a) => a.effectiveStatus === 'Available').length;
    const flaggedCount = list.filter((a) => agentAlert(a, DEFAULT_ALERT_THRESHOLDS, nowMs) !== null).length;
    return {
      routingProfileId,
      routingProfileName: list[0]!.routingProfileName,
      agents: sortAgentsForDisplay(list, nowMs),
      flaggedCount,
      staffing: classifyStaffing(available, minAvailableFor(list[0]!.routingProfileName)),
    };
  });
  return groups.sort((a, b) => {
    if ((a.flaggedCount > 0) !== (b.flaggedCount > 0)) return a.flaggedCount > 0 ? -1 : 1;
    const riskDiff = STAFFING_RISK_ORDER[a.staffing.risk] - STAFFING_RISK_ORDER[b.staffing.risk];
    if (riskDiff !== 0) return riskDiff;
    return b.agents.length - a.agents.length;
  });
}
```

Now render the list. Insert this JSX right after `<NeedsAttentionPanel .../>` in the `AgentRoster` component's `return (...)` block:

```tsx
          <AgentList agents={filteredAgents} groupByProfile={groupByProfile} nowMs={nowMs} isLoading={query.isLoading} />
```

Append the list-rendering components to the end of the file:

```tsx
function AgentRow({ agent, nowMs }: { agent: AgentRosterEntry; nowMs: number }): ReactNode {
  const alert = agentAlert(agent, DEFAULT_ALERT_THRESHOLDS, nowMs);
  const rowTint = alert?.sev === 'error' ? 'bg-red-50/50' : alert?.sev === 'warn' ? 'bg-amber-50/50' : '';
  return (
    <div className={`flex items-center gap-3 px-4 py-2.5 ${rowTint}`}>
      <Avatar name={agent.agentName || agent.agentId} tone={agentStatusTone(agent.effectiveStatus)} />
      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium text-gray-800 truncate">{agent.agentName || agent.agentId}</div>
        <div className="text-[11px] text-gray-400 truncate">
          {alert ? `${alert.why} · ${agent.routingProfileName}` : agent.routingProfileName}
        </div>
      </div>
      <StatusChip tone={agentStatusTone(agent.effectiveStatus)} label={agent.effectiveStatus === 'Unavailable' ? 'Away' : agent.effectiveStatus} />
      <span className="w-14 text-right font-mono text-sm tabular-nums text-gray-600 shrink-0">
        {formatRuntime(elapsedSeconds(agent.statusStartTimestamp, nowMs))}
      </span>
    </div>
  );
}

function AgentList({
  agents, groupByProfile, nowMs, isLoading,
}: {
  agents: AgentRosterEntry[]; groupByProfile: boolean; nowMs: number; isLoading: boolean;
}): ReactNode {
  return (
    <div className="rounded-xl border border-gray-200 bg-white overflow-hidden">
      <div className="px-4 py-2.5 border-b border-gray-100 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-800">
          Agents <span className="text-gray-400 font-normal">({agents.length})</span>
        </h2>
        <span className="text-[11px] text-gray-400">Time in current status · flagged agents first</span>
      </div>

      {isLoading && <div className="px-4 py-10 text-center text-sm text-gray-400">Loading agents…</div>}

      {!isLoading && agents.length === 0 && (
        <div className="px-4 py-10 text-center">
          <div className="text-sm text-gray-500">No agents match</div>
          <div className="text-xs text-gray-400 mt-1">Try clearing a filter or the search.</div>
        </div>
      )}

      {!isLoading && agents.length > 0 && !groupByProfile && (
        <div className="divide-y divide-gray-100">
          {sortAgentsForDisplay(agents, nowMs).map((a) => <AgentRow key={a.agentId} agent={a} nowMs={nowMs} />)}
        </div>
      )}

      {!isLoading && agents.length > 0 && groupByProfile && (
        <div className="divide-y divide-gray-100">
          {groupAgentsByProfile(agents, nowMs).map((group) => (
            <div key={group.routingProfileId}>
              <div className="px-4 py-2 bg-gray-50 flex items-center gap-2 text-xs">
                <span className="font-medium text-gray-700">{group.routingProfileName}</span>
                <span className="text-gray-400">
                  {group.agents.length} · {group.agents.filter((a) => a.effectiveStatus === 'Available').length} available
                </span>
                {group.flaggedCount > 0 && (
                  <span className="text-amber-600">{group.flaggedCount} flagged</span>
                )}
              </div>
              <div className="divide-y divide-gray-100">
                {group.agents.map((a) => <AgentRow key={a.agentId} agent={a} nowMs={nowMs} />)}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
```

Add `formatRuntime, elapsedSeconds` to the existing `@/lib/utils` import line (extending it alongside `elapsedMinutes`/`formatElapsed` already imported in Tasks 3-4).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd frontend && npx vitest run src/pages/AgentRoster.test.ts`
Expected: all pass.

- [ ] **Step 5: Run the full suite and typecheck**

Run: `cd frontend && npx vitest run && npm run typecheck && npm run build`
Expected: typecheck/build clean; same 3 pre-existing unrelated `chainMap.test.ts` failures, all else passing.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/AgentRoster.tsx frontend/src/pages/AgentRoster.test.ts
git commit -m "feat(frontend): add AgentRoster grouped/flat agent list with live M:SS timers"
```

---

### Task 6: Wire `AgentRoster` into `BrandedMonitor.tsx`, delete `AgentsView`

**Files:**
- Modify: `frontend/src/pages/BrandedMonitor.tsx`

**Interfaces:**
- Consumes: `AgentRoster` (default/named export from `frontend/src/pages/AgentRoster.tsx`, Tasks 3-5).
- Produces: nothing new for later tasks — this is the integration point.

- [ ] **Step 1: Delete the old agents view and its now-unused helpers**

In `frontend/src/pages/BrandedMonitor.tsx`, delete:
- `type AlertFilter = 'all' | 'alerts' | 'idle' | 'break';` and `type SortCol = 'name' | 'status' | 'time';` and `const STATUS_ORDER: ...` (the block right before `AgentsKpiBar`)
- `function AgentsKpiBar(...) { ... }` (the whole function)
- `function AgentsView(): ReactNode { ... }` (the whole function, through its closing `}`)
- `function agentIdleAlert(agent: AgentRosterEntry): 'idle' | 'break' | null { ... }` (now superseded by `agentAlert` in `lib/agentRoster.ts`) — but first confirm nothing else in this file still calls `agentIdleAlert` (it's used by `AgentAvailabilitySidebar` at the line found via `grep -n "agentIdleAlert" frontend/src/pages/BrandedMonitor.tsx` — if `AgentAvailabilitySidebar` still calls it, leave `agentIdleAlert` in place for now; do not break that call site as part of this task, since replacing it is out of this plan's scope (it belongs to the `LiveView`/`Live Campaigns` work, not this plan)).

Run `grep -n "agentIdleAlert" frontend/src/pages/BrandedMonitor.tsx` before deleting anything — `AgentAvailabilitySidebar` (used by `LiveView`, staying) calls `agentIdleAlert` at line 696, so keep that one function and only delete the `AlertFilter`/`SortCol`/`STATUS_ORDER`/`AgentsKpiBar`/`AgentsView` block.

**This repo's `tsconfig.json` sets `noUnusedLocals: true` — an unused import is a hard build failure, not a lint warning.** `AgentsView` was the *only* consumer in this file of 4 imports at the top:
- `TEAM_LABELS`, `BRANDED_MONITOR_TEAMS`, `teamForProfile` (all from the `@/lib/routingProfileTeams` import at line 12) — confirmed via `grep -n "TEAM_LABELS\|BRANDED_MONITOR_TEAMS\|teamForProfile" frontend/src/pages/BrandedMonitor.tsx`: every call site is inside `AgentsView` (lines 983-1104). Delete this entire import line.
- `RoutingProfileSummary` (from the `@/lib/api` import block at the top) — confirmed via `grep -n "RoutingProfileSummary" frontend/src/pages/BrandedMonitor.tsx`: its only use is `AgentsView`'s `routingProfiles: RoutingProfileSummary[]` at line 978. Remove just this one type name from the `@/lib/api` import's list (the import line has several other names — `AgentRosterEntry`, `BrandedCampaignRecord`, etc. — that are still used elsewhere in the file and must stay).

Before moving to Step 2, run `cd frontend && npm run typecheck` once specifically to catch any *other* now-unused import `noUnusedLocals` flags that this list didn't anticipate — treat a `TS6133` error as the source of truth over this list, not the other way around.

- [ ] **Step 2: Add the import and wire the tab**

Add to the imports at the top of the file:

```ts
import { AgentRoster } from './AgentRoster';
```

Change line ~1299 (`{tab === 'agents' && <AgentsView />}`) to:

```tsx
      {tab === 'agents'  && <AgentRoster />}
```

- [ ] **Step 3: Run the full suite, typecheck, and build**

Run: `cd frontend && npx vitest run && npm run typecheck && npm run build`
Expected: typecheck/build clean; same pre-existing unrelated `chainMap.test.ts` failures only. No test references `AgentsView`/`AgentsKpiBar` directly (they were never separately tested — presentational, per convention), so no test file needs updating for this deletion.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/BrandedMonitor.tsx
git commit -m "feat(frontend): wire AgentRoster into BrandedMonitor's Agents tab, delete old AgentsView"
```

---

### Task 7: Full verification

**Files:** none (verification only)

- [ ] **Step 1: Full backend suite**

Run: `cd services/api-metrics && python3 -m pytest tests/unit -v`
Expected: all pass (28 pre-existing + 3 new from Task 1 = 31; confirm exact number).

- [ ] **Step 2: Full frontend suite**

Run: `cd frontend && npx vitest run`
Expected: all pass except the 3 known pre-existing, unrelated `chainMap.test.ts` failures.

- [ ] **Step 3: Typecheck and build**

Run: `cd frontend && npm run typecheck && npm run build`
Expected: both clean.

- [ ] **Step 4: Manual smoke test**

Run: `cd frontend && npm run dev` with `VITE_PREVIEW_MODE=false` (this page needs real Connect data — `mockApi.ts`'s `brandedMonitor` stubs are empty and cannot exercise it) against a real backend + Cognito login, navigate to `/plans/branded-monitor`, click the "Agents" tab. Confirm: the 6 workforce summary tiles show real counts; the capacity table lists both branded teams' routing profiles with a proportional stacked bar and a risk chip; any genuinely idle/on-break/long-call/long-ACW agent appears in "Needs attention" and is sorted to the top of its group; search narrows by name/profile/team; the status/alert filter buttons narrow the list; "Group by profile" toggles between grouped and flat; the rightmost `M:SS` timer on each row visibly increments once per second without a full data refetch (open devtools Network tab and confirm no new `/metrics/branded/agents` request fires every second — only every 60s).

---

## Self-Review Notes

- **Spec coverage:** workforce summary (Task 3), capacity table + stacked bar + risk classification (Task 2 pure logic + Task 3 rendering), needs-attention panel (Task 4), search/status/team/profile/alert filtering (Task 4), group-by-profile toggle (Task 5), per-second ticking timers (Task 3's `useNowTick` + Task 5's `AgentRow`), flagged-first sorting (Task 5). Not built, by design and confirmed with Sebastian: the mockup's popover-with-scrim multi-select UI (simplified to row-button filters, matching this repo's existing convention) and the 8-team scope (kept at 2 branded teams).
- **Placeholder scan:** `MIN_AVAILABLE_BY_PROFILE` ships empty with a documented fallback rather than fabricated per-profile numbers — this is a deliberate, disclosed placeholder (see Global Constraints), not an oversight.
- **Type consistency:** `AgentAlertKey`/`AgentAlert` (Task 2) is consumed identically by `NeedsAttentionPanel`/`AgentRow`/`ControlBar`'s `AlertFilter` (Task 4) and `sortAgentsForDisplay`/`groupAgentsByProfile` (Task 5). `RoutingProfileAvailability` (Task 2, moved from `AgentAvailabilityPanel.tsx`) is consumed identically by `CapacityTable`/`StaffingBar` (Task 3). `StaffingStatus`/`StaffingRisk`/`STAFFING_RISK_ORDER` (Task 2) are consumed identically by `CapacityTable` (Task 3) and `groupAgentsByProfile` (Task 5).
