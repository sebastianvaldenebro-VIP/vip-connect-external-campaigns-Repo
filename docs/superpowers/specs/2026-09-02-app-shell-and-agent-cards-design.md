# App Shell Redesign + Agent Availability Compact Cards — Design

## Goal

Replace the app's current top-nav header with a persistent, collapsible left
sidebar + top bar (per reference mockups), and redesign both existing
"Agent availability" widgets into a compact, routing-profile-grouped card
style (per a second reference mockup) that reuses the staffing-risk logic
already built this session. Also make the existing Plans-internal sidebar
(`PlansLayout.tsx`) fully collapsible-with-reflow instead of collapsing only
per-group.

## Context

This follows the "UI Polish Round" plan (merged, deployed). After deploying,
Sebastian reviewed the live app against reference design images and found:
gaps in what "UI Polish Round" delivered (the Agent-availability cards don't
match the intended compact-by-routing-profile design; the sidebar only
collapses per-group, not as a whole with reflow) and a request to adopt a
new app-wide navigation shell shown in two reference mockups (a top bar and
a sidebar). A separate, already-identified bug (missing "End" time on
`PlanDetail.tsx`'s chain-style `CampaignCard`, a different component than
the one "UI Polish Round" fixed) was found and fixed directly outside this
design cycle — see "Already-fixed, out of scope" below.

## Out of scope

- **`PlanDetail.tsx`'s `CampaignCard` End-time fix**: already implemented
  (added an `End` entry to `timingItems` when `cs.completedAt` is set,
  replacing the projected `ETA` once real). Verified (typecheck clean, same
  128-test baseline). Not committed yet — pending Sebastian's go-ahead since
  it was made directly on `main`, not in an SDD worktree. Not a task in the
  implementation plan that follows this spec; mentioned here only for
  traceability.
- **Real per-routing-profile minimum thresholds**: `MIN_AVAILABLE_BY_PROFILE`
  stays empty (default `1` for every profile) as Sebastian chose earlier
  this session. This redesign only changes how the existing risk data is
  *displayed*, not what thresholds are configured.
- **"N campaigns" per routing profile**: no data source exists (no
  queue↔routing-profile mapping in the frontend or backend). Omitted from
  the compact card entirely — not stubbed, not approximated.
- **Content of `PlansLayout.tsx`'s inner sub-nav** (Today's plan / Live
  monitor / History / Templates / Scheduler / How to use / Branded Monitor):
  unchanged. Only its collapse *mechanism* changes (see Section A).

## Section A — App Shell (Sidebar + TopBar)

### New components

- `frontend/src/components/Sidebar.tsx` (new) — the app-wide left nav rail.
- `frontend/src/components/TopBar.tsx` (new) — the top bar shown next to the
  sidebar.
- `frontend/src/components/Layout.tsx` (modified) — composes `Sidebar` +
  `TopBar` + `<Outlet/>` instead of the current single horizontal header.

### Sidebar navigation mapping

One group labeled `CONTACT CENTER`:

| Sidebar item | Route          | Today's equivalent           |
|---|---|---|
| Monitor       | `/dashboard`    | "Dashboard" (top nav today)   |
| History       | `/plans/history`| "Plans → Operations → History"|
| Plans         | `/plans`        | "Plans" (top nav today)       |
| Templates     | `/plans/templates` | "Plans → Configuration → Templates" |
| Segments      | `/segments`     | "Segments" (top nav today)    |

A second group, labeled `ADMIN`, holds the remaining top-nav items that
aren't in the reference mockup:

| Sidebar item | Route |
|---|---|
| Campaigns | `/campaigns` |
| Profiles  | `/profiles` |
| Audit     | `/audit` |
| Artifacts | `/contact-artifacts` |

`History` and `Templates` are intentionally reachable both from this global
sidebar (as shortcuts) and from `PlansLayout.tsx`'s own inner sub-nav (their
existing location) — this redundancy is deliberate, not a bug to resolve.

Each item: icon (raw inline SVG, matching existing icon style — no icon
library) + label, active-state highlight via `NavLink`, same visual language
`PlansLayout.tsx` already uses for its own nav items.

### Collapse behavior

The sidebar can be toggled between expanded (full width, icon + label) and
collapsed (narrow rail, icon only). The main content area (`<Outlet/>`)
reflows to reclaim the freed width — this is a real width change, not an
overlay. Collapsed/expanded state persists in `localStorage` (a UI
preference only — no PHI, no session data).

`PlansLayout.tsx`'s existing inner sidebar changes from today's per-group
collapse (`Operations`/`Configuration` toggle independently) to a single
whole-sidebar collapse with the same reflow behavior. Content of that inner
sidebar is unchanged.

### Bottom context box

A static box at the bottom of the global sidebar, always showing a fixed
label (e.g. "CONTEXT / Outbound scheduling") — no dynamic data, purely a
visual module identifier matching the reference mockup.

### TopBar contents

