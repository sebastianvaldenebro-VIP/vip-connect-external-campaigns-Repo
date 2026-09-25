# Connect Location Drift Detector — Design

## Context

On 2026-09-23, Virginia (Reston) and Georgia (Johns Creek, Sandy Springs)
were live in Amazon Connect — queues named `fd reston`, `fd johns creek`,
`fd sandy springs` with `Description: "Front Desk VA"` / `"Front Desk GA"`,
plus canonical phone numbers already provisioned (`VA - Front Desk`,
`VA - Vein Leads`, `GA - Front Desk`, `GA - Georgia Leads`) — but nobody had
added the corresponding items to the `VipLocationMapping` DynamoDB table,
so the states never appeared in the vip-connect-integrated-ui webapp's
state dropdown. The gap was found and fixed by hand (manual scan of Connect
queues/phone numbers, manual `put-item` into `VipLocationMapping`, manual
`areaCodeMap.ts` patch).

This spec covers a Lambda that detects this class of drift automatically —
a Connect queue that implies a state/location with no matching entry in
`VipLocationMapping` — going forward, so onboarding a new physical location
doesn't silently fail to show up in the webapp until someone notices.

## Non-goals

- Auto-determining `areaCodes` for a new state. That was a human judgment
  call for VA/GA (no Connect data gives area-code routing coverage) and
  stays one here — the detector always leaves `areaCodes: []` on any item
  it creates.
- Real-time detection. A daily poll is enough for an event class (opening
  a new office) that isn't latency-sensitive; a CloudTrail/EventBridge
  event-driven trigger would add infra and a dependency on Connect admin
  actions reliably reaching CloudTrail, for no real benefit here.
- Editing `frontend/src/lib/areaCodeMap.ts`. That file's
  `STATE_DEFAULT_PHONES`/`STATE_AREA_CODES` maps still need a human PR once
  a draft is reviewed and confirmed — out of scope for this detector.
