# Dynamic Location→State Mapping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix Pennsylvania (`PA - Center City`) and `TX - Westover Hills` so campaign-enable auto-suggestion (phone number + contact flow) works for them, and stop future new locations from requiring a frontend code change at all.

**Architecture:** `VipLocationMapping` (DynamoDB) is already the single source of truth, already exposed live via `GET /location-mapping` → `useLocationMapping()` (react-query, 10 min staleTime, backend 1h cache). The bug is that three frontend modules keep their own **hardcoded, parallel** copies of state/location/phone/flow data that never sync with that source: `STATE_LOCATION_MAP` (locations by state), `STATE_DEFAULT_PHONES`/`STATE_AREA_CODES` (phone by state), `STATE_FLOW_PATTERNS` (Connect flow name pattern by state). This plan (a) makes the location→state derivation take the *live* map as a parameter instead of silently defaulting to the stale static one, and (b) adds the one confirmed missing phone-number entry. It does **not** touch the phone/flow *matching* mechanism itself — that already works by live pattern-matching against Connect's actual phone/flow lists, so a **new state's** phone and flow will keep resolving automatically once someone names them following the existing `"<STATE> - Vein Leads"` / `"campaign-<CODE>"` conventions, with zero code changes.

**Tech Stack:** React 18, TypeScript, `@tanstack/react-query`, Vitest.

**Spec:** This document (no separate spec file — investigation and requirements are inline below, confirmed against live AWS/DynamoDB data in this session, not invented).

## Global Constraints

- Do not delete `STATE_LOCATION_MAP`, `LOCATION_TO_STATE`, or `KNOWN_STATE_CODES` — confirmed via `grep -rn` that nothing outside `stateLocationMap.ts` imports them directly, but they remain the documented "fallback while loading" default and the default parameter value for the functions this plan changes. Removing them is out of scope.
- Do not add a `PA` entry to `STATE_FLOW_PATTERNS` in `EnableCampaignModal.tsx` — its existing fallback (`STATE_FLOW_PATTERNS[state] ?? [state]`) already searches for the bare state code, which will match a future `campaign-PA` flow automatically. Adding an entry now would be dead code (see Task 5 — no code, ops task).
- All new/changed exported functions keep their existing call signature as a **prefix** (new parameters are optional with a default) — no existing call site should need to change unless it's being upgraded to pass live data on purpose (Tasks 2–3).
- TypeScript: run `npx tsc --noEmit` from `frontend/` after every task. Tests: `npm test` (vitest) from `frontend/` after every task.

---

## Investigation findings (confirmed against live AWS data — nothing below is inferred or assumed)

1. **`VipLocationMapping` DynamoDB table has 76 items**, confirmed via `aws dynamodb scan`. It already contains `PA - Center City` (`stateCode: PA`, `stateName: Pennsylvania`, `slug: Pennsylvania`, `stateSortOrder: 8`) and `TX - Westover Hills` (`stateCode: TX`, existing state).
2. **The dynamic backend path has no bug.** `builders.get_all_location_groups()` → `_load_location_mapping()` (`services/api-plans/src/builders.py:38-99`) scans the full table with pagination and returns everything, subject to a 1h in-process cache. `SegmentNew.tsx`'s interactive location picker already uses `useLocationMapping()` (live data) — new locations show up there once the cache refreshes, no code change needed.
3. **The bug is 3 hardcoded, never-synced frontend structures**, all in files that nothing regenerates from `VipLocationMapping`:
   - `frontend/src/lib/stateLocationMap.ts`: `STATE_LOCATION_MAP` has no Pennsylvania entry at all (no `PA` state group exists), and its Texas entry is missing `TX - Westover Hills`. `LOCATION_TO_STATE` and `KNOWN_STATE_CODES` are derived from this static list.
   - `frontend/src/lib/areaCodeMap.ts`: `STATE_DEFAULT_PHONES` and `STATE_AREA_CODES` have no `PA` key.
   - `frontend/src/components/EnableCampaignModal.tsx`: `STATE_FLOW_PATTERNS` has no `PA` key (its fallback already covers this — see Global Constraints).