- **Branding**: keeps the literal text "VIP Connect Admin" (not renamed to
  the mockup's "Medwork / Orchestrator") — only the mockup's visual layout
  (icon + wordmark) is adopted.
- **Breadcrumb**: `Contact center / {label}`, where `{label}` is the active
  top-level sidebar item's label, derived from the current route.
- **Live clock**: current time in Colombia time (COT, UTC-5, fixed offset),
  `HH:MM` format, no seconds — reuses `fmtTime`'s existing convention rather
  than the mockup's literal "ET, 12h, with seconds" styling. Updates via a
  `setInterval` tick (matching the existing tick pattern in
  `AgentRoster.tsx`'s elapsed-time displays).
- **Alerts bell + counter**: aggregate count of (a) per-agent alerts already
  computed by `agentRoster.ts`'s `agentAlert()` (idle/break/longCall/
  longAcw) and (b) per-routing-profile staffing risks from
  `classifyStaffing()` that are not `healthy`/`off-hours`
  (no-coverage/understaffed/at-minimum), summed across all routing profiles
  system-wide. Uses `useQuery({ queryKey: ['agent-roster', 'all'], ... })` —
  the exact same query key `AgentAvailabilityPanel.tsx` already uses, so
  React Query dedupes the network call when both are mounted on the same
  page. No click-through behavior in this pass (just the count).
- **Avatar**: a circle showing initials derived from the existing
  `useAuth()`'s `user.username` (already available, no new auth work).
  Clicking it reveals the "Sign out" action (replacing today's always-visible
  "Sign out" button).

### Known cost, accepted

Because `TopBar` is mounted globally (every route, via `Layout.tsx`), the
`['agent-roster', 'all']` query — and its `refetchInterval: 20_000` —  now
polls continuously on every page, including pages that never fetched this
data before (Dashboard, Segments, Campaigns, Audit, Artifacts, Profiles).
This is a deliberate, accepted tradeoff of making the alerts badge always
current; Sebastian confirmed the added polling load is acceptable at the
existing 20s interval.

## Section B — Agent Availability compact cards

### New shared component

`frontend/src/components/AgentAvailabilityCard.tsx` (new) — a single
presentational card for one routing profile's availability, used by both:

- `AgentAvailabilitySidebar` (inside `frontend/src/pages/BrandedMonitor.tsx`)
  — scope unchanged: agents whose team (via `teamForProfile`) is in
  `BRANDED_MONITOR_TEAMS` (both branded teams).
- `AgentAvailabilityPanel.tsx` (Campaign Monitor's compact widget) — scope
  unchanged: agents whose team is `patient-success` only (via the existing
  `PANEL_TEAM` constant).

Both call sites keep their own data-fetching/scoping logic exactly as it is
today; only the per-profile card's JSX and the surrounding
header/footer/sort change.

### Data (no new business logic — pure reuse)

- `aggregateByRoutingProfile(agents)` — already exists in `agentRoster.ts`.
- `classifyStaffing(available, minAvailableFor(profileName))` — already
  exists.
- `STAFFING_RISK_ORDER` — already exists; used to sort profiles so at-risk
  ones surface first (same ordering `CapacityTable` already uses).

### Card layout (per routing profile)

- Profile name, bold, top-left.
- Large "AVAILABLE" count, top-right, color-coded by risk tone
  (`classifyStaffing`'s `tone`: danger=red, warning=amber, success=green).
- One compact row: `N CALL · N ACW · N OFF` (using the same
  `RoutingProfileAvailability` fields `aggregateByRoutingProfile` already
  returns: `onCall`, `acw`, `offline + unavailable`).
- If risk is not `healthy`/`off-hours`: a banner using `classifyStaffing`'s
  `label`, extended with the actual threshold for the "at-minimum"/
  "understaffed" cases — e.g. "Below minimum of {minAvailableFor(name)}".
- Clicking the card preserves today's existing click-through behavior
  (navigate to Agent Roster with that specific profile pre-filtered) —
  unchanged from "UI Polish Round".

### Header / footer

- Header: existing scope-qualified title (already shipped: "Agent
  availability — Branded teams" / "Agent availability — Patient Access") +
  a new "All profiles →" link that navigates to the Agent Roster with no
  profile/team filter applied.
- Footer: "+N profiles" for routing profiles that exist in today's roster
  scope but have zero agents currently in the roster response — no "no
  active campaign" wording (no data to back that specific claim).
- No cap on visible cards — every profile with ≥1 agent shows as its own
  card; only genuinely-zero-agent profiles collapse into the footer count.

## Testing

- Presentational JSX (`Sidebar.tsx`, `TopBar.tsx`, `AgentAvailabilityCard.tsx`)
  is not unit-tested, per this repo's established convention — only pure,
  exported functions get tests.
- If any non-trivial pure function is extracted during implementation (e.g.
  a `breadcrumbLabelForPath(pathname)` helper, or an `alertsCount(agents)`
  aggregator for the TopBar bell), that function gets real, non-tautological
  unit tests — matching how `sortSegmentsForRender` was tested in "UI Polish
  Round".
- No existing test should need to change: `aggregateByRoutingProfile`,
  `classifyStaffing`, `minAvailableFor`, `agentAlert`, `STAFFING_RISK_ORDER`
  are consumed as-is, not modified.

## Global constraints (carried forward, still binding)

- `@/components/ui` collision: directory primitives (`StatTile`, `Avatar`,
  `StatusChip`, etc.) always imported from their specific file, never the
  bare `@/components/ui` path.
- COT-only timezone: any time display uses `fmtTime`, never
  `toLocaleTimeString`/browser-local formatting. The new TopBar clock is COT,
  not the mockup's literal ET — per Sebastian's explicit choice this session.
- No icon library, no obscure Unicode glyphs: raw inline SVG only, matching
  `PlansLayout.tsx`'s existing icon style.
- `BRANDED_MONITOR_TEAMS` / `teamForProfile` / `PANEL_TEAM`: reused for team
  scoping, never redefined or duplicated with ad-hoc string matching.
- Presentational JSX untested; only pure exported functions get tests.