- **Producing a perfectly-formatted `location` string or perfectly
  deduplicating every naming variant.** Every item this detector creates
  carries `pendingReview: true` and is never surfaced to the webapp until
  a human reviews it (see Decisions #2 and the `builders.py` change below).
  That human already has to fill in `areaCodes` by hand — fixing a casing
  glitch, an extra space, or a same-office duplicate in the same pass costs
  nothing extra and is far cheaper than encoding every naming-convention
  edge case into the parser. The detector should be reasonably tolerant of
  the naming variance seen in production (see Evidence below), not perfect.

## Evidence gathered (production, read-only)

- All 50 queues named `fd *` in the Connect instance
  (`6b3f17ba-68a4-472a-9b20-db1991507009`) have a `Description` field of
  the exact shape `"Front Desk <LABEL>"`. This is a reliable signal.
- `<LABEL>` is not always the canonical `stateCode` used in
  `VipLocationMapping`/`STATE_LOCATION_MAP`:
  - Direct matches: `VA`, `GA`, `PA`, `NJ`, `MD`, `TX`, `CT`, `LI`.
  - Known aliases needed: `"South CA"` → `SCA`, `"North CA"` → `NCA`,
    `"NYC"` → `NY`.
  - Ambiguous/unresolved case found live: `fd glendale` →
    `"Front Desk CA"` — bare `"CA"` doesn't match any current code
    (`SCA`/`NCA`). Left unresolved by design; see Decisions.
- Phone number `PhoneNumberDescription` values follow a looser
  `"<LABEL> - <purpose>"` convention (e.g. `"VA - Vein Leads"`,
  `"South CA - Front Desk"`, `"GA - Georgia Leads"`).

## Decisions (confirmed with Sebastian during brainstorming)

1. **Sources watched:** a Connect queue (`fd *` naming + `"Front Desk
   <LABEL>"` description) is the only signal that opens a new drift draft
   — it's the one 100%-consistent signal found in production. Phone
   numbers are cross-referenced only to *enrich* an already-drifted
   queue's draft (fill `canonicalPhone`) — a phone number never opens a
   draft on its own. A location whose phone is provisioned before its
   queue is a known, accepted gap in this iteration.
2. **Automation level:** hybrid. The detector **always** creates a draft
   item in `VipLocationMapping` (never only-alerts, never skips creation
   for ambiguous cases) and **always** publishes an SNS alert. Confidence
   only changes how many fields the draft arrives with pre-filled versus
   left blank for a human to complete. `areaCodes` is always blank —
   that's always a human call.
3. **Trigger:** EventBridge Schedule, `rate(1 day)` polling.
4. **Alert dedup:** re-alert every day for as long as an item stays
   `pendingReview: true`, so a missed first alert can't make a gap
   invisible indefinitely. To avoid an alert flood — the first full-instance
   run is expected to surface every one of the ~50 `fd *` queues never
   checked before, not just VA/GA — new drifts and still-pending reminders
   are each collapsed into a single digest SNS per run when there's more
   than a couple of them (see step 8-9 below), instead of one message per
   location.
5. **Label→code alias resolution:** hardcoded map in the Lambda source,
   not a separate editable config table. Matches the existing convention
   in this codebase (`STATE_LOCATION_MAP` in the frontend is also a
   hardcoded list); a new label needing an alias is rare enough to be a
   reviewed one-line PR.
6. **Test coverage:** ≥90% line coverage on the new module
   (`location_drift_detector.py`), enforced in CI via `pytest --cov`.

## Architecture

```
EventBridge Rule (rate(1 day))
        │
        ▼
Lambda: vip-connect-location-drift-detector
        │
        ├─ connect:ListQueues + DescribeQueue   (read-only, filter name startswith "fd ")
        ├─ dynamodb:Scan on VipLocationMapping  (existing locations + existing pendingReview drafts)
        ├─ dynamodb:PutItem (conditional, attribute_not_exists(location)) — new drafts only
        ├─ dynamodb:UpdateItem (conditional) — lastAlertedAt bump on existing pending drafts
        └─ sns:Publish on vip-plans-alerts      (new drift + daily reminders)
```

Follows the same infra pattern already used by
`location_onboarding_guard`: the IAM role cannot be created by CDK (the
CloudFormation execution role lacks `iam:CreateRole` under the engineering
permission boundary), so it is created via CLI and imported into the stack
with `iam.Role.fromRoleArn(..., { mutable: false })`. Same for the log
group (`logs.LogGroup.fromLogGroupName`, created via CLI).

### New IAM role: `vip-connect-location-drift-detector-role`

Inline policy, scoped to what's actually called:
- `connect:ListQueues`, `connect:DescribeQueue` on the Connect instance ARN
  (`arn:aws:connect:us-east-1:165505826690:instance/6b3f17ba-...`) and its
  `instance/<id>/*` sub-resources — same scoping already used for
  `ConnectReadInstanceResources` in `api-plans-stack.ts`.
- `connect:ListPhoneNumbersV2` in its **own statement**, scoped to
  `arn:aws:connect:us-east-1:165505826690:phone-number/*` plus the bare
  instance ARN — **not** an instance sub-resource. This action evaluates
  against the account-level `phone-number/*` resource type regardless of
  which instance the number belongs to, per this same codebase's own
  documented finding in `infra/lib/stacks/api-campaigns-stack.ts:110-119`
  and the matching `ConnectPhoneNumberV2` statement in
  `api-plans-stack.ts:191-200`. Granting it on `instance/<id>/phone-number/*`
  (an instance sub-resource) would deny every call at runtime — this was
  caught during design review by checking this codebase's own precedent,
  not assumed.
- `dynamodb:Scan`, `dynamodb:PutItem`, `dynamodb:UpdateItem` on
  `VipLocationMapping`.
- `sns:Publish` on `vip-plans-alerts`.
- `kms:Decrypt`, `kms:GenerateDataKey*` on the data CMK (required because
  `vip-plans-alerts` is SSE-KMS-encrypted — same reasoning documented for
  the existing guard role).
- Standard Lambda logging (`logs:CreateLogStream`, `logs:PutLogEvents`) on
  its own log group.

### CDK changes (`infra/lib/stacks/api-plans-stack.ts`)

- Import the role and log group (same pattern as
  `LocationOnboardingGuardRole`/`LocationOnboardingGuardLogs`).
- Define the `lambda.Function` (Python 3.12, code from
  `services/api-plans/src`, handler
  `location_drift_detector.lambda_handler`, memory 256MB, timeout 120s —
  enough margin over the worst case of ~50 sequential `describe_queue`
  calls plus a handful of paginated Scan/List calls, `reservedConcurrentExecutions: 1`,
  `environmentEncryption: props.dataKey`).
- Define an `events.Rule` with `schedule: events.Schedule.rate(Duration.days(1))`
  and `targets.LambdaFunction(detectorFunction)`.
- Environment: `SNS_ALERTS_TOPIC_ARN`, `LOCATION_MAPPING_TABLE`,
  `CONNECT_INSTANCE_ID`, `LOG_LEVEL`.
- Like `location_onboarding_guard`, this Lambda has no VPC and no DLQ —
  add `skipCheckovChecks(detectorFunction, [...])` for `CKV_AWS_117`/`116`
  with justifications specific to this function (EventBridge-scheduled,
  not stream-triggered; a failed run is caught by its own failure SNS plus
  tomorrow's scheduled retry). Confirm via `cdk synth && checkov -d cdk.out
  --framework cloudformation` which checks actually fire rather than
  assuming the guard's skip list transfers unchanged.

### Code change: `services/api-plans/src/location_onboarding_guard.py`

The guard already deployed in this stack fires a `"New state detected
with no canonical phone"` SNS on any DynamoDB Streams `INSERT` into
`VipLocationMapping` that introduces a new `stateCode` with no
`canonicalPhone`. Every draft this detector creates (step 6's `put_item`)
is exactly that kind of `INSERT` — so a low-confidence draft (no phone
match) would trigger the guard's own alert *in addition to* this
detector's own new-drift alert, duplicating the notification for the same
event. Fix: add `if image.get("pendingReview"): continue` as the first
check in `location_onboarding_guard.lambda_handler`'s per-record loop, so
the guard only ever fires on a real, already-reviewed onboarding — which
by then has `pendingReview: false` and still triggers exactly as before.
Add a regression test to the guard's existing suite: an `INSERT` with
`pendingReview: true` and no `canonicalPhone` does not trigger the SNS.

### Code change: `services/api-plans/src/builders.py`

`get_all_location_groups()` must exclude items where `pendingReview` is
truthy — a draft mid-review must not appear in the webapp's state dropdown
before a human confirms it. `locations_for_state_codes()` and
`all_known_locations()` (used by segment/campaign building) need the same
exclusion — a pending draft's `stateCode` shouldn't be usable to build a
real segment yet.

Concretely: `_load_location_mapping()` currently builds `by_code`/
`groups_map` inside the `for item in items:` loop, then builds
`_cache_all_locations` afterward from a *separate* statement that iterates
the full, unfiltered `items` list independently of that loop. Filtering
only inside the loop would fix `by_code`/`groups_map` but silently leave
pending drafts inside `_cache_all_locations`. Filter once, before any of
the three caches are derived — right after the Scan/pagination loop
finishes: `items = [i for i in items if not i.get("pendingReview")]` —
and build all three caches from that same filtered list. Add a regression
test asserting `all_known_locations()` (not just `get_all_location_groups()`)
excludes a `pendingReview: true` item.

**Accepted limitation:** `_load_location_mapping()`'s existing 1-hour
in-process cache means an approved draft (`pendingReview` flipped to
`false`) can take up to an hour to appear in the webapp on an already-warm
Lambda instance. This is the same bounded staleness the cache already has
today for any `VipLocationMapping` edit — not new behavior introduced by
this detector, and not worth building a cache-invalidation path around for
a human-paced review workflow.

## Data flow / algorithm (`location_drift_detector.py`)

1. Scan `VipLocationMapping` (following `LastEvaluatedKey` across pages) →
   build:
   - `known_locations: set[str]` — existing PKs, normalized (see step 4)
     for a tolerant comparison.
   - `known_state_names: dict[str, str]` — `stateCode` → `stateName`, built
     from any non-pending item that has a `stateName` (skip pending drafts
     and any item with `stateName is None` when populating this — first
     non-null, non-pending value wins; Scan order isn't guaranteed, but
     since production data always has all-or-nothing name/slug per
     confirmed row, this is a non-issue in practice).
   - `pending_items: list[dict]` — items where `pendingReview` is truthy,
     for the daily-reminder pass (step 9).
2. `list_queues` (paginated) → filter `Name` starting with `"fd "`
   case-insensitively (`name.strip().lower().startswith("fd ")`) →
   `describe_queue` each → parse `Description` against
   `^Front Desk (.+)$` (case-insensitive). Skip (log at DEBUG) anything
   that doesn't match either filter — most queues aren't location queues
   at all (`Arbitrations`, `BasicQueue`), and that's expected, not an
   error.
3. `list_phone_numbers_v2` (paginated) → parse `PhoneNumberDescription`
   against `^(.+?) - (.+)$` → `(raw_label, purpose)`. Skip non-matching
   descriptions (many numbers have no description, or no state label —
   also expected).
4. Resolve `raw_label` → canonical `stateCode` via:
   ```python
   LABEL_ALIASES = {
       "NYC": "NY",
       "SOUTH CA": "SCA",
       "NORTH CA": "NCA",
   }
   ```
   plus a direct pass-through when `raw_label.strip().upper()` matches
   `^[A-Z]{2,3}$` (2-3 letters — covers every current `stateCode`
   convention) — this lets a brand-new, well-formed code (the first-ever
   `VA` or `GA`) resolve directly without needing to already exist in
   `known_state_names`. Anything else (bare `"CA"`, or a label that's
   neither an alias nor a 2-3 letter code) resolves to `None` —
   unresolved, not guessed.
5. For each queue match, derive a candidate `location` string:
   `f"{resolved_code or raw_label.strip().upper()} - {name_without_fd_prefix.strip().title()}"`
   (e.g. `fd reston` + resolved `VA` → `"VA - Reston"`). `title()`'s known
   quirks (`"mcallen".title()` → `"Mcallen"`, an apostrophe getting
   capitalized) are an accepted casing imperfection a human fixes during
   review, same as any other field — not worth a custom title-casing
   implementation (see Non-goals).

   Compare the candidate against `known_locations` on a normalized basis —
   `_normalize(s) = re.sub(r"\s+", " ", s.strip()).casefold()` (collapse
   whitespace, fold case) — not raw exact-string equality, so the VA/GA
   rows already inserted by hand (which may not match this exact `title()`
   convention byte-for-byte) don't cause a false daily re-detection.
   Anything not matching an existing entry after normalization is a
   **drift**.
6. For each drift, look among the parsed phone entries (step 3) for a
   `canonicalPhone` candidate: a phone whose resolved label matches the
   drift's resolved code (or raw label, if unresolved), whose `purpose`
   case-insensitively equals `"Front Desk"` (not merely contains it — a
   state can have multiple same-label phones for different purposes, e.g.
   VA has both `"VA - Front Desk"` and `"VA - Vein Leads"`; matching by
   label alone risks grabbing the wrong one), and whose number isn't
   already `canonicalPhone` on an existing item for that state. If exactly
   one candidate matches, use it; if zero or more than one, leave
   `canonicalPhone` blank (ambiguous — human decides).

   Track locations claimed by an earlier drift in this same run in a
   plain `set()` and skip a later drift that normalizes to the same value
   (a queue with a same-office duplicate name, drifting twice in the same
   run) — one line of dedup, not a general-purpose fuzzy matcher. If two
   distinct drifts in the same run would both want the one available
   candidate phone, whichever is processed first gets it; the other falls
   back to no `canonicalPhone`/`"low"` confidence — accepted as rare and
   caught by the human review this whole design already requires before
   any draft affects real routing.
7. Confidence: `"high"` if `resolved_code is not None` and exactly one
   phone candidate was found; `"low"` otherwise. This measures structural
   completeness, not "this is verified to be a real physical office" — a
   non-office `fd`-named queue (`"fd va overflow"`, `"fd va test"`) with a
   matching `"Front Desk VA"` description would still score `"high"`. This
   is an accepted trade-off, not a gap to engineer around: **every** draft,
   regardless of confidence, requires a human to review and clear
   `pendingReview` before it can affect the webapp or any real segment
   (Decision 2, `builders.py` change) — the worst case of a wrongly-`"high"`
   non-office draft is one wasted human review cycle, not an incorrect
   production write.
8. Build the draft item and `put_item` with
   `ConditionExpression="attribute_not_exists(#loc)"` (never clobbers a
   human-edited draft or a same-run duplicate):
   ```python
   {
     "location": location,                       # PK
     "stateCode": resolved_code,                  # may be None
     "stateName": known_state_names.get(resolved_code),  # None for a brand-new code
     "slug": (known_state_names.get(resolved_code) or "").replace(" ", "") or None,
     "canonicalPhone": phone_candidate,            # may be None
     "areaCodes": [],                              # always blank — human call
     "pendingReview": True,
     "driftConfidence": confidence,                # "high" | "low"
     "firstDetectedAt": now_iso,
     "lastAlertedAt": now_iso,
   }
   ```
   A `ConditionalCheckFailedException` here is expected (already drafted,
   or onboarded between scan and put) — caught and treated as "already
   tracked", not an error.
9. Alerting, in two passes:
   - **New drifts** (drafts successfully created in step 8 this run): if
     there are 3 or fewer, publish one SNS per drift as before (subject:
     `"New Connect location drift detected: {location}"`, body includes
     confidence and which fields are blank). If more than 3 (expected on
     the first full-instance run against all 50 `fd *` queues), publish a
     single digest SNS instead, listing all of them.
   - **Still-pending reminder** (every item in `pending_items` from step
     1, i.e. `pendingReview: true` before this run started — excludes
     anything just created in step 8, which already got its own alert
     above): re-check each with a consistent-read `get_item` immediately
     before alerting, to catch a human who resolved it in the seconds/
     minutes since step 1's scan — skip (log INFO) anything no longer
     pending. Publish exactly one digest SNS (subject: `"Reminder: N
     locations pending review"`) listing whatever remains, with days
     pending (via `firstDetectedAt`). Skip the publish entirely if nothing
     remains. Only after that publish succeeds, bump `lastAlertedAt` via a
     conditional `update_item` (`ConditionExpression="pendingReview =
     :true"`) for each — if the digest publish fails, skip the bump for
     everyone so `lastAlertedAt` never claims an alert that didn't go out.
10. If `list_queues`, `list_phone_numbers_v2`, or the initial Scan raises,
    log at ERROR, publish one `"Location drift detector run failed"` SNS,
    and return without performing any writes — never act on partial data.
    A per-queue `describe_queue` failure does *not* abort the run — caught,
    logged at WARNING, that queue skipped.

## Error handling

- Top-level try/except covers the three all-or-nothing sources named in
  step 10 (`list_queues`, `list_phone_numbers_v2`, the initial Scan) — not
  the per-item writes in steps 6-9, so one item's failure can't get
  mis-reported as a total run failure.
- Every per-item write (`put_item` in step 8, the reminder pass's
  `get_item`/`update_item` in step 9) and every SNS publish (per-item or
  digest, in steps 9-10) is wrapped in its own try/except: on failure, log
  at ERROR with the relevant `location` and continue the loop — that item
  simply reappears as a fresh drift or still-pending item on tomorrow's
  run. This is required because these run *after* the top-level guarded
  calls have already succeeded; letting one item's exception bubble up
  would either falsely report a total failure (contradicting "never act on
  partial data" — partial data would already be written) or, worse,
  propagate uncaught with no failure SNS at all.
- The top-level failure-notification `sns.publish` (step 10) is itself
  wrapped in a nested try/except — if even that fails, log at
  ERROR/CRITICAL and return, so the Lambda never errors out with zero
  signal for the run.
- `reservedConcurrentExecutions: 1` prevents overlapping runs.
- No DLQ: this is a scheduled, not stream-triggered, Lambda. A failed run
  is caught by its own SNS alert plus tomorrow's scheduled retry.
- **Known gap:** a hard Lambda timeout kills the runtime before any
  try/except can run, so it's the one failure mode with zero SNS output.
  Mitigated by the 120s timeout margin above; a genuine timeout is only
  visible via the Lambda's own CloudWatch `Duration`/`Errors` metrics — no
  dedicated alarm is added here, flagged as a follow-up rather than
  silently accepted.

## Testing

Follow the existing convention in
`services/api-plans/tests/unit/test_location_onboarding_guard.py`:
manual `MagicMock`/`monkeypatch` fakes for `boto3` clients, no
`moto`/stubber. New file:
`services/api-plans/tests/unit/test_location_drift_detector.py`.

Cases to cover:
- Alias resolution: direct match, known alias (`NYC`, `South CA`,
  `North CA`), unresolved bare label (`CA` alone), and a brand-new
  well-formed code never seen before (e.g. first-ever `VA`) resolving via
  the structural pass-through.
- Queue name filter: `fd `-prefixed names reach `describe_queue`;
  non-`fd` names (`"Arbitrations"`, `"BasicQueue"`) don't.
- Description parsing: `"Front Desk VA"` matches; a non-matching
  description is skipped without raising.
- Phone description parsing: a matching `"<LABEL> - <purpose>"` parses;
  one with no `" - "` separator is skipped, not an error.
- Drift detection: an existing location → no drift; a new one → drift.
- Drift detection, normalized match: an existing entry differing from the
  derived candidate only in case/whitespace (e.g. `"va -  reston"` vs.
  `"VA - Reston"`) is treated as already-known, not a false drift.
- Drift detection, same-run dedup: two queues in the same run derive the
  identical normalized candidate → the second is skipped, not double-drafted.
- Confidence: high (resolved code + exactly one phone candidate) vs. low
  (unresolved code, zero candidates, or more than one ambiguous candidate).
- `canonicalPhone` purpose filter: a matching-label phone whose purpose
  isn't `"Front Desk"` is excluded from candidates.
- `canonicalPhone` exclusion: a phone already `canonicalPhone` on an
  existing item for that state is excluded.
- `canonicalPhone` same-run exclusion: two new drifts for the same state
  in one run, one unclaimed matching phone → first drift claims it
  (`"high"`), second falls back to `None`/`"low"`.
- Conditional put: `ConditionalCheckFailedException` on an already-tracked
  location is swallowed, not raised.
- Draft item's `stateName`/`slug`: populated for an already-known code,
  both `None` for a brand-new one; `areaCodes` is always `[]`.
- New-drift alerting: ≤3 new drifts → one SNS per drift; >3 → one digest
  SNS covering all of them.
- Reminder pass: a `pendingReview: true` item from before this run is
  included in the digest and gets `lastAlertedAt` bumped after the digest
  publish succeeds; an item resolved between the scan and the re-check is
  excluded (logged INFO, not alerted, not bumped); a draft created this
  run is not double-counted into this same pass.
- Reminder pass, digest failure: if the digest publish raises, no item's
  `lastAlertedAt` is bumped, and the run doesn't crash.
- Top-level failure path: an exception from `list_queues` (or
  `list_phone_numbers_v2`, or the initial Scan) results in exactly one
  failure SNS and zero DynamoDB writes; that failure-notification publish
  itself raising doesn't propagate a second unhandled exception.
- Per-queue `describe_queue` failure: that queue is skipped (logged
  WARNING), the run completes normally for the rest.
- Per-item write/publish failure (e.g. one `put_item` among several
  raising something other than the expected conditional-check exception):
  that item is logged at ERROR and skipped; the rest of the run proceeds
  normally.
- Pagination: a two-page response (`NextToken`/`LastEvaluatedKey`) for
  `list_queues`, `list_phone_numbers_v2`, and the Scan is each followed to
  completion, not stopped after page one.
- `builders.get_all_location_groups()`, `locations_for_state_codes()`,
  and `all_known_locations()` all exclude `pendingReview: true` items —
  regression tests for each of the three (they're derived from separate
  code paths in `_load_location_mapping()`, so `all_known_locations()`
  needs its own assertion, not just `get_all_location_groups()`).
- `location_onboarding_guard`: an `INSERT` with `pendingReview: true` and
  no `canonicalPhone` does not trigger its SNS (alongside its existing
  coverage of the real, non-pending case).

CI must enforce `pytest --cov=location_drift_detector --cov-fail-under=90`
(or an equivalent project-wide coverage gate covering the new module) so
the ≥90% requirement is checked automatically, not just claimed.

## Rollout

1. Create the IAM role via CLI (cannot go through CDK — permission
   boundary), following the exact grant list above.
2. Create the log group via CLI, same as the existing guard.
3. Deploy via CDK (`ApiPlansStack`) — adds the Lambda, EventBridge rule,
   the `location_onboarding_guard.py` fix, and the `builders.py` filter
   change.
4. The first run will very likely surface most or all of the 50 `fd *`
   queues never checked before (only VA/GA were spot-checked in this
   investigation), not just future new locations — expected, not a bug.
   Per Decision 4 and step 9, this is covered by the digest threshold:
   Sebastian gets one digest SNS for the burst, not 15-30 individual
   messages, and one digest per day afterward for whatever's still open.
   Sebastian reviews each draft (fills `areaCodes`, fixes any casing/
   duplicate issues, clears `pendingReview`) and, for `fd glendale`
   specifically, decides whether it belongs under `SCA`/`NCA` and adds the
   label alias if it's a genuinely new sub-region.
