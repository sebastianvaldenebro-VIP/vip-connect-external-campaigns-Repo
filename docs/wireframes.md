# Wireframes — VIP Connect Admin UI

ASCII/markdown sketches of the 9 screens in the MVP. Text-only on purpose — goal is behavior alignment, not pixel-perfect design.

## Conventions

- `[ Button ]` — clickable button
- `[====]` — text input field
- `[ v ]` — dropdown
- `[ ☑ ]` / `[ ☐ ]` — checkbox
- `[ ● ]` / `[ ○ ]` — radio button
- `━━━` — visual divider
- `░░░` — disabled state
- `(i)` — tooltip / info icon
- `→` — navigation target
- `[loader]` — spinner / skeleton loader
- Greyed-out cells = read-only / computed

---

## Layout shell (applies to all authenticated screens)

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│ [VIP Connect Admin]   Dashboard  Segments  Profiles  Campaigns  Analytics  Audit │
│                                                   sebastian.valdenebro@… [ v ] 🔔 │
├──────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│   [ breadcrumb: Dashboard > Segments > NJ 1st Attempt ]                          │
│                                                                                  │
│   {PAGE CONTENT}                                                                 │
│                                                                                  │
│                                                                                  │
└──────────────────────────────────────────────────────────────────────────────────┘
  Last sync: 42s ago   |   Env: prod   |   Build a1b2c3d   |   [Help]
