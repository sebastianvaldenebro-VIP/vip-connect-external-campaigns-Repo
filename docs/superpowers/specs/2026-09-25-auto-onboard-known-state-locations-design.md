# Auto-onboard new locations under an already-known state — Design

## Context

On 2026-09-25, leads with `location` values `"CA - Glendale"` and
`"CT - West Hartford"` were not being called. Root cause, verified against
live production code and data:

- `executor.py::_create_segment` (line ~4367-4376) builds a campaign's lead
  filter as `FilterRule(field="location", operator=IN,
  values=tuple(locations_for_state_codes(state_codes)))` —
  `locations_for_state_codes` returns exactly the `location` strings
  already present in `VipLocationMapping` for the campaign's selected
  states.
- `"CA - Glendale"` and `"CT - West Hartford"` had no row in
  `VipLocationMapping` at all, so they were never part of that `IN` list —
  `matches_group` correctly excludes any lead whose `location` isn't in
  it, so those leads never enter `entries` and never get called.
- A pre-existing, separate block in the same function (line ~4420-4469)
  already detects this exact condition — it scans every Redis record's
  `location` against `all_known_locations()` and, for anything unknown,
  emits a CloudWatch metric (`VipConnect/ProgressiveDialer` /
  `UnknownLocation`) that a pre-existing alarm (`vip-plans-unknown-location`,
  live since 2026-07-17) watches, alerting 5 subscribers by email including
  Sebastian. It fired correctly today. **This block is telemetry only — it
  does not affect which records enter `entries`.**
- The two locations were fixed by hand today via manual `put-item` (same
  as the VA/GA incident on 2026-09-23). The gap this design closes: the
  fix is currently always manual, so every occurrence costs missed calls
  for as long as it takes a human to notice the alarm and run the CLI
  command.
- "CT - West Hartford" (state `CT`) is a location under a state that was
  **already onboarded** — every existing row for a given `stateCode`
  carries an identical `canonicalPhone`/`areaCodes`/`slug`/`stateSortOrder`
  (verified against all 4 `CT` rows in production). Adding a new location
  under an *already-known* state is therefore mechanical — copy those four
  fields from any sibling row — no human judgment call is needed, unlike
  onboarding a genuinely new state (VA/GA precedent), where
  `canonicalPhone`/`areaCodes` don't exist yet anywhere to copy from.
