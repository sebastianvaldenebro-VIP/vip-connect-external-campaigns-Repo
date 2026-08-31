# Reconcile Expected/Actual Normalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `_create_segment`'s already-computed "how many leads matched vs. how many ended up in the segment" counts available as `cs["reconcile"] = {"expected": int, "actual": int, "retries": int}` on every campaign state, uniformly across telephony, branded, SMS, and pre-warmed campaigns — so the future Campaign Monitor UI's reconcile badge ("expected → actual · N retries") has real data for every delivery type, not just the one path that happens to track retries today.

**Architecture:** `_create_segment` already computes `total_matched` (leads matching the filter, before the 3,000-member truncation) and effectively computes the final phone count (`len(phones_e164)`, after truncation and phone-normalization exclusions) — both are calculated today and then discarded, only surfacing in a log line when truncation happens. This plan changes `_create_segment`'s return type from `tuple[str, str]` to `tuple[str, str, int, int]` (name, arn, expected, actual) and threads those two ints through its 4 call sites (3 inside `_start_one_campaign`, 1 inside `_create_campaign_only`) plus `_create_campaign_only`'s own 2 callers. `retries` reuses the existing `cs.get("reconcileRetries", 0)` — only the telephony-native path increments it today (it has the only pre-existing retry loop), so the other paths correctly report `retries: 0`, which is the truth, not a gap.

**Tech Stack:** Python 3.12, pytest + `unittest.mock`.

**Spec:** No separate spec file — this plan's Architecture section, backed by the line-by-line verification recorded in Global Constraints, is the authority. `executor.py` runs in production today for 4 active plans.

## Global Constraints

- This is the **one plan-B increment that changes an existing function's return arity**, not pure instrumentation — treat it with the same production care as the Day Activity Feed plan, but recognize this diff necessarily includes real `-`/`+` line pairs at every call site (unpacking one more value is not "additive-only" in the literal sense Plan B1 used — don't apply that same "zero `-` lines" bar here; the bar instead is "every existing behavior/control-flow/value used downstream is unchanged, only a new field is added to `cs`").
- `_create_campaign_only`'s type hint (`-> tuple[str, str, str]`) and docstring (`Returns (connectCampaignId, segmentName, segmentArn)`) are **already stale** on `main` — the function actually returns a 4-tuple including `warmup_started` (verified: both of its call sites already unpack 4 values). This plan adds 2 more values (6-tuple total) and should fix the docstring/type hint to reflect reality while doing so — but do not touch anything else about this pre-existing inconsistency.
- The prestart-check handler's call to `_create_campaign_only` (the one passing `run={}`) has no real run/plan-run context — there is no `cs` to persist `cs["reconcile"]` into at that call site. Do not invent one; the plan's own design is to receive and discard the 2 new values there (`_, _`).
- `expected`/`actual` are `None` (not `0`) on any path that skips `_create_segment` entirely (the operator-pinned-segment fast paths) — `None` means "not applicable" (a manually pinned segment, not measured), which is a different, legitimate state from "0 leads matched." Never coerce `None` to `0`.
- `cs["reconcile"]` is only ever set on a path that reaches a **successful** segment creation. A campaign that errors out before a segment exists must not get a `cs["reconcile"]` key at all (absence, not a zero-filled placeholder) — this mirrors how `cs["segmentName"]`/`cs["segmentArn"]` are already only set on success in this codebase.
- Run the full `services/api-plans` test suite before and after each task (`cd services/api-plans && python3 -m pytest tests/unit -q`). Baseline going into this plan: 449 passed, 0 failed (post Day-Activity-Feed plan + its final-review fix).
- Do not touch the Day Activity Feed plan's `_record_plan_event` call sites, reconcile-retry event payload shape, or any of its 12 instrumentation points — this plan is purely about the `cs["reconcile"]` data field, a separate concern that happens to live near some of the same code.

---

## File Structure

```
services/api-plans/src/executor.py                    # MODIFY — _create_segment signature (+2 call sites' return), _create_campaign_only signature (+2 callers), 3 _start_one_campaign call sites
services/api-plans/tests/unit/test_executor_v2.py      # MODIFY — new tests for _create_segment's new return values and cs["reconcile"] at each of the 4+2 sites
```

---