4. **Confirmed impact:** `frontend/src/pages/Segments.tsx:356-360` and `frontend/src/pages/SegmentDetail.tsx:222-226` compute `segmentStates` via `stateCodesFromSegmentGroups`/`stateCodesFromSegmentName`, which default to the static `STATE_LOCATION_MAP`. For a segment whose only location is `PA - Center City`, `stateCodesFromSegmentGroups` returns `[]` (Pennsylvania isn't in `LOCATION_TO_STATE`), so `EnableCampaignModal` gets `segmentStates=[]`. That disables `pickPhoneForStates`'s state-match step (falls through to "first available phone" — could be any state's number), disables `suggestCampaignFlow`'s pattern lookup, and — because the "wrong number" warning banner (`EnableCampaignModal.tsx:281,286-287`) only fires `segmentStates.length > 0`, **no warning is shown either.** Silent, not crashed.
5. **A Pennsylvania outbound phone number already exists in Amazon Connect** — confirmed via `aws connect list-phone-numbers-v2`: `+12154009167`, description `"PA - Vein Leads"`, area code 215. This matches the exact naming convention already used for every other state's canonical number (`"TX - Vein Leads"` → `STATE_DEFAULT_PHONES.TX`, `"NJ - Vein Leads"` → `STATE_DEFAULT_PHONES.NJ`, etc.). **No number needs to be provisioned — it's already there, just not in the map.**
6. **No Pennsylvania Connect contact flow exists yet.** Confirmed via `aws connect list-contact-flows`: flows named `campaign-CT`, `campaign-LI`, `campaign-TX`, `campaign-SCA`, `campaign-NJ`, `campaign-NCA`, `campaign-MD`, `campaign-NY` all exist; **no `campaign-PA`.** This is a gap this plan cannot close — it's Amazon Connect contact-flow authoring (content/IVR decisions), not a frontend code change. Flagged in Task 5 below as a to-do for Sebastian / the Connect admin, not invented or worked around.
7. `TX - Westover Hills` needs no phone/flow map changes — Texas already has `STATE_DEFAULT_PHONES.TX`, `STATE_AREA_CODES.TX`, and `campaign-TX`. Once Task 2/3 make the location→state derivation dynamic, any segment using this location resolves to `TX` automatically, same as any other Texas city.
8. `frontend/src/lib/stateLocationMap.ts:161-163` already has an unused, ready-to-use helper: `buildLocationToState(map: readonly StateGroup[]): Record<string, string>` — confirmed via `grep -rn` that it is defined but called nowhere. This plan wires it in instead of writing a new equivalent.
9. No existing test files for `stateLocationMap.ts`, `areaCodeMap.ts`, or `EnableCampaignModal.tsx` (confirmed via `find`). `frontend/src/lib/chainMap.test.ts` is the closest existing convention to mirror: colocated `<name>.test.ts`, `vitest` `describe`/`it`, imported directly from the module under test.

## What is still missing (cannot be resolved without you / the Connect admin — not invented, not worked around)

- **A `campaign-PA` Amazon Connect contact flow does not exist.** Until one is created (recommend cloning an existing single-state flow like `campaign-MD` or `campaign-NJ` and adjusting IVR content), `suggestCampaignFlow` will correctly find no match for Pennsylvania and the operator will need to pick a flow manually when enabling a Pennsylvania campaign. This plan does not create that flow — it's outside code/this repo's scope (Connect Contact Flow designer + a content decision about what the PA flow should say).
- Nothing else is missing. The phone number exists, the DynamoDB location entry exists, the API path is already dynamic, and the helper function needed for the code fix already exists unused.

---

## Task 1: Dynamic location→state helpers usable outside the fallback

**Files:**
- Modify: `frontend/src/lib/stateLocationMap.ts` (add one function, no existing exports change)
- Test: `frontend/src/lib/stateLocationMap.test.ts` (new file)

**Interfaces:**
- Consumes: existing `StateGroup` type, existing `buildLocationToState` (already defined, line 161).
- Produces: `codesFromMap(map: readonly StateGroup[]): Set<string>` — new export, used by Task 1's own change to `stateCodesFromSegmentName` and by nothing else yet.

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/lib/stateLocationMap.test.ts`:

```typescript
import { describe, expect, it } from 'vitest';

import {
  buildLocationToState,
  codesFromMap,
  stateCodesFromSegmentGroups,
  stateCodesFromSegmentName,
  type StateGroup,
} from './stateLocationMap';

const testMap: readonly StateGroup[] = [
  { state: 'Texas', slug: 'Texas', code: 'TX', locations: ['Texas', 'TX - Addison'] },
  { state: 'Pennsylvania', slug: 'Pennsylvania', code: 'PA', locations: ['PA - Center City'] },
];

describe('codesFromMap', () => {
  it('returns the set of state codes present in the map', () => {
    expect(codesFromMap(testMap)).toEqual(new Set(['TX', 'PA']));
  });

  it('returns an empty set for an empty map', () => {
    expect(codesFromMap([])).toEqual(new Set());
  });
});

describe('buildLocationToState with a live map (not the static fallback)', () => {
  it('resolves a location only present in the live map, not in STATE_LOCATION_MAP', () => {
    const reverse = buildLocationToState(testMap);
    expect(reverse['PA - Center City']).toBe('Pennsylvania');
  });
});

describe('stateCodesFromSegmentGroups with an explicit map parameter', () => {
  const segmentGroups = {
    Groups: [
      {
        Dimensions: [
          {
            ProfileAttributes: {
              Attributes: { location: { Values: ['PA - Center City'] } },
            },
          },
        ],
      },
    ],
  };

  it('resolves Pennsylvania when given a map that includes it', () => {
    expect(stateCodesFromSegmentGroups(segmentGroups, testMap)).toEqual(['PA']);
  });

  it('returns empty when given a map that does not include it (documents current static-map gap)', () => {
    expect(stateCodesFromSegmentGroups(segmentGroups, [])).toEqual([]);
  });
});

describe('stateCodesFromSegmentName with an explicit map parameter', () => {
  it('resolves a PA token when given a map that includes the PA code', () => {
    expect(stateCodesFromSegmentName('29-4-26-PA-3NL-1202-v4', testMap)).toEqual(['PA']);
  });

  it('returns empty for a code not present in the given map', () => {
    expect(stateCodesFromSegmentName('29-4-26-PA-3NL-1202-v4', [])).toEqual([]);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd frontend && npx vitest run src/lib/stateLocationMap.test.ts`
Expected: FAIL — `codesFromMap` is not exported yet, and `stateCodesFromSegmentGroups`/`stateCodesFromSegmentName` don't accept a second parameter yet (TypeScript will also fail to compile the test file itself).

- [ ] **Step 3: Add `codesFromMap` and give the two lookup functions an optional `map` parameter**

In `frontend/src/lib/stateLocationMap.ts`, add right after `buildLocationToState` (after line 163):

```typescript
/** Build a live set of known state codes from a dynamically fetched StateGroup array. */
export function codesFromMap(map: readonly StateGroup[]): Set<string> {
  return new Set(map.map((g) => g.code));
}
```

Replace the existing `stateCodesFromSegmentGroups` function (current lines 197-218) with:

```typescript
export function stateCodesFromSegmentGroups(
  segmentGroups: unknown,
  map: readonly StateGroup[] = STATE_LOCATION_MAP,
): string[] {
  const found = new Set<string>();
  const groups = (segmentGroups as { Groups?: unknown[] } | undefined)?.Groups;
  if (!Array.isArray(groups)) return [];
  const locationToState = buildLocationToState(map);
  for (const group of groups) {
    const dimensions = (group as { Dimensions?: unknown[] }).Dimensions;
    if (!Array.isArray(dimensions)) continue;
    for (const dim of dimensions) {
      const attrs = (dim as { ProfileAttributes?: { Attributes?: Record<string, { Values?: string[] }> } })
        .ProfileAttributes?.Attributes;
      const locationAttr = attrs?.location;
      if (!locationAttr || !Array.isArray(locationAttr.Values)) continue;
      for (const value of locationAttr.Values) {
        const stateName = locationToState[value];
        if (!stateName) continue;
        const code = map.find((g) => g.state === stateName)?.code;
        if (code) found.add(code);
      }
    }
  }
  return Array.from(found);
}
```

Replace the existing `stateCodesFromSegmentName` function (current lines 230-235) with:

```typescript
export function stateCodesFromSegmentName(
  name: string,
  map: readonly StateGroup[] = STATE_LOCATION_MAP,
): string[] {
  const knownCodes = codesFromMap(map);
  for (const token of name.toUpperCase().split('-')) {
    if (knownCodes.has(token)) return [token];
  }
  return [];
}
```

Leave `KNOWN_STATE_CODES` (line 220) exactly as-is — it's unused outside this file per the investigation, but removing it is out of scope for this plan.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/lib/stateLocationMap.test.ts`
Expected: PASS, all 6 tests.

- [ ] **Step 5: Typecheck**

Run: `cd frontend && npx tsc --noEmit`
Expected: no errors. (Existing call sites in `Segments.tsx`/`SegmentDetail.tsx` still compile — they call these functions with one argument, which is legal since `map` is optional.)

- [ ] **Step 6: Commit**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns
git add frontend/src/lib/stateLocationMap.ts frontend/src/lib/stateLocationMap.test.ts
git commit -m "feat(frontend): allow stateCodesFromSegment{Groups,Name} to take a live location map"
```

---

## Task 2: Wire live location data into `Segments.tsx`'s campaign-enable flow

**Files:**
- Modify: `frontend/src/pages/Segments.tsx:356-360` (and the `import` line at the top of the file, line 14)

**Interfaces:**
- Consumes: `useLocationMapping()` (existing hook, `frontend/src/lib/stateLocationMap.ts:242-260`), `stateCodesFromSegmentGroups(segmentGroups, map)` and `stateCodesFromSegmentName(name, map)` from Task 1.
- Produces: nothing new for other tasks — this is a leaf consumer.

- [ ] **Step 1: Add the hook import and call**

In `frontend/src/pages/Segments.tsx`, change line 14 from:

```typescript
import { stateCodesFromSegmentGroups, stateCodesFromSegmentName } from '@/lib/stateLocationMap';
```

to:

```typescript
import { stateCodesFromSegmentGroups, stateCodesFromSegmentName, useLocationMapping } from '@/lib/stateLocationMap';
```

Inside the component that currently computes `segmentStates` (the one containing lines 356-360 — it already has `useState`/`useQuery` calls above this point), add:

```typescript
const { locationMap } = useLocationMapping();
```

Then change the existing block:

```typescript
  const segmentStates = detail.data
    ? (stateCodesFromSegmentGroups(detail.data.segmentGroups).length > 0
        ? stateCodesFromSegmentGroups(detail.data.segmentGroups)
        : stateCodesFromSegmentName(segment.name))
    : stateCodesFromSegmentName(segment.name);
```

to:

```typescript
  const segmentStates = detail.data
    ? (stateCodesFromSegmentGroups(detail.data.segmentGroups, locationMap).length > 0
        ? stateCodesFromSegmentGroups(detail.data.segmentGroups, locationMap)
        : stateCodesFromSegmentName(segment.name, locationMap))
    : stateCodesFromSegmentName(segment.name, locationMap);
```

- [ ] **Step 2: Typecheck**

Run: `cd frontend && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 3: Manual verification (no existing test harness for this page)**

Run: `cd frontend && npm run dev`, open the Segments page, open "Enable Campaign" on a segment whose location is `PA - Center City` (or any segment — this is a regression check, not just a PA-specific one). Confirm the modal loads without a console error and, for a segment that includes a recognized state, still auto-picks a phone/flow as before (regression check that Task 1's default-parameter change didn't break the non-PA path).

- [ ] **Step 4: Commit**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns
git add frontend/src/pages/Segments.tsx
git commit -m "fix(frontend): use live location map for campaign-enable state detection in Segments list"
```

---

## Task 3: Wire live location data into `SegmentDetail.tsx`'s campaign-enable flow

**Files:**
- Modify: `frontend/src/pages/SegmentDetail.tsx:14` (import) and `:222-226` (usage)

**Interfaces:**
- Consumes: same as Task 2.
- Produces: nothing new.

- [ ] **Step 1: Add the hook import and call**

Change line 14 from:

```typescript
import { stateCodesFromSegmentGroups, stateCodesFromSegmentName } from '@/lib/stateLocationMap';
```

to:

```typescript
import { stateCodesFromSegmentGroups, stateCodesFromSegmentName, useLocationMapping } from '@/lib/stateLocationMap';
```

Add `const { locationMap } = useLocationMapping();` inside the page component (top-level, alongside its other hooks — this file is the segment detail page component itself, not a nested row component like in Task 2).

Change:

```typescript
        segmentStates={
          stateCodesFromSegmentGroups(seg.segmentGroups).length > 0
            ? stateCodesFromSegmentGroups(seg.segmentGroups)
            : stateCodesFromSegmentName(seg.name)
        }
```

to:

```typescript
        segmentStates={
          stateCodesFromSegmentGroups(seg.segmentGroups, locationMap).length > 0
            ? stateCodesFromSegmentGroups(seg.segmentGroups, locationMap)
            : stateCodesFromSegmentName(seg.name, locationMap)
        }
```

- [ ] **Step 2: Typecheck**

Run: `cd frontend && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 3: Manual verification**

Run: `cd frontend && npm run dev`, open a segment's detail page directly (not via the list), click "Enable Campaign", confirm no console error and existing (non-PA) auto-suggestion still works.

- [ ] **Step 4: Commit**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns
git add frontend/src/pages/SegmentDetail.tsx
git commit -m "fix(frontend): use live location map for campaign-enable state detection in Segment detail"
```

---

## Task 4: Add the confirmed Pennsylvania phone number

**Files:**
- Modify: `frontend/src/lib/areaCodeMap.ts`
- Test: `frontend/src/lib/areaCodeMap.test.ts` (new file)

**Interfaces:**
- Consumes: nothing new.
- Produces: nothing new for other tasks.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/lib/areaCodeMap.test.ts`:

```typescript
import { describe, expect, it } from 'vitest';

import { pickPhoneForStates, STATE_DEFAULT_PHONES } from './areaCodeMap';

describe('STATE_DEFAULT_PHONES', () => {
  it('has the confirmed Pennsylvania canonical number', () => {
    expect(STATE_DEFAULT_PHONES.PA).toBe('+12154009167');
  });
});

describe('pickPhoneForStates for Pennsylvania', () => {
  it('picks the canonical PA number when it is in the provisioned phone list', () => {
    const phones = [
      { number: '+19734949660' }, // NJ
      { number: '+12154009167' }, // PA - Vein Leads
      { number: '+15126508970' }, // TX
    ];
    const picked = pickPhoneForStates(phones, ['PA']);
    expect(picked?.number).toBe('+12154009167');
  });

  it('falls back to the 215 area code when the exact canonical number is absent', () => {
    const phones = [
      { number: '+19734949660' }, // NJ
      { number: '+12154009168' }, // some other 215 number, not the canonical one
    ];
    const picked = pickPhoneForStates(phones, ['PA']);
    expect(picked?.number).toBe('+12154009168');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/lib/areaCodeMap.test.ts`
Expected: FAIL — `STATE_DEFAULT_PHONES.PA` is `undefined`; the second test falls through to "first available phone" (`phones[0]`, the NJ number) instead of the 215 fallback, because `STATE_AREA_CODES.PA` doesn't exist yet either.

- [ ] **Step 3: Add the PA entries**

In `frontend/src/lib/areaCodeMap.ts`, add to `STATE_DEFAULT_PHONES` (after the `NCA` line):

```typescript
  PA:         '+12154009167',  // PA - Vein Leads (Philadelphia, area code 215)
```

Add to `STATE_AREA_CODES` (after the `LI` line):

```typescript
    PA:  ['215'],
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/lib/areaCodeMap.test.ts`
Expected: PASS, both tests.

- [ ] **Step 5: Typecheck**

Run: `cd frontend && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns
git add frontend/src/lib/areaCodeMap.ts frontend/src/lib/areaCodeMap.test.ts
git commit -m "fix(frontend): add confirmed Pennsylvania canonical phone (+12154009167, area 215)"
```

---

## Task 5: No code — flag the missing Connect contact flow

**Files:** none (this task produces a decision/action item, not a commit).

- [ ] **Step 1: Confirm with Sebastian / the Connect admin that a `campaign-PA` contact flow should be created**, and who will author it (recommend cloning `campaign-MD` or `campaign-NJ`, both single-office states with no per-city IVR branching, as the closest existing template — confirm this assumption with whoever owns Connect Flow content before cloning).
- [ ] **Step 2: Once created and named exactly `campaign-PA`**, no frontend code change is needed — `STATE_FLOW_PATTERNS[state] ?? [state]` in `EnableCampaignModal.tsx` already falls back to searching for the bare code `PA`, which matches `campaign-PA` via substring (`"CAMPAIGN-PA".includes("PA")`). Verify this manually once the flow exists: open Enable Campaign on a Pennsylvania segment and confirm the flow auto-populates.
- [ ] **Step 3: No commit** — this task's deliverable is the Connect flow itself (external system), not a code change.

---

## Self-review (spec coverage)

- Pennsylvania location resolves to state code → Task 1 + 2 + 3.
- TX - Westover Hills resolves correctly → covered by Task 1-3's dynamic derivation; no state-specific entry needed since Texas already has phone/area-code/flow coverage (investigation finding 7).
- Pennsylvania phone auto-suggestion works → Task 4.
- Pennsylvania contact-flow auto-suggestion → cannot be closed by code; Task 5 documents exactly what's missing and why, per "no inventes nada."
- Future new locations require no code change → Task 1-3 make the derivation take live `VipLocationMapping` data; a new city within an existing state needs zero changes anywhere. A brand-new state still needs its phone number provisioned and (for full automation) a `campaign-<CODE>` flow created in Connect — both are inherently external-system actions, not something code can pre-empt.