- "CA - Glendale" is a **different, harder case** and is *not* solved by
  this design (see Non-goals): its raw label, `"CA"`, is genuinely
  ambiguous between `SCA` and `NCA` — every SCA and every NCA location is
  stored as `"CA - <city>"` (verified against
  `frontend/src/lib/stateLocationMap.ts`, which mirrors
  `VipLocationMapping`), so there is no `VipLocationMapping` row whose
  `stateCode` is literally `"CA"`, only `"SCA"`/`"NCA"`. Resolving it by
  hand today required looking at the lead's area code (818) against
  `SCA`'s `areaCodes` — a per-record signal this design's label-prefix
  algorithm does not have available (`unknown_locs` carries only the
  distinct `location` strings, not each record's phone number). "CA -
  Glendale" is called out here only to make the scope boundary explicit,
  not as a case this design closes.

## Non-goals

- **Auto-onboarding a genuinely new state** (no existing row for that
  `stateCode` at all). `canonicalPhone`/`areaCodes` can't be derived from
  nothing — stays a human decision, same as VA/GA. The existing
  `UnknownLocation` alarm continues to fire for this case exactly as it
  does today; this design does not change that path.
- **Resolving an ambiguous multi-code label family, e.g. bare `"CA"`**
  (matches both `SCA` and `NCA`, as in the "CA - Glendale" example above).
  Label-prefix matching against `groups_by_code`/`_LABEL_ALIASES` is
  structurally the wrong mechanism here — disambiguating it correctly
  requires a per-record signal (the lead's area code against each
  candidate code's `areaCodes`) that `unknown_locations` (a set of
  distinct location strings, already stripped of any per-record context)
  does not carry. This stays a human decision, same as a genuinely new
  state — the existing `UnknownLocation` alarm continues to fire for it.
  A future iteration could thread area codes through `unknown_locs` (e.g.
  as a `location -> set[area_code]` mapping instead of a bare set) to
  close this gap, but that's a larger change to the detection pipeline in
  `_create_segment` and is out of scope here.
- **Retroactively including the newly-onboarded location in the specific
  `_create_segment` call whose per-record loop discovered it.** `rules`
  (and its `locations` list) are already constructed earlier in that same
  call, before the per-record loop that discovers the unknown location
  runs — that particular campaign's own segment build cannot benefit from
  a discovery it makes partway through itself. This is an accepted latency
  of "one `_create_segment` call," not a promise of same-call correction
  for the campaign that made the discovery.
  Note this is narrower than "one plan run": a single Lambda invocation's
  tick loop (`executor.py` line ~3376-3381, `for ci in newly_ready: ...
  _start_one_campaign(...)`) can call `_start_one_campaign` — and therefore
  `_create_segment` — for multiple ready campaigns in the same tick. Because
  auto-onboarding resets the module-global `builders._cache_by_code = None`
  synchronously (Decision #4), a *later* campaign processed in that same
  invocation, whose `state_codes` includes the code just onboarded, gets a
  rescanned `locations_for_state_codes()` result and includes the
  newly-onboarded location in its own `rules` immediately — same-invocation
  benefit, not a full plan-run-cycle wait. The "one plan run cycle" latency
  only applies when the discovering campaign is the only one (or the last
  one) touching that state code in a given invocation; it is not a
  universal guarantee across every campaign in the run.
- **A previously-superseded design**, `docs/superpowers/specs/2026-09-24-connect-location-drift-detector-design.md`
  (a separate Lambda polling Connect queue names daily). That design's
  signal (Connect queue naming) would have caught "CA - Glendale" only
  weakly (ambiguous label) and would **not have caught "CT - West Hartford"
  at all** — no queue or phone number in Connect carries any signal for it.
  This design uses the strictly better, already-existing signal (real lead
  `location` values compared against `VipLocationMapping`, computed on
  every segment build) instead. That spec/plan are left in place
  (untouched, not deleted) but superseded — no further work should proceed
  on them.

## Decisions

1. **Where the fix lives:** inside `builders.py`, as a new function called
   from `executor.py::_create_segment` right where `unknown_locs` is
   already computed (line ~4425-4431) — reuses the exact detection this
   codebase already runs on every segment build, rather than a new
   Lambda/schedule.
2. **Scope of automatic action:** only ever inserts a **complete**,
   non-draft row (no `pendingReview` flag — unlike the superseded design,
   there is no ambiguous field left to review, since every field is copied
   verbatim from an existing sibling). Never invents `canonicalPhone`/
   `areaCodes` for a `stateCode` that has no existing row, and never
   resolves a label that matches more than one existing code (see
   Non-goals) — `_resolve_known_code` requires an exact or aliased match
   against a single `groups_by_code` key.
3. **Alerting:** locations that get auto-onboarded are removed from
   `unknown_locs` **before** the existing CloudWatch metric/alarm block
   runs, so the alarm only fires for the genuinely-unresolved case (new
   state, or an unresolvable label) — not for something this same
   invocation just fixed. No new alerting infrastructure.
4. **Cache correctness:** after inserting a new row, reset
   `builders._cache_by_code = None` (mirroring the test file's own
   `_reset_cache()` helper, `test_builders_location_mapping.py:19-23`) so
   the next `_load_location_mapping()` call in this same warm Lambda
   instance re-scans immediately, rather than serving a stale cache for up
   to the existing 1-hour TTL. Setting `_cache_ts = 0` alone is **not**
   sufficient: `_load_location_mapping()`'s guard is `if _cache_by_code is
   not None and (now - _cache_ts) < _CACHE_TTL: return cached`, where `now
   = time.monotonic()`. `time.monotonic()`'s reference point is typically
   the boot of the underlying execution environment (a Firecracker
   microVM for Lambda), not this request — so for a warm instance alive
   less than the 1-hour TTL, `now` is already `< _CACHE_TTL`, and
   `(now - 0) < _CACHE_TTL` stays `True` while `_cache_by_code is not
   None` is also still `True` (the dict itself was never cleared), so the
   very next call would keep serving the stale, pre-onboarding cache.
   Clearing `_cache_by_code` is what actually forces the `is not None`
   check to fail and triggers a real rescan, regardless of how long the
   instance has been alive.
5. **IAM:** the main `api-plans` Lambda role (`role` in
   `api-plans-stack.ts`, backing `vip-admin-ui-api-plans`) currently has
   only `locationMappingTable.grantReadData(role)` — needs `PutItem` added.
   This role is a normal CDK-managed `iam.Role` (not imported with
   `{ mutable: false }` like the guard/detector roles), so this is a
   scoped `dynamodb:PutItem` grant via CDK — no CLI workaround needed.
   Scoped rather than a plain `grantWriteData`/`grantReadWriteData`: the
   only DynamoDB call `auto_onboard_known_state_locations` ever makes is a
   single-item conditional `put_item` (Decisions #2 — it never updates or
   deletes an existing row), and this role also carries broad Connect
   Campaigns/Customer Profiles permissions for the same
   `vip-admin-ui-api-plans` Lambda, so it should not gain
   `UpdateItem`/`DeleteItem`/`BatchWriteItem` on `VipLocationMapping` — a
   live, prod call-routing config table — beyond what this feature's code
   actually calls. No KMS grant needs to be added alongside this: the
   table's CMK
   (`arn:aws:kms:us-east-1:165505826690:key/752b91cf-9bd3-45c5-a297-808c517eb646`)
   carries a resource-based key policy statement granting
   `kms:Decrypt`/`Encrypt`/`GenerateDataKey*` to any principal in account
   `165505826690` when accessed via `kms:ViaService =
   dynamodb.us-east-1.amazonaws.com` (verified via `aws kms
   get-key-policy`) — the same reason the existing `grantReadData` already
   works today without an explicit identity-based KMS statement on `role`.
   Implementers should not "fix" a `put_item` `AccessDeniedException` by
   adding a KMS grant here; if one occurs, the cause is elsewhere.

## Code changes

### `services/api-plans/src/builders.py`

Extend `_load_location_mapping()`'s `groups_map` entries to also carry
`canonicalPhone` and `areaCodes` (currently only `state`/`slug`/`code`/
`stateSortOrder`/`locations`):

```python
groups_map[code] = {
    "state": item["stateName"],
    "slug": item["slug"],
    "code": code,
    "stateSortOrder": int(item.get("stateSortOrder", 99)),
    "canonicalPhone": item.get("canonicalPhone"),
    "areaCodes": item.get("areaCodes") or set(),
    "locations": [],
}
```

**Type note:** deliberately `item.get("areaCodes") or set()`, not
`list(item.get("areaCodes") or [])`. Production `areaCodes` is written as a
DynamoDB String Set (`SS`) — confirmed at
`infra/scripts/backfill-location-canonical-phone.py` line ~76
(`ExpressionAttributeValues={..., ":a": set(codes)}`, which boto3's
resource layer serializes as `SS`) — and boto3's resource layer
deserializes an `SS` attribute back into a Python `set`. Forcing it through
`list(...)` here would silently convert every group's cached value from
`set` to `list`, and since `auto_onboard_known_state_locations` copies this
cached value verbatim into a new item's `put_item` call, that `list` would
then get serialized as DynamoDB type `List` (`L`) instead of `SS` for the
newly-onboarded row — a different attribute type than every hand-onboarded
sibling row for the same `stateCode`, breaking Decision #2's "copied
verbatim" claim at the storage-type level even though the values match.
Keeping the native `set` (falling back to an empty `set()`, not `[]`, when
the item has no `areaCodes` yet) preserves the original DynamoDB type all
the way through the copy, so a new row's `areaCodes` round-trips as `SS`
just like its sibling.

`get_all_location_groups()` must keep stripping internal-only fields from
its public API response — extend its existing strip set from just
`stateSortOrder` to also exclude `canonicalPhone`/`areaCodes`:

```python
return [
    {k: v for k, v in g.items() if k not in ("stateSortOrder", "canonicalPhone", "areaCodes")}
    for g in groups
]
```

Add:

```python
_LABEL_ALIASES = {"NYC": "NY", "SOUTH CA": "SCA", "NORTH CA": "NCA"}


def _resolve_known_code(raw_label: str, groups_by_code: dict[str, dict]) -> str | None:
    """Resolve a location string's leading label to a stateCode that
    ALREADY has at least one row in VipLocationMapping. Never invents a
    new code — a label with no existing match returns None."""
    key = raw_label.strip().upper()
    if key in groups_by_code:
        return key
    aliased = _LABEL_ALIASES.get(key)
    if aliased and aliased in groups_by_code:
        return aliased
    return None


def auto_onboard_known_state_locations(unknown_locations: set[str]) -> set[str]:
    """For each location string with a resolvable, already-known stateCode
    prefix, insert a complete VipLocationMapping row by copying
    canonicalPhone/areaCodes/slug from an existing sibling row of that
    code. Returns the subset that could NOT be auto-onboarded (unresolvable
    prefix, or a genuinely new stateCode) for the caller to still alert on.
    """
    if not unknown_locations:
        return set()

    _, groups, _ = _load_location_mapping()
    groups_by_code = {g["code"]: g for g in groups}
    table = boto3.resource("dynamodb").Table(_LOCATION_TABLE)
    import logging

    logger = logging.getLogger(__name__)

    still_unresolved: set[str] = set()
    onboarded_any = False

    for loc in unknown_locations:
        if " - " not in loc:
            still_unresolved.add(loc)
            continue
        raw_label, _, _rest = loc.partition(" - ")
        code = _resolve_known_code(raw_label, groups_by_code)
        if code is None:
            still_unresolved.add(loc)
            continue

        sibling = groups_by_code[code]
        item = {
            "location": loc,
            "stateCode": code,
            "stateName": sibling["state"],
            "slug": sibling["slug"],
            "canonicalPhone": sibling["canonicalPhone"],
            "areaCodes": sibling["areaCodes"],
            "stateSortOrder": sibling["stateSortOrder"],
        }
        try:
            table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(#loc)",
                ExpressionAttributeNames={"#loc": "location"},
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                # Already onboarded — race with a manual add or a concurrent
                # invocation. The row now genuinely exists in DynamoDB either
                # way, but THIS instance's in-process _cache_by_code/_cache_groups
                # (built by _load_location_mapping()) is still the pre-onboarding
                # snapshot — it doesn't contain the row just because some other
                # process wrote it. Treat this the same as a successful onboard
                # for cache-invalidation purposes, so this instance re-scans on
                # its next call instead of serving a stale cache for up to the
                # remaining 1-hour TTL.
                onboarded_any = True
                continue
            logger.warning(
                "auto_onboard_known_state_locations put_item failed for %s: %s", loc, exc
            )
            still_unresolved.add(loc)
            continue
        onboarded_any = True

    if onboarded_any:
        global _cache_by_code
        _cache_by_code = None

    return still_unresolved
```

Clearing `_cache_by_code` rather than `_cache_ts` is deliberate (see
Decision #4): `_cache_ts = 0` alone does not reliably force
`_load_location_mapping()`'s next call to rescan, because
`time.monotonic()` is not guaranteed to already exceed `_CACHE_TTL` for a
warm instance. Setting `_cache_by_code = None` directly fails the `is not
None` half of the cache-hit guard, forcing a real rescan unconditionally
on the next call — the same technique the test file's `_reset_cache()`
helper already relies on.

`ClientError` needs importing in `builders.py` (`from botocore.exceptions
import ClientError`) — not currently imported there (it's imported in
`executor.py`, a different module).

### `services/api-plans/src/executor.py`

Import the new function alongside the existing `builders` imports
(line ~53-62): add `auto_onboard_known_state_locations` to the `from
builders import (...)` block.

Change the existing block (line ~4439, currently `if unknown_locs:`) to
resolve what can be auto-fixed before deciding what to alert on:

```python
    if unknown_locs:
        try:
            unknown_locs = auto_onboard_known_state_locations(unknown_locs)
        except Exception as exc:
            logger.warning("auto_onboard_known_state_locations failed: %s", exc)

    if unknown_locs:
        # existing CloudWatch metric block below is unchanged, but now only
        # sees whatever auto-onboarding above couldn't resolve.
```

The `if unknown_locs:` truth test that gates entry into this block runs
once, before auto-onboarding reassigns `unknown_locs` — if every location
in the original set gets successfully auto-onboarded, `unknown_locs`
becomes `set()`, and without a second gate the existing `_slog.warn(
"unknown_locations_detected", locations=[])` would still log misleadingly
(nothing is actually still-unknown) and the unconditional "Dimensionless
total" `cw.put_metric_data(... Value=len(loc_list) ...)` (outside the
per-20 dimensional loop) would still fire with `Value: 0` — a real,
avoidable CloudWatch API call plus a confusing log line for a run where
auto-onboarding resolved everything. Re-checking `if unknown_locs:` after
the auto-onboard call (splitting the single `if` into two, as shown above)
skips the log/metric block entirely in that case, matching Decisions #3's
intent that the alarm/telemetry path should only see the genuinely-
unresolved case.

Everything below that second `if unknown_locs:` (the existing `_slog.warn`
and `cloudwatch.put_metric_data` calls) stays exactly as-is, now operating
on the narrowed `unknown_locs`, and simply doesn't execute at all when the
narrowed set is empty.

### `infra/lib/stacks/api-plans-stack.ts`

Keep the existing read grant and add a scoped write grant for exactly the
action the new code calls:

```typescript
locationMappingTable.grantReadData(role);
locationMappingTable.grant(role, 'dynamodb:PutItem');
```

(line ~354-355). Deliberately not `grantWriteData`/`grantReadWriteData` —
those also grant `UpdateItem`/`DeleteItem`/`BatchWriteItem`, none of which
`auto_onboard_known_state_locations` ever calls (see Decisions #5). This
role (`FunctionRole`) is attached only to `FunctionPlans`
(`vip-admin-ui-api-plans`) and already carries broad Connect Campaigns
V2/Customer Profiles/EventBridge permissions for that same function; the
sibling `LocationOnboardingGuardFunction` uses a separate, CLI-created,
immutable `guardRole` and is unaffected by this change. Verified by
grepping every `role,`/`role:` usage in `api-plans-stack.ts`: the
CDK-managed `role` is attached only to `this.lambdaFunction`
(`FunctionPlans`); no other Lambda in this stack shares it, so the scoped
`PutItem` grant added here cannot leak onto `LocationOnboardingGuardFunction`
or any other function in this file — safe to proceed with the grant as
specified, no additional scoping needed. A "simpler"
implementation that swaps `grantReadData(role)` for
`grantReadWriteData(role)` instead of adding the scoped `PutItem` grant
must **not** be used — it widens this role's blast radius on a live,
`DeletionProtection`-enabled prod table with an active DynamoDB Stream for
no functional benefit, since `Update`/`Delete`/`BatchWrite` are never
called. This is the only infra change — no new Lambda, no new IAM role, no
new EventBridge rule, no CLI steps.

**Existing test to update:** `infra/lib/stacks/api-plans-stack.test.ts`
(around line 370-374) already has a test, `'grants read access to the
imported VipLocationMapping table unconditionally'`, whose last assertion
is `expect(actions).not.toContain('dynamodb:PutItem')`. `actionsForResource`
aggregates the `Action` list from every IAM policy statement whose
`Resource` contains `'VipLocationMapping'`, regardless of which `grant*`
call produced it — so once the new `grant(role, 'dynamodb:PutItem')` call
lands, that assertion will fail. Rename the test to `'grants read and
scoped write access to the imported VipLocationMapping table'`, drop the
`.not.toContain('dynamodb:PutItem')` line, and add in its place assertions
mirroring the existing `'grants read-write access to the PlansTable
itself'` test just above it:

```typescript
expect(actions).toEqual(
  expect.arrayContaining(['dynamodb:GetItem', 'dynamodb:Query', 'dynamodb:PutItem']),
);
expect(actions).not.toContain('dynamodb:UpdateItem');
expect(actions).not.toContain('dynamodb:DeleteItem');
expect(actions).not.toContain('dynamodb:BatchWriteItem');
```

## Error handling

- `auto_onboard_known_state_locations` never raises out of its own loop —
  a `put_item` failure (other than the expected
  `ConditionalCheckFailedException`) for one location is logged and that
  location falls through to `still_unresolved` (so it still gets the
  existing CloudWatch alarm treatment) rather than being silently dropped
  or aborting the whole segment build.
- The call site in `executor.py` wraps the call in its own try/except —
  if `auto_onboard_known_state_locations` itself raises unexpectedly
  (e.g. the DynamoDB table resource construction fails), `unknown_locs`
  is left as the original, full set, so the existing alarm still fires for
  everything — this new code path can only ever reduce the set the alarm
  sees, never suppress it entirely on its own failure.
- `_create_segment`'s caller already tolerates this function taking
  slightly longer (a `put_item` per genuinely-new location, at most a
  handful per run in practice) — no timeout change needed; this runs
  inside the already-provisioned `vip-admin-ui-api-plans` Lambda
  (`timeout: 300` seconds per its CDK definition), not a new function with
  its own budget to size.

## Testing

New tests in `services/api-plans/tests/unit/test_builders_location_mapping.py`
(alongside the existing `_load_location_mapping` tests, same
`MagicMock`/`patch("boto3.resource", ...)` convention).

**Cache reset required in every new test that reaches the loop:**
`auto_onboard_known_state_locations()` calls the real, module-cached
`builders._load_location_mapping()`, and `builders._cache_by_code` /
`_cache_groups` / `_cache_ts` are module-level globals that persist across
tests in the same process — exactly why every existing test in this file
(`test_scans_table_and_builds_grouped_caches`,
`test_uses_cached_value_within_ttl_without_rescanning`, etc.) calls the
file's existing `_reset_cache()` helper at the start (and end) of the
test. Every new `auto_onboard_known_state_locations`/`_resolve_known_code`
test that reaches the `for loc in unknown_locations` loop (i.e. anything
other than the pure-dict `_resolve_known_code` cases, which take
`groups_by_code` directly and never touch the cache) must call
`_reset_cache()` at the start and end of the test too, or it will
nondeterministically see a prior test's mocked `scan.return_value` (via a
non-zero `_cache_ts` short-circuiting the scan) — this matters especially
for the bullet below that asserts on `builders._cache_by_code`'s
before/after identity.

**Fixture must carry real `canonicalPhone`/`areaCodes`, not the bare
`_fake_items()` stub:** the file's only existing fixture, `_fake_items()`,
has no `canonicalPhone`/`areaCodes` on any item (only
`location`/`stateCode`/`stateName`/`slug`/`stateSortOrder`), so
`item.get("canonicalPhone")` and `item.get("areaCodes") or []` resolve to
`None`/`[]` for every row it produces. The "already-known code" test below
needs the `CT` sibling row it mocks via `scan.return_value` to carry a
concrete, non-empty `canonicalPhone` (e.g. `"+18605551234"`) and
`areaCodes` (e.g. `{"860", "959"}` — a Python `set`, matching the real
`SS` type boto3 returns for this attribute, not a list) — either by
extending `_fake_items()` with an optional `canonical_phone`/`area_codes`
kwarg, or by building a dedicated fixture for this test — so the assertion
that the new row copies these fields from the sibling is actually proving
the copy mechanism works, rather than trivially comparing `None == None`.
The test must also assert the new item's `areaCodes` is still a `set` (not
a `list`) after the copy — i.e. round-trips as the same DynamoDB type as
the sibling's (`SS`, not `L`) — not just the same member values, per the
type-preservation note in the `builders.py` section above.

- `_resolve_known_code`: direct match against an existing code; known
  alias (`NYC`, `South CA`, `North CA`) resolving to an existing code;
  unresolvable bare label `CA` (the documented Non-goal — it matches
  neither `SCA` nor `NCA` directly, and is genuinely ambiguous between the
  two, not a mechanism bug) returns `None`; a well-formed but
  never-before-seen code (e.g. `VA`, no existing row) also returns `None`
  — the whole point is it must never resolve a code that isn't already
  onboarded.
- `auto_onboard_known_state_locations`:
  - A location under an already-known code (`"CT - West Hartford"`, `CT`
    already has rows) gets `put_item`'d with the sibling's
    `canonicalPhone`/`areaCodes`/`slug`/`stateName`/`stateSortOrder`
    copied exactly, including `areaCodes`'s type (`set`, not `list`), and
    is removed from the returned still-unresolved set.
  - A location with an ambiguous, unresolvable label (`"CA - Glendale"`,
    bare `CA` — the documented Non-goal case) is left in the returned
    set, no `put_item` call for it.
  - A location string with no `" - "` separator is left in the returned
    set, no `put_item` call, no exception.
  - `ConditionalCheckFailedException` on `put_item` (already onboarded by
    a concurrent run) is swallowed — that location is removed from the
    returned set anyway (it's not actually unresolved, just already
    handled).
  - A generic `put_item` failure (not the conditional-check case) leaves
    that location in the returned set and does not raise.
  - After at least one successful `put_item`, `builders._cache_by_code` is
    reset to `None` (Decision #4); with zero successful onboardings (e.g.
    everything was unresolvable), `_cache_by_code` is left untouched
    instead. Asserting "left untouched" against the post-`_reset_cache()`
    value of `None` would be vacuous (`None is None` proves nothing — the
    same trivial-comparison pitfall called out above for the
    `canonicalPhone`/`areaCodes` fixture), so this test must instead give
    `_cache_by_code` a real, non-`None` value before calling
    `auto_onboard_known_state_locations`, then assert that exact object is
    still in place afterward. Concretely: after `_reset_cache()`, call the
    real `builders._load_location_mapping()` once (with `scan.return_value`
    already mocked, same as every other test in this file) so it performs
    its scan and populates `_cache_by_code` for real; capture that resulting
    dict via `populated = builders._cache_by_code`. Then call
    `auto_onboard_known_state_locations` with an unresolvable-only input
    (e.g. `{"CA - Glendale"}`, zero successful onboardings), and assert
    `builders._cache_by_code is populated` — the exact same dict object,
    untouched — proving the code genuinely skips the cache-invalidation
    reset when `onboarded_any` is `False`, rather than merely observing an
    already-`None` value stay `None`. For the successful-onboarding case
    above, assert the opposite: `builders._cache_by_code is None` after the
    call, proving the reset actually ran.
  - Empty input returns an empty set with no DynamoDB calls at all.
- `get_all_location_groups()` (existing test file
  `test_location_mapping.py`): still excludes `canonicalPhone`/
  `areaCodes`/`stateSortOrder` from its response now that `groups_map`
  carries the first two — regression test alongside the existing
  `stateSortOrder`-not-present assertion. This requires updating
  `_STUB_GROUPS` (module-level, lines ~29-40) to actually include
  `canonicalPhone`/`areaCodes` on at least one group first — today it
  only has `state`/`slug`/`code`/`locations`, so an
  `assert "canonicalPhone" not in g` added without this change would pass
  vacuously (the key was never in the stubbed dict to begin with, so it
  proves nothing about `get_all_location_groups()`'s actual strip
  behavior). Add both keys to the stub, matching what
  `_load_location_mapping()` will now really produce, then assert they're
  absent from the handler's JSON response.

In `services/api-plans/tests/unit/test_executor_create_segment_gaps.py`
(`TestUnknownLocationMetricEmission`, alongside its existing
`test_emits_cloudwatch_metric_for_unknown_locations` and
`test_swallows_cloudwatch_metric_emit_failure` — same `patch("executor.X",
...)` convention against a real `_create_segment` call, not a unit test of
an isolated function):

- A new test feeds **two** Redis records with distinct, unlisted `location`
  values (e.g. `"MYSTERY"` and `"MYSTERY2"`, neither present in the mocked
  `VipLocationMapping` scan) so the real, pre-auto-onboard `unknown_locs`
  has two members — not the single-member set every other test in this
  file produces. It then patches
  `executor.auto_onboard_known_state_locations` to return a strict,
  non-empty subset (e.g. `{"MYSTERY2"}`, simulating `"MYSTERY"` being
  auto-resolved) and asserts the CloudWatch `put_metric_data` mock's
  captured `Dimensions`/`Value` reflect that narrowed, still-non-empty
  subset, not the original two-member `unknown_locs`. A single-location
  input is unsuitable here: every existing `MYSTERY`-only test in this file
  produces exactly one unknown location, so the only non-trivial "narrowing"
  of a one-element set is to `set()` — and with an empty result, the
  second `if unknown_locs:` gate in `executor.py` is `False`,
  `cw.put_metric_data` is never called at all, and there is nothing for
  this test to assert `Dimensions`/`Value` against.
- A new test patches `executor.auto_onboard_known_state_locations` to
  raise, and asserts `_create_segment` still completes (the existing
  `all_known_locations`/metric-emission behavior is unaffected — the
  original full `unknown_locs` set reaches the CloudWatch call, same as
  today).
- **Required alongside this change** (not optional cleanup): add
  `patch("executor.auto_onboard_known_state_locations", return_value=<the
  same unknown_locs set the test already expects, unchanged>)` to the 3
  pre-existing tests in this file that feed a `location="MYSTERY"` record
  (no `" - "` separator) but don't yet mock this call —
  `TestUnknownLocationFetchFailure.test_falls_back_to_empty_known_locs_on_exception`,
  `TestUnknownLocationMetricEmission.test_emits_cloudwatch_metric_for_unknown_locations`,
  and `test_swallows_cloudwatch_metric_emit_failure`. Without this, all
  three would call the real, unmocked
  `auto_onboard_known_state_locations` — which calls
  `boto3.resource("dynamodb")` — on every run once `executor.py` is wired
  per this design, reviving exactly the ambient-AWS-config hang/flake risk
  this repo's `tests/unit/conftest.py` already documents and worked around
  for CloudWatch. The implementer must add this mocking as part of this
  change, not as a follow-up.

`from botocore.exceptions import ClientError` needs adding to `builders.py`'s
top-level imports (line ~13-21) — verified not already present. For the
warning log inside `auto_onboard_known_state_locations`, follow this
file's own existing convention (verified at lines 341 and 425: a local
`import logging` + `logger = logging.getLogger(__name__)` inside the
function itself, not a module-level import) rather than introducing a
new, inconsistent module-level `import logging`.

The same `from botocore.exceptions import ClientError` import also needs
adding to `test_builders_location_mapping.py` itself — that file currently
imports only `os`, `sys`, `time`, and `MagicMock`/`patch` from
`unittest.mock` (`test_executor_create_segment_gaps.py` already has this
import, but that's a different file). The two `auto_onboard_known_state_locations`
cases above that construct a `ClientError` as `mock_table.put_item.side_effect`
(the `ConditionalCheckFailedException` case and the generic-failure case)
need it to build those instances.