### Task 1: `_create_segment` returns expected/actual; wire the 3 direct `_start_one_campaign` call sites

**Files:**
- Modify: `services/api-plans/src/executor.py` — `_create_segment` (currently `def` at line 4306, two `return` statements at lines 4487 and 4495) and its 3 direct call sites inside `_start_one_campaign`: branded (line 3612), SMS (line 3681), telephony-native fresh-start (line 3796, inside a retry loop, success continuation at lines 3900-3901).
- Test: `services/api-plans/tests/unit/test_executor_v2.py`

**Interfaces:**
- Produces: `_create_segment(bucket, campaign=None) -> tuple[str, str, int, int]` — `(segment_name, segment_arn, expected, actual)`, where `expected = total_matched` (leads matching the filter before the 3,000-member truncation) and `actual = len(phones_e164)` (final count of valid E.164 phones actually placed into the Customer Profiles segment, after truncation AND after excluding leads whose phone didn't normalize). Task 2 (this plan) consumes this same signature at `_create_campaign_only`'s call site.
- Produces: `cs["reconcile"] = {"expected": int, "actual": int, "retries": int}` set on 3 of the eventual sites (branded/SMS/telephony-native), only on the success path of each.

**Read this first, verify against the live file before writing code:** lines 4306-4497 (`_create_segment`'s full body), lines 3607-3656 (branded path), lines 3660-3707 (SMS path), lines 3788-3901 (telephony-native path: retry loop + success continuation). Line numbers may have drifted since this brief was written if any other plan landed in between — locate every anchor by the quoted code below, not by trusting these numbers blindly.

- [ ] **Step 1: Write the failing test for `_create_segment`'s new return values**

Add to `services/api-plans/tests/unit/test_executor_v2.py` (this file already has extensive fixtures/mocks for `_create_segment` — search for existing tests calling it directly or via `_start_one_campaign` with a mocked Redis source, and model your fixture setup on one of those rather than inventing a new mock shape from scratch):

```python
class TestCreateSegmentReconcileCounts:
    def test_returns_expected_and_actual_when_no_truncation(self):
        """expected == actual when every matched lead has a valid phone and none are truncated."""
        import executor

        redis_source = MagicMock()
        redis_source.is_ready.return_value = True
        redis_source.iter_records.return_value = [
            {"customerid": "c1", "phone": "5551234567", "location": "NY"},
            {"customerid": "c2", "phone": "5551234568", "location": "NY"},
        ]
        cp = MagicMock()
        cp.create_segment_definition.return_value = {"SegmentDefinitionArn": "arn:cp:seg1"}
        with (
            patch("executor.build_redis", return_value=redis_source),
            patch("executor.build_cp", return_value=cp),
            patch("executor.all_known_locations", return_value=frozenset({"NY"})),
        ):
            name, arn, expected, actual = executor._create_segment({"segmentFilters": {}})
        assert expected == 2
        assert actual == 2

    def test_actual_excludes_unnormalizable_phones(self):
        """A lead with an empty/invalid phone counts toward `expected` but not `actual`."""
        import executor

        redis_source = MagicMock()
        redis_source.is_ready.return_value = True
        redis_source.iter_records.return_value = [
            {"customerid": "c1", "phone": "5551234567", "location": "NY"},
            {"customerid": "c2", "phone": "", "location": "NY"},
        ]
        cp = MagicMock()
        cp.create_segment_definition.return_value = {"SegmentDefinitionArn": "arn:cp:seg1"}
        with (
            patch("executor.build_redis", return_value=redis_source),
            patch("executor.build_cp", return_value=cp),
            patch("executor.all_known_locations", return_value=frozenset({"NY"})),
        ):
            name, arn, expected, actual = executor._create_segment({"segmentFilters": {}})
        assert expected == 2
        assert actual == 1

    def test_actual_reflects_truncation_to_max_segment_members(self):
        """expected > _MAX_SEGMENT_MEMBERS truncates actual to the cap."""
        import executor

        redis_source = MagicMock()
        redis_source.is_ready.return_value = True
        redis_source.iter_records.return_value = [
            {"customerid": f"c{i}", "phone": f"555123{i:04d}", "location": "NY"}
            for i in range(executor._MAX_SEGMENT_MEMBERS + 5)
        ]
        cp = MagicMock()
        cp.create_segment_definition.return_value = {"SegmentDefinitionArn": "arn:cp:seg1"}
        with (
            patch("executor.build_redis", return_value=redis_source),
            patch("executor.build_cp", return_value=cp),
            patch("executor.all_known_locations", return_value=frozenset({"NY"})),
        ):
            name, arn, expected, actual = executor._create_segment({"segmentFilters": {}})
        assert expected == executor._MAX_SEGMENT_MEMBERS + 5
        assert actual == executor._MAX_SEGMENT_MEMBERS
```

If `build_redis`/`build_cp`/`all_known_locations` aren't patchable at those exact dotted paths (they're local imports inside `_create_segment` in the current code — verify whether this file's existing tests patch them via `patch("executor.build_redis", ...)` after a prior task made them module-level imports, or via some other established mechanism for function-local imports, e.g. patching the underlying module directly: `patch("vip_shared.infrastructure.persistence.redis_lead_source.build_from_env", ...)`). Search this test file for an existing test that already exercises `_create_segment` successfully (there are several, since branded/SMS/telephony tests all go through it) and copy its exact mocking mechanism — don't guess a new one.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd services/api-plans && python3 -m pytest tests/unit/test_executor_v2.py::TestCreateSegmentReconcileCounts -v`
Expected: FAIL — `_create_segment` still returns a 2-tuple, so unpacking into 4 names raises `ValueError: not enough values to unpack`.

- [ ] **Step 3: Change `_create_segment`'s signature and both return statements**

In `services/api-plans/src/executor.py`, change the `def` line:

```python
def _create_segment(bucket: dict, campaign: dict | None = None) -> tuple[str, str, int, int]:
```

Locate the line `total_matched = len(entries)` (currently line 4432) — this is already `expected`, no change needed there, just note it for the next step.

Locate the line `phones_e164 = [phone for _, phone, _ in entries if phone]` (currently line 4466). Immediately after it (and after its adjacent `if not phones_e164: raise _EmptySegmentError(...)` guard), the actual count is `len(phones_e164)` — do not add a new variable assignment there; reference `len(phones_e164)` directly at both return sites below (it's cheap and keeps `total_matched` as the sole named "expected" variable, avoiding a second name that could drift out of sync if either list is mutated later — there's no such mutation today, but avoid introducing the risk).

Change the two `return` statements (currently lines 4487 and 4495):

```python
        return segment_name, resp["SegmentDefinitionArn"], total_matched, len(phones_e164)
```

```python
            return segment_name, existing["SegmentDefinitionArn"], total_matched, len(phones_e164)
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `cd services/api-plans && python3 -m pytest tests/unit/test_executor_v2.py::TestCreateSegmentReconcileCounts -v`
Expected: PASS (3 tests). Note this will also immediately break every EXISTING test that calls `_create_segment` and unpacks a 2-tuple — that's expected and exactly what Steps 5-10 below fix. Do not consider this step's job done until you've also fixed those (this step is written narrowly so its own 3 new tests are the RED→GREEN evidence; the full-suite run in Step 11 is where you confirm nothing else broke).

- [ ] **Step 5: Update the branded call site (currently around line 3607-3655)**

Read the live code at this anchor first (search for `_stop_branded_campaign(cs)` and `_emit_branded_metric("BrandedCampaignStarted")` to re-locate it if line numbers drifted). Current shape:

```python
        try:
            pinned_arn = campaign.get("pinnedSegmentArn")
            if pinned_arn:
                seg_name = pinned_arn.rsplit("/", 1)[-1]
            else:
                seg_name, _ = _create_segment(bucket, campaign)
            seeded = _invoke_seeder(
```

Change to:

```python
        try:
            pinned_arn = campaign.get("pinnedSegmentArn")
            expected = actual = None
            if pinned_arn:
                seg_name = pinned_arn.rsplit("/", 1)[-1]
            else:
                seg_name, _, expected, actual = _create_segment(bucket, campaign)
            seeded = _invoke_seeder(
```

Then, at the success point — immediately before `cs["connectCampaignId"] = None` / `cs["status"] = "running"` (currently the last two lines before `return` at the end of the branded block, right after `_write_branded_run_start(...)`), insert:

```python
        if expected is not None:
            cs["reconcile"] = {
                "expected": expected,
                "actual": actual,
                "retries": cs.get("reconcileRetries", 0),
            }
        cs["connectCampaignId"] = None
        cs["status"] = "running"
```

(The `if expected is not None:` guard means a pinned-segment branded campaign correctly gets no `cs["reconcile"]` key at all, per the Global Constraints.)

- [ ] **Step 6: Write the branded test**

Model this on an existing test that already exercises the branded success path in `_start_one_campaign` (search for `_is_branded`/`BrandedCampaignStarted` in this test file) — copy its fixture, add the `_create_segment` mock to return 4 values, and assert `cs["reconcile"]` afterward:

```python
class TestBrandedReconcile:
    def test_branded_success_sets_reconcile(self):
        # Model fixture setup on the existing branded-success test in this file.
        # Mock executor._create_segment to return ("seg", "arn", 50, 47).
        # After _start_one_campaign(...) completes, assert:
        #   cs["reconcile"] == {"expected": 50, "actual": 47, "retries": 0}
        ...

    def test_branded_pinned_segment_sets_no_reconcile(self):
        # Model on a branded test using campaign["pinnedSegmentArn"].
        # Assert "reconcile" not in cs after _start_one_campaign(...) completes.
        ...
```

Write both as complete, real tests before moving on — no test may be left as a sketch. Run `cd services/api-plans && python3 -m pytest tests/unit/test_executor_v2.py::TestBrandedReconcile -v` and confirm both pass before continuing.

- [ ] **Step 7: Update the SMS call site (currently around line 3660-3706)**

Read the live code first (search for `_smsRunsPlanId` to re-locate). Current shape:

```python
        try:
            pinned_arn = campaign.get("pinnedSegmentArn")
            if pinned_arn:
                seg_name = pinned_arn.rsplit("/", 1)[-1]
                seg_arn = pinned_arn
            else:
                seg_name, seg_arn = _create_segment(bucket, campaign)
            cs["segmentName"] = seg_name
            cs["segmentArn"] = seg_arn
            _invoke_sms_sender(
```

Change to:

```python
        try:
            pinned_arn = campaign.get("pinnedSegmentArn")
            expected = actual = None
            if pinned_arn:
                seg_name = pinned_arn.rsplit("/", 1)[-1]
                seg_arn = pinned_arn
            else:
                seg_name, seg_arn, expected, actual = _create_segment(bucket, campaign)
            cs["segmentName"] = seg_name
            cs["segmentArn"] = seg_arn
            if expected is not None:
                cs["reconcile"] = {
                    "expected": expected,
                    "actual": actual,
                    "retries": cs.get("reconcileRetries", 0),
                }
            _invoke_sms_sender(
```

- [ ] **Step 8: Write the SMS test**

Same pattern as Step 6, modeled on an existing SMS-success test in this file:

```python
class TestSmsReconcile:
    def test_sms_success_sets_reconcile(self):
        # Mock executor._create_segment to return ("seg", "arn", 30, 28).
        # Assert cs["reconcile"] == {"expected": 30, "actual": 28, "retries": 0} after success.
        ...
```

Run `cd services/api-plans && python3 -m pytest tests/unit/test_executor_v2.py::TestSmsReconcile -v` and confirm it passes.

- [ ] **Step 9: Update the telephony-native fresh-start call site (currently around line 3774-3901)**

Read the live code first (search for `# Fresh start: create segment → create Connect campaign → start` to re-locate the `pinned_segment_arn` split, `reconcile_retry_limit = int(bucket.get("reconcileRetryLimit", 5))` to re-locate the retry loop, and `cs["segmentName"] = segment_name` / `cs["segmentArn"] = segment_arn` to re-locate the success continuation).

**This is the one site where getting the initialization scope wrong causes a `NameError` in production** — `expected`/`actual` must be defined on BOTH the `if pinned_segment_arn:` branch and the `else:` (retry-loop) branch, because the success continuation that reads them (Step 9 below) sits after both branches have already returned control to the same indentation level. Current shape at the very top of the split (before the `if`, not inside either branch):

```python
    # Fresh start: create segment → create Connect campaign → start
    pinned_segment_arn = campaign.get("pinnedSegmentArn")

    if pinned_segment_arn:
        # Operator-pinned segment: skip Redis lookup and segment auto-creation entirely.
        # Used for testing with hand-crafted CP segments.
        segment_arn = pinned_segment_arn
        segment_name = pinned_segment_arn.rsplit("/", 1)[-1]
        logger.info(
```

Change the first two lines to add the initialization here — BEFORE the `if`, not inside the `else:` block — so the `if pinned_segment_arn:` branch (which never calls `_create_segment`) also leaves `expected`/`actual` defined as `None`:

```python
    # Fresh start: create segment → create Connect campaign → start
    pinned_segment_arn = campaign.get("pinnedSegmentArn")
    expected = actual = None

    if pinned_segment_arn:
        # Operator-pinned segment: skip Redis lookup and segment auto-creation entirely.
        # Used for testing with hand-crafted CP segments.
        segment_arn = pinned_segment_arn
        segment_name = pinned_segment_arn.rsplit("/", 1)[-1]
        logger.info(
```

Then, separately, change ONLY the `_create_segment(...)` call's unpacking inside the `else:` (retry-loop) branch — do NOT add `expected`/`actual` to that branch's `segment_name = segment_arn = None` line; they're already initialized above the `if/else` split (previous change), and re-initializing them here too would be harmless but redundant:

```python
    else:
        reconcile_retry_limit = int(bucket.get("reconcileRetryLimit", 5))
        on_exhausted = bucket.get("onReconcileExhausted", "continue")

        segment_name = segment_arn = None
        last_exc: Exception | None = None
        for attempt in range(reconcile_retry_limit + 1):
            try:
                segment_name, segment_arn, expected, actual = _create_segment(bucket, campaign)
                last_exc = None
                break
```

Finally, at the success continuation reached by BOTH branches after the whole `if pinned_segment_arn: ... else: ...` block ends (currently):

```python
    cs["segmentName"] = segment_name
    cs["segmentArn"] = segment_arn
```

Change to:

```python
    cs["segmentName"] = segment_name
    cs["segmentArn"] = segment_arn
    if expected is not None:
        cs["reconcile"] = {
            "expected": expected,
            "actual": actual,
            "retries": cs.get("reconcileRetries", 0),
        }
```

With `expected = actual = None` now set before the `if/else` split (not inside either branch), both the pinned path (never overwrites them, so they stay `None`) and the retry-loop path (overwrites them on a successful `_create_segment` call) leave a defined value by the time this line runs — no `NameError` risk on either path.

- [ ] **Step 10: Write the telephony-native tests**

Model on this file's existing telephony fresh-start success test and its existing empty-segment-retry test:

```python
class TestTelephonyNativeReconcile:
    def test_fresh_start_success_sets_reconcile(self):
        # Mock executor._create_segment to return ("seg", "arn", 100, 95) on first attempt.
        # Assert cs["reconcile"] == {"expected": 100, "actual": 95, "retries": 0}.
        ...

    def test_success_after_retries_records_retry_count(self):
        # Mock executor._create_segment to raise _EmptySegmentError twice then succeed
        # with (name, arn, 40, 38). Assert cs["reconcile"]["retries"] == 2 and
        # cs["reconcile"]["expected"] == 40, cs["reconcile"]["actual"] == 38.
        ...

    def test_pinned_segment_sets_no_reconcile(self):
        # campaign["pinnedSegmentArn"] set — _create_segment never called.
        # Assert "reconcile" not in cs after _start_one_campaign(...) completes.
        ...
```

Write all three as complete, real tests. Run `cd services/api-plans && python3 -m pytest tests/unit/test_executor_v2.py::TestTelephonyNativeReconcile -v` and confirm all pass.

- [ ] **Step 11: Run the full package suite**

Run: `cd services/api-plans && python3 -m pytest tests/unit -q`
Expected: every test that calls `_create_segment` (directly or via `_start_one_campaign`) and unpacks its return value now passes with 4-value unpacking. Fix any test broken by the arity change that Step 4 flagged — those are existing tests, not new ones; update their unpacking to `name, arn, expected, actual = _create_segment(...)` (or `name, arn, *_ = ...` if a given existing test truly doesn't care about the new values) rather than skipping or deleting them.

- [ ] **Step 12: Commit**

```bash
git add services/api-plans/src/executor.py services/api-plans/tests/unit/test_executor_v2.py
git commit -m "feat(executor): _create_segment returns expected/actual counts, wire branded/sms/telephony reconcile"
```

---

### Task 2: `_create_campaign_only` (pre-warm path) — thread expected/actual through both callers

**Files:**
- Modify: `services/api-plans/src/executor.py` — `_create_campaign_only` (currently `def` at line 4075, its own `_create_segment` call at line 4094, `return` at line 4182) and its 2 callers: `_prestart_next_bucket` (call site currently around line 2185) and the global prestart-check handler (call site currently around line 2585).
- Test: `services/api-plans/tests/unit/test_executor_v2.py`

**Interfaces:**
- Consumes: `_create_segment`'s new 4-tuple return (Task 1).
- Produces: `_create_campaign_only(bucket, campaign, run) -> tuple[str, str, str, bool, int | None, int | None]` — `(connectCampaignId, segmentName, segmentArn, warmupStarted, expected, actual)`. `expected`/`actual` are `None` on the pinned-segment fast path, matching Task 1's convention.

**Read this first:** lines 4075-4183 (`_create_campaign_only`'s full body — note its current type hint `-> tuple[str, str, str]` and docstring are already stale/wrong relative to the real 4-value return; fix both while you're touching this signature anyway), lines ~2170-2200 (`_prestart_next_bucket`'s call site, has a real `cs`), lines ~2575-2600 (the global prestart-check handler's call site, called with `run={}` — no real run context, builds a `warmed` list of plain dicts instead of a persisted `cs`).

- [ ] **Step 1: Write the failing test for `_create_campaign_only`'s new return values**

```python
class TestCreateCampaignOnlyReconcileCounts:
    def test_returns_expected_and_actual_from_create_segment(self):
        import executor

        with (
            patch("executor._create_segment", return_value=("seg1", "arn:cp:seg1", 60, 55)),
            patch("executor.build_oc") as mock_build_oc,
            patch("executor.resolve_campaign_flow_arn", return_value="arn:flow"),
            patch("executor.build_campaign_params", return_value={"connectCampaignFlowArn": "arn:flow"}),
        ):
            mock_oc = mock_build_oc.return_value
            mock_oc.create_campaign.return_value = {"id": "connect-1"}
            connect_id, seg_name, seg_arn, warmup_started, expected, actual = (
                executor._create_campaign_only({"buckets": []}, {"id": "c1"}, {"planId": "p1", "runId": "r1"})
            )
        assert expected == 60
        assert actual == 55

    def test_pinned_segment_returns_none_expected_actual(self):
        import executor

        with (
            patch("executor.build_oc") as mock_build_oc,
            patch("executor.resolve_campaign_flow_arn", return_value="arn:flow"),
            patch("executor.build_campaign_params", return_value={"connectCampaignFlowArn": "arn:flow"}),
        ):
            mock_oc = mock_build_oc.return_value
            mock_oc.create_campaign.return_value = {"id": "connect-1"}
            connect_id, seg_name, seg_arn, warmup_started, expected, actual = (
                executor._create_campaign_only(
                    {"buckets": []},
                    {"id": "c1", "pinnedSegmentArn": "arn:cp:pinned/seg-pinned"},
                    {"planId": "p1", "runId": "r1"},
                )
            )
        assert expected is None
        assert actual is None
```

If the exact patch targets (`build_oc`, `resolve_campaign_flow_arn`, `build_campaign_params`) don't match what this file's existing `_create_campaign_only` tests already use, search for an existing test exercising this function successfully and copy its mocks exactly — don't guess.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd services/api-plans && python3 -m pytest tests/unit/test_executor_v2.py::TestCreateCampaignOnlyReconcileCounts -v`
Expected: FAIL — 6-value unpacking of a 4-value return raises `ValueError`.

- [ ] **Step 3: Change `_create_campaign_only`'s signature, docstring, and internal call**

Change the `def` line and docstring:

```python
def _create_campaign_only(
    bucket: dict,
    campaign: dict,
    run: dict,
) -> tuple[str, str, str, bool, int | None, int | None]:
    """Create Connect campaign without starting it (for pre-start warming).

    Returns (connectCampaignId, segmentName, segmentArn, warmupStarted, expected, actual).
    `expected`/`actual` are None when a pinned segment is used (no `_create_segment` call).
    """
```

Locate the pinned/else split near the top of the function body (currently):

```python
    now = _now_utc()
    pinned_arn = campaign.get("pinnedSegmentArn")
    if pinned_arn:
        segment_arn = pinned_arn
        segment_name = pinned_arn.rsplit("/", 1)[-1]
    else:
        segment_name, segment_arn = _create_segment(bucket, campaign)
```

Change to:

```python
    now = _now_utc()
    pinned_arn = campaign.get("pinnedSegmentArn")
    expected = actual = None
    if pinned_arn:
        segment_arn = pinned_arn
        segment_name = pinned_arn.rsplit("/", 1)[-1]
    else:
        segment_name, segment_arn, expected, actual = _create_segment(bucket, campaign)
```

Change the final `return` (currently `return campaign_id, segment_name, segment_arn, warmup_started`):

```python
    return campaign_id, segment_name, segment_arn, warmup_started, expected, actual
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd services/api-plans && python3 -m pytest tests/unit/test_executor_v2.py::TestCreateCampaignOnlyReconcileCounts -v`
Expected: PASS (2 tests). This will break both existing callers' 4-value unpacking — fixed in Steps 5-8 below.

- [ ] **Step 5: Update `_prestart_next_bucket`'s call site (currently around line 2185)**

Read the live code first (search for `cs["warmupStarted"] = warmup_started` to re-locate). Current shape:

```python
                    connect_id, seg_name, seg_arn, warmup_started = (
                        _create_campaign_only(next_bucket, campaign, run)
                    )
                    cs["status"] = "warming"
                    cs["connectCampaignId"] = connect_id
                    cs["segmentName"] = seg_name
                    cs["segmentArn"] = seg_arn
                    cs["warmupStarted"] = warmup_started
```

Change to:

```python
                    connect_id, seg_name, seg_arn, warmup_started, expected, actual = (
                        _create_campaign_only(next_bucket, campaign, run)
                    )
                    cs["status"] = "warming"
                    cs["connectCampaignId"] = connect_id
                    cs["segmentName"] = seg_name
                    cs["segmentArn"] = seg_arn
                    cs["warmupStarted"] = warmup_started
                    if expected is not None:
                        cs["reconcile"] = {
                            "expected": expected,
                            "actual": actual,
                            "retries": 0,
                        }
```

(`retries: 0` is correct here — the pre-warm path has no retry loop of its own; if the pre-warmed campaign's later "official start" ever needed to redo segment creation due to a stale warmup, that path already falls through to the telephony-native retry loop in `_start_one_campaign`, which would overwrite `cs["reconcile"]` with its own accurate retry count at that point — this pre-warm value is simply the starting state.)

- [ ] **Step 6: Write the `_prestart_next_bucket` test**

Model on an existing `_prestart_next_bucket` success test in this file:

```python
class TestPrestartNextBucketReconcile:
    def test_warming_success_sets_reconcile(self):
        # Mock executor._create_campaign_only to return
        # ("connect-1", "seg", "arn", True, 20, 18).
        # Assert cs["reconcile"] == {"expected": 20, "actual": 18, "retries": 0}.
        ...
```

Run `cd services/api-plans && python3 -m pytest tests/unit/test_executor_v2.py::TestPrestartNextBucketReconcile -v` and confirm it passes.

- [ ] **Step 7: Update the global prestart-check handler's call site (currently around line 2585)**

Read the live code first (search for `warmed.append(` in the function that builds a `warmed`/`already_warmed` list across multiple plans — this is the one invoked with `run={}`). Current shape:

```python
            connect_id, seg_name, seg_arn, warmup_started = _create_campaign_only(
                bucket, campaign, {}
            )
```

Change to:

```python
            connect_id, seg_name, seg_arn, warmup_started, _, _ = _create_campaign_only(
                bucket, campaign, {}
            )
```

No further change at this call site — there is no persisted `cs` here (this function builds a `warmed` list of plain dicts describing what got warmed across plans, not a run-scoped campaign state), so the 2 new values are read and discarded, exactly as the Global Constraints specify. Do not add a `reconcile` key to the `warmed` list's dicts — that would be inventing a consumer for data nothing reads.

- [ ] **Step 8: Confirm the existing test for this call site still passes with the updated unpacking**

Find this file's existing test(s) for the global prestart-check handler (search for `prestart_check` and `warmed.append` or similar) and update their `_create_campaign_only` mock to return a 6-tuple instead of 4 — e.g. `return_value=("connect-1", "seg", "arn", True, 10, 9)` — so the unpacking in Step 7 doesn't raise. This is fixing an existing test's fixture for the new arity, not writing a new test (no new behavior to verify at this specific call site beyond "it still works").

Run: `cd services/api-plans && python3 -m pytest tests/unit/test_executor_v2.py -k "prestart_check" -v`
Expected: all prestart_check tests pass.

- [ ] **Step 9: Run the full package suite**

Run: `cd services/api-plans && python3 -m pytest tests/unit -q`
Expected: every test touching `_create_campaign_only` (directly or via its 2 callers) passes with 6-value unpacking. Fix any other existing test the arity change broke, the same way as Task 1 Step 11.

- [ ] **Step 10: Commit**

```bash
git add services/api-plans/src/executor.py services/api-plans/tests/unit/test_executor_v2.py
git commit -m "feat(executor): thread reconcile counts through _create_campaign_only pre-warm path"
```

---

### Task 3: Full verification

**Files:** none (verification only)

- [ ] **Step 1: Full backend suite**

Run: `cd services/api-plans && python3 -m pytest tests/unit -q`
Expected: 0 failures. Compute and report the exact new-vs-baseline test count delta (baseline 449) so the count is verifiable, not asserted.

- [ ] **Step 2: Grep for any other `_create_segment`/`_create_campaign_only` call site this plan might have missed**

Run: `cd services/api-plans && grep -n "_create_segment(\|_create_campaign_only(" src/executor.py`
Expected: exactly 4 call sites for `_create_segment` (the 3 in `_start_one_campaign` plus the 1 inside `_create_campaign_only` itself) and exactly 2 call sites for `_create_campaign_only` (both handled in Task 2) — plus each function's own `def` line. If this grep shows a call site not covered by Tasks 1-2, stop and report it rather than silently leaving it on the old arity (it would be a hard runtime crash the next time that code path executes, not a silent bug — but it should be caught here, not in production).

- [ ] **Step 3: Confirm `cs["reconcile"]` never appears with a stale/wrong shape**

Run: `cd services/api-plans && grep -n 'cs\["reconcile"\]' src/executor.py`
Expected: exactly 4 assignment sites (branded, SMS, telephony-native, pre-warm) — all with the identical `{"expected": ..., "actual": ..., "retries": ...}` shape. No reads of `cs["reconcile"]` should exist yet anywhere in `executor.py` itself (this plan only produces the field; a future page-plan consumes it via the API layer) — confirm that's still true, and if any read exists, it's out of this plan's scope to have been added and should be flagged.

---

## Self-Review Notes

- **Spec coverage:** all 4 direct `_create_segment` call sites and both `_create_campaign_only` callers are covered. The one call site intentionally left without a `cs["reconcile"]` write (the global prestart-check handler, `run={}`) is documented as a deliberate scope boundary, not an oversight — there's no persisted campaign state to write into there.
- **Placeholder scan:** Task 1 Steps 6/8/10 and Task 2 Step 6 have test bodies described in comments rather than fully written out (unlike Task 1 Step 1's and Task 2 Step 1's fully-written tests) — this mirrors the Day Activity Feed plan's Task 5 pattern for tests that must be modeled on a specific pre-existing test the plan's author couldn't quote verbatim without making this document unreviewable in length. Every one of those steps' text is explicit that the test must be written complete and real before moving on, and gives the exact mock return value and exact assertion — nothing is left to invent.
- **Type consistency:** `_create_segment`'s new 4-tuple order (`name, arn, expected, actual`) is used identically at all 4 call sites. `_create_campaign_only`'s new 6-tuple order (`connectCampaignId, segmentName, segmentArn, warmupStarted, expected, actual`) is used identically at both of its callers. `cs["reconcile"]`'s shape (`{"expected", "actual", "retries"}`) is identical across all 4 write sites.