```

**Nav highlights:**
- Top nav stays fixed, keeps current section highlighted
- User chip on top-right opens menu: Profile, Sign out, Theme toggle (post-MVP)
- Bell icon shows count of recent errors / alerts (click to open audit log filtered to errors)
- Footer: session status + env badge + build hash for quick verification during UAT

---

## 1. Login (Cognito Hosted UI)

**This is hosted by Cognito, not by our Amplify app.** We style it via Cognito Hosted UI Customization (CSS only; no JS).

```
                      ┌────────────────────────────────────────┐
                      │                                        │
                      │         VIP Medical Group              │
                      │         Connect Admin                  │
                      │                                        │
                      │  ┌──────────────────────────────────┐  │
                      │  │ Email                            │  │
                      │  │ [==================================│  │
                      │  └──────────────────────────────────┘  │
                      │                                        │
                      │  ┌──────────────────────────────────┐  │
                      │  │ Password                         │  │
                      │  │ [··································│  │
                      │  └──────────────────────────────────┘  │
                      │                                        │
                      │              [  Sign in  ]             │
                      │                                        │
                      │  Forgot password?  Need help?          │
                      │                                        │
                      └────────────────────────────────────────┘

Flow:
  1. Email + password → submit
  2. If MFA enrolled → prompt "Enter TOTP code" (or SMS backup)
  3. If first login → prompt "Set new password + set up MFA (TOTP with QR)"
  4. Success → redirect to Dashboard
```

**Failure paths:**
- Invalid credentials → inline error "Incorrect username or password"
- Locked account (too many failures) → "Account locked. Contact admin."
- Disabled account → "Account disabled. Contact admin."

---

## 2. Dashboard

**Purpose:** Landing page after login. At-a-glance status + quick actions.

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│ Dashboard                                                                        │
├──────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│   ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐             │
│   │ SEGMENTS    │  │ CAMPAIGNS   │  │ CALLS 24H   │  │ ORPHANS     │             │
│   │             │  │             │  │             │  │             │             │
│   │    48       │  │     3       │  │   1,247     │  │    316      │             │
│   │  active     │  │  running    │  │   dialed    │  │  estimated  │             │
│   │             │  │ 2 paused    │  │ 862 answered│  │  (last scan)│             │
│   │ [ View → ]  │  │ [ View → ]  │  │ [ View → ]  │  │ [ Clean → ] │             │
│   └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘             │
│                                                                                  │
│ ─── Active Campaigns ───────────────────────────────────────  [+ New Campaign]   │
│                                                                                  │
│   Name                     Segment               State    Progress      Actions  │
│   ────────────────────────────────────────────────────────────────────────────   │
│   NJ New Leads 4-23        NJ 1 Attempt          Running  47% ▓▓▓▒░░░░  ⏸ ⏹ ✎    │
│   NY 1st Attempt 4-23      NY 1 Attempt          Running  12% ▓░░░░░░░  ⏸ ⏹ ✎    │
│   TX 1st Attempt 4-22      TX 1 Attempt          Paused   78% ▓▓▓▓▓▓▒░  ▶ ⏹ ✎    │
│                                                                                  │
│ ─── Recent Activity ────────────────────────────────  [ View full audit log → ]  │
│                                                                                  │
│   2 min ago   sebastian.valdenebro  Started campaign "NJ New Leads 4-23"         │
│   15 min ago  sebastian.valdenebro  Refreshed estimate for "NJ 1 Attempt" (8342) │
│   1h ago      sebastian.valdenebro  Created segment "MD 2nd Attempt reviewed"    │
│   2h ago      sebastian.valdenebro  Deleted campaign "MD 9-10 Attempt 4-15"      │
│                                                                                  │
└──────────────────────────────────────────────────────────────────────────────────┘
```

**Interactions:**
- KPI tiles auto-refresh every 30s
- Active Campaigns table auto-refreshes every 60s
- Clicking a campaign name → campaign detail page
- Action buttons (▶ Resume, ⏸ Pause, ⏹ Stop, ✎ Edit) have confirmation modals for destructive ops
- Recent activity shows last 20, link takes to full audit log

---

## 3. Segments List

**Purpose:** Browse all segments with member counts + quick actions.

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│ Segments                                                     [ + New Segment ]   │
├──────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  [Search segments...        ]   Sort: [ Updated ▼ ]  Show: [ All ▼ ]             │
│                                                                                  │
│  ┌────────────────────────────────────────────────────────────────────────────┐  │
│  │ Name ↓                        Filters summary                   Members ↕   Actions   │
│  ├────────────────────────────────────────────────────────────────────────────┤  │
│  │ NJ 1 Attempt excluding ids    available=True AND groups∈{NL/1st}            │  │
│  │                                 AND location starts "NJ -"        8,342     │  │
│  │                                 AND Attributes.ID ∉ {…excluded}   (26m ago) │  │
│  │                                                                   [↻]       🔍 ✎ 📋 🗑 │
│  ├────────────────────────────────────────────────────────────────────────────┤  │
│  │ NY 7-13 attempt               available=True AND groups∈{NL/7..13}          │  │
│  │                                 AND location starts "NY -"        3,127     │  │
│  │                                                                   (2h ago)  │  │
│  │                                                                   [↻]       🔍 ✎ 📋 🗑 │
│  ├────────────────────────────────────────────────────────────────────────────┤  │
│  │ TX 1st Attempt                available=True AND groups∈{NL/1st}            │  │
│  │                                 AND location starts "TX -"        2,890     │  │
│  │                                                                   (5h ago)  │  │
│  │                                                                   [↻]       🔍 ✎ 📋 🗑 │
│  ├────────────────────────────────────────────────────────────────────────────┤  │
│  │ External-Campaign-Placeholder  ID="NEVER_MATCHES_…"                0       │  │
│  │                                                                  (never)    │  │
│  │                                                                   [↻]       🔍 ✎ 📋 🗑 │
│  └────────────────────────────────────────────────────────────────────────────┘  │
│                                                                                  │
│  48 segments total  |  page 1 of 3  |  < 1 2 3 >                                 │
│                                                                                  │
└──────────────────────────────────────────────────────────────────────────────────┘
```

**Columns:**
- **Name** — sortable. Click → segment detail page (read-only + option to duplicate/edit)
- **Filters summary** — humanized rendering of `SegmentGroups.Groups[].Dimensions`. Truncated with tooltip on hover for long ones
- **Members** — last known count + "(X ago)" showing age of `lastComputedAt`. Red text if age > 6h. `[↻]` button triggers `CreateSegmentEstimate` inline (row shows [loader] until complete)
- **Actions**:
  - 🔍 View/preview (opens detail with first 20 member profiles)
  - ✎ Edit (opens Segment Create/Edit page in edit mode)
  - 📋 Clone (opens Create screen pre-populated with this segment's filters)
  - 🗑 Delete (confirmation modal; blocks if segment is in use by any active campaign — shows list of blocking campaigns)

**Filters row:**
- Search = client-side substring match on name
- Sort = Updated / Created / Name / Member count (desc/asc)
- Show = All / Active (used in last 30 days) / Stale (not used > 30 days) / Placeholder

---

## 4. Segment Create / Edit

**Purpose:** Visual segment builder. This is the key productivity win — what Connect console does today but with member-count preview inline.

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│ ◄ Back        Create Segment                                                     │
├──────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  Name *           [ NJ 2nd Attempt 4-23                      ]                   │
│  Display name     [ NJ 2nd Attempt (refreshed 4-23)          ]                   │
│  Description      [ Active 2nd attempt leads in NJ               ]               │
│                                                                                  │
│ ─── Filter groups ────────────────────────────────────────────────────────────── │
│                                                                                  │
│   Include  [ ALL ▼ ]   ← ALL = AND across groups, ANY = OR across groups         │
│                                                                                  │
│   ┌─ Group 1  ── Match [ ANY ▼ ] ── Type [ ANY ▼ ] ──────────────────────[ 🗑 ]─┐ │
│   │                                                                             │ │
│   │   Field [ available v ]    Op [ EQ v ]      Value [ True v ]          [ x ] │ │
│   │                                                                             │ │
│   │   Field [ groups v ]       Op [ IN v ]                                [ x ] │ │
│   │                            Values: [New Lead / 2nd Attempt ×] [+ add]       │ │
│   │                                                                             │ │
│   │   Field [ location v ]     Op [ STARTS_WITH v ]  Value [ NJ -          ]    │ │
│   │                                                                        [ x ] │ │
│   │                                                                             │ │
│   │                              [ + Add dimension ]                            │ │
│   └─────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
│                             [ + Add filter group ]                               │
│                                                                                  │
│ ─── Preview ────────────────────────────────────────────────────────────────────│
│                                                                                  │
│    Estimated members:   [ 1,472 ]  (computed 00:42 ago)   [ ↻ Refresh ]          │
│                                                                                  │
│    Sample of first 20 matching profiles:                                         │
│    ┌──────────────────────────────────────────────────────────────────────────┐  │
│    │ first_name  last_name   phone              location       groups          │  │
│    │ ───────────────────────────────────────────────────────────────────────   │  │
│    │ Maria       Ramirez     +12017***1027      NJ - Hoboken   New Lead/2nd   │  │
│    │ John        Smith       +19735***3532      NJ - Clifton   New Lead/2nd   │  │
│    │ ...                                                                       │  │
│    └──────────────────────────────────────────────────────────────────────────┘  │
│    [ Load next 20 ]                                                              │
│                                                                                  │
│                                                                                  │
│            [ Cancel ]           [ Save as draft ]           [ Save segment ]     │
│                                                                                  │
└──────────────────────────────────────────────────────────────────────────────────┘
```

**Interactions:**
- **Field dropdown** populated from the custom object type fields: `available`, `groups`, `location`, `location_id`, `campaign`, `attempt`, `subgroup_id`, `utc_name`, `utc_offset`, plus standard fields
- **Operator dropdown** changes valid value input:
  - `EQ`, `NEQ` → single value
  - `IN`, `NOT_IN` → tag/chip input (multiple)
  - `STARTS_WITH`, `ENDS_WITH`, `CONTAINS` → single text
  - `GTE`, `LTE` → numeric / date
- **Filter groups** allow nested logic (`ANY`/`ALL` at group level + at dimension level)
- **Preview count** — clicking `[ ↻ Refresh ]` calls `POST /segments/draft/estimate` which:
  1. Creates a temp segment (if name is set) OR uses in-memory definition
  2. Calls `CreateSegmentEstimate`
  3. Polls `GetSegmentEstimate` every 2s
  4. Updates the count + age when complete
  - Shows `[loader]` with elapsed time
  - Cancel button available
- **Sample members** — calls `CreateSegmentSnapshot` to export → downloads first 20 rows. Only enabled once count > 0.
- **Save as draft** — stores in DynamoDB `SegmentDrafts` table (not in Customer Profiles yet) — lets operator work on complex segments across sessions
- **Save segment** — only enabled when name is valid + at least 1 filter present + preview > 0 (or override with "Save anyway" confirmation)

**Edit mode:**
- Same layout, but name field is **read-only** (segments are immutable)
- Banner: "Editing this segment will delete the old one and create a new one. Campaigns currently using this segment will need re-linking."
- Shows list of affected campaigns (if any)
- `Save` button reads: "Replace segment"

**Draft mode indicator:**
- Top banner: "🔸 Draft — not saved to Customer Profiles yet"
- "Last saved as draft 5 min ago"

---

## 5. Profiles Browser

**Purpose:** Search individual profiles, inspect their attributes, see matching segments.

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│ Profiles                                                                         │
├──────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  Search by:  [ Phone ▼ ]   [ +12017801027                    ]    [ Search ]    │
│              Other keys: customerid, email, first+last name                      │
│                                                                                  │
│ ─── Results (1) ──────────────────────────────────────────────────────────────── │
│                                                                                  │
│  ┌────────────────────────────────────────────────────────────────────────────┐  │
│  │ ProfileId  3fb18453242f4fd3aa9d96dc035b38ac                                │  │
│  │                                                                            │  │
│  │ First name:  Patrina                  Phone:      +12017801027             │  │
│  │ Last name:   Crawford                 Created:    2026-03-12 14:23 UTC     │  │
│  │                                       Updated:    2026-04-22 19:47 UTC     │  │
│  │                                                                            │  │
│  │ Attributes:                                                                │  │
│  │   ID:              6ae814a6-94aa-4a5b-a69d-2eb6ee5dfb86                    │  │
│  │   available:       false                                                   │  │
│  │   groups:          New Lead / 2nd Attempt                                  │  │
│  │   location:        NJ - Hoboken                                            │  │
│  │   campaign:        New Jersey Vein                                         │  │
│  │   attempt:         two                                                     │  │
│  │   [ Show all 15 attributes ]                                               │  │
│  │                                                                            │  │
│  │ Calculated attributes:                                                     │  │
│  │   days_since_last_contact:   3                                             │  │
│  │   total_contacts_30d:        2                                             │  │
│  │                                                                            │  │
│  │ ┌─ Tabs ────────────────────────────────────────────────────────────────┐  │  │
│  │ │ [ Attributes ] [ Objects ] [ Matching segments ] [ Recent contacts ] │  │  │
│  │ └──────────────────────────────────────────────────────────────────────┘  │  │
│  │                                                                            │  │
│  │  Matching segments (computed 12s ago)  [ ↻ ]                               │  │
│  │  ─────────────────────────────────────                                     │  │
│  │  ✗ NJ 1 Attempt               (available=False excludes this profile)      │  │
│  │  ✓ NJ 2nd Attempt 4-23        (all criteria match)                         │  │
│  │  ✗ NJ New Lead - 1st attempt  (groups=2nd Attempt excludes)                │  │
│  │                                                                            │  │
│  └────────────────────────────────────────────────────────────────────────────┘  │
│                                                                                  │
└──────────────────────────────────────────────────────────────────────────────────┘
```

**Interactions:**
- **Search by** dropdown: Phone, CustomerId, Email, FirstName+LastName combined, ProfileId
- Hitting Enter or clicking Search calls `GET /profiles/search`
- Result card shows all PHI-relevant fields with formatting (phone E.164 display, timestamps in local time)
- **Tabs:**
  - Attributes (default) — full key-value table of raw profile attributes
  - Objects — raw `ProfileObjects` from the ingestion (history of what the Redis feeder wrote)
  - Matching segments — runs `GetSegmentMembership` for each active segment (cached 30s)
  - Recent contacts — `SearchContacts` for this phone in the last 30 days
- **No edit actions** — profiles are read-only from this UI (populated only by `connectcampaignRedisSub`)

**Empty state (no search yet):**
```
Empty state screen:
  Icon + "Search for a profile to view details"
  Quick links: "Recent profiles" (last 10 viewed by this user, from session storage)
```

**Not-found state:**
```
"No profile found with phone +12017801027.
 This could mean:
   - The lead isn't in the CRM yet (check Redis source)
   - The phone format is wrong (try +1 prefix)
   - The profile was deleted (orphan cleanup)"
```

---

## 6. Campaigns List

**Purpose:** Manage all campaigns with lifecycle controls.

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│ Campaigns                                              [ + New Campaign ]        │
├──────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  Filter: [ All states ▼ ]  [ All segments ▼ ]  [ Date range: Last 7 days ▼ ]     │
│  Search: [                            ]                                          │
│                                                                                  │
│  Campaigns quota: 7 / 10 used  ⚠ (3 slots free — delete old ones to free up)     │
│                                                                                  │
│  ┌────────────────────────────────────────────────────────────────────────────┐  │
│  │ Name ↓                  Segment          State    Schedule            Actions │  │
│  ├────────────────────────────────────────────────────────────────────────────┤  │
│  │ NJ New Leads 4-23       NJ 1 Attempt    ● Run   04-23 14:00 → 22:00  ⏸ ⏹ ✎ 🗑 │  │
│  │                                                  (3h remaining)              │  │
│  │                                         Progress: 487 / 1,040 (47%) ▓▓▒░░   │  │
│  ├────────────────────────────────────────────────────────────────────────────┤  │
│  │ NY 1st Attempt 4-23     NY 1 Attempt    ● Run   04-23 15:00 → 21:00  ⏸ ⏹ ✎ 🗑 │  │
│  │                                                  (4h remaining)              │  │
│  │                                         Progress: 63 / 520 (12%) ▓░░░░░░   │  │
│  ├────────────────────────────────────────────────────────────────────────────┤  │
│  │ TX 1st Attempt 4-22     TX 1 Attempt    ⏸ Paused 04-22 14:00 → 22:00  ▶ ⏹ ✎ 🗑 │  │
│  │                                                  (expired — will not resume) │  │
│  │                                         Progress: 405 / 520 (78%) ▓▓▓▓▒░░  │  │
│  ├────────────────────────────────────────────────────────────────────────────┤  │
│  │ MD 9-10 Attempt 4-15    MD 9-10         ● Compl 04-15 10:00 → 16:00    ✎ 🗑  │  │
│  │                                                                              │  │
│  │                                         Final: 342 / 342 (100%) ▓▓▓▓▓▓▓▓   │  │
│  └────────────────────────────────────────────────────────────────────────────┘  │
│                                                                                  │
│  Showing 4 of 7   |   < 1 >                                                      │
│                                                                                  │
└──────────────────────────────────────────────────────────────────────────────────┘
```

**Column semantics:**
- **State** with color: ● green=Running, ⏸ yellow=Paused, ⏹ red=Stopped, ● grey=Completed/Initialized
- **Schedule** relative time remaining or elapsed
- **Progress** — dialed contacts / estimated segment size (refreshed from CloudWatch metrics every 60s)
- **Actions** per state:
  - Running: ⏸ Pause, ⏹ Stop, ✎ Edit, 🗑 Delete (disabled — must stop first)
  - Paused: ▶ Resume, ⏹ Stop, ✎ Edit, 🗑 Delete (disabled)
  - Stopped/Completed: ✎ Edit (clone only), 🗑 Delete

**Quota widget:** if > 80% used, shows warning with inline "Delete old" action that opens a modal listing completed campaigns sorted oldest-first.

**Filters:**
- State: All / Running / Paused / Stopped / Completed / Initialized
- Segment: dropdown of segments currently linked to any campaign
- Date range: Last 24h / 7d / 30d / Custom

---

## 7. Campaign Create / Edit

**Purpose:** Create a new campaign (or edit existing) with all config. Step-by-step form to reduce cognitive load.

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│ ◄ Back   Create Campaign                                          Step 1 of 4    │
├──────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  ● Basics   ○ Dialing   ○ Schedule   ○ Review                                    │
│                                                                                  │
│ ─── Basics ──────────────────────────────────────────────────────────────────── │
│                                                                                  │
│  Campaign name *    [ NJ 2nd Attempt 2026-04-23              ]                   │
│                                                                                  │
│  Segment *          [ NJ 2nd Attempt 4-23  (1,472 members)  v ]                  │
│                     (i) Segment was recomputed 2 min ago. [Refresh]              │
│                                                                                  │
│  Contact flow *     [ *Agent-staffed Campaign AMD           v ]                  │
│                     (i) Must be a published flow with "Check call progress"      │
│                                                                                  │
│  Campaign flow *    [ Outbound Campaign Flow (v1)            v ]                 │
│                     (i) Orchestrates retry/disposition logic                     │
│                                                                                  │
│  Queue *            [ agents outbound   (3 avail / 24 online) v ]               │
│                                                                                  │
│  Source phone *     [ +1 (973) 494-9660   DID - NJ          v ]                  │
│                                                                                  │
│                                                        [ Cancel ]   [ Next → ]   │
│                                                                                  │
└──────────────────────────────────────────────────────────────────────────────────┘


┌──────────────────────────────────────────────────────────────────────────────────┐
│                                                                  Step 2 of 4    │
├──────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  ○ Basics   ● Dialing   ○ Schedule   ○ Review                                    │
│                                                                                  │
│ ─── Dialing configuration ──────────────────────────────────────────────────── │
│                                                                                  │
│  Dialing mode *     [ ● Progressive    ○ Predictive    ○ Agentless ]             │
│                                                                                  │
│  Agent allocation   [====================●----] 100%                             │
│                     (i) Portion of agents dedicated to this campaign             │
│                                                                                  │
│  Dialing capacity   [====================●----] 100%                             │
│                     (i) Share of dial slots if multiple campaigns run            │
│                                                                                  │
│  Ring timeout       [ 60 ] seconds                                               │
│                                                                                  │
│  Answer machine detection                                                        │
│     [ ☑ ] Enable AMD (recommended for leaving agents useful time)                │
│     [ ☑ ] Wait for full prompt before classifying                                │
│                                                                                  │
│                                        [ ← Back ]  [ Cancel ]   [ Next → ]      │
│                                                                                  │
└──────────────────────────────────────────────────────────────────────────────────┘


┌──────────────────────────────────────────────────────────────────────────────────┐
│                                                                  Step 3 of 4    │
├──────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  ○ Basics   ○ Dialing   ● Schedule   ○ Review                                    │
│                                                                                  │
│ ─── Schedule ───────────────────────────────────────────────────────────────── │
│                                                                                  │
│  Time zone *        [ America/New_York v ]   ← All times below in this TZ        │
│                                                                                  │
│  Start date *       [ 2026-04-23 ]   Start time * [ 14:00 ]                      │
│  End date *         [ 2026-04-23 ]   End time *   [ 22:00 ]                      │
│                                                                                  │
│  ⚠ Start must be at least 5 min from now. End must be after start.               │
│                                                                                  │
│ ─── Communication limits (optional) ───────────────────────────────────────── │
│                                                                                  │
│  Max calls per recipient:                                                        │
│     [ 3 ]  per day                                                               │
│     [ 10 ] per week                                                              │
│     [ 20 ] per month                                                             │
│                                                                                  │
│  Active hours (only dial during these hours):                                    │
│     Monday    [ ☑ ]   09:00 → 17:00                                              │
│     Tuesday   [ ☑ ]   09:00 → 17:00                                              │
│     Wednesday [ ☑ ]   09:00 → 17:00                                              │
│     Thursday  [ ☑ ]   09:00 → 17:00                                              │
│     Friday    [ ☑ ]   09:00 → 17:00                                              │
│     Saturday  [ ☐ ]   09:00 → 17:00                                              │
│     Sunday    [ ☐ ]   09:00 → 17:00                                              │
│                                                                                  │
│                                        [ ← Back ]  [ Cancel ]   [ Next → ]      │
│                                                                                  │
└──────────────────────────────────────────────────────────────────────────────────┘


┌──────────────────────────────────────────────────────────────────────────────────┐
│                                                                  Step 4 of 4    │
├──────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  ○ Basics   ○ Dialing   ○ Schedule   ● Review                                    │
│                                                                                  │
│ ─── Review ─────────────────────────────────────────────────────────────────── │
│                                                                                  │
│  Summary                                                                         │
│  ───────                                                                         │
│  Campaign:           NJ 2nd Attempt 2026-04-23                                   │
│  Segment:            NJ 2nd Attempt 4-23 (1,472 members — ⚠ estimate 2h old)     │
│                      [ Refresh now ]                                             │
│  Dialing:            Progressive, 100% allocation, 100% capacity, 60s ring       │
│                      AMD enabled, await prompt                                   │
│  Schedule:           04-23 14:00 → 22:00 America/New_York                        │
│  Limits:             3/day, 10/week, 20/month                                    │
│  Active hours:       Mon-Fri 09:00-17:00                                         │
│                                                                                  │
│  What happens when you click Launch:                                             │
│   1. Segment snapshot is taken (fresh as of now)                                 │
│   2. Campaign created in Outbound Campaigns V2                                   │
│   3. Starts immediately (or at startTime if in future)                           │
│   4. You can pause/stop anytime from Campaigns list                              │
│                                                                                  │
│                            [ ← Back ]  [ Cancel ]   [ Launch campaign ]          │
│                                                                                  │
└──────────────────────────────────────────────────────────────────────────────────┘
```

**Edit mode:** Same 4 steps but only allowed fields are editable (name, schedule, source segment). Dialing config + contact flow are locked post-creation. Banner explains the constraint.

---

## 8. Analytics / Metrics

**Purpose:** Per-campaign and aggregate metrics with CloudWatch-backed widgets.

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│ Analytics                                                                        │
├──────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  Scope:   [ ● Campaign   ○ Queue   ○ Aggregate ]                                 │
│  Campaign [ NJ New Leads 4-23                     v ]                            │
│  Period:  [ ● Last 24h   ○ 7d    ○ 30d    ○ Custom ]                             │
│                                                                                  │
│ ─── KPIs ──────────────────────────────────────────────────────────────────────│
│                                                                                  │
│   ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐               │
│   │ Delivered │ │ Placed   │ │ Answered │ │ Abandoned│ │ AMD      │               │
│   │          │ │          │ │ (human)  │ │          │ │ detected │               │
│   │  1,247   │ │  1,128   │ │    612   │ │    34    │ │   247    │               │
│   │          │ │          │ │   54.2%  │ │    3%    │ │   20%    │               │
│   └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘               │
│                                                                                  │
│ ─── Call volume over time ────────────────────────────────────────────────────  │
│                                                                                  │
│   │                                                                              │
│   │                          ▂▄▆▇█▇▆▅▄                                           │
│   │                       ▂▄▆              ▃▂                                    │
│   │                    ▁▂▄                    ▁                                  │
│   │ ────────────────────────────────────────────────────────                     │
│   │ 14:00  15:00  16:00  17:00  18:00  19:00  20:00  21:00                       │
│                                                                                  │
│ ─── Disposition breakdown ────────────────────────────────────────────────────  │
│                                                                                  │
│   Answered (human)        ████████████████████████████████▒ 612  (54.2%)        │
│   Voicemail (AMD)         █████████████▒                    247  (21.9%)        │
│   No answer / Busy        ██████████▒                       182  (16.1%)        │
│   Abandoned               ██▒                                34   (3.0%)        │
│   Failed / Expired        ▒                                  53   (4.7%)        │
│                                                                                  │
│ ─── Agent performance ────────────────────────────────────────────────────────  │
│                                                                                  │
│   Avg handle time         4m 12s                                                 │
│   Avg wait time           8s                                                     │
│   Contacts per agent hour 11.3                                                   │
│                                                                                  │
│  [ Download CSV ]  [ Open in CloudWatch ]                                        │
│                                                                                  │
└──────────────────────────────────────────────────────────────────────────────────┘
```

**Interactions:**
- Period selector triggers re-query of CloudWatch
- "Open in CloudWatch" opens the corresponding dashboard/widget in the AWS console (deep link with pre-filled filters)
- CSV export hits `/metrics/campaigns/{id}/export?period=24h&format=csv`
- Auto-refresh every 60s (toggle to disable)

**Aggregate scope:** same layout but sums metrics across all running + completed campaigns in the period.

---

## 9. Audit Log

**Purpose:** Immutable record of all admin actions. Read-only.

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│ Audit Log                                                                        │
├──────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  Filters:                                                                        │
│    User   [ All users            v ]                                             │
│    Action [ All actions          v ]   (create, update, delete, start, stop…)    │
│    Entity [ All entities         v ]   (segment, campaign)                       │
│    From   [ 2026-04-16 ]   To [ 2026-04-23 ]                                     │
│    Search [ ]                                                                    │
│                                                                                  │
│  [ Export CSV ]   [ ↻ Refresh ]                                                  │
│                                                                                  │
│  ┌────────────────────────────────────────────────────────────────────────────┐  │
│  │ When ↓              User                    Action         Entity           │  │
│  ├────────────────────────────────────────────────────────────────────────────┤  │
│  │ 2026-04-23 14:12    sebastian.valdenebro@…  start          campaign:      │  │
│  │                                                             NJ New Leads   │  │
│  │                                                             4-23           │  │
│  │                                                             [ Details ]    │  │
│  ├────────────────────────────────────────────────────────────────────────────┤  │
│  │ 2026-04-23 14:11    sebastian.valdenebro@…  create         campaign:      │  │
│  │                                                             NJ New Leads   │  │
│  │                                                             4-23           │  │
│  │                                                             [ Details ]    │  │
│  ├────────────────────────────────────────────────────────────────────────────┤  │
│  │ 2026-04-23 14:08    sebastian.valdenebro@…  estimate       segment:       │  │
│  │                                                             NJ 1 Attempt   │  │
│  │                                                             (count=8342)   │  │
│  │                                                             [ Details ]    │  │
│  ├────────────────────────────────────────────────────────────────────────────┤  │
│  │ 2026-04-23 14:02    sebastian.valdenebro@…  update         segment:       │  │
│  │                                                             MD 2nd Attempt │  │
│  │                                                             (filter added) │  │
│  │                                                             [ Details ]    │  │
│  ├────────────────────────────────────────────────────────────────────────────┤  │
│  │ 2026-04-23 13:55    sebastian.valdenebro@…  delete         campaign:      │  │
│  │                                                             MD 9-10 4-15   │  │
│  │                                                             [ Details ]    │  │
│  └────────────────────────────────────────────────────────────────────────────┘  │
│                                                                                  │
│  Showing 5 of 247  |  page 1 of 50  |  < 1 2 3 … 50 >                            │
│                                                                                  │
└──────────────────────────────────────────────────────────────────────────────────┘
```

**Details modal (click [Details]):**

```
┌─────────────────────────────────────────────────────────────────┐
│ Audit entry                                                     │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│ Timestamp:  2026-04-23 14:02:18 UTC                             │
│ Actor:      sebastian.valdenebro@medwork.io                     │
│ Actor sub:  AROASNCHKP6BJHAWFWYUF:…                             │
│ IP:         18.212.243.142                                      │
│ Action:     update                                              │
│ Entity:     segment:MD-2nd-Attempt-abc123                       │
│                                                                 │
│ ─── Before ───────────────────────────────────────────────────  │
│ {                                                               │
│   "name": "MD 2nd Attempt",                                     │
│   "filters": [ ... ]                                            │
│ }                                                               │
│                                                                 │
│ ─── After ────────────────────────────────────────────────────  │
│ {                                                               │
│   "name": "MD 2nd Attempt",                                     │
│   "filters": [ ..., +new filter ]                               │
│ }                                                               │
│                                                                 │
│ Diff:                                                           │
│   + filters[3] = { field: "available", op: "eq", value: true }  │
│                                                                 │
│                                           [ Close ]   [ Copy ]  │
└─────────────────────────────────────────────────────────────────┘
```

**Export:** CSV export respects current filters. Rows limit: 10,000 per export (for larger, UI suggests time range split).

**Immutability guarantee:** UI has no edit/delete controls for audit rows. IAM policy on Lambda enforces the same — no `dynamodb:UpdateItem` or `DeleteItem` permissions.

---

## Cross-screen behaviors

### Error handling

Global error boundary shows toast notification for unexpected errors:

```
                              ┌───────────────────────────────────────┐
                              │ ⚠  Error                       [  x  ]│
                              │    Campaign couldn't be started.      │
                              │    Reason: Schedule start time needs  │
                              │    to be at least 5 minutes from now. │
                              │                                       │
                              │    Request ID: abc-123-xyz            │
                              │    [ Copy ID ]  [ View logs ]         │
                              └───────────────────────────────────────┘
```

### Loading states

Every async fetch shows a skeleton loader (not a spinner) to preserve layout. Destructive actions disable all UI during processing.

### Confirmation modals

Destructive operations (delete, stop) require confirmation with typed entity name for campaigns with progress > 50%:

```
                 ┌──────────────────────────────────────────────────┐
                 │ Stop campaign?                                   │
                 ├──────────────────────────────────────────────────┤
                 │                                                  │
                 │ "NJ New Leads 4-23" has dialed 487 of 1,040      │
                 │ contacts (47%). Stopping is irreversible — you   │
                 │ cannot resume a stopped campaign.                │
                 │                                                  │
                 │ To confirm, type the campaign name:              │
                 │ [ NJ New Leads 4-23                    ]         │
                 │                                                  │
                 │                       [ Cancel ]   [ Stop ]      │
                 └──────────────────────────────────────────────────┘
```

### Empty states

Every list screen has a specific empty state with a call-to-action:

```
Segments (empty):
  ┌─────────────────────────────────────────┐
  │                                         │
  │          No segments yet                │
  │                                         │
  │   Segments define which profiles get    │
  │   dialed by a campaign.                 │
  │                                         │
  │       [ + Create your first segment ]   │
  │                                         │
  └─────────────────────────────────────────┘
```

### Accessibility

- All interactive elements keyboard-navigable (Tab + Enter)
- ARIA labels on icon-only buttons (🗑 = "Delete")
- Color is never the only differentiator (states use icon + color + text)
- Focus visible ring on all focusable elements
- Contrast ratio ≥ 4.5:1 for text

### Responsive (desktop-first)

Designed for 1280x720+ (operator workstation). Below 1024px, nav collapses to hamburger and tables go horizontal-scroll. No mobile optimization — not a requirement.

---

## Validation criteria — Fase B closing

Operator (single user) reviews these wireframes and confirms:

- [ ] All 9 screens cover the actions they do today in the Connect console
- [ ] The "member count preview" UX in Segment Create/Edit addresses the 24h lag pain
- [ ] The Audit Log meets the HIPAA visibility expectations
- [ ] Campaign Create/Edit 4-step wizard is not too many steps
- [ ] Nothing critical is missing from Dashboard

Once confirmed, **Fase C — implementation** begins with sprint 3 (AuthStack + DataStack + api-segments Lambda).
