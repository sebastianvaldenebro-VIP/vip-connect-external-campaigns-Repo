# Phase I: Pre-Call SMS ("The Pre-Call Touch") — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver Phase I of the "Omni-Channel Lead Engagement & Automation" spec: immediately before a plan's outbound voice campaign begins dialing a list of leads, that **same list** receives a personalized, specialty-specific "we're about to call you" SMS — with the SMS guaranteed to go out strictly before the first dial — and **both** channels gated on each recipient's own local TCPA quiet-hours window rather than on the call-center's clock.

**Architecture:** Built entirely inside `vip-connect-external-campaigns` Plans V2. No Amazon Connect contact flow is involved.

The pre-call SMS reuses this repo's own already-deployed, already-tested bulk SMS pipeline — `services/api-sms/src/sms_sender_handler.py` → SQS → `sms_processor_handler.py`, driven from `executor.py`. That is the same pipeline that received the opt-out gate, so the pre-call SMS inherits opt-out suppression, per-contact queue rows, run counters, and abort/complete semantics for free. The alternative — cloning and repairing CloudHesive's `*PreCallSMSFlow` — means owning a vendor flow we cannot keep in sync with their parallel edits and that investigation found **seven** independent defects in (Appendix A). Depending on it buys nothing this repo cannot already do.

### Sequencing: fire on bucket activation, not via `dependsOn`

Sebastian's priority is **guaranteed order** (SMS strictly before the first dial), not a specific lead time. Three mechanisms were evaluated against the real code. The winner is the third:

| Option | Mechanism | Verdict |
|---|---|---|
| Two buckets + `dependsOn` + a multi-hour lead timer | voice campaign declares `dependsOn: [sms]`, held N hours | **Rejected** — 3 fatal problems below |
| A genuinely new "SMS block" type in the same bucket | new campaign/step kind with its own dispatch | **Rejected** — strictly more work than option 3 for the same guarantee |
| **Auto-fire on bucket activation** | a helper invoked at the moment a bucket goes live, immediately before its voice campaigns start | **Chosen** |

The `dependsOn` design was rejected on three verified grounds:

1. **`dependsOn` silently disables pre-warming.** Both prestart paths only warm campaigns with *no* dependencies — `_prestart_plan` (`executor.py:2571-2573`, `stage1_campaigns = [c for c in bucket.get("campaigns", []) if not c.get("dependsOn")]`) and `_prestart_next_bucket` (`executor.py:2180`, `if not campaign.get("dependsOn"):`). Adding `dependsOn: [sms]` to a voice campaign therefore **forfeits its warmup entirely**, so the Connect campaign is created cold at dial time instead of during the prestart window (`_PRESTART_MINUTES: Final = 5`, `executor.py:118`). That is a real performance and reliability regression, and it is invisible — nothing errors. (The 5 here is *when* the warm fires relative to the bucket's scheduled start; the 6 in the lead-time paragraph below is the `startTime` offset the warm step then writes onto the Connect campaign. Two different numbers, both verified.)
2. **A long wait state trips two alarms.** `_NO_ACTIVE_CAMPAIGN_MINUTES = 5` (`executor.py:131`) plus `_bucket_has_only_legitimate_waits` (`executor.py:3421`, whose `dependsOn` branch at `3481-3486` returns `False` precisely when all parents are terminal) means a `NoActiveCampaign` metric fires **5 minutes** into every run and keeps firing for the whole lead time. `_STUCK_RUN_HOURS = 4` (`executor.py:124`) then pages oncall for lead times over 4 hours.
3. **It needed a new cross-campaign segment-sharing field** to guarantee both channels hit the same list, touching all four `_create_segment` call sites and five segment-cleanup guards — with a segment-deletion hazard if any one was missed.

Firing on bucket activation dissolves all three:

- **Order is structural, not timed.** A bucket moves `queued` → `warming` → `running`. Firing the SMS inside that transition, before the campaign-start loop, means "SMS before dial" is an invariant of the lifecycle rather than the outcome of timer arithmetic. Nothing to drift, nothing to race.
- **No wait state exists**, so `_NO_ACTIVE_CAMPAIGN_MINUTES` and `_STUCK_RUN_HOURS` never see one. **All three `dependsOn` patches and the alarm-predicate fix are unnecessary.**
- **The segment is already built and already in hand.** The warm step created it and stored `cs["segmentName"]` / `cs["segmentArn"]` (`executor.py:2190-2191`; bucket 0 stores the same pair in `pendingWarmup`, `executor.py:2603-2611`). The SMS reads that exact ARN. **The whole `reuseSegmentFromCampaignId` field, its resolver, and the five cleanup guards are unnecessary** — same-list is guaranteed by construction because there is only ever one segment.
- **Pre-warming is preserved**, because no campaign gains a `dependsOn`.
- **Specialty copy falls out naturally**: the pre-call config lives on each voice campaign, so each campaign carries its own copy and its own segment. No new campaign type.

Real lead time: the SMS is enqueued at bucket activation, while `_create_campaign_only` sets the Connect campaign's own `startTime` to `now + 6 minutes` at warm time (`executor.py:4136`). So the first dial lands roughly **1–6 minutes** after the SMS is enqueued. Short, but strictly ordered — which is the requirement. A longer explicit lead is a follow-up (**OQ-1**), not a Phase I need.

### Personalization

Personalization is a hard requirement (`Hi [Name]! This is [Clinic Name]...`). The existing PHI guard `_validate_sms_campaign` (`services/api-plans/src/handlers/plans.py:503-552`) blocks `{{...}}` and `${...}` outright (`_PHI_PATTERNS`, lines 528-529). Investigation established **why**, and it is not primarily a PHI judgment:

> **There is no renderer anywhere in the pipeline.** `sms_processor_handler.py:92` passes `"MessageBody": message_template` straight to EUM `send_text_message`, verbatim. The sender copies the template into the SQS body unmodified (`sms_sender_handler.py:118`). So a template containing `{{FirstName}}` would deliver the literal characters `{{FirstName}}` to the patient.

That reframes the fix. The guard is load-bearing **until a renderer exists**, so the renderer must be built first and the guard narrowed second — never the reverse. Task 3 does both, in that order, and keeps every other PHI pattern intact.

Sending a patient their own first name is ordinary clinical communication — the recipient *is* the data subject, so this is not a disclosure. The allowlist is nonetheless kept deliberately narrow, split by where the value comes from:

| Placeholder | Source | Rationale |
|---|---|---|
| `{{FirstName}}` | Customer Profiles `FirstName` per recipient | The only per-recipient field needed. CP already returns it — `_get_segment_phones` fetches full profiles via `batch_get_profile` and currently **discards everything except the phone number** (`sms_sender_handler.py:229-232`). Coverage is **100%**, confirmed by Sebastian 2026-09-10 (not independently measured — Customer Profiles offers no cheap existence-filter API for this, so no aggregate query was run). |
| `{{ClinicName}}` | `campaignConfig.precallSms.clinicName` | Campaign-level constant, identical for every recipient. Not recipient data at all. |

That is the whole allowlist — two fields. An earlier draft of this plan also allowlisted `{{Specialty}}`; the business-approved copy that arrived 2026-09-10 bakes the specialty into the sentence instead of substituting a noun, so it was removed rather than left as dead surface. Task 5, "The approved copy", point (1) records the exact sites and confirms re-adding it is purely additive.

Everything else stays blocked, including `{{LastName}}` — CP exposes it (`services/api-profiles/src/handlers/profiles.py:140-141`) but a surname adds identifiability with no engagement benefit, so it is denied by omission. `${...}` remains banned outright: no renderer supports that syntax, so it can only ever be a mistake. All the original PHI patterns — SSN, email, dates, long numeric IDs, URLs, clinical terms — remain unchanged and are still enforced against the template.

**Tech Stack:** Python 3.12 Lambdas (`api-plans`, `api-sms`), DynamoDB (`VipConnectPlans`, `VipSmsCampaignQueue`, `VipSmsCampaignRuns`, `VipConnectOptOutList`), SQS (KMS-CMK encrypted), Amazon Connect Customer Profiles, `connectcampaignsv2` (botocore 1.43.90 confirmed for every field used), `phonenumbers` (new dependency), pytest, AWS CDK (TypeScript), React + TypeScript frontend with hand-rolled Tailwind forms (no component library, no `react-hook-form`/`zod`) and Vitest configured `environment: 'node'` — so frontend tests exercise exported pure functions, never rendered JSX.

**Spec:** "Phase I: Omni-Channel Lead Engagement & Automation", Phase I ("The Pre-Call Touch"), plus Sebastian's design decisions of 2026-09-09: SMS-campaign-then-voice over a Connect flow; guaranteed order over lead time; personalization non-negotiable.

---

## Global Constraints

- **Branch from `origin/main`. PR #7 has already merged — this is not a prerequisite to wait on.** Verified against `origin/main` after a fresh `git fetch` on 2026-09-10: `origin/main`'s tip **is** `ad96df1 feat: TCPA opt-out gap — cross-channel opt-out enforcement (#7)`. The opt-out gate and `services/shared/python/vip_shared/infrastructure/persistence/opt_out.py` are both present on `main`. Every `sms_sender_handler.py` anchor in this plan is taken from that tree (285 lines, `StructuredLogger`-based). Four things an implementer must not get wrong:
  - **Resolve refs against `origin/main`, never a local `main` ref.** The local `main` in the primary checkout is **23 commits stale** (`5dc2eb1`) and does *not* contain the gate; the branch checked out there is `audit/2026-09-02`, which also predates it. Reading either one produces the false conclusion that the merge is still pending — a mistake made twice while writing this plan. Always `git fetch origin` first and read `origin/main` explicitly.
  - The gate's actual symbols are `from vip_shared.infrastructure.persistence.opt_out import build_from_env as build_opt_out_from_env` (lines 22-23), module-level `_opt_out = build_opt_out_from_env()` (38), `if _opt_out.is_blocked(phone):` inside the send loop (115), and the counter `totalSkippedOptOut` (start record 86, `UpdateExpression` 178, log field `skipped_opt_out` 193). **There is no `OPT_OUT_TABLE` literal in this handler** — the table name is resolved inside the shared module's `build_from_env()` and injected as a Lambda env var. Grepping the handler for `OPT_OUT_TABLE` returns zero *even though the gate is present*, which reads exactly like "the merge has not landed." Grep for `_opt_out.is_blocked` instead.
  - **`OPT_OUT_TABLE` is already wired in CDK too**: `infra/lib/stacks/api-sms-stack.ts:167` sets `OPT_OUT_TABLE: 'VipConnectOptOutList'` inside `smsSenderFunction`'s `environment` block (`162-168`). Task 1 Step 6 appends to that block; it does not create it.
  - Do **not** branch from `.claude/worktrees/tcpa-optout-gap` or from the primary checkout's working tree, and do not edit a copy that predates the gate — that would silently revert a deployed production gate. The existing `.claude/worktrees/precall-sms-phase1` worktree is at `ad96df1` and is a correct base.
- **Do not modify any CloudHesive-owned resource.** Nothing here needs to; the constraint stands so no one "helpfully" fixes their flow while in the area. Verified live 2026-09-09, account `165505826690` / us-east-1:
  - Lambdas (all 8 `cloudhesive-integration-*`): `connectcampaign_sms_lookup`, `callback-tz-TimezoneCheck`, `callback-tz-RequeueCallbacks`, `agent-initiatied-sms-send-sms`, `agent-initiatied-sms-get-history`, `agent-initiatied-sms-receive-sms`, `agent-initiatied-sms-list-sessions`, `agent-initiatied-sms-ws-handler`
  - Contact flows `*PreCallSMSFlow` (`f85f1cd2-91b0-4252-9844-05b8b1c72967`) and `*PreCallSMSFlow-test` (`412a4278-75ba-4495-b670-cd97846e3b63`, owner unknown — leave alone too)
  - Wisdom message template `PreCallSMS` (`.../1b43ace8-dc25-475c-b605-744ff7718e9c/aad1d044-8f7f-460b-98f2-e913dda9756a`)
  - DynamoDB `cloudhesive-integration-VIP_SMS_Journey_Texts` and the three `cloudhesive-integration-agent-initiatied-sms-*` tables
  - Connect Data Table `VIP_SMS_Journey_Texts` (`4ecc384d-c8b3-44f1-ada7-9c0962165d7f`) — not CloudHesive-prefixed, but it backs their flow's content and is **out of scope**; no task writes to it.
  - **Owed to CloudHesive:** version 1 of the `PreCallSMS` Wisdom template was created and activated 2026-09-08 by `sebastian.valdenebro@medwork.io`, and `cloudhesive-integration-connectcampaign_sms_lookup` was added to the Connect instance's classic Lambda association list — both before we learned they own that flow. Additive and harmless, but tell them; their flow pins `...:1`.
- **Do not touch the opt-out work.** `VipConnectOptOutList`, `OptOutRepository`, the legacy `vip-connect-deny-list` table, and the inbound STOP handler are done and deployed. Task 1 adds a *second, orthogonal* gate beside the opt-out check in the same loop; it must not replace or reorder it. **The quiet-hours check goes strictly after the opt-out check**, because opt-out is permanent and absolute while quiet hours is a deferral: a phone that is both opted out and inside quiet hours must land in `totalSkippedOptOut`, so the two counters keep meaning "will never be contacted" and "was not contacted right now" respectively.
- **Do not delete or rewrite the COT staffing checks.** `_within_working_hours`, `_is_working_day`, `_now_cot_hhmm`, `_DAILY_CUTOFF_HOUR` (`executor.py:5142-5188`; call sites 869/898/937/3084) use a fixed UTC-5 with no DST and answer "are the Bogotá agents on shift?". That is correct for what it does. Task 1 Step 6 changes **comments only**.
- **Do not lift the PHI guard.** Task 3 removes exactly **one** of the ten `_PHI_PATTERNS` entries — the `\{\{[^}]+\}\}` blanket ban — and replaces it with a named allowlist. The other **nine** stay byte-identical, including the `\$\{[^}]+\}` ban. Deleting `_PHI_PATTERNS`, disabling `_validate_sms_campaign`, or allowing arbitrary placeholders is out of bounds. **The renderer must land before the guard is narrowed** (Task 3 step order is not negotiable) or patients receive literal `{{FirstName}}`.
- **Do not add `dependsOn` to a voice campaign as a sequencing device.** Verified: it disables that campaign's pre-warming (`executor.py:2180`, `2571-2573`). Ordering comes from the bucket-activation hook instead.
- **Do not implement Phase II or Phase III.** No event-driven post-call SMS, no Lex triage, no Luma Health handoff. `*PostCallSMSFlow` and `*OutboundFollowupSMSFlow` exist in the instance — irrelevant, leave them.
- **Task 7 is the gate.** No Phase II or Phase III work begins until Task 7's end-to-end verification passes with observed evidence. One of its eleven steps (the Sunday check) can only be run on a Sunday — plan for that rather than discovering it at the end.
- **PHI.** Phone numbers are HIPAA identifier #4; first names are identifier #1. Never log either, and never log a rendered message body. **There is no `_last4()` helper in this repo** — do not go looking for one. The three real conventions, verified: `services/api-sms/src/sms_sender_handler.py` logs **no phone at all** (its own module docstring, lines 6-8: "Phone numbers are NOT logged"; the failure path at line 182 comments "no phone numbers here — only the SQS-assigned Id and error code"), which is the convention Task 1 and Task 3 must follow in that file; `executor.py` stores an inline `phone[-4:]` under `phone_last4` / `sourcePhoneLast4` field names (`424-446`, `671`, `696`, `4497`) when a last-4 must be persisted; and `services/api-deny-list/src/handlers/deny_list.py:37` has a `_mask()` returning `****1234` for log lines. Copy the convention of the file you are in — do not introduce a fourth. The SQS send queue is KMS-CMK encrypted (`alias/vip-data-key`, `infra/lib/stacks/api-sms-stack.ts:70-106`, created via CLI and imported with `keyArn: props.dataKeyArn`), which already covers phone numbers in message bodies and therefore covers an added first name — but Task 3 Step 8 re-verifies it live, because that queue is created by a manual CLI step and can drift.
- **Contact window is 08:00–21:00 recipient-local, Monday through Saturday. No contact on Sunday.** Decided by Sebastian 2026-09-10: use the **full statutory TCPA window** on the hours axis (08:00–21:00, not the earlier tighter 08:00–20:00 proposal) and be **stricter than statute on the day axis** (TCPA does not exempt Sunday; this is a business choice). Both axes are load-bearing: the hours check alone is not enough, so **day-of-week is a real logic addition**, not a constant change (Tasks 1 and 2).
- **If the `phonenumbers` layer budget check fails, stop and report.** Task 1 Step 4's contingency was **explicitly accepted** by Sebastian, not defaulted to: the hand-rolled area-code→timezone map is a documented fallback that requires its own decision, not a shortcut an implementer may take unilaterally when the layer measurement comes back unhappy.
- **Do not invent patient-facing copy.** Business-approved SMS copy exists for Vein and Pain Management only (Task 5). Fibroid and General have none, and none is drafted here (**OQ-8**). If a specialty has no approved string, it does not ship — a plausible-looking placeholder in a patient-facing, TCPA-relevant message is worse than an empty row.

---

## File Structure

**New files:**
- `services/shared/python/vip_shared/domain/services/quiet_hours.py` (Task 1)
- `services/shared/tests/unit/test_quiet_hours.py` (Task 1)
- `services/shared/python/vip_shared/domain/services/sms_template.py` (Task 3)
- `services/shared/tests/unit/test_sms_template.py` (Task 3)
- `frontend/src/lib/precallSms.ts` (Task 6) — pure validator/renderer helpers, extracted so they are testable under this repo's `environment: 'node'` Vitest config
- `frontend/src/lib/precallSms.test.ts` (Task 6)

**Modified files:**
- `services/shared/requirements.txt` — add `phonenumbers` (Task 1)
- `services/api-sms/src/sms_sender_handler.py` — quiet-hours gate + counter (Task 1); carry `FirstName` and render per recipient (Task 3)
- `services/api-sms/tests/unit/test_sms_sender.py` (Tasks 1, 3)
- `infra/lib/stacks/api-sms-stack.ts:162` — four env vars on `smsSenderFunction` only: `QUIET_HOURS_START`, `QUIET_HOURS_END`, `QUIET_HOURS_DAYS`, `QUIET_HOURS_DEFAULT_TZ` (Task 1). The processor's env block at `204-209` is **not** touched.
- `services/api-campaigns/src/builders.py:93-99` + its `test_builders.py` (Task 2)
- `services/api-plans/src/builders.py:548-564` + its `test_builders.py` (Task 2)
- `services/api-plans/src/handlers/plans.py:503-552` — narrow the placeholder patterns to an allowlist (Task 3); validate the pre-call block (Task 5)
- `services/api-plans/tests/unit/test_plan_validation_sms.py` (Tasks 3, 5)
- `services/api-plans/src/executor.py` — `_fire_precall_sms` helper + two call sites in `start_run` and `_activate_warming_bucket` (Task 4); comment-only clarification at `5142` (Task 1)
- `services/api-plans/tests/unit/test_executor_v2.py` — fixture at line 7209 updated to an allowlisted placeholder (Task 3); `_fire_precall_sms` tests (Task 4)
- `frontend/src/lib/api.ts:309-327` — `precallSms` on `BucketCampaignConfig` (Task 4); the type Task 6's panel binds to. `CampaignDef` (439-465) is **not** modified — it already carries `campaignConfig?: BucketCampaignConfig` at 450.
- `frontend/src/pages/PlanNew.tsx` — pre-call panel inside `CampaignCard` (513-1007), wired into `handleSave()` (1401-1430) and the existing `useQuery(['sms','numbers'])` (1368-1373) (Task 6)
- `docs/runbook.md` — operator recipe and verification evidence (Tasks 5, 7)

---

### Task 1: Per-lead TCPA quiet-hours gate in the bulk SMS pipeline

Gates the pre-call SMS itself, so it is on Phase I's critical path. Independent of Task 2 — the two can run in parallel.

**Files:** `services/shared/python/vip_shared/domain/services/quiet_hours.py` (new), `services/shared/tests/unit/test_quiet_hours.py` (new), `services/shared/requirements.txt`, `services/api-sms/src/sms_sender_handler.py`, `services/api-sms/tests/unit/test_sms_sender.py`, `services/api-plans/src/executor.py` (comments only), `infra/lib/stacks/api-sms-stack.ts`

**Interfaces:**
- Consumes: nothing.
- Produces: `vip_shared.domain.services.quiet_hours.is_within_quiet_hours(phone: str, *, now: datetime | None = None) -> bool` — gating on **both** the recipient-local hour window (08:00–21:00) **and** the recipient-local day of week (Monday–Saturday; Sunday always blocked) — plus a `totalSkippedQuietHours` counter on `VipSmsCampaignRuns` that the end-to-end verification task reads.

**Day of week is evaluated in the recipient's local timezone, not UTC.** This is the whole point and it is easy to get wrong: **2026-06-15 03:00 UTC is a Monday in UTC, but 2026-06-14 20:00 — Sunday evening — for a Pacific number.** 20:00 is *inside* the 08:00–21:00 hour window, so the hour axis says yes and only the recipient-local **day** axis can refuse it. A UTC-based day check would text that lead on a Sunday night. `test_day_of_week_is_evaluated_in_recipient_local_time_not_utc` below pins exactly that instant and that number.

Use the Pacific case, not an Eastern one, when reasoning about this: 02:00 UTC Monday is 22:00 Sunday Eastern, which the *hour* check already rejects, so it proves nothing about the day axis. The gap only shows up where the local time is inside the window and the local day is Sunday.

Two facts that shape this task:

- **`phonenumbers` is not a dependency anywhere.** Grepped every `requirements*.txt`, `pyproject.toml`, and `.py` in this repo and in `Connect-batch-redis-refactor`, `connect-campaigns-webapp`, and `rcm-sms-inbox`: zero hits; `import phonenumbers` fails locally. `services/shared/requirements.txt` holds only `redis>=5.0.0,<6` and `requests>=2.31.0`.
- **The gate cannot live in `executor.py`.** The executor invokes the sender **once per campaign** with a `segmentArn`; no phone numbers exist at that point — the sender resolves them itself via `_get_segment_phones()`. The only place an individual lead's number exists is the per-phone loop in `sms_sender_handler.py`, right after the opt-out check.

Why the COT staffing gate does not already cover this: the pre-call SMS fires on the plan's COT schedule. 08:00 COT is 09:00 EDT but **06:00 PDT** — two hours inside the TCPA quiet period for a Pacific lead. The staffing gate cannot see that; it is not measuring the recipient.

- [ ] **Step 1: Confirm the baseline before touching the file**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns
git fetch --all && git log --oneline -3 origin/main
grep -n "build_opt_out_from_env\|_opt_out.is_blocked\|totalSkippedOptOut" services/api-sms/src/sms_sender_handler.py
grep -n "for phone in phones" services/api-sms/src/sms_sender_handler.py
ls services/shared/python/vip_shared/infrastructure/persistence/opt_out.py
```

Expected — verified against `origin/main` @ `ad96df1` on 2026-09-10, which is where PR #7 merged: the aliased import (22-23), `_opt_out = build_opt_out_from_env()` (38), `if _opt_out.is_blocked(phone):` (115), the `totalSkippedOptOut` writes (86, 178, 193), the send loop (112), and the shared module file. The file is **285 lines** and uses `StructuredLogger`, not `print()`.

**If they are absent, you are not on the right tree — do not proceed and do not "add" the gate.** The likely cause is reading a stale local `main` ref (23 commits behind as of 2026-09-10) or the `audit/2026-09-02` branch, both of which predate the merge. Re-check with `git log --oneline -1 origin/main` after a fetch; if that shows `ad96df1`, the gate exists and your checkout is the problem. Writing a second opt-out gate on top of a stale tree would revert a deployed production gate on deploy.

**Do not substitute `grep OPT_OUT_TABLE`.** That string does not appear in this handler even on the tree that has the gate — the table name is resolved inside the shared module's `build_from_env()` — so a zero result reads exactly like "the gate is missing" and will send you down the wrong path. (This mistake was made while writing this plan.)

The loop on `origin/main` looks like this, with the opt-out gate already in place:

```python
    for phone in phones:                    # 112
        if not _E164_RE.match(phone):       # 113
            continue                        # 114
        if _opt_out.is_blocked(phone):      # 115
            opted_out += 1                  # 116
            continue                        # 117
        item_sk = f"{now_iso}#{uuid.uuid4().hex[:8]}"   # 118
```

The quiet-hours check goes **between 117 and 118** — after the opt-out gate, never before it (rationale in Global Constraints). Note the counter variable convention: `opted_out` is a plain local initialised at 107 alongside `enqueued` / `failed`, flushed into the run record by the `UpdateExpression` at 177. Task 1 Step 5's counter follows the same shape but carries **three distinct names** — keep them straight: the Python local is `outside_quiet_hours`, the DynamoDB attribute is `totalSkippedQuietHours` (paired with `totalSkippedOptOut`), and the structured-log field is `skipped_quiet_hours`.

- [ ] **Step 2: Write the failing tests**

Create `services/shared/tests/unit/test_quiet_hours.py`:

```python
"""Tests for per-lead TCPA quiet-hours resolution.

Distinct from executor.py's _within_working_hours, which is a Colombia-time
call-center staffing gate. This module answers a different question: is it
legal to contact *this* patient right now, in *their* local time?
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from vip_shared.domain.services.quiet_hours import (
    is_within_quiet_hours,
    resolve_timezone,
)


def test_resolves_eastern_area_code():
    assert resolve_timezone("+12125551234") == "America/New_York"  # 212 = NYC


def test_resolves_pacific_area_code():
    assert resolve_timezone("+14155551234") == "America/Los_Angeles"  # 415 = SF


def test_unparseable_number_falls_back_to_default():
    assert resolve_timezone("not-a-number") == "America/New_York"


@pytest.mark.parametrize(
    "utc_hour,expected",
    [
        # 2026-06-15 is a MONDAY. In June, Eastern is EDT (UTC-4).
        (12, True),   # 08:00 Eastern Mon — window opens
        (13, True),   # 09:00 Eastern Mon
        (17, True),   # 13:00 Eastern Mon
        (23, True),   # 19:00 Eastern Mon
        (11, False),  # 07:00 Eastern Mon — too early
    ],
)
def test_eastern_number_gated_on_eastern_local_time(utc_hour, expected):
    now = datetime(2026, 6, 15, utc_hour, 0, tzinfo=timezone.utc)
    assert is_within_quiet_hours("+12125551234", now=now) is expected


@pytest.mark.parametrize(
    "utc,expected",
    [
        # These two straddle the 21:00 close on a Monday, and are the tests that
        # would have FAILED under the earlier 20:00 proposal — they pin the
        # decision to use the full statutory window.
        (datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc), True),   # 20:00 ET Mon
        (datetime(2026, 6, 16, 1, 0, tzinfo=timezone.utc), False),  # 21:00 ET Mon — closed
    ],
)
def test_window_closes_at_21_00_local_not_20_00(utc, expected):
    assert is_within_quiet_hours("+12125551234", now=utc) is expected


def test_saturday_is_allowed():
    """Mon-Sat, so Saturday is a contact day. 2026-06-13 is a Saturday."""
    now = datetime(2026, 6, 13, 17, 0, tzinfo=timezone.utc)  # 13:00 ET Sat
    assert is_within_quiet_hours("+12125551234", now=now) is True


def test_sunday_is_never_allowed():
    """No contact on Sunday, at any hour. 2026-06-14 is a Sunday."""
    for utc_hour in (13, 17, 23):
        now = datetime(2026, 6, 14, utc_hour, 0, tzinfo=timezone.utc)
        assert is_within_quiet_hours("+12125551234", now=now) is False, utc_hour


def test_day_of_week_is_evaluated_in_recipient_local_time_not_utc():
    """THE subtle one, and the reason day-of-week cannot be read off UTC.

    2026-06-15 03:00 UTC is a MONDAY in UTC. For a Pacific number (PDT, UTC-7)
    it is 2026-06-14 20:00 — SUNDAY 20:00, which is *inside* the 08:00-21:00
    hour window. The hour axis says yes; only the recipient-local day axis can
    refuse it. A UTC-based day check would text this lead on a Sunday evening.
    """
    utc_monday = datetime(2026, 6, 15, 3, 0, tzinfo=timezone.utc)
    assert is_within_quiet_hours("+14155551234", now=utc_monday) is False


def test_same_instant_differs_by_recipient_timezone():
    """The whole point: one instant, two recipients, two answers.
    13:00 UTC is 09:00 Eastern (allowed) but 06:00 Pacific (blocked)."""
    now = datetime(2026, 6, 15, 13, 0, tzinfo=timezone.utc)
    assert is_within_quiet_hours("+12125551234", now=now) is True
    assert is_within_quiet_hours("+14155551234", now=now) is False


def test_multi_zone_number_fails_closed(monkeypatch):
    """A number mapping to several zones must be inside the window in EVERY
    candidate zone before we send — fail closed, not open.

    Ambiguity is injected rather than hunted for in a real area code: which
    NANP prefixes phonenumbers reports as multi-zone is library-data dependent
    and would make this test drift with a dependency bump. The instant chosen
    (13:00 UTC Monday) is 09:00 Eastern — inside the window — and 06:00 Pacific
    — outside it. Two candidate zones, two answers, so the fail-closed rule is
    the only thing that can produce False.
    """
    import vip_shared.domain.services.quiet_hours as qh

    monkeypatch.setattr(
        qh,
        "_candidate_zones",
        lambda _phone: ["America/New_York", "America/Los_Angeles"],
    )
    now = datetime(2026, 6, 15, 13, 0, tzinfo=timezone.utc)  # 09:00 ET / 06:00 PT
    assert qh.is_within_quiet_hours("+15555551234", now=now) is False
```

The single-zone assertions above (`+1212…` Eastern, `+1415…` Pacific) do depend on real `phonenumbers` data. Confirm both resolve as expected the first time the module is importable (`python -c "import phonenumbers; from phonenumbers.timezone import time_zones_for_number; print(time_zones_for_number(phonenumbers.parse('+12125551234','US')))"`) rather than assuming; if the library disagrees, fix the test's expectation, not the implementation.

In `services/api-sms/tests/unit/test_sms_sender.py`, add (following the file's existing `patch.object(handler, "_attr", mock)`-after-reload convention, same as the opt-out tests):

```python
def test_sender_skips_phone_outside_recipient_local_quiet_hours():
    handler = _load_handler()

    mock_ddb = MagicMock()
    mock_runs_table = MagicMock()
    mock_queue_table = MagicMock()
    mock_ddb.Table.side_effect = lambda name: (
        mock_runs_table if "Runs" in name else mock_queue_table
    )

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_sqs", MagicMock()),
        patch.object(handler, "_cp", _make_mock_cp(phones=["+12125551234", "+14155551234"])),
        patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda *_: False)),
        patch.object(
            handler, "_is_within_quiet_hours", lambda p, **_: p == "+12125551234"
        ),
    ):
        result = handler.lambda_handler(_base_event(), None)

    assert result["enqueued"] == 1
    assert mock_runs_table.update_item.call_args.kwargs[
        "ExpressionAttributeValues"
    ][":q"] == 1


def test_opt_out_is_checked_before_quiet_hours():
    """Ordering matters: an opted-out contact must count as opted out, not as
    quiet-hours-skipped, or the two suppression reasons blur in reporting."""
    handler = _load_handler()
    calls: list[str] = []

    mock_ddb = MagicMock()
    mock_ddb.Table.return_value = MagicMock()

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_sqs", MagicMock()),
        patch.object(handler, "_cp", _make_mock_cp(phones=["+12125551234"])),
        patch.object(
            handler,
            "_opt_out",
            MagicMock(is_blocked=lambda *_: (calls.append("opt_out"), True)[1]),
        ),
        patch.object(
            handler,
            "_is_within_quiet_hours",
            lambda *_a, **_k: (calls.append("quiet_hours"), True)[1],
        ),
    ):
        handler.lambda_handler(_base_event(), None)

    assert calls == ["opt_out"]  # quiet_hours never reached
```

Also add `patch.object(handler, "_is_within_quiet_hours", lambda *_a, **_k: True)` to **every** pre-existing test's `with (...)` block in this file. Without it those tests silently become time-of-day dependent — the real function would run against the CI wall clock, so the suite would pass in the afternoon and fail overnight.

- [ ] **Step 3: Run the tests to verify they fail**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/shared && python -m pytest tests/unit/test_quiet_hours.py -v
```
Expected: `ModuleNotFoundError: No module named 'vip_shared.domain.services.quiet_hours'`.

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-sms && python -m pytest tests/unit/test_sms_sender.py -v
```
Expected: `AttributeError: <module 'sms_sender_handler'> does not have the attribute '_is_within_quiet_hours'`.

- [ ] **Step 4: Add the dependency and check the layer budget**

Append to `services/shared/requirements.txt`:

```
phonenumbers>=8.13
```

`phonenumbers.timezone.time_zones_for_number()` does the work and returns a **tuple** — an area code can legitimately map to several zones. `infra/lib/utils/python-bundling.ts` installs this file into the shared layer, and `infra/lib/utils/shared-layer.ts` builds a **per-stack copy**.

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/infra
cdk synth VipAdminApiSmsStack > /dev/null
du -sh cdk.out/asset.*/python 2>/dev/null | sort -h | tail -3
```

Lambda's hard limit is 250 MB unzipped for layers + code. If the layer passes ~50 MB, **stop and report** — a static area-code→timezone map is a cheaper fallback than blowing the budget.

If that fallback is ever approved, note that **no such map exists in this repo today**. `frontend/src/lib/areaCodeMap.ts` has a promising name and is not it: it is `STATE_DEFAULT_PHONES` plus `pickPhoneForStates()`, a map from marketing state code (NY/LI/NJ/MD/CT/TX/SCA/NCA/PA) to the *outbound caller-ID number* to dial from, with **zero timezone data** and no area-code keys. Do not mistake it for a starting point.

- [ ] **Step 5: Implement**

Create `services/shared/python/vip_shared/domain/services/quiet_hours.py`:

```python
"""Per-recipient TCPA quiet-hours gate for the bulk SMS pipeline.

WHY THIS EXISTS SEPARATELY FROM executor.py's _within_working_hours:

  executor.py's _within_working_hours / _is_working_day / _now_cot_hhmm answer
  "is the Bogota call-center staffed right now?" — a fixed UTC-5, no-DST
  question about *our* operating hours. That is correct for what it does and is
  not being replaced.

  This module answers a different question: "is it legal to text *this patient*
  right now, in *their* local time?" TCPA quiet hours are a property of the
  recipient's location, not ours. Conflating the two is how a plan that fires at
  08:00 COT texts a Pacific-timezone lead at 06:00 local.

  Pipeline A (deliveryType 'campaign'/'journey') gets the equivalent for free
  from Connect Campaigns V2's own localTimeZoneDetection=AREA_CODE + openHours.
  Pipeline B (deliveryType 'sms') never touches Connect Campaigns, so it needs
  this.

Fails closed: an unresolvable or multi-zone number is blocked unless it is
inside the window in every candidate zone.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import phonenumbers
from phonenumbers.timezone import time_zones_for_number

_DEFAULT_TZ = os.environ.get("QUIET_HOURS_DEFAULT_TZ", "America/New_York")
_START_HHMM = os.environ.get("QUIET_HOURS_START", "08:00")
_END_HHMM = os.environ.get("QUIET_HOURS_END", "21:00")

# Permitted contact days, as Python weekday() integers (Monday=0 .. Sunday=6).
# Default is Monday-Saturday: TCPA itself does not exempt Sunday, so excluding it
# is a VIP business choice, not a statutory requirement. Kept as an env var so it
# can be widened to include Sunday without a code change.
_DAYS_ENV = os.environ.get("QUIET_HOURS_DAYS", "0,1,2,3,4,5")
_ALLOWED_WEEKDAYS = frozenset(int(d) for d in _DAYS_ENV.split(",") if d.strip())

# phonenumbers returns this sentinel when it cannot map a number.
_UNKNOWN_TZ = "Etc/Unknown"


def _minutes(hhmm: str) -> int:
    hours, mins = (int(part) for part in hhmm.split(":"))
    return hours * 60 + mins


def _candidate_zones(phone: str) -> list[str]:
    try:
        parsed = phonenumbers.parse(phone, "US")
        zones = [z for z in time_zones_for_number(parsed) if z != _UNKNOWN_TZ]
    except Exception:
        return [_DEFAULT_TZ]
    return zones or [_DEFAULT_TZ]


def resolve_timezone(phone: str) -> str:
    """Return the single best IANA timezone for `phone`, or the default.

    Never raises — an unparseable number yields the default so the caller's
    window check still runs rather than the whole batch failing.
    """
    return _candidate_zones(phone)[0]


def is_within_quiet_hours(phone: str, *, now: datetime | None = None) -> bool:
    """True if `phone` may be contacted right now in its own local time.

    Two independent axes, both evaluated in the RECIPIENT's timezone:
      1. hour-of-day inside [_START_HHMM, _END_HHMM)
      2. day-of-week in _ALLOWED_WEEKDAYS (Mon-Sat by default, no Sunday)

    Evaluating the day in the recipient's zone rather than UTC is load-bearing:
    2026-06-15 03:00 UTC is Monday in UTC but Sunday 20:00 Pacific -- inside
    the hour window, so only the day axis can refuse it.

    Fails closed on ambiguity: a number mapping to several timezones must pass
    BOTH checks in ALL of them.
    """
    instant = now or datetime.now(timezone.utc)
    start, end = _minutes(_START_HHMM), _minutes(_END_HHMM)
    for zone in _candidate_zones(phone):
        try:
            local = instant.astimezone(ZoneInfo(zone))
        except Exception:
            return False
        if local.weekday() not in _ALLOWED_WEEKDAYS:
            return False
        if not (start <= local.hour * 60 + local.minute < end):
            return False
    return True
```

Note on the two axes: they are checked independently and both must pass, so a multi-zone number straddling a local midnight can be refused on the day axis in one zone and the hour axis in another. That is the intended fail-closed behaviour, not a bug — the function answers "is it safe in every candidate zone?", and it only takes one "no".

In `services/api-sms/src/sms_sender_handler.py`, add the import beside the existing opt-out import (~line 22-24):

```python
from vip_shared.domain.services.quiet_hours import (
    is_within_quiet_hours as _is_within_quiet_hours,
)
```

Add the counter beside `opted_out` (line 107 on `origin/main`, alongside `enqueued` 105 / `failed` 106):

```python
    opted_out = 0
    outside_quiet_hours = 0
```

Insert the gate in the per-phone loop, immediately **after** the opt-out check (loop `112-118` on `origin/main`; the new check goes between `117` and `118`):

```python
    for phone in phones:
        if not _E164_RE.match(phone):
            continue
        if _opt_out.is_blocked(phone):
            opted_out += 1
            continue
        # TCPA: the recipient's own local time, not the call-center's. This is
        # the per-patient gate; executor.py's COT workingHours check is about
        # whether our Bogota staff are on shift and does not answer this.
        if not _is_within_quiet_hours(phone):
            outside_quiet_hours += 1
            continue
        item_sk = f"{now_iso}#{uuid.uuid4().hex[:8]}"
```

Extend the counts write (`update_item` at 174, `UpdateExpression` 176-179, `ExpressionAttributeValues` 180-185 on `origin/main`). `totalSkippedQuietHours` means "never enqueued", so it belongs with `totalSkippedOptOut`, not with the processor-owned `totalFailed`/`totalOptedOut`:

```python
        UpdateExpression=(
            "SET totalEnqueued = :n, totalSqsSendFailed = :f, "
            "totalSkippedOptOut = :o, totalSkippedQuietHours = :q, updatedAt = :t"
        ),
        ExpressionAttributeValues={
            ":n": enqueued,
            ":f": failed,
            ":o": opted_out,
            ":q": outside_quiet_hours,
            ":t": now_iso,
        },
```

Add `skipped_quiet_hours=outside_quiet_hours` to the `sms_sender_enqueued` log call (`188-194` on `origin/main`, beside the existing `skipped_opt_out=opted_out` at 193), and add `"totalSkippedQuietHours": 0` to the start record's initial `put_item` (`Item` block `70-91`, beside `"totalSkippedOptOut": 0` at 86) so the attribute always exists for readers.

- [ ] **Step 6: Clarify the COT staffing gate — comments only, no behaviour change**

In `services/api-plans/src/executor.py`, add above `_COT_TZ` (line 5142) and tighten the docstrings of `_is_working_day` (5152-5156) and `_within_working_hours` (5167-5171):

```python
# Colombia Time, fixed UTC-5, no DST. This is the CALL-CENTER STAFFING clock:
# every guard built on it (workingHours, loop.startTime/endTime, the 19:00 daily
# cutoff at _DAILY_CUTOFF_HOUR) answers "are our Bogota agents on shift?"
#
# It is NOT a TCPA quiet-hours gate and must not be used as one. TCPA is a
# property of the RECIPIENT's local time; see
# vip_shared.domain.services.quiet_hours, applied per lead by the bulk SMS
# sender, and communicationTimeConfig.localTimeZoneDetection=AREA_CODE, applied
# per recipient by Connect Campaigns for the voice path.
_COT_TZ = timezone(timedelta(hours=-5))
```

Change no logic. Every existing `_within_working_hours` test must keep passing untouched — that is the proof the staffing gate survived.

- [ ] **Step 7: Add the env vars in CDK**

Extend the `smsSenderFunction` environment block — `infra/lib/stacks/api-sms-stack.ts:162` (function declared at 142, `functionName: 'vip-admin-sms-sender'`; the second block at 204 is `smsProcessorFunction`, do not edit it).

On `origin/main` that block spans `162-168` and holds exactly five vars — `SMS_CAMPAIGN_QUEUE_TABLE` (163), `SMS_CAMPAIGN_RUNS_TABLE` (164), `SMS_SQS_QUEUE_URL` (165), `PROFILES_DOMAIN_NAME` (166), and `OPT_OUT_TABLE: 'VipConnectOptOutList'` (167, already wired by PR #7). Leave all five alone and append only the four new vars:

```typescript
        // Per-recipient TCPA window: full statutory hours (08:00-21:00 local),
        // stricter than statute on days (Mon-Sat, no Sunday — a VIP business
        // choice). Env vars so counsel can narrow either axis without a deploy
        // of new code. QUIET_HOURS_DAYS is Python weekday(): Monday=0..Sunday=6.
        QUIET_HOURS_START: '08:00',
        QUIET_HOURS_END: '21:00',
        QUIET_HOURS_DAYS: '0,1,2,3,4,5',
        QUIET_HOURS_DEFAULT_TZ: 'America/New_York',
```

If `OPT_OUT_TABLE` is **not** in that block when you get here, you are on a stale tree, not a pre-merge one — same diagnosis and same stop condition as Step 1. Fix the checkout; do not add the var yourself.

- [ ] **Step 8: Run all tests to verify they pass**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/shared  && python -m pytest tests/unit/ -v
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-sms && python -m pytest tests/unit/ -v
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-plans && python -m pytest tests/unit/ -v
```

Capture a baseline before the change and confirm the failure set did not grow.

- [ ] **Step 9: Lint (baseline-compare, touched files only)**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns
ruff check services/shared/python/vip_shared/domain/services/quiet_hours.py \
  services/shared/tests/unit/test_quiet_hours.py \
  services/api-sms/src/sms_sender_handler.py \
  services/api-sms/tests/unit/test_sms_sender.py \
  services/api-plans/src/executor.py
ruff format --check services/shared/python/vip_shared/domain/services/quiet_hours.py \
  services/api-sms/src/sms_sender_handler.py
```

- [ ] **Step 10: Synth, commit, deploy (ask for explicit confirmation before deploying)**

`npm run build` and lint are broken in this repo — `cdk synth` is the real validation (see the `build-and-synth` skill).

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/infra
cdk synth VipAdminApiSmsStack > /tmp/sms-synth.yaml
grep -E "QUIET_HOURS_(START|END|DAYS|DEFAULT_TZ)" /tmp/sms-synth.yaml
cd /home/devaju/projects/vip-connect-external-campaigns
git add services/shared services/api-sms services/api-plans/src/executor.py infra/lib/stacks/api-sms-stack.ts
git commit -m "fix(tcpa): gate bulk SMS on per-lead local quiet hours; document COT check as staffing-only"
cd infra && cdk deploy VipAdminApiSmsStack
```

- [ ] **Step 11: Verify the deployed function, not the synth**

```bash
aws lambda get-function-configuration --profile production --region us-east-1 \
  --function-name vip-admin-sms-sender \
  --query 'Environment.Variables.[QUIET_HOURS_START,QUIET_HOURS_END,QUIET_HOURS_DAYS,OPT_OUT_TABLE]' --output text
```

Expected: `08:00  21:00  0,1,2,3,4,5  VipConnectOptOutList`. The last value proves this deploy did not clobber the opt-out gate.

---

### Task 2: Per-recipient quiet hours for the voice campaign (Connect Campaigns V2, native)

Runs in parallel with Task 1.

**Files:** `services/api-campaigns/src/builders.py:93-99`, `services/api-campaigns/tests/unit/test_builders.py`, `services/api-plans/src/builders.py:548-564`, `services/api-plans/tests/unit/test_builders.py`

**Interfaces:**
- Consumes: nothing.
- Produces: the `communicationTimeConfig` shape used by both `connectcampaignsv2:CreateCampaign` call sites. Task 7 reads it back from a live campaign.

API shapes verified against installed botocore 1.43.90 — every field below is real:

```
CommunicationTimeConfig := { localTimeZoneConfig, telephony, sms, email, whatsApp }
LocalTimeZoneConfig     := { defaultTimeZone, localTimeZoneDetection, localTimeZoneDetectionScope }
LocalTimeZoneDetectionType enum = ['ZIP_CODE', 'AREA_CODE']
TimeWindow  := { openHours, restrictedPeriods }   # openHours REQUIRED
OpenHours   := union { dailyHours: map<DayOfWeek, TimeRangeList> }   # only member
DayOfWeek enum = ['MONDAY','TUESDAY',...,'SATURDAY','SUNDAY']
TimeRange   := { startTime: Iso8601Time, endTime: Iso8601Time }   # both required
Iso8601Time := string, pattern T\d{2}:\d{2}   # model has no ^ $ anchors
RestrictedPeriods := union { restrictedPeriodList: [ {name, startDate, endDate} ] }
```

**The `T` prefix is not optional, and nothing local will tell you if you get it wrong.** `Iso8601Time`'s regex in the botocore model (`connectcampaignsv2/2024-04-23/service-2.json.gz`) is `T\d{2}:\d{2}`, so the literals must be `"T08:00"` / `"T21:00"`.

**Do not expect botocore to catch a missing `T`.** Verified by running `botocore.validate.ParamValidator` directly against `CreateCampaign`'s input shape with an otherwise-complete payload: `"T08:00"`, `"08:00"`, `"8:00"` and even `"garbage"` all return **VALIDATES CLEAN**. Botocore's parameter validator enforces types, required members and enums — it does **not** enforce string `pattern` constraints. So a missing `T` is signed and sent, and only the service rejects it (or, worse, interprets it in some way nobody here has observed). An earlier draft of this plan asserted the opposite; that assertion was wrong and is corrected here.

The consequence: **the unit tests in Step 1 are the only local guard**, which is why `test_communication_time_config_sets_telephony_open_hours_monday_to_saturday` asserts the exact literal `{"startTime": "T08:00", "endTime": "T21:00"}` rather than something looser, and `test_open_hours_times_carry_the_iso8601_t_prefix` asserts the prefix separately. Do not weaken either into a regex or a "looks like a time" check. No code in this repo uses `openHours` today, so there is no existing example to copy from and nothing to compare a mistake against.

**Excluding Sunday: two syntactically valid encodings, one unverified semantic.** `DailyHours` is a plain map with no required keys, and `TimeRangeList` has no minimum item count, so botocore accepts both "omit the `SUNDAY` key" and `"SUNDAY": []`. Which one Connect *interprets* as "never contact on this day" is **not documented and not verified** — and it cannot be verified without calling `CreateCampaign`, which this planning pass must not do. This plan chooses **omitting the key**, on the reading that an "open hours" map with no entry for a day declares no open hours for it, and that an empty list is more likely to be treated as unset/ignored than as a closed day. Step 6 and Task 7 must prove this empirically before anyone relies on it. `restrictedPeriods` is **not** an alternative: it is a list of absolute date ranges (`startDate`/`endDate`) and cannot express a weekly recurrence.

`AREA_CODE` over `ZIP_CODE` because every phone number has an area code; `ZIP_CODE` needs a populated Customer Profiles address we have not confirmed exists.

Current state, verified: `services/api-campaigns/src/builders.py:94` guards with `if "segmentArn" in body and body.get("communicationTime"):` and then sets only `defaultTimeZone` (line 98) from a timezone the operator picks **once for the whole campaign** — one timezone for every recipient. `services/api-plans/src/builders.py:554-556` is worse: hardcoded `America/New_York` with **no `openHours` at all**, so Connect enforces no quiet-hours window and only the UTC campaign `schedule` prevents a 6 a.m. Pacific dial. Both are the wrong axis.

- [ ] **Step 1: Write the failing tests**

In `services/api-campaigns/tests/unit/test_builders.py` (reuse the body fixture the existing test at lines 50-62 already uses — do not add a second one):

```python
_CONTACT_DAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY"]


def test_communication_time_config_uses_per_recipient_area_code_detection():
    """One operator-picked timezone per campaign is the wrong axis — TCPA quiet
    hours are per-recipient-local, so Connect must resolve each recipient's own
    timezone from their area code."""
    params = build_create_campaign_params(
        _body(),
        connect_instance_id="instance-1",
        profiles_domain_arn="arn:aws:profile:us-east-1:123:domains/d",
    )
    ltz = params["communicationTimeConfig"]["localTimeZoneConfig"]
    assert ltz["localTimeZoneDetection"] == ["AREA_CODE"]
    assert ltz["defaultTimeZone"] == "America/New_York"  # fallback only


def test_communication_time_config_sets_telephony_open_hours_monday_to_saturday():
    params = build_create_campaign_params(
        _body(),
        connect_instance_id="instance-1",
        profiles_domain_arn="arn:aws:profile:us-east-1:123:domains/d",
    )
    daily = params["communicationTimeConfig"]["telephony"]["openHours"]["dailyHours"]
    assert sorted(daily) == sorted(_CONTACT_DAYS)
    for day in _CONTACT_DAYS:
        assert daily[day] == [{"startTime": "T08:00", "endTime": "T21:00"}]


def test_sunday_key_is_absent_from_daily_hours():
    """No contact on Sunday. Encoded by OMITTING the key, not by an empty list —
    see the shape table: both are syntactically valid, and this is the one whose
    semantics we are betting on. Task 7 verifies it against a real dial attempt.
    """
    params = build_create_campaign_params(
        _body(),
        connect_instance_id="instance-1",
        profiles_domain_arn="arn:aws:profile:us-east-1:123:domains/d",
    )
    daily = params["communicationTimeConfig"]["telephony"]["openHours"]["dailyHours"]
    assert "SUNDAY" not in daily


def test_open_hours_times_carry_the_iso8601_t_prefix():
    """Iso8601Time's pattern is T\\d{2}:\\d{2}.

    botocore does NOT enforce string patterns, so a missing T validates clean
    locally and is sent to the service — this test is the only thing standing
    between a typo and a rejected (or misread) CreateCampaign in production.
    Nothing else in this repo uses openHours, so there is no precedent to
    compare against.
    """
    params = build_create_campaign_params(
        _body(),
        connect_instance_id="instance-1",
        profiles_domain_arn="arn:aws:profile:us-east-1:123:domains/d",
    )
    daily = params["communicationTimeConfig"]["telephony"]["openHours"]["dailyHours"]
    for ranges in daily.values():
        for rng in ranges:
            assert rng["startTime"].startswith("T")
            assert rng["endTime"].startswith("T")


def test_communication_time_config_emitted_even_without_communication_time_key():
    """Previously a segment campaign created without body['communicationTime']
    got NO communicationTimeConfig at all — i.e. zero quiet-hours enforcement.
    That hole is the point of this change."""
    body = _body()
    body.pop("communicationTime", None)
    params = build_create_campaign_params(
        body,
        connect_instance_id="instance-1",
        profiles_domain_arn="arn:aws:profile:us-east-1:123:domains/d",
    )
    assert "communicationTimeConfig" in params
```

In `services/api-plans/tests/unit/test_builders.py`, replace the bare `assert "communicationTimeConfig" in params` at line 348 with real assertions:

```python
def test_campaign_params_gates_telephony_on_per_lead_local_open_hours():
    params = build_campaign_params(
        _campaign_bucket(),
        segment_arn="arn:aws:profile:us-east-1:123:domains/d/segment-definitions/s",
        connect_instance_id="instance-1",
        profiles_domain_arn="arn:aws:profile:us-east-1:123:domains/d",
        start_time="2026-05-01T13:00:00Z",
        end_time="2026-05-01T21:00:00Z",
        campaign_name="test-campaign",
    )
    ctc = params["communicationTimeConfig"]
    assert ctc["localTimeZoneConfig"]["localTimeZoneDetection"] == ["AREA_CODE"]
    assert ctc["localTimeZoneConfig"]["defaultTimeZone"] == "America/New_York"
    daily = ctc["telephony"]["openHours"]["dailyHours"]
    assert daily["SATURDAY"] == [{"startTime": "T08:00", "endTime": "T21:00"}]
    assert "SUNDAY" not in daily  # no contact on Sunday
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-campaigns && python -m pytest tests/unit/test_builders.py -v
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-plans && python -m pytest tests/unit/test_builders.py -v
```
Expected: `KeyError: 'localTimeZoneDetection'` / `KeyError: 'telephony'`.

- [ ] **Step 3: Implement in `services/api-campaigns/src/builders.py`**

Add after the imports:

```python
# TCPA quiet hours are a property of the *recipient's* local time, not of a
# timezone the operator picks once per campaign. Connect Campaigns V2 resolves
# the recipient's timezone itself; we only declare the window.
#
# AREA_CODE over ZIP_CODE: every phone number has an area code, whereas
# ZIP_CODE needs a populated Customer Profiles address we have not confirmed.
#
# The window is the FULL statutory TCPA span on the hours axis (08:00-21:00
# recipient-local) and stricter than statute on the day axis: Monday-Saturday
# only. TCPA does not exempt Sunday; excluding it is a VIP business choice.
#
# The "T" prefix is mandatory — Iso8601Time's pattern is T\d{2}:\d{2}. Note
# that botocore does NOT enforce string patterns: a bare "08:00" validates
# clean locally and is sent to the service. The unit tests are the only guard.
#
# SUNDAY is excluded by OMITTING the key from dailyHours. An empty list
# ("SUNDAY": []) is equally valid per the model but its semantics are
# undocumented; see the shape table in this task.
_QUIET_HOURS_START = "T08:00"
_QUIET_HOURS_END = "T21:00"
_CONTACT_DAYS = (
    "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY",
)


def _open_hours() -> dict[str, Any]:
    """A TimeWindow gating one channel to the recipient-local quiet-hours window."""
    return {
        "openHours": {
            "dailyHours": {
                day: [{"startTime": _QUIET_HOURS_START, "endTime": _QUIET_HOURS_END}]
                for day in _CONTACT_DAYS
            }
        }
    }
```

Replace lines 93-99 (comment through the closing brace):

```python
    # communicationTimeConfig only valid for segment-source campaigns, not event-trigger
    if "segmentArn" in body:
        comm_time = body.get("communicationTime") or {}
        params["communicationTimeConfig"] = {
            "localTimeZoneConfig": {
                # Fallback only — used when Connect cannot resolve the area code.
                "defaultTimeZone": comm_time.get("timezone", "America/New_York"),
                "localTimeZoneDetection": ["AREA_CODE"],
            },
            "telephony": _open_hours(),
        }
```

Note the guard change: `communicationTime` is no longer required for the block to be emitted. The `"segmentArn" in body` half stays — `eventTrigger` campaigns still reject `communicationTimeConfig`, which the existing test at line 135 asserts. Update the body-contract docstring at line 33 to say `communicationTime.timezone` is now only the ambiguous-area-code fallback.

- [ ] **Step 4: Implement in `services/api-plans/src/builders.py`**

Add the same constants and `_open_hours()` helper to this module. Do **not** import across services: `api-campaigns` and `api-plans` are separately bundled Lambdas with no shared import path, `vip_shared` is the only shared code, and this is 12 lines of literal config — below the bar for a new shared module.

Replace lines 554-556:

```python
        "communicationTimeConfig": {
            "localTimeZoneConfig": {
                # Fallback only, for area codes Connect cannot resolve.
                "defaultTimeZone": "America/New_York",
                "localTimeZoneDetection": ["AREA_CODE"],
            },
            "telephony": _open_hours(),
        },
```

Leave the `if delivery_type == "journey":` block at 559-564 alone. The pre-call SMS is not a journey campaign, so no `sms` `TimeWindow` is needed here — Task 1 covers that channel.

- [ ] **Step 5: Run tests, lint, commit, deploy (ask for explicit confirmation)**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-campaigns && python -m pytest tests/unit/ -v
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-plans && python -m pytest tests/unit/ -v
cd /home/devaju/projects/vip-connect-external-campaigns
ruff check services/api-campaigns/src/builders.py services/api-plans/src/builders.py \
  services/api-campaigns/tests/unit/test_builders.py services/api-plans/tests/unit/test_builders.py
git add services/api-campaigns services/api-plans/src/builders.py services/api-plans/tests/unit/test_builders.py
git commit -m "fix(tcpa): gate voice campaigns on per-recipient local quiet hours via AREA_CODE detection + openHours"
cd infra && cdk deploy VipAdminApiCampaignsStack && cdk deploy VipAdminApiPlansStack
```

- [ ] **Step 6: Verify against a real campaign, not the synth**

```bash
aws connectcampaignsv2 describe-campaign --profile production --region us-east-1 \
  --id <campaign-id> --query 'campaign.communicationTimeConfig' --output json
```

Expected: `localTimeZoneDetection == ["AREA_CODE"]`, and `telephony.openHours.dailyHours` containing exactly six keys — `MONDAY` through `SATURDAY`, each `[{"startTime": "T08:00", "endTime": "T21:00"}]` — with **no `SUNDAY` key**.

Two things to confirm here, not assume:

1. **Did the `T` prefix survive?** If `CreateCampaign` raised `ParamValidationError` locally (before any HTTP call), the prefix is missing. If it raised `ValidationException` from the service, capture the exact message — it names the offending field — and reconcile against the shape table above before changing anything else.
2. **Did the service keep `SUNDAY` absent, or did it normalise the map?** If `describe-campaign` echoes back a `SUNDAY` key you did not send (with any value, including `[]`), the omission encoding is **not** how Connect represents a closed day, and Task 7's Sunday dial test becomes a hard blocker rather than a confirmation. Record what came back verbatim.

---

### Task 3: Template personalization — renderer first, then a scoped allowlist

Logically independent of Tasks 1 and 2, and must land before **Task 4** (which sends a template that has to render) and before **Task 5** (which validates pre-call copy against the allowlist).

**One sequencing caveat, not a logical dependency:** Task 1 and this task both edit the same per-phone loop in `sms_sender_handler.py`. Step 6 below rewrites that loop to iterate recipient dicts instead of bare phone strings, including the quiet-hours line Task 1 adds. **Land Task 1 first** to avoid resolving that by hand; if this task somehow lands first, Task 1's gate must be written against `recipient["phone"]`. Task 2 touches neither file and is genuinely parallel to both.

**Files:** `services/shared/python/vip_shared/domain/services/sms_template.py` (new), `services/shared/tests/unit/test_sms_template.py` (new), `services/api-sms/src/sms_sender_handler.py`, `services/api-sms/tests/unit/test_sms_sender.py`, `services/api-plans/src/handlers/plans.py`, `services/api-plans/tests/unit/test_plan_validation_sms.py`, `services/api-plans/tests/unit/test_executor_v2.py` (one fixture at line 7209)

**Interfaces:**
- Consumes: nothing.
- Produces: `vip_shared.domain.services.sms_template` — `RECIPIENT_FIELDS`, `CAMPAIGN_FIELDS`, `ALLOWED_FIELDS`, `extract_placeholders(tmpl) -> set[str]`, `render(tmpl, *, recipient, campaign) -> str`, `max_rendered_length(tmpl, *, campaign) -> int`; and `_get_segment_recipients` replacing `_get_segment_phones` in the sender. Task 5 validates against the same allowlist; Task 4 relies on rendering already working.

**Step order is not negotiable: the renderer lands before the guard is narrowed.** Verified reason — nothing in the pipeline interpolates anything today. `sms_processor_handler.py:92` sets `"MessageBody": message_template` verbatim, and `sms_sender_handler.py:118` copies the template into the SQS body unchanged. Narrowing the guard first would let a template with `{{FirstName}}` through to a patient as literal braces.

**Where rendering happens: the sender.** `_get_segment_phones` (`sms_sender_handler.py:201-239`) already calls `batch_get_profile` and receives whole profiles, then keeps **only** `PhoneNumber`/`MobilePhoneNumber` and discards the rest (lines 229-232). The recipient's `FirstName` is therefore already being fetched and thrown away — rendering in the sender needs no new API call. The processor stays **completely unchanged**: the sender keeps writing the finished text under the existing `messageTemplate` SQS key.

> **Do not rename that SQS key.** Renaming `messageTemplate` → `messageBody` would strand every in-flight message during the deploy window (the processor would `KeyError` on messages enqueued by the old sender). Keep the key and document that it now carries rendered text.

**Minimum necessary:** carry only the allowlisted fields out of the profile, never the whole record.

- [ ] **Step 1: Write the failing renderer tests**

Create `services/shared/tests/unit/test_sms_template.py`:

```python
"""Tests for pre-call SMS template rendering with a scoped placeholder allowlist."""

from __future__ import annotations

import pytest

from vip_shared.domain.services.sms_template import (
    CAMPAIGN_FIELDS,
    RECIPIENT_FIELDS,
    extract_placeholders,
    max_rendered_length,
    render,
)

_CAMPAIGN = {"clinicName": "VIP Medical Group"}


def test_allowlists_are_narrow_and_explicit():
    """Guard against scope creep: widening these sets is a PHI decision."""
    assert RECIPIENT_FIELDS == {"FirstName"}
    assert CAMPAIGN_FIELDS == {"ClinicName"}


def test_extract_finds_all_placeholders():
    """extract_placeholders is deliberately allowlist-blind — it reports what the
    template contains so validation can diff against ALLOWED_FIELDS and name the
    offender. {{Specialty}} is used here precisely because it is NOT allowlisted."""
    tmpl = "Hi {{FirstName}}! This is {{ClinicName}} about {{Specialty}}."
    assert extract_placeholders(tmpl) == {"FirstName", "ClinicName", "Specialty"}


def test_renders_recipient_and_campaign_fields():
    out = render(
        "Hi {{FirstName}}! This is {{ClinicName}} about your visit.",
        recipient={"FirstName": "Maria"},
        campaign=_CAMPAIGN,
    )
    assert out == "Hi Maria! This is VIP Medical Group about your visit."


def test_missing_first_name_uses_neutral_fallback():
    """Never deliver 'Hi !' or a literal placeholder when CP has no name."""
    out = render("Hi {{FirstName}}!", recipient={}, campaign=_CAMPAIGN)
    assert out == "Hi there!"
    assert "{{" not in out


@pytest.mark.parametrize("junk", ["", "   ", None])
def test_blank_first_name_uses_fallback(junk):
    out = render("Hi {{FirstName}}!", recipient={"FirstName": junk}, campaign=_CAMPAIGN)
    assert out == "Hi there!"


def test_name_is_normalized_to_title_case():
    out = render("Hi {{FirstName}}!", recipient={"FirstName": "  mARIA  "}, campaign=_CAMPAIGN)
    assert out == "Hi Maria!"


def test_junk_name_containing_digits_falls_back():
    """CP records contain test rows like 'TEST 12345'. Interpolating that would
    inject a long numeric string into an SMS — exactly what the MRN pattern in
    the plan-side PHI guard exists to prevent."""
    out = render("Hi {{FirstName}}!", recipient={"FirstName": "TEST 12345"}, campaign=_CAMPAIGN)
    assert out == "Hi there!"


def test_absurdly_long_name_is_truncated():
    out = render("Hi {{FirstName}}!", recipient={"FirstName": "A" * 200}, campaign=_CAMPAIGN)
    assert len(out) < 60


def test_unknown_placeholder_is_never_rendered():
    """Defense in depth: validation should have rejected this template, but the
    renderer must not silently interpolate an unlisted field if one slips in."""
    with pytest.raises(ValueError, match="Diagnosis"):
        render("Your {{Diagnosis}}", recipient={"Diagnosis": "x"}, campaign=_CAMPAIGN)


def test_max_rendered_length_budgets_for_the_longest_realistic_name():
    """A 140-char template with {{FirstName}} can exceed 160 once rendered."""
    tmpl = "Hi {{FirstName}}! " + ("x" * 140)
    assert max_rendered_length(tmpl, campaign=_CAMPAIGN) > len(tmpl)
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/shared && python -m pytest tests/unit/test_sms_template.py -v
```
Expected: `ModuleNotFoundError: No module named 'vip_shared.domain.services.sms_template'`.

- [ ] **Step 3: Implement the renderer**

Create `services/shared/python/vip_shared/domain/services/sms_template.py`:

```python
"""Pre-call SMS template rendering with a deliberately narrow placeholder allowlist.

WHY AN ALLOWLIST RATHER THAN FREE INTERPOLATION:

  handlers/plans.py's _validate_sms_campaign blocks {{...}} entirely today. That
  ban existed for two reasons, and only one of them is going away:

    1. NO RENDERER EXISTED. sms_processor_handler.py passes the template straight
       to EUM SendTextMessage, so {{FirstName}} would reach the patient as
       literal braces. This module is that renderer — reason (1) is now resolved.

    2. PHI. An unrestricted placeholder mechanism is a channel for putting
       anything from a profile into an outbound message. That reason STANDS.

  So interpolation is allowed only for named fields, split by origin:

    RECIPIENT_FIELDS — per-recipient, from the patient's own Customer Profile.
      Sending patients their own first name is ordinary clinical communication:
      the recipient IS the data subject, so there is no third-party disclosure.
      LastName is deliberately NOT here — CP exposes it, but a surname adds
      identifiability with no engagement benefit.

    CAMPAIGN_FIELDS — campaign-level constants, identical for every recipient.
      Not recipient data at all.

  Widening either set is a PHI decision, not an implementation detail. Every
  other pattern in _PHI_PATTERNS (SSN, email, dates, MRN-like numbers, URLs,
  clinical terms) remains enforced against the template independently.
"""

from __future__ import annotations

import re

RECIPIENT_FIELDS = {"FirstName"}
# Specialty is deliberately NOT here: the business-approved copy bakes the
# specialty into the sentence ("your vein consultation request") rather than
# substituting a noun, so an interpolatable {{Specialty}} would be dead surface.
# See Task 5, "The approved copy", point (1) — re-adding it is additive.
CAMPAIGN_FIELDS = {"ClinicName"}
ALLOWED_FIELDS = RECIPIENT_FIELDS | CAMPAIGN_FIELDS

_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")

# Used when a profile has no usable first name. Keeps the copy grammatical
# instead of delivering "Hi !" or a literal placeholder.
_FIRST_NAME_FALLBACK = "there"

# A first name is one word of a person's name, not a free-text field. CP holds
# test/junk rows ("TEST 12345"), and interpolating those would push digits into
# the message body — the exact shape the plan-side MRN pattern guards against.
_NAME_MAX_LEN = 20
_NAME_OK_RE = re.compile(r"^[A-Za-z][A-Za-z'\- ]*$")

# Length budget for validation: assume the longest name we would ever render.
_NAME_BUDGET = _NAME_MAX_LEN


def extract_placeholders(template: str) -> set[str]:
    """Return every `{{Field}}` name appearing in `template`."""
    return set(_PLACEHOLDER_RE.findall(template or ""))


def _clean_first_name(raw: object) -> str:
    if not isinstance(raw, str):
        return _FIRST_NAME_FALLBACK
    name = raw.strip()
    if not name or not _NAME_OK_RE.match(name):
        return _FIRST_NAME_FALLBACK
    return name[:_NAME_MAX_LEN].title()


def render(template: str, *, recipient: dict, campaign: dict) -> str:
    """Interpolate allowlisted placeholders in `template`.

    Raises ValueError for any placeholder outside ALLOWED_FIELDS — validation
    should already have rejected such a template, so reaching here means the
    guard was bypassed and sending would be worse than failing.
    """
    unknown = extract_placeholders(template) - ALLOWED_FIELDS
    if unknown:
        raise ValueError(
            f"template contains non-allowlisted placeholder(s): {sorted(unknown)}"
        )

    values = {
        "FirstName": _clean_first_name(recipient.get("FirstName")),
        "ClinicName": str(campaign.get("clinicName") or "").strip(),
    }
    return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template)


def max_rendered_length(
    template: str, *, campaign: dict, name_budget: int = _NAME_BUDGET
) -> int:
    """Worst-case rendered length, for the SMS length validation budget.

    A template can be under the ceiling and still render over it once a name is
    substituted, so validation must measure the rendered worst case. The default
    budget is _NAME_MAX_LEN because render() truncates there, which makes it a
    real upper bound rather than a guess.

    `name_budget` is overridable only so tests and the OQ-9 analysis can ask
    "what if we truncated names shorter?" without mutating a module global.
    """
    return len(
        render(
            template,
            recipient={"FirstName": "A" * name_budget},
            campaign=campaign,
        )
    )
```

- [ ] **Step 4: Write the failing sender tests**

```python
def test_sender_renders_first_name_per_recipient():
    handler = _load_handler()
    sent_bodies = []
    mock_sqs = MagicMock()
    mock_sqs.send_message_batch.side_effect = lambda **kw: (
        sent_bodies.extend(json.loads(e["MessageBody"]) for e in kw["Entries"]),
        {"Failed": []},
    )[1]

    mock_cp = _make_mock_cp_profiles(
        [
            {"PhoneNumber": "+12125551234", "FirstName": "Maria"},
            {"PhoneNumber": "+12125555678", "FirstName": "Jose"},
        ]
    )

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", _mock_ddb()),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_cp", mock_cp),
        patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda *_: False)),
        patch.object(handler, "_is_within_quiet_hours", lambda *_a, **_k: True),
    ):
        handler.lambda_handler(
            _base_event(
                message_template="Hi {{FirstName}}! This is {{ClinicName}}.",
                clinic_name="VIP Medical Group",
            ),
            None,
        )

    bodies = sorted(b["messageTemplate"] for b in sent_bodies)
    assert bodies == [
        "Hi Jose! This is VIP Medical Group.",
        "Hi Maria! This is VIP Medical Group.",
    ]


def test_sender_never_writes_a_rendered_body_to_dynamo():
    """The queue item must stay body-free — it is the long-lived record."""
    handler = _load_handler()
    queue_table = MagicMock()
    ...
    written = [c.kwargs["Item"] for c in queue_table.batch_writer().__enter__().put_item.call_args_list]
    for item in written:
        assert "messageTemplate" not in item and "messageBody" not in item


def test_sender_never_logs_a_name_or_a_rendered_body(caplog):
    ...
    assert "Maria" not in caplog.text
```

- [ ] **Step 5: Run to verify they fail**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-sms && python -m pytest tests/unit/test_sms_sender.py -v
```
Expected: the enqueued body is the raw template with literal `{{FirstName}}`.

- [ ] **Step 6: Implement in the sender**

Change `_get_segment_phones` (`sms_sender_handler.py:201-239`) to return the allowlisted recipient fields alongside the phone, instead of a bare `list[str]`. Rename it `_get_segment_recipients` and have it return `list[dict]` of `{"phone": ..., "FirstName": ...}` — **only** those keys, never the whole profile:

```python
                for profile in batch_resp.get("Profiles", []):
                    raw = profile.get("PhoneNumber") or profile.get("MobilePhoneNumber") or ""
                    if raw:
                        # Minimum necessary: carry ONLY the allowlisted render
                        # fields out of the profile, never the whole record.
                        recipients.append(
                            {
                                "phone": _normalize_phone(raw),
                                "FirstName": profile.get("FirstName") or "",
                            }
                        )
```

Update the per-phone loop to iterate recipients, keeping the opt-out check first and the quiet-hours check second (both operate on `recipient["phone"]`), and render once per recipient:

```python
        body = _render(
            message_tmpl,
            recipient=recipient,
            campaign={"clinicName": event.get("clinicName", "")},
        )
```

Add the import beside the quiet-hours one from Task 1:

```python
from vip_shared.domain.services.sms_template import render as _render
```

Extend the documented event shape in the `lambda_handler` docstring (`sms_sender_handler.py:42-52`) with the new optional key `clinicName`. This is not cosmetic: if the handler does not read it off the event, `{{ClinicName}}` renders empty and patients receive `This is .` — Task 5's `test_precall_requires_clinic_name_if_template_uses_it` is the save-time guard, and this docstring is what stops the next reader from assuming the key is unused.

Put `body` into the SQS `MessageBody` under the existing `"messageTemplate"` key. Add a comment there recording that the key name is retained deliberately for deploy compatibility. **Do not add the rendered body to the DynamoDB queue item** — that item is the long-lived record (30-day TTL) and today holds no message content; keep it that way.

If `render` raises `ValueError` (a non-allowlisted placeholder slipped past validation), skip that campaign rather than sending — count it and log the *field names*, never the template or the values.

- [ ] **Step 7: Narrow the plan-side guard**

Write these failing tests in `services/api-plans/tests/unit/test_plan_validation_sms.py` first.

Three existing tests reference placeholders. All were checked; **none needs its expectation inverted**, but two need a comment so a later reader does not "fix" them:

- `test_plan_validation_sms.py:234` asserts `"Hello {{firstName}}, ..."` is rejected. It **stays rejected** — the allowlist is case-sensitive `FirstName`, so lowercase `firstName` is not an allowed field. Keep the test and add a comment recording that this is deliberate case-sensitivity, not an oversight.
- `test_plan_validation_sms.py:239` asserts `"Hello ${firstName}, ..."` is rejected. Unchanged — `${...}` stays banned outright.
- `test_executor_v2.py:7209` is a **fixture**, not a validation test (`TestSmsReconcile._sms_campaign`, line 7201) — it never calls validation, so the guard change cannot break it. But its `"smsMessageTemplate": "Hello {{firstName}}"` is now a template the API would reject, so update the fixture to `"Hello {{FirstName}}"` to stop the suite enshrining an invalid config.

```python
def test_allowlisted_placeholders_are_accepted():
    errors = _validate(
        _campaign(template="Hi {{FirstName}}! This is {{ClinicName}}, calling shortly."),
        "b", 0,
    )
    assert errors == []


def test_non_allowlisted_placeholder_is_rejected_by_name():
    errors = _validate(_campaign(template="Your {{Diagnosis}} result is ready."), "b", 0)
    assert any("Diagnosis" in e for e in errors)


def test_lastname_is_not_allowlisted():
    errors = _validate(_campaign(template="Hi {{FirstName}} {{LastName}}!"), "b", 0)
    assert any("LastName" in e for e in errors)


def test_dollar_brace_syntax_stays_banned():
    """No renderer supports ${...}; it can only be a mistake."""
    errors = _validate(_campaign(template="Hi ${FirstName}!"), "b", 0)
    assert errors != []


def test_other_phi_patterns_still_enforced_alongside_placeholders():
    """Narrowing the placeholder rule must not weaken the eight non-placeholder
    patterns (SSN, email, dates, long numeric IDs, URLs, clinical terms, ...)."""
    for bad in (
        "Hi {{FirstName}}, ssn 123-45-6789",
        "Hi {{FirstName}}, see https://x.co",
        "Hi {{FirstName}}, your diagnosis is ready",
        "Hi {{FirstName}}, acct 12345678",
        "Hi {{FirstName}}, on 01/02/2026",
    ):
        assert _validate(_campaign(template=bad), "b", 0) != [], bad


def test_length_is_measured_on_the_rendered_worst_case():
    """158 raw chars passes a raw check but renders to 165, over the ceiling.

    "Hi {{FirstName}}! " is 18 chars, of which the placeholder is 13; at the
    20-char name budget the prefix becomes 25, so 25 + 140 = 165 > 160.
    A raw len() check would have accepted this template.
    """
    tmpl = "Hi {{FirstName}}! " + "x" * 140
    errors = _validate(_campaign(template=tmpl), "b", 0)
    assert any("160" in e for e in errors)
```

Then in `services/api-plans/src/handlers/plans.py`, inside `_validate_sms_campaign`:

- **Remove exactly one entry** from `_PHI_PATTERNS`: the `\{\{[^}]+\}\}` pattern (one of the two "template placeholder" entries at lines 528-529). **Keep** its sibling `\$\{[^}]+\}`. Leave all nine remaining entries byte-identical.
- Add an allowlist check in their place, and evaluate the remaining PHI patterns against the template with placeholders **stripped**, so `{{FirstName}}` cannot itself trip a pattern while real violations elsewhere in the copy still do.
- Replace the raw `len(tmpl) > 160` check (line 514) with the rendered worst case.

**Read the paragraph that follows this code block before you write any of it.** The block assumes `vip_shared` is importable from `api-plans`; that assumption has to be confirmed first, and if it is false the block changes shape.

```python
    # Placeholder policy: a NAMED ALLOWLIST, not a blanket ban.
    # The blanket {{...}} ban existed partly because no renderer existed —
    # sms_processor_handler passes the template verbatim to EUM, so a
    # placeholder would reach the patient as literal braces. That renderer now
    # exists (vip_shared.domain.services.sms_template), so the ban narrows to
    # "only these fields". Everything else stays blocked, and ${...} stays
    # banned outright because no renderer supports it.
    unknown = extract_placeholders(tmpl) - ALLOWED_FIELDS
    if unknown:
        errors.append(
            f"{prefix}: smsMessageTemplate uses non-allowlisted placeholder(s) "
            f"{sorted(unknown)}. Allowed: {sorted(ALLOWED_FIELDS)}."
        )
    # Run the remaining PHI patterns against the template with placeholders
    # removed, so an allowlisted placeholder cannot trip them while real
    # violations in the surrounding copy still do.
    scannable = _PLACEHOLDER_RE.sub("", tmpl)
```

**This is the check the block above depends on — run it first.** Confirm with `grep -rn "vip_shared" services/api-plans/src/ | head` that the shared layer is genuinely importable from `api-plans`. Note that validation needs **three** things from `sms_template`, not just a set literal: `ALLOWED_FIELDS`, `extract_placeholders` (i.e. `_PLACEHOLDER_RE`), and `max_rendered_length`. So:

- **If `vip_shared` is importable here** (the expected case): import all three. No duplication.
- **If it is not**: mirror the allowlist sets *and* the placeholder regex locally, and compute the worst case as `len(tmpl_with_placeholders_substituted_by_20_A's)` rather than importing the renderer. Then add a test asserting the mirrored allowlist and regex match `vip_shared`'s, so the two copies cannot drift.

Do not silently mirror only the sets — that would leave the 160-char budget measuring the unrendered template, which is the bug `test_length_is_measured_on_the_rendered_worst_case` exists to catch.

- [ ] **Step 8: Re-verify SQS encryption, since bodies now carry names**

The main queue is created by a manual CLI step (`infra/lib/stacks/api-sms-stack.ts:70-106` — the CLI recipe is the comment block at `70-101`, the `fromQueueAttributes` import is `102-106`) and imported, so CDK cannot enforce its encryption. Names in message bodies make this worth confirming rather than assuming:

```bash
aws sqs get-queue-attributes --profile production --region us-east-1 \
  --queue-url "$(aws sqs get-queue-url --queue-name vip-sms-campaign-queue \
    --query QueueUrl --output text --region us-east-1 --profile production)" \
  --attribute-names KmsMasterKeyId VisibilityTimeout --output json
```

Expected: `KmsMasterKeyId` set to the `alias/vip-data-key` CMK and `VisibilityTimeout` 180. If `KmsMasterKeyId` is absent, **stop** — do not ship personalization onto an unencrypted queue; report it.

- [ ] **Step 9: Run tests, lint, commit, deploy (ask for explicit confirmation)**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/shared && python -m pytest tests/unit/ -v
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-sms && python -m pytest tests/unit/ -v
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-plans && python -m pytest tests/unit/ -v
cd /home/devaju/projects/vip-connect-external-campaigns
ruff check services/shared/python/vip_shared/domain/services/sms_template.py \
  services/shared/tests/unit/test_sms_template.py \
  services/api-sms/src/sms_sender_handler.py \
  services/api-sms/tests/unit/test_sms_sender.py \
  services/api-plans/src/handlers/plans.py \
  services/api-plans/tests/unit/test_plan_validation_sms.py \
  services/api-plans/tests/unit/test_executor_v2.py
git add services/shared services/api-sms services/api-plans
git commit -m "feat(sms): render allowlisted template placeholders; narrow PHI guard from blanket ban to named allowlist"
cd infra && cdk synth VipAdminApiSmsStack > /dev/null && cdk deploy VipAdminApiSmsStack && cdk deploy VipAdminApiPlansStack
```

Deploy the sender **before or with** the plans stack. The reverse order would let an operator save a template with `{{FirstName}}` that an un-updated sender then delivers verbatim to patients.

---

### Task 4: Fire the pre-call SMS on bucket activation

**Depends on Task 3** (rendering must work). This replaces the withdrawn `dependsOn`/lead-time design.

**Files:** `services/api-plans/src/executor.py`, `services/api-plans/tests/unit/test_executor_v2.py`, `frontend/src/lib/api.ts`

**Interfaces:**
- Consumes: `sms_template.render` (Task 3).
- Produces: `_fire_precall_sms(run, plan, bucket_index) -> None`; campaign config block `campaignConfig.precallSms`; state marker `cs["precallSmsSentAt"]`. Task 5 validates the config; Task 7 proves the ordering.

**Design.** One helper, invoked at each point where a bucket becomes live, before its campaigns are started. For every campaign in that bucket that (a) has `precallSms` configured and (b) actually reached `warming` with **both** a real `connectCampaignId` and a real `segmentArn` — the same predicate the activation loop itself uses at `executor.py:2028-2029` — invoke the existing SMS sender against **that campaign's own segment**.

Two call sites, both verified:

| Bucket | Call site | Why here |
|---|---|---|
| 0 | `start_run` (`executor.py:765`), right after the `pendingWarmup` consumption at `806` and before campaigns are started | `pendingWarmup` carries `segmentName`/`segmentArn` per campaign (`executor.py:2603-2611`), and the run now exists so `runId` is available |
| ≥1 | `_activate_warming_bucket` (`executor.py:1978`), inserted between the `_schedule_tick` block ending at `2024` and the "Start all warming campaigns" loop at `2026` | the warm step already stored `cs["segmentName"]`/`cs["segmentArn"]` (`executor.py:2190-2191`) |

Why not fire from `_prestart_plan` for bucket 0, which would give a longer 4-6 minute lead: **the run does not exist yet.** `_prestart_plan` calls `_create_campaign_only(bucket, campaign, {})` with an empty run dict (`executor.py:2600-2602`), and the SMS sender requires `planId` + `runId` for its `VipSmsCampaignRuns` bookkeeping — it reads `event["planId"]`/`event["runId"]` with `[]`, so a missing key is a `KeyError`. Inventing a synthetic run id to get a longer lead would pollute the runs table and break the counters Task 7 reads. Ordering is what Sebastian asked for; `start_run` guarantees it.

**Idempotency is mandatory, not optional.** `_prestart_plan` is explicitly retry-aware and can be invoked several times inside the 4-6 minute window (`executor.py:2575-2583`, merging `already_warmed`), and `_activate_warming_bucket` can re-run after a failed `save_run`. Without a marker the same cohort gets texted twice. Use `cs["precallSmsSentAt"]` and skip when set, and derive the SMS campaign id deterministically the way the existing SMS path does (`uuid.uuid5` over `planId#runId#bucketIndex#campaignIndex`, `executor.py:3685-3690`) so retries reuse one `VipSmsCampaignRuns` row. **Reuse that namespace UUID but prefix the name with `precall#`** — without the prefix, a pre-call SMS in the same bucket/campaign slot as a real `deliveryType: 'sms'` campaign would generate an identical id and the two would collide on one `VipSmsCampaignRuns` row.

**Never text a cohort we will not call.** Fire only for campaigns the activation loop will actually start, which means matching its own predicate at `executor.py:2028-2029` — `cs["status"] == "warming" and cs.get("connectCampaignId")` — plus a non-empty `segmentArn`. `connectCampaignId` is not redundant with the status check: the warm step has four failure paths (`_RedisRebuildingError` at `2229`, `_EmptySegmentError` at `2240`, `_CutoffTooCloseError` at `2283`, generic at `2295`), and a campaign left `warming` without a `connectCampaignId` is skipped by the start loop and never dialed. Texting it would be a promise we do not keep.

- [ ] **Step 1: Write the failing tests**

Fixture contract: `_make_run_with_precall_voice()` must return a run whose voice campaign state already carries a non-empty `connectCampaignId`, because that is what a genuinely warmed state looks like. The positive tests below rely on it; the negative tests null it out explicitly.

```python
class TestFirePrecallSms:
    """The pre-call SMS fires at bucket activation, strictly before any dial."""

    def test_fires_for_a_warmed_campaign_with_precall_config(self, mocker):
        run, plan = _make_run_with_precall_voice()
        cs = _find_cs(run, "voice-vein")
        cs["status"] = "warming"
        cs["segmentArn"] = "arn:cp:seg/vein-abc"
        cs["segmentName"] = "vein-abc"
        invoke = mocker.patch("executor._invoke_sms_sender")

        executor._fire_precall_sms(run, plan, 0)

        assert invoke.call_count == 1
        kwargs = invoke.call_args.kwargs
        assert kwargs["segmentArn"] == "arn:cp:seg/vein-abc"
        assert "{{FirstName}}" in kwargs["messageTemplate"]  # rendered by the sender
        assert cs["precallSmsSentAt"]

    def test_uses_the_same_segment_as_the_voice_campaign(self, mocker):
        """Same-list is the core guarantee. There is only ever ONE segment —
        the one the warm step built — so this must read it, never rebuild."""
        run, plan = _make_run_with_precall_voice()
        cs = _find_cs(run, "voice-vein")
        cs["status"] = "warming"
        cs["segmentArn"] = "arn:cp:seg/vein-abc"
        cs["segmentName"] = "vein-abc"
        create_seg = mocker.patch("executor._create_segment")
        mocker.patch("executor._invoke_sms_sender")

        executor._fire_precall_sms(run, plan, 0)

        create_seg.assert_not_called()

    def test_is_idempotent_across_retries(self, mocker):
        """_prestart_plan is retry-aware and _activate_warming_bucket can re-run
        after a failed save — a second pass must not re-text the cohort."""
        run, plan = _make_run_with_precall_voice()
        cs = _find_cs(run, "voice-vein")
        cs["status"] = "warming"
        cs["segmentArn"] = "arn:cp:seg/vein-abc"
        cs["segmentName"] = "vein-abc"
        invoke = mocker.patch("executor._invoke_sms_sender")

        executor._fire_precall_sms(run, plan, 0)
        executor._fire_precall_sms(run, plan, 0)

        assert invoke.call_count == 1

    @pytest.mark.parametrize("status", ["error", "cancelled", "queued"])
    def test_does_not_fire_for_a_campaign_that_failed_to_warm(self, mocker, status):
        """Texting 'we're about to call you' to people we will never call is
        worse than sending nothing."""
        run, plan = _make_run_with_precall_voice()
        cs = _find_cs(run, "voice-vein")
        cs["status"] = status
        cs["segmentArn"] = "arn:cp:seg/vein-abc"
        invoke = mocker.patch("executor._invoke_sms_sender")

        executor._fire_precall_sms(run, plan, 0)

        invoke.assert_not_called()

    def test_does_not_fire_without_a_segment_arn(self, mocker):
        run, plan = _make_run_with_precall_voice()
        cs = _find_cs(run, "voice-vein")
        cs["status"] = "warming"
        cs["segmentArn"] = None
        invoke = mocker.patch("executor._invoke_sms_sender")

        executor._fire_precall_sms(run, plan, 0)

        invoke.assert_not_called()

    def test_does_not_fire_without_a_connect_campaign_id(self, mocker):
        """Mirrors the activation loop's own predicate (executor.py:2028-2029).
        A campaign left 'warming' with no connectCampaignId is skipped by that
        loop and never dialed, so texting it would promise a call we never make."""
        run, plan = _make_run_with_precall_voice()
        cs = _find_cs(run, "voice-vein")
        cs["status"] = "warming"
        cs["segmentArn"] = "arn:cp:seg/vein-abc"
        cs["connectCampaignId"] = None
        invoke = mocker.patch("executor._invoke_sms_sender")

        executor._fire_precall_sms(run, plan, 0)

        invoke.assert_not_called()

    def test_sms_campaign_id_does_not_collide_with_a_real_sms_campaign(self):
        """The existing SMS path derives its id from the same uuid5 namespace over
        planId#runId#bucketIndex#campaignIndex (executor.py:3685-3690). Without the
        'precall#' prefix, a pre-call send in the same slot as a real sms-delivery
        campaign would share one VipSmsCampaignRuns row."""
        run, plan = _make_run_with_precall_voice()
        assert executor._precall_sms_campaign_id(run, 0, 0) != str(
            uuid.uuid5(
                uuid.UUID("a3e4b7c1-1234-5678-9012-d5e6f7a8b9c0"),
                f"{run['planId']}#{run['runId']}#0#0",
            )
        )

    def test_skips_campaigns_without_precall_config(self, mocker):
        """Every existing plan must be completely unaffected."""
        run, plan = _make_run_with_precall_voice()
        plan["buckets"][0]["campaigns"][0]["campaignConfig"].pop("precallSms")
        _find_cs(run, "voice-vein")["status"] = "warming"
        invoke = mocker.patch("executor._invoke_sms_sender")

        executor._fire_precall_sms(run, plan, 0)

        invoke.assert_not_called()

    def test_sms_send_failure_does_not_block_the_voice_campaign(self, mocker):
        """A failed pre-call text must degrade to 'no text', never to 'no call'."""
        run, plan = _make_run_with_precall_voice()
        cs = _find_cs(run, "voice-vein")
        cs["status"] = "warming"
        cs["segmentArn"] = "arn:cp:seg/vein-abc"
        cs["segmentName"] = "vein-abc"
        mocker.patch("executor._invoke_sms_sender", side_effect=RuntimeError("boom"))

        executor._fire_precall_sms(run, plan, 0)  # must not raise

    def test_each_specialty_campaign_gets_its_own_copy_and_segment(self, mocker):
        """Specialty copy comes from per-campaign config, not a lookup table."""
        run, plan = _make_run_with_two_specialties()
        invoke = mocker.patch("executor._invoke_sms_sender")

        executor._fire_precall_sms(run, plan, 0)

        assert invoke.call_count == 2
        pairs = {
            (c.kwargs["segmentArn"], c.kwargs["messageTemplate"])
            for c in invoke.call_args_list
        }
        assert len(pairs) == 2


class TestPrecallSmsOrdering:
    """Ordering is structural: the SMS is enqueued before any campaign starts."""

    def test_activate_warming_bucket_fires_sms_before_starting_campaigns(self, mocker):
        run, plan = _make_run_with_precall_voice()
        order: list[str] = []
        mocker.patch(
            "executor._fire_precall_sms",
            side_effect=lambda *a, **k: order.append("sms"),
        )
        mocker.patch("executor._schedule_tick", return_value="sched")
        mocker.patch(
            "executor.oc",
            MagicMock(start_campaign=lambda **k: order.append("dial")),
        )

        executor._activate_warming_bucket(run, plan, 0)

        assert order and order[0] == "sms"

    def test_start_run_fires_sms_before_starting_bucket_zero(self, mocker):
        ...
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-plans && python -m pytest tests/unit/test_executor_v2.py -k "Precall" -v
```
Expected: `AttributeError: module 'executor' has no attribute '_fire_precall_sms'`.

- [ ] **Step 3: Implement the helper**

Add near the other SMS helpers (`_invoke_sms_sender` is at `executor.py:367`):

```python
def _precall_sms_campaign_id(run: dict, bucket_index: int, campaign_index: int) -> str:
    """Deterministic smsCampaignId for a pre-call send, so retries reuse one
    VipSmsCampaignRuns row.

    Same uuid5 namespace as the deliveryType='sms' path (executor.py:3685-3690),
    but the name is prefixed "precall#". Without that prefix a pre-call send in
    the same bucket/campaign slot as a real SMS campaign would derive an
    identical id and the two would collide on one runs row.
    """
    return str(
        uuid.uuid5(
            uuid.UUID("a3e4b7c1-1234-5678-9012-d5e6f7a8b9c0"),
            f"precall#{run['planId']}#{run['runId']}#{bucket_index}#{campaign_index}",
        )
    )


def _fire_precall_sms(run: dict, plan: dict, bucket_index: int) -> None:
    """Send each configured campaign's pre-call SMS, just before the bucket dials.

    WHY HERE AND NOT VIA dependsOn:

      Ordering is structural. A bucket moves queued -> warming -> running, and
      this runs inside that final transition, before any campaign is started.
      "SMS strictly before the first dial" is therefore an invariant of the
      lifecycle rather than the result of timer arithmetic.

      The dependsOn alternative was rejected on three counts: (1) a campaign
      with dependsOn is never pre-warmed (_prestart_plan / _prestart_next_bucket
      both filter on `not campaign.get("dependsOn")`), so it would silently cost
      the voice campaign its warmup; (2) a long queued-with-terminal-parents
      wait trips NoActiveCampaign after _NO_ACTIVE_CAMPAIGN_MINUTES and StuckRun
      after _STUCK_RUN_HOURS; (3) it needed a new cross-campaign
      segment-sharing field to guarantee both channels hit the same list.

      Here, all three vanish: no dependsOn, no wait state, and the segment the
      warm step already built is read directly — there is only ever ONE segment,
      so same-list is guaranteed by construction rather than by a resolver.

    Failure is non-fatal by design: a pre-call text that does not go out must
    degrade to "no text", never to "no call".
    """
    bucket = plan["buckets"][bucket_index]
    bucket_state = run["bucketStates"][bucket_index]

    for ci, campaign in enumerate(bucket.get("campaigns", [])):
        precall = (campaign.get("campaignConfig") or {}).get("precallSms") or {}
        if not precall.get("enabled"):
            continue

        cs = bucket_state["campaignStates"][ci]
        if cs.get("precallSmsSentAt"):
            continue  # already sent — _prestart_plan retries and re-activation
        # Only text a cohort we are actually about to call. This mirrors the
        # activation loop's own predicate exactly (status == "warming" AND a real
        # connectCampaignId): the warm step has four failure paths
        # (_RedisRebuildingError, _EmptySegmentError, _CutoffTooCloseError,
        # generic) and a campaign left warming without a connectCampaignId is
        # skipped by that loop and never dialed.
        if (
            cs.get("status") != "warming"
            or not cs.get("connectCampaignId")
            or not cs.get("segmentArn")
        ):
            continue

        sms_campaign_id = _precall_sms_campaign_id(run, bucket_index, ci)
        try:
            _invoke_sms_sender(
                campaignId=sms_campaign_id,
                planId=run["planId"],
                runId=run["runId"],
                segmentArn=cs["segmentArn"],
                segmentName=cs["segmentName"],
                messageTemplate=precall.get("messageTemplate", ""),
                originationNumberArn=precall.get("originationNumberArn", ""),
                clinicName=precall.get("clinicName", ""),
            )
            cs["precallSmsSentAt"] = _now_iso()
            _slog.info(
                "precall_sms_fired",
                plan_id=run["planId"],
                run_id=run["runId"],
                bucket_index=bucket_index,
                campaign_index=ci,
                sms_campaign_id=sms_campaign_id,
                segment_name=cs["segmentName"],
            )
        except Exception as exc:
            # Non-fatal: never let a failed pre-call text stop the dial.
            _slog.error(
                "precall_sms_failed",
                plan_id=run["planId"],
                run_id=run["runId"],
                bucket_index=bucket_index,
                campaign_index=ci,
                error_type=type(exc).__name__,
            )
```

`_invoke_sms_sender` needs **no change**: it is `def _invoke_sms_sender(**kwargs)` (`executor.py:367`) and JSON-dumps the whole kwargs dict straight into the Lambda payload, so new keys pass through untouched. The work is on the **receiving** side — `sms_sender_handler.lambda_handler` must read `clinicName` off its `event` (Task 3 Step 6) and extend the docstring's documented event shape (`sms_sender_handler.py:42-52`). If that key is not read, the renderer silently produces an empty string and patients receive `This is .`, so Task 5's `test_precall_requires_clinic_name_if_template_uses_it` is the guard against shipping that.

- [ ] **Step 4: Wire the two call sites**

In `_activate_warming_bucket` (`executor.py:1978`), insert the call between the `_schedule_tick` try/except that ends at line `2024` and the `# Start all warming campaigns` comment at line `2026`. That position is deliberate on both sides:

- **After `_schedule_tick`** because that block `raise`s on failure (line `2024`) and aborts activation. Texting a cohort whose bucket then never activates would be a false promise.
- **Before the start loop** (`2028`) because that is what makes the ordering structural.
- The recovery block at `1985-1994` has already run by then, resetting failed-to-warm campaigns from `error` to `queued`, so they are naturally excluded by the status predicate.

In `start_run` (`executor.py:765`), call it after the `pendingWarmup` consumption at line 806 populates the campaign states, and before campaigns are started. Read the 2-hour `pendingWarmup` staleness guard just below 806 — if warmup was discarded as stale, the segment came from a cold `_create_segment` instead, which is still correct here (one segment, still shared), but the assumption should be verified rather than assumed while editing.

- [ ] **Step 5: Add the frontend type**

In `frontend/src/lib/api.ts`, add it to **`BucketCampaignConfig` (309-327)**, immediately after `phiAcknowledged` (326) and near `smsMessageTemplate` (324). **Not** to `CampaignDef` (439-465): `campaignConfig` is declared there as `campaignConfig?: BucketCampaignConfig` (450), so the config block is the right home and `CampaignDef` needs no new field at all. Getting this wrong puts the type one level up from where every reader — `cfg` at `PlanNew.tsx:538`, `updateCfg` at 622, the executor's `campaign.get("campaignConfig")` — actually looks.

```typescript
  /**
   * Pre-call SMS: texted to this campaign's own segment at bucket activation,
   * immediately before the first dial. Ordering is guaranteed by the executor's
   * bucket lifecycle, not by a timer — do NOT model this with dependsOn, which
   * would disable the voice campaign's pre-warming.
   * messageTemplate may use only {{FirstName}} and {{ClinicName}}.
   */
  precallSms?: {
    enabled: boolean;
    messageTemplate: string;
    originationNumberArn: string;
    clinicName: string;
  };
```

This type is what Task 6's authoring UI binds to, so get the field names right here — the UI, the validator (Task 5) and the executor (`_fire_precall_sms` above) all read the same four keys, and there is no runtime schema to catch a typo.

- [ ] **Step 6: Run tests, lint, commit, deploy (ask for explicit confirmation)**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-plans && python -m pytest tests/unit/ -v
cd /home/devaju/projects/vip-connect-external-campaigns
ruff check services/api-plans/src/executor.py services/api-plans/tests/unit/test_executor_v2.py
git add services/api-plans frontend/src/lib/api.ts
git commit -m "feat(plans): fire pre-call SMS at bucket activation, strictly before the first dial"
cd infra && cdk synth VipAdminApiPlansStack > /dev/null && cdk deploy VipAdminApiPlansStack
```

---

### Task 5: Pre-call copy and plan validation

**Depends on Tasks 3 and 4.**

**Files:** `services/api-plans/src/handlers/plans.py`, `services/api-plans/tests/unit/test_plan_validation_sms.py`, `docs/runbook.md`

**Interfaces:**
- Consumes: the allowlist (Task 3), the `precallSms` config shape (Task 4).
- Produces: validation errors surfaced by the plan save API (and by Task 6's UI), and the runbook recipe Task 7 follows.

**No new template store.** `_validate_sms_campaign` already establishes the convention: message content is an inline string on `campaignConfig`, length-checked and PHI-screened. `precallSms.messageTemplate` follows it exactly. Each specialty is one voice campaign carrying its own copy, so a separate template table would duplicate the PHI guard and add a store nothing else needs.

#### The approved copy

Sebastian supplied the following as **business-approved, verbatim** on 2026-09-10. It is reproduced exactly, with only `[Name]` / `[Clinic Name]` rewritten as the two allowlisted placeholders:

| Specialty | Status | `precallSms.messageTemplate` |
|---|---|---|
| Vein | **APPROVED (final)** | `Hi {{FirstName}}! This is {{ClinicName}}. We're about to give you a quick call regarding your vein consultation request. Look out for our call!` |
| Pain Management | **APPROVED (final)** | `Hi {{FirstName}}! {{ClinicName}} here. We're calling you in just a moment to discuss your pain management request. Talk soon!` |

**Phase I ships these two specialties and no others.** Sebastian, 2026-09-10: "por el momento solo vein y pein" — for now, only Vein and Pain. **Fibroid and General are out of scope for this plan** (see Explicitly Out of Scope), not merely "awaiting copy": no config rows, no tests, no verification steps for them. Adding a third or fourth specialty later is additive — one new voice campaign carrying its own `precallSms` block — and requires only real business-approved copy, no code change.

The Vein string above is **final and differs from Sebastian's first draft**. His original closing sentence was `Look out for a call from this number!`, which rendered 168 chars and failed the 160 ceiling. He chose the shortened closing `Look out for our call!` on 2026-09-10 (OQ-9, option 3). Use the string in the table verbatim; do not restore the longer closing.

Three things follow from this copy that change earlier drafts of this plan. Read all three before implementing.

**(1) These are distinct templates per specialty, not one template plus a `{{Specialty}}` substitution.** The specialty is baked into the human-written sentence ("your vein consultation request", "your pain management request"), and the two templates differ in structure, not just in one noun — Vein opens `This is {{ClinicName}}.`, Pain opens `{{ClinicName}} here.`. **Neither approved template uses `{{Specialty}}` at all.**

Consequence, flagged so it can be reversed rather than discovered: `{{Specialty}}` becomes dead surface, so this plan **narrows the allowlist to `{{FirstName}}` + `{{ClinicName}}`** and drops `specialty` from the `precallSms` config. Concretely that means, relative to the code written in Tasks 3 and 4:

- Task 3: `CAMPAIGN_FIELDS = {"ClinicName"}` (not `{"ClinicName", "Specialty"}`); drop the `"Specialty"` entry from `render()`'s `values` dict; drop `Specialty` from the two placeholder tests.
- Task 4: drop the `specialty=precall.get("specialty", "")` kwarg from `_fire_precall_sms`, and the `"specialty": event.get("specialty", "")` read in the sender's campaign dict.
- Task 5: delete `test_precall_requires_specialty_if_template_uses_it` outright.
- The `precallSms` TypeScript/config shape loses its `specialty: string` field.

If Sebastian wants `{{Specialty}}` retained as an option for future copy, re-adding it is purely additive at exactly those five sites — one set member, one dict entry, one kwarg, one event read, one config field. Nothing else in the design depends on it. **Raise this before implementing Task 3, because Task 3 is where the set is defined.**

**(2) Both final templates fit the repo's 160-character rule, with headroom.** Measured against the exact strings in the table above, not estimated:

| Template | Raw template | Rendered, 20-char name | Rendered, 5-char name ("Maria") | Longest name that still fits |
|---|---|---|---|---|
| Vein | 143 | **153 — fits** | 138 | 27 |
| Pain | 125 | **135 — fits** | 120 | 45 |

`clinicName = "VIP Medical Group"` (17 chars) in all rows. The renderer truncates `FirstName` at `_NAME_MAX_LEN = 20`, so the 20-char column is the true worst case and is what `max_rendered_length()` measures. Both templates clear 160 there, so `_validate_precall_sms` accepts both as written. Both are pure ASCII with straight apostrophes, so both stay GSM-7 — a curly `’` (U+2019) would force UCS-2 and a 70-char segment, so **do not let an editor smart-quote these strings.**

The earlier `test_every_specialty_variant_renders_within_160_chars` has been **deleted**: it was written against a shorter draft template and would have passed for the wrong reason. Its replacements below assert the two real strings by name and pin the measured worst case, so a copy edit that pushes either template over the ceiling fails a test instead of failing a patient.

**(3) Neither approved template contains an opt-out instruction, and none is being added.** Earlier drafts of this plan appended `Reply STOP to opt out.` — that wording was this plan's invention, not Sebastian's. It was surfaced as an explicit recommendation and **Sebastian declined it on 2026-09-10: "no lo agreguemos"** (let's not add it). Both templates ship exactly as in the table. Do not append opt-out language, and do not reopen this as an implementation detail — it is a business decision that was asked and answered (OQ-10, resolved-no).

Two consequences to be aware of rather than act on: `VipConnectOptOutList` still only ever populates from *inbound* `STOP` messages, so the gate remains correct and deployed but nothing in this campaign's own copy tells a recipient how to trigger it. That does **not** block verification — Task 7 Step 9 texts `STOP` from the test handset directly, which works regardless of whether the outbound message advertised it. And the origination number is registered `TRANSACTIONAL` (see the config example below), which is the correct type for this use case but is *not* an exemption from opt-out handling.

#### Origination number

Use the number Sebastian designated on 2026-09-10:

```
+16106009752
arn:aws:sms-voice:us-east-1:165505826690:phone-number/phone-ba711707215947e3a0e5112c0872014b
```

Verified: `ACTIVE`, `TEN_DLC`, `SMS` capability, no `TwoWayChannelArn`, and unreferenced across all four candidate repos — nothing else is using it. Two properties to be aware of, neither a blocker:

- `MessageType` is `TRANSACTIONAL`, which fits "we are calling you in a moment" and gets better carrier treatment than `PROMOTIONAL`. It does not change the opt-out obligation — the deployed `VipConnectOptOutList` gate still applies to every send.
- It shares a `RegistrationId` with `+19378702788`, which is **CloudHesive-owned**. Sharing a 10DLC campaign registration means throughput and carrier reputation are shared. Record it; do not attempt to change the registration — that would touch a CloudHesive-owned resource, which this plan forbids.

The other two ACTIVE toll-free numbers in the account, `+18443527135` and `+18444152389`, are **already claimed by the separate `rcm-sms-inbox` application** and must not be reused here.

Example valid config, used verbatim by Task 7:

```json
{
  "precallSms": {
    "enabled": true,
    "messageTemplate": "Hi {{FirstName}}! {{ClinicName}} here. We're calling you in just a moment to discuss your pain management request. Talk soon!",
    "clinicName": "VIP Medical Group",
    "originationNumberArn": "arn:aws:sms-voice:us-east-1:165505826690:phone-number/phone-ba711707215947e3a0e5112c0872014b"
  }
}
```

Either approved template is valid here; Pain is shown because it is the shorter of the two. Task 7's own E2E campaign uses this exact block.

- [ ] **Step 1: Write the failing tests**

```python
def test_precall_requires_template_when_enabled():
    plan = _plan_with_precall(precall={"enabled": True})
    assert any("messageTemplate" in e for e in validate_plan(plan))


def test_precall_requires_origination_number_when_enabled():
    plan = _plan_with_precall(precall={"enabled": True, "messageTemplate": "Hi!"})
    assert any("originationNumberArn" in e for e in validate_plan(plan))


def test_precall_requires_clinic_name_if_template_uses_it():
    """An unset config value renders as an empty string — 'This is .' shipped
    to a patient. Require the value whenever the placeholder is present."""
    plan = _plan_with_precall(
        precall={
            "enabled": True,
            "messageTemplate": "Hi {{FirstName}}! This is {{ClinicName}}.",
            "originationNumberArn": "arn:x",
            "clinicName": "",
        }
    )
    assert any("clinicName" in e for e in validate_plan(plan))


def test_precall_template_goes_through_the_same_phi_guard():
    plan = _plan_with_precall(
        precall={
            "enabled": True,
            "messageTemplate": "Hi {{FirstName}}, your {{Diagnosis}} is ready.",
            "originationNumberArn": "arn:x",
        }
    )
    assert any("Diagnosis" in e for e in validate_plan(plan))


def test_precall_is_rejected_on_a_non_voice_campaign():
    """precallSms on an SMS campaign is nonsense — there is no dial to precede."""
    plan = _plan_with_precall(delivery_type="sms")
    assert any("precallSms" in e for e in validate_plan(plan))


def test_precall_is_rejected_when_the_campaign_has_dependsOn():
    """A campaign with dependsOn is never pre-warmed (executor.py:2180, 2571-2573),
    so it has no segmentArn at activation and the pre-call SMS would silently
    never fire. Reject at save time instead of failing quietly at run time."""
    plan = _plan_with_precall(depends_on=["other"])
    assert any("dependsOn" in e for e in validate_plan(plan))


# The literal approved strings, not a paraphrase. If these ever diverge from the
# business document the test is worthless, so keep them verbatim and dated.
_APPROVED_COPY_2026_09_10 = {
    "Vein": (
        "Hi {{FirstName}}! This is {{ClinicName}}. We're about to give you a "
        "quick call regarding your vein consultation request. "
        "Look out for our call!"
    ),
    "Pain": (
        "Hi {{FirstName}}! {{ClinicName}} here. We're calling you in just a "
        "moment to discuss your pain management request. Talk soon!"
    ),
}


def test_approved_pain_copy_renders_within_the_length_ceiling():
    """Pain fits at the 20-char worst case: 135 rendered."""
    rendered = max_rendered_length(
        _APPROVED_COPY_2026_09_10["Pain"],
        campaign={"clinicName": "VIP Medical Group"},
    )
    assert rendered == 135
    assert rendered <= _MAX_SMS_CHARS


def test_approved_vein_copy_renders_within_the_length_ceiling():
    """Vein fits at the 20-char worst case: 153 rendered.

    This is the SHORTENED closing Sebastian approved on 2026-09-10 (OQ-9,
    option 3): "Look out for our call!". His first draft ended "Look out for a
    call from this number!", which rendered 168 and did not fit. Asserting the
    exact number, not just <= the ceiling, so restoring the longer closing fails
    here instead of failing silently at the API boundary.
    """
    rendered = max_rendered_length(
        _APPROVED_COPY_2026_09_10["Vein"],
        campaign={"clinicName": "VIP Medical Group"},
    )
    assert rendered == 153
    assert rendered <= _MAX_SMS_CHARS


def test_both_approved_templates_pass_the_real_validator():
    """The measurements above are worthless if the validator disagrees."""
    for specialty, tmpl in _APPROVED_COPY_2026_09_10.items():
        plan = _plan_with_precall(
            precall={
                "enabled": True,
                "messageTemplate": tmpl,
                "clinicName": "VIP Medical Group",
                "originationNumberArn": "arn:x",
            }
        )
        assert validate_plan(plan) == [], specialty


def test_approved_copy_is_pure_gsm7():
    """A curly apostrophe (U+2019) instead of ASCII ' silently forces UCS-2
    encoding, which cuts the per-segment budget from 160 to 70 — the message
    would split into three segments and this plan's whole length analysis would
    be wrong. Copy pasted out of a Word/Google doc is the usual source.
    """
    for specialty, tmpl in _APPROVED_COPY_2026_09_10.items():
        assert "’" not in tmpl, specialty
        assert "‘" not in tmpl, specialty
        assert "“" not in tmpl and "”" not in tmpl, specialty
        assert "—" not in tmpl and "–" not in tmpl, specialty
        assert "…" not in tmpl, specialty


def test_specialty_placeholder_is_not_allowlisted():
    """No approved template uses {{Specialty}}, so it is not interpolatable and a
    template using it must be rejected like any other unknown field."""
    plan = _plan_with_precall(
        precall={
            "enabled": True,
            "messageTemplate": "Hi {{FirstName}}, about your {{Specialty}} visit.",
            "originationNumberArn": "arn:x",
            "clinicName": "VIP Medical Group",
        }
    )
    assert any("Specialty" in e for e in validate_plan(plan))


def test_valid_precall_campaign_has_no_errors():
    assert validate_plan(_plan_with_precall()) == []
```

`_MAX_SMS_CHARS` and `max_rendered_length`'s `name_budget` keyword do not exist yet. Introduce both in Step 3: replace the bare `160` literal at `services/api-plans/src/handlers/plans.py:514` with a module constant (a magic number governing legal copy should be named), and give `max_rendered_length` in the shared renderer a `name_budget: int = _NAME_MAX_LEN` keyword so no existing caller changes. Both are needed by whichever OQ-9 resolution is chosen, and the keyword is the only way to test the 12-char claim without mutating a module global.

- [ ] **Step 2: Run to verify they fail**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-plans && python -m pytest tests/unit/test_plan_validation_sms.py -v
```

- [ ] **Step 3: Implement `_validate_precall_sms`**

Add beside `_validate_sms_campaign` and call it from the same place, for every campaign. Reuse the existing template screening rather than duplicating the patterns — factor the template checks out of `_validate_sms_campaign` into a helper both call, so the pre-call copy and the bulk-SMS copy can never drift apart in what they allow. The `dependsOn` rejection is the important one: it converts a silent no-op into a save-time error.

- [ ] **Step 4: Run tests and lint**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-plans && python -m pytest tests/unit/ -v
cd /home/devaju/projects/vip-connect-external-campaigns
ruff check services/api-plans/src/handlers/plans.py services/api-plans/tests/unit/test_plan_validation_sms.py
```

- [ ] **Step 5: Write the operator recipe into `docs/runbook.md`**

Add a "Pre-Call SMS (Phase I)" section. Task 6 adds the authoring UI, so this section documents the semantics the UI cannot express, not the click path:

- The pre-call SMS is configured **on the voice campaign itself**, under `campaignConfig.precallSms` (the "Pre-Call SMS" panel on the campaign card in the plan editor). There is no separate SMS campaign to create and no bucket to add.
- Required: `enabled: true`, `messageTemplate` (only `{{FirstName}}` and `{{ClinicName}}`), `originationNumberArn`, `clinicName`.
- The campaign must **not** have `dependsOn` — that disables pre-warming, so no segment exists at activation and the SMS would never fire. Validation rejects the combination, and the UI disables the toggle while dependencies are checked.
- Ordering is automatic: the SMS is enqueued at bucket activation, and Connect's own campaign `startTime` is warm-time + 6 minutes, so the first dial follows roughly 1–6 minutes later. There is no lead-time setting to tune.
- Specialty copy: **one voice campaign per specialty, each with its own hand-written `messageTemplate`.** The specialty is part of the sentence, not a substituted value — copy the approved string for that specialty verbatim from this plan's Task 5 table. Only Vein and Pain Management have approved copy as of 2026-09-10.
- Quiet hours are enforced per recipient, not per campaign: a lead whose area code puts them outside 08:00–21:00 local, or in any timezone where it is Sunday, is skipped and counted in `totalSkippedQuietHours` on the run record. A pre-call SMS run with fewer sends than the segment size is normal, not a failure.

- [ ] **Step 6: Commit and deploy (ask for explicit confirmation)**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns
git add services/api-plans docs/runbook.md
git commit -m "feat(plans): validate pre-call SMS config and document the operator recipe"
cd infra && cdk synth VipAdminApiPlansStack > /dev/null && cdk deploy VipAdminApiPlansStack
```

---

### Task 6: Pre-call SMS authoring UI in the existing plan editor

**Depends on Tasks 4 and 5** (the config shape and the validation rules the UI must not contradict).

**Files:** `frontend/src/pages/PlanNew.tsx`, `frontend/src/lib/precallSms.ts` (new), `frontend/src/lib/precallSms.test.ts` (new), `frontend/src/lib/api.ts`

**Interfaces:**
- Consumes: `campaignConfig.precallSms` (Task 4 Step 5's type), `api.sms.listNumbers()`, and Task 5's validation errors.
- Produces: a "Pre-Call SMS" panel on the campaign card. Task 7 configures its test campaign **through this UI**, not by direct API calls.

**Where this goes, verified.** `frontend/src/pages/PlanNew.tsx` is the **only** plan/campaign editor in the repo. `PlanDetail.tsx` and `CampaignDetail.tsx` are read-only run views (`CampaignDetail.tsx:156` literally renders `<pre>{JSON.stringify(campaign, null, 2)}</pre>`), and `CampaignNew.tsx` is an unrelated standalone Connect Campaigns V2 form with no Plans/Buckets concept. The component chain inside `PlanNew.tsx`: `PlanNew()` 1299-1682 → `BucketEditor()` 1076-1269 → `DagCanvas()` 1011-1072 → **`CampaignCard()` 513-1007**, which is the per-campaign form body. The new panel goes in `CampaignCard`.

**The pattern to copy is already there.** The existing bulk-SMS block at `PlanNew.tsx:853-901` (rendered only when `campaign.deliveryType === 'sms'`) is the exact precedent:

- Origination-number dropdown at **858-870**: a `<select>` over `smsNumbers.map(n => <option value={n.arn}>{n.phoneNumber} ({n.numberType})</option>)`, fed by an inline `useQuery({ queryKey: ['sms','numbers'], queryFn: () => api.sms.listNumbers(), staleTime: 5*60_000 })` at **1368-1373**. `api.sms.listNumbers()` is `api.ts:919-921`; the backend route is `GET /sms/numbers` → `services/api-plans/src/router.py:43` → `handlers/sms.py:14-36` (`list_origination_numbers`, paginating `describe_phone_numbers` filtered to `status=ACTIVE`, returning `arn`/`phoneNumber`/`numberType`/`countryCode`/`twoWayEnabled`/`optOutListName`/`status` per number). Reuse the **same** `useQuery` result — do not add a second query for the same key.
- Template textarea at **872-887**: a plain `<textarea>` with `maxLength={160}`, local Tailwind classes, plus a live counter at **875** (`({(cfg.smsMessageTemplate ?? '').length}/160)`) and a PHI warning at **884-886**.

Follow that convention, not `frontend/src/components/ui.tsx`. Those primitives (`Input`, `Select`, `Textarea`, `Field`) exist and are used elsewhere (`EnableCampaignModal.tsx`, `SegmentNew.tsx`, …) but `PlanNew.tsx` **never imports them** — its only `ui` import is `Spinner` (line 5), and every control writes its Tailwind classes as an inline literal (there is no shared `inputCls` constant in this file; copy the class string off the sibling `<select>`/`<textarea>` verbatim). Introducing `ui.tsx` primitives into this one card would make the file internally inconsistent; matching the neighbouring block is the smaller change. There is no `react-hook-form`, `zod`, or `Formik` anywhere in `frontend/package.json` — forms are `useState` + manual `onChange` + a hand-written validator (`handleSave()` at 1401-1430).

**Do not confuse two endpoints.** `api.campaigns.phoneNumbers()` (used by `CampaignNew.tsx:111`) returns **Connect caller-ID** numbers and is a different thing. The SMS origination list is `api.sms.listNumbers()`.

**One thing must NOT be copied from the neighbouring block: `maxLength={160}` on the raw template.** For `precallSms` the ceiling applies to the **rendered** string, and the approved Vein copy is 158 raw / 168 rendered. A raw `maxLength={160}` would let an operator type copy the backend then rejects, with the counter reading a reassuring `158/160`. The counter and any length cap must both measure the rendered worst case (`{{FirstName}}` → 20 chars, `{{ClinicName}}` → the configured `clinicName`), which is precisely the arithmetic Task 3's `max_rendered_length` does server-side.

- [ ] **Step 1: Write the failing tests**

The repo's frontend tests are Vitest with `environment: 'node'` (`frontend/vitest.config.ts`) and **no `@testing-library/react`, no jsdom** — every existing `*.test.ts` next to a component imports plain exported functions and tests them directly (`EnableCampaignModal.test.ts` tests `suggestCampaignFlow`/`resolveCampaignFlowArn`; `frontend/src/lib/chainMap.test.ts` tests DAG logic over `dependsOn`). **Do not invent a rendering test** — it cannot run in this config.

So put the logic in a pure module and test that. Create `frontend/src/lib/precallSms.test.ts`:

```typescript
import { describe, it, expect } from 'vitest';
import {
  PRECALL_ALLOWED_PLACEHOLDERS,
  extractPlaceholders,
  renderedWorstCaseLength,
  validatePrecallSms,
  precallSmsAvailability,
} from './precallSms';

const CLINIC = 'VIP Medical Group';
const PAIN =
  "Hi {{FirstName}}! {{ClinicName}} here. We're calling you in just a moment " +
  'to discuss your pain management request. Talk soon!';
const VEIN =
  "Hi {{FirstName}}! This is {{ClinicName}}. We're about to give you a quick " +
  'call regarding your vein consultation request. Look out for a call from ' +
  'this number!';

describe('placeholder allowlist', () => {
  it('mirrors the backend allowlist exactly', () => {
    // Drift here means the UI accepts copy the API rejects, or vice versa.
    // Backend source of truth: vip_shared.domain.services.sms_template
    // (RECIPIENT_FIELDS | CAMPAIGN_FIELDS).
    expect([...PRECALL_ALLOWED_PLACEHOLDERS].sort()).toEqual([
      'ClinicName',
      'FirstName',
    ]);
  });

  it('rejects a non-allowlisted placeholder by name', () => {
    const errs = validatePrecallSms({
      enabled: true,
      messageTemplate: 'Hi {{FirstName}}, your {{Diagnosis}} is ready.',
      clinicName: CLINIC,
      originationNumberArn: 'arn:x',
    });
    expect(errs.some((e) => e.includes('Diagnosis'))).toBe(true);
  });

  it('rejects {{Specialty}} — not allowlisted, the copy bakes it into the text', () => {
    const errs = validatePrecallSms({
      enabled: true,
      messageTemplate: 'Hi {{FirstName}}, about your {{Specialty}} visit.',
      clinicName: CLINIC,
      originationNumberArn: 'arn:x',
    });
    expect(errs.some((e) => e.includes('Specialty'))).toBe(true);
  });

  it('is case sensitive, like the backend regex', () => {
    expect(extractPlaceholders('Hi {{firstName}}!')).toEqual(
      new Set(['firstName']),
    );
    const errs = validatePrecallSms({
      enabled: true,
      messageTemplate: 'Hi {{firstName}}!',
      clinicName: CLINIC,
      originationNumberArn: 'arn:x',
    });
    expect(errs.some((e) => e.includes('firstName'))).toBe(true);
  });
});

describe('rendered length, not raw length', () => {
  it('measures the 20-char worst-case name, matching max_rendered_length', () => {
    expect(PAIN.length).toBe(125);
    expect(renderedWorstCaseLength(PAIN, CLINIC)).toBe(135);
  });

  it('catches the template that is under 160 raw but over 160 rendered', () => {
    // This is why maxLength={160} on the raw textarea would be a bug: the
    // approved Vein copy is 158 raw and 168 rendered.
    expect(VEIN.length).toBeLessThanOrEqual(160);
    expect(renderedWorstCaseLength(VEIN, CLINIC)).toBe(168);
    const errs = validatePrecallSms({
      enabled: true,
      messageTemplate: VEIN,
      clinicName: CLINIC,
      originationNumberArn: 'arn:x',
    });
    expect(errs.some((e) => e.includes('160'))).toBe(true);
  });
});

describe('required fields', () => {
  it('reports nothing when disabled, however empty', () => {
    expect(validatePrecallSms({ enabled: false })).toEqual([]);
  });

  it.each(['messageTemplate', 'originationNumberArn', 'clinicName'])(
    'requires %s when enabled',
    (field) => {
      const cfg = {
        enabled: true,
        messageTemplate: PAIN,
        clinicName: CLINIC,
        originationNumberArn: 'arn:x',
      } as Record<string, unknown>;
      delete cfg[field];
      expect(validatePrecallSms(cfg).some((e) => e.includes(field))).toBe(true);
    },
  );

  it('accepts the approved Pain copy with every field set', () => {
    expect(
      validatePrecallSms({
        enabled: true,
        messageTemplate: PAIN,
        clinicName: CLINIC,
        originationNumberArn: 'arn:x',
      }),
    ).toEqual([]);
  });
});

describe('availability — the UI must not offer a config the API rejects', () => {
  it('is unavailable on an SMS-delivery campaign', () => {
    const a = precallSmsAvailability({ deliveryType: 'sms', dependsOn: [] });
    expect(a.available).toBe(false);
    expect(a.reason).toMatch(/sms/i);
  });

  it('is unavailable when the campaign has dependsOn', () => {
    // Mirrors Task 5's server-side rejection: a campaign with dependsOn is
    // never pre-warmed (executor.py:2180, 2571-2573), so it has no segmentArn
    // at activation and the SMS would silently never fire.
    const a = precallSmsAvailability({
      deliveryType: 'campaign',
      dependsOn: ['other'],
    });
    expect(a.available).toBe(false);
    expect(a.reason).toMatch(/depend/i);
  });

  it('is available on a plain voice campaign with no dependencies', () => {
    expect(
      precallSmsAvailability({ deliveryType: 'campaign', dependsOn: [] })
        .available,
    ).toBe(true);
  });

  it('is available on a journey campaign too', () => {
    expect(
      precallSmsAvailability({ deliveryType: 'journey', dependsOn: [] })
        .available,
    ).toBe(true);
  });
});
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/frontend && npx vitest run src/lib/precallSms.test.ts
```
Expected: `Failed to resolve import "./precallSms"`.

- [ ] **Step 3: Implement `frontend/src/lib/precallSms.ts`**

A pure module — no React, no network — so it is testable under `environment: 'node'` and reusable by `handleSave()`.

```typescript
/**
 * Client-side mirror of the pre-call SMS rules enforced by
 * services/api-plans/src/handlers/plans.py (_validate_precall_sms).
 *
 * SCOPE, deliberately narrow: this duplicates only the cheap, stable rules —
 * required fields, the placeholder allowlist, the rendered-length ceiling, and
 * the dependsOn/deliveryType conflicts. It does NOT reimplement the ten PHI
 * regexes in _PHI_PATTERNS. Duplicating those in TypeScript would create two
 * sources of truth for a compliance rule and they would drift. The server stays
 * authoritative; the UI's job is to catch the common mistakes early and to
 * surface whatever the server says verbatim when it says no.
 */

export const PRECALL_ALLOWED_PLACEHOLDERS = new Set(['FirstName', 'ClinicName']);

/** Matches vip_shared…sms_template._PLACEHOLDER_RE. Case sensitive on purpose. */
const PLACEHOLDER_RE = /\{\{\s*(\w+)\s*\}\}/g;

/** Matches _NAME_MAX_LEN in the Python renderer, which truncates there. */
const NAME_BUDGET = 20;

/** Matches _MAX_SMS_CHARS in handlers/plans.py. One GSM-7 segment. */
export const MAX_SMS_CHARS = 160;

export function extractPlaceholders(template: string): Set<string> {
  return new Set(
    [...(template ?? '').matchAll(PLACEHOLDER_RE)].map((m) => m[1]),
  );
}

/** Worst-case rendered length: the longest name the server would ever render. */
export function renderedWorstCaseLength(
  template: string,
  clinicName: string,
): number {
  return (template ?? '')
    .replace(/\{\{\s*FirstName\s*\}\}/g, 'A'.repeat(NAME_BUDGET))
    .replace(/\{\{\s*ClinicName\s*\}\}/g, clinicName ?? '').length;
}

export function precallSmsAvailability(campaign: {
  deliveryType?: string;
  dependsOn?: string[];
}): { available: boolean; reason?: string } {
  if (campaign.deliveryType === 'sms') {
    return {
      available: false,
      reason:
        'Pre-call SMS applies to voice campaigns — an SMS campaign has no dial to precede.',
    };
  }
  if ((campaign.dependsOn ?? []).length > 0) {
    return {
      available: false,
      reason:
        'Pre-call SMS is unavailable while this campaign waits on another: a dependent campaign is not pre-warmed, so it has no lead segment when the bucket activates and the text would never be sent. Remove the dependency to enable it.',
    };
  }
  return { available: true };
}

export function validatePrecallSms(
  cfg: Record<string, unknown> | undefined,
): string[] {
  if (!cfg?.enabled) return [];
  const errors: string[] = [];
  const template = String(cfg.messageTemplate ?? '');
  const clinicName = String(cfg.clinicName ?? '');

  if (!template.trim()) errors.push('Pre-call SMS: messageTemplate is required');
  if (!String(cfg.originationNumberArn ?? '').trim())
    errors.push('Pre-call SMS: originationNumberArn is required');
  if (!clinicName.trim())
    errors.push('Pre-call SMS: clinicName is required (it is interpolated into the message)');

  const unknown = [...extractPlaceholders(template)].filter(
    (f) => !PRECALL_ALLOWED_PLACEHOLDERS.has(f),
  );
  if (unknown.length)
    errors.push(
      `Pre-call SMS: placeholder(s) not allowed: ${unknown.sort().join(', ')}. ` +
        `Only {{FirstName}} and {{ClinicName}} may be used.`,
    );

  const rendered = renderedWorstCaseLength(template, clinicName);
  if (rendered > MAX_SMS_CHARS)
    errors.push(
      `Pre-call SMS: renders to ${rendered} characters with a long first name, over the ${MAX_SMS_CHARS} limit`,
    );

  return errors;
}
```

- [ ] **Step 4: Add the panel to `CampaignCard`**

Insert immediately after the existing SMS block (`PlanNew.tsx:853-901`) so the two message-authoring areas sit together. The panel is **always rendered**; `precallSmsAvailability(campaign)` decides whether its controls are enabled. When it is unavailable, show the toggle disabled with `reason` as helper text and the fields hidden — an operator who cannot use the feature should learn *why* rather than find a blank space where a colleague's screenshot had a panel. Hiding it entirely is the one thing not to do.

**Write through the existing `updateCfg` helper (`PlanNew.tsx:622`), and watch the nesting.** It is `(patch: Partial<BucketCampaignConfig>) => onChange({ ...campaign, campaignConfig: { ...cfg, ...patch } })` — a **shallow** spread. Every neighbouring call passes a flat scalar (`updateCfg({ smsMessageTemplate: ... })`), but `precallSms` is an object, so `updateCfg({ precallSms: { enabled: true } })` would **discard the other three fields**. Always merge explicitly:

```typescript
const patchPrecall = (patch: Partial<NonNullable<BucketCampaignConfig['precallSms']>>) =>
  updateCfg({ precallSms: { ...(cfg.precallSms ?? EMPTY_PRECALL), ...patch } });
```

Do **not** add `precallSms` to `DEFAULT_CAMPAIGN_CONFIG` (`PlanNew.tsx:38`). `cfg` falls back to that default at 538, so seeding it there would make every campaign in every plan — including the hundreds that will never use this — save an empty `precallSms` block. Leaving it `undefined` and reading `cfg.precallSms?.enabled` is both smaller and what the optional type says.

Contents, mirroring the neighbouring block's markup and copying its inline class strings:

1. **Toggle** — checkbox bound to `cfg.precallSms?.enabled`. Disabled (not hidden) when unavailable.
2. **Template textarea** — same classes as line 877-883, but **no `maxLength`**: see the note above. Counter shows the rendered worst case, `({renderedWorstCaseLength(tmpl, clinicName)}/160)`, turning red past 160. Put the allowlist inline beneath it, verbatim: `Placeholders: {{FirstName}} (the patient's first name) and {{ClinicName}}. Nothing else is permitted.` Keep the existing PHI warning text from 884-886 word for word — it is already the right warning.
3. **`clinicName`** — text input. Label it as interpolated so the operator understands it appears in the message body.
4. **Origination number** — `<select>` reusing the **same** `smsNumbers` query result as line 858-870 (`useQuery(['sms','numbers'])` at 1368-1373). Show `{phoneNumber} ({numberType})` per option, exactly as the existing dropdown does. Default the empty state to the number recorded in Task 5 (`+16106009752`) only as helper text — do **not** hardcode the ARN in the frontend; it must come from the endpoint so a number change does not need a frontend deploy.
5. **Approved-copy hint** — a short line naming the two specialties with approved copy and pointing at the runbook. Operators must not compose their own copy.

No separate PHI-acknowledgment checkbox. The bulk-SMS block has one (`phiAcknowledged`, enforced at `handleSave()` line 1418) because an operator composes that copy freely; pre-call copy is transcribed from an approved list and PHI-screened server-side, so a second checkbox would be ceremony. If Sebastian wants parity, it is a one-line addition — say so rather than adding it unasked.

**Wire `validatePrecallSms` into `handleSave()` (1401-1430) following that function's own convention**, which is *first error wins, then scroll*: `setErrorAndScroll(msg)` (defined at 1392) sets one string and the caller `return`s immediately. Match the existing per-campaign message format exactly so the operator can find the campaign:

```typescript
if (cfg?.precallSms?.enabled) {
  const [first] = validatePrecallSms(cfg.precallSms);
  if (first) {
    setErrorAndScroll(`"${c.name || `Campaign ${ci + 1}`}" in bucket ${bi + 1}: ${first}`);
    return;
  }
}
```

Put it inside the existing `for` loops next to the `deliveryType === 'sms'` block (1414-1419), not after them.

Also clear `campaignConfig.precallSms` when the operator adds a `dependsOn` — the dependency checkbox list is at 827-851 with `toggleDep()` at 625-630, and the same cleanup discipline already exists for campaign/bucket removal at 1106-1115 and 1434-1485. Leaving a stale enabled `precallSms` on a now-dependent campaign would make the save fail with a server error that reads like a bug.

**Do not touch** `assignStages()` (125-155), `availableDeps` (545-557), or any existing validation. Run the existing `frontend/src/lib/chainMap.test.ts` to prove the DAG logic is untouched.

- [ ] **Step 5: Surface the server's errors — this needs a real fix, not just a banner**

The banner already exists: `PlanNew.tsx:1528-1534` renders `saveError ?? String((saveMutation.error as Error)?.message ?? 'Save failed')` in a red box with a `errorBannerRef` scroll target. Reuse it; add no toast library and no second pattern.

**But it currently shows nothing useful, and that is a verified pre-existing defect this task has to fix.** The plans API returns validation failures as:

```json
{"error": {"code": "VALIDATION_ERROR", "messages": ["...", "..."]}}
```

— key `messages`, **plural, a list** (`services/api-plans/src/handlers/plans.py:170`, `217`, `342`; note the local is named `branded_errors` at all three sites even where the payload is not branded-specific, so don't let the name mislead you into thinking Task 5's errors take a different route — they land in the same list). The API client's `request()` in `frontend/src/lib/api.ts` reads only `err.message`, **singular** — the ternary at `76-79`, inside the `if (!res.ok)` block at `72-86` — and falls back to `` `HTTP ${res.status}` `` when it is absent. `err` there is already the unwrapped `payload.error` object (`73-75`), so `err.messages` is the correct access path. Nothing under `frontend/src/` reads `messages` (grepped: zero hits). So **every plan validation error from this API surfaces in the UI today as the bare string "HTTP 400"**, with all specifics discarded. That is true right now, before this plan, for the branded and bulk-SMS validators too.

Task 5 adds a validator whose most confusing failure — "this campaign has `dependsOn`, so pre-call SMS can never fire" — is precisely the one nobody will guess from "HTTP 400". So fix it in `request()`, where it belongs, rather than special-casing plans:

```typescript
const messages = Array.isArray((err as { messages?: unknown }).messages)
  ? ((err as { messages: unknown[] }).messages.filter((m) => typeof m === 'string') as string[])
  : [];
const message =
  typeof err.message === 'string' && err.message
    ? err.message
    : messages.length
      ? messages.join('\n')
      : `HTTP ${res.status}`;
```

Two notes. This is a **shared** code path: the change is additive (`message` still wins when present) but it now surfaces text for every endpoint that returns `messages`, so re-check the branded and bulk-SMS save paths still read sensibly. And the banner renders a single string in one `<span>` — a joined multi-line message needs `whitespace-pre-line` on that span, or the newlines collapse and several errors run together into one unreadable sentence.

Flag this as an **`incidental_fixes` entry** in the PR body: it is a pre-existing defect in a file this task touches anyway, not scope creep, and a reviewer should see it called out rather than buried in the diff.

- [ ] **Step 6: Run tests and lint**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/frontend
npx vitest run
npx tsc --noEmit
```

`npx tsc --noEmit` is the load-bearing check here — there is no render test that would catch a mis-typed field name, so the compiler is the only thing verifying the panel binds to the `precallSms` shape declared in `api.ts` (Task 4 Step 5). Capture a baseline before the change: this repo's `npm run build`/lint scripts are unreliable, so compare failure sets rather than expecting zero.

- [ ] **Step 7: Commit and deploy the frontend (ask for explicit confirmation)**

The frontend deploys separately from the Lambda stacks. Confirm the repo's frontend deploy path before running anything — do not assume a local `npm run build && aws s3 sync`.

```bash
cd /home/devaju/projects/vip-connect-external-campaigns
git add frontend/src/lib/precallSms.ts frontend/src/lib/precallSms.test.ts \
  frontend/src/pages/PlanNew.tsx frontend/src/lib/api.ts
git commit -m "feat(ui): author pre-call SMS on the campaign card in the plan editor"
```

---

### Task 7: End-to-end verification — the gate for all Phase II/III work

**Depends on Tasks 1-6 all being deployed.** Nothing in Phase II or Phase III may start until every checkbox below is ticked with **observed** evidence.

Use **test phone numbers the team controls** — at minimum one Eastern-area-code and one Pacific-area-code number — with synthetic Customer Profiles carrying a known `FirstName`. No real patient PHI in any verification step.

- [ ] **Step 1: Confirm neither test number is already suppressed**

Two independent suppression layers exist and either would produce a silent no-SMS that looks like a pipeline bug:

```bash
for n in '<eastern-E164>' '<pacific-E164>'; do
  aws dynamodb get-item --profile production --region us-east-1 \
    --table-name VipConnectOptOutList --key "{\"ContactNumber\":{\"S\":\"$n\"}}" --consistent-read
  aws dynamodb get-item --profile production --region us-east-1 \
    --table-name vip-connect-deny-list --key "{\"ContactNumber\":{\"S\":\"$n\"}}" --consistent-read
done
```

`--consistent-read` is not optional: an eventually-consistent read has previously returned stale pre-write state and produced a false conclusion. Expected: no `Item` from any of the four calls. If a number is present from opt-out testing, **pick a different number — do not delete the row.**

- [ ] **Step 2: Build the test plan per the Task 5 recipe — through the UI**

Author it in the **Task 6 authoring panel**, not with a direct `PUT /plans/{id}` call. The point of doing it through the UI is that it exercises the client-side validator, the origination-number dropdown, and the error banner on the same save that exercises the backend validator — a curl-only test would prove the API works and leave the UI unverified.

One voice campaign with `precallSms.enabled = true`, `clinicName: "VIP Medical Group"`, the origination number from the dropdown (`+16106009752`), and **no** `dependsOn`. Use the **Pain Management** approved copy, because as of 2026-09-10 the Vein copy renders to 168 characters and Task 5's validator legitimately rejects it (**OQ-9**) — reaching for Vein here would produce a save failure that looks like a bug and is not one. There is no `specialty` field; the specialty is inside the sentence.

While here, confirm two UI negatives that Task 6's unit tests assert but cannot observe in the real form:

1. Tick a `dependsOn` checkbox on the same campaign and confirm the pre-call panel goes unavailable with its reason shown, and that saving does not send an enabled `precallSms`.
2. Paste the Vein copy and confirm the rendered-length counter reads over 160 and turns red **before** you press save — the raw string is 158, so a counter measuring raw length would read `158/160` and mislead.

- [ ] **Step 3: Run it and prove the four Phase I guarantees**

**(a) The SMS arrived, personalized, before the call.** The Eastern handset receives, byte for byte:

```
Hi <KnownFirstName>! VIP Medical Group here. We're calling you in just a moment to discuss your pain management request. Talk soon!
```

with the real name, and **no literal `{{FirstName}}`, no `Hi !`, no empty clinic name, and no `Reply STOP` tail** — no approved template carries opt-out language (**OQ-10**). Check the apostrophe in `We're` renders as a straight `'`: a curly `’` would silently switch the message to UCS-2 and split it into two 70-character segments.

**(b) The SMS strictly preceded the first dial.** Compare the `precall_sms_fired` log timestamp against the campaign's first CTR `InitiationTimestamp`:

```bash
aws logs filter-log-events --profile production --region us-east-1 \
  --log-group-name /aws/lambda/vip-admin-ui-api-plans \
  --start-time $(( ($(date +%s) - 86400) * 1000 )) \
  --filter-pattern '"precall_sms_fired"' --query 'events[].message' --output text
```

The SMS timestamp must be earlier than the first dial. If they are inverted or equal, the ordering guarantee failed and **Phase I fails.**

**(c) One segment, shared.** Under this design there is structurally only one segment, so this is a regression check rather than a risk: confirm the `precall_sms_fired` log's `segment_name` equals the voice campaign's `segmentName` in the run record.

```bash
aws dynamodb get-item --profile production --region us-east-1 \
  --table-name VipConnectPlans \
  --key '{"pk":{"S":"PLAN#<planId>"},"sk":{"S":"RUN#<runId>"}}' --consistent-read \
  --query 'Item.bucketStates' --output json
```

**(d) Retry safety.** Confirm exactly one `VipSmsCampaignRuns` row exists for the pre-call send and `totalEnqueued` matches the cohort size — proof `precallSmsSentAt` suppressed any duplicate fire.

- [ ] **Step 4: Verify Pipeline A quiet-hours gating by differential outcome**

```bash
aws connectcampaignsv2 describe-campaign --profile production --region us-east-1 \
  --id <campaign-id> --query 'campaign.communicationTimeConfig' --output json
```

Expected: `localTimeZoneDetection == ["AREA_CODE"]`, `defaultTimeZone == "America/New_York"`, and a `telephony.openHours.dailyHours` map with **exactly six keys** — `MONDAY` through `SATURDAY`, each `[{"startTime": "T08:00", "endTime": "T21:00"}]` — and **no `SUNDAY` key at all**.

Two things to read off that output rather than skim past, both of which the earlier draft of this plan would have got wrong:

1. **The `T` prefix must still be there.** `Iso8601Time`'s pattern is `^T\d{2}:\d{2}$` (verified against botocore 1.43.90). If the echo shows bare `08:00`, something normalised it and the whole shape needs re-checking.
2. **`SUNDAY` must still be absent.** If `describe-campaign` returns a `SUNDAY` key you never sent — with any value, `[]` included — then omission is not how Connect encodes a closed day, Task 2's chosen encoding is wrong, and Step 6 below stops being a confirmation and becomes a **hard blocker**.

Then run a campaign containing both test numbers in the **13:00–15:00 UTC** band — 09:00 Eastern (allowed) but 06:00 Pacific (blocked, because 06:00 is before the 08:00 open) — and confirm from the CTR that Connect dialed the Eastern number and **not** the Pacific one. A stored config is not proof of enforcement; the differential outcome is.

That band is still correct under the 08:00–21:00 window: it discriminates on the **open** edge, which did not move when the close edge went from 20:00 to 21:00. Run it on a **Monday-through-Saturday** date, or the day axis will block both numbers and the test proves nothing about the hour axis.

- [ ] **Step 5: Verify Pipeline B quiet-hours gating by differential outcome**

In the same 13:00–15:00 UTC band, on the same Monday-through-Saturday date, with both numbers in the segment:

```bash
aws dynamodb get-item --profile production --region us-east-1 \
  --table-name VipSmsCampaignRuns \
  --key '{"planId":{"S":"<planId>"},"sk":{"S":"<runId>#<smsCampaignId>"}}' \
  --consistent-read \
  --query 'Item.[totalEnqueued,totalSkippedOptOut,totalSkippedQuietHours]'
```

Expected: `totalEnqueued == 1`, `totalSkippedQuietHours == 1`, and only the Eastern handset receives a message.

- [ ] **Step 6: Verify the day axis — nothing goes out on a Sunday**

**This step can only be run on a Sunday.** It is the one item in this gate that is calendar-bound, so schedule it deliberately instead of discovering at the end that Phase I cannot close for six days. It is also the only empirical test of the **unverified** `SUNDAY`-omission encoding from Task 2 — until this passes, "Connect treats an absent day key as closed" is an assumption this plan is betting on, not a fact it established.

Pick a Sunday inside 13:00–15:00 UTC (09:00–11:00 Eastern — comfortably inside the *hour* window, so only the day axis can refuse), and with the same two test numbers:

**(a) Pipeline B (our code) must skip both.**

```bash
aws dynamodb get-item --profile production --region us-east-1 \
  --table-name VipSmsCampaignRuns \
  --key '{"planId":{"S":"<planId>"},"sk":{"S":"<runId>#<smsCampaignId>"}}' \
  --consistent-read \
  --query 'Item.[totalEnqueued,totalSkippedOptOut,totalSkippedQuietHours]'
```

Expected: `totalEnqueued == 0`, `totalSkippedQuietHours == 2`, and **no handset receives anything**. A non-zero `totalEnqueued` means `QUIET_HOURS_DAYS` never reached the function or the day check reads UTC instead of recipient-local.

**(b) Pipeline A (Connect) must not dial.** Confirm from the CTR that no dial was placed for either number. If Connect dials anyway, the `SUNDAY`-omission encoding is wrong: switch Task 2 to `"SUNDAY": []`, redeploy, and repeat this step. If **that** also dials, then `openHours` cannot express a closed weekday at all, and the day axis has to move into our own code for the voice channel too — which is a design change, not a fix, and must go back to Sebastian.

Record which encoding was proven, verbatim, in Step 11's evidence block. This is the single most likely thing in Phase I to behave differently from the plan.

- [ ] **Step 7: Confirm no false watchdog alarm**

The old `dependsOn` design would have fired `NoActiveCampaign` 5 minutes into every run. This design should produce none — verify that empirically rather than trusting the reasoning:

```bash
aws logs filter-log-events --profile production --region us-east-1 \
  --log-group-name /aws/lambda/vip-admin-ui-api-plans \
  --start-time $(( ($(date +%s) - 86400) * 1000 )) \
  --filter-pattern '"no active campaign"' --query 'events[].message' --output text
```

Expected: no hits for this plan. Also confirm the voice campaign **was** pre-warmed (a `prestart_next_bucket_campaign_ok` or `prestart_plan_campaign_ok` log line for it) — that is the proof the pre-call feature did not cost the campaign its warmup.

- [ ] **Step 8: Regression — the COT staffing gate still works**

Confirm a plan whose `workingHours` window has closed still declines to start, with its COT reasoning intact:

```bash
aws logs filter-log-events --profile production --region us-east-1 \
  --log-group-name /aws/lambda/vip-admin-ui-api-plans \
  --start-time $(( ($(date +%s) - 86400) * 1000 )) \
  --filter-pattern 'working hours' --query 'events[].message' --output text | tail -20
```

- [ ] **Step 9: Regression — opt-out still works and composes**

Text `STOP` from the Eastern handset, confirm the row lands in `VipConnectOptOutList` (`--consistent-read`), then run one more pre-call SMS including that number and confirm `totalSkippedOptOut` increments while no message arrives. This proves the quiet-hours gate sits **beside** the opt-out gate rather than shadowing it.

- [ ] **Step 10: PHI check — no names or bodies in logs or long-lived records**

```bash
aws logs filter-log-events --profile production --region us-east-1 \
  --log-group-name /aws/lambda/vip-admin-sms-sender \
  --start-time $(( ($(date +%s) - 86400) * 1000 )) \
  --filter-pattern '"<KnownFirstName>"' --query 'events[].message' --output text
```

Expected: no hits, from this and the processor and api-plans log groups. Also confirm the `VipSmsCampaignQueue` items for this run contain **no** message body attribute.

- [ ] **Step 11: Record the evidence and close the gate**

Append to `docs/runbook.md`'s "Pre-Call SMS (Phase I)" section: plan/run/campaign ids, the SMS-fired and first-dial timestamps proving order, the shared segment name, the `communicationTimeConfig` **exactly as `describe-campaign` echoed it** (so the six-day map, the `T` prefixes and the absent `SUNDAY` are on the record), the deployed `QUIET_HOURS_START`/`END`/`DAYS`/`DEFAULT_TZ` values, the `VipSmsCampaignRuns` counters from **both** the weekday run (Step 5) and the Sunday run (Step 6), which Sunday encoding was proven, the PHI-check results, and an explicit "Phase I verified end-to-end on `<date>`" line. Commit:

```bash
cd /home/devaju/projects/vip-connect-external-campaigns
git add docs/runbook.md
git commit -m "docs: record Phase I pre-call SMS end-to-end verification evidence"
```

**Only after this commit exists may Phase II or Phase III planning begin.** If any step above failed, Phase I is not done and the gate stays shut regardless of how much of it works.

---

## Open Questions / Blockers

### Still open — four items. OQ-8, OQ-9 and OQ-10 block Tasks 3 and 5; OQ-11 does not block, but gates Task 7

- **OQ-8 — approved copy is missing for Fibroid and General. BLOCKS shipping those two specialties.** Sebastian supplied business-approved copy for **Vein** and **Pain Management** only (Task 5, "The approved copy"). No copy has been drafted for Fibroid or General and **none will be invented here** — the copy is patient-facing and TCPA-relevant, so a placeholder that looks final is worse than an empty row. Two ways forward: get the real strings from Sebastian before implementing, or ship Phase I with only the two approved specialties and treat the other two as a follow-up. Either is fine; guessing is not. **Whatever arrives must be re-measured against the 160-character ceiling the same way OQ-9 measures the existing two — do not assume new copy fits.**
- **OQ-9 — the approved Vein copy is 8 characters over the rendered ceiling. Needs a decision, not a workaround.** Measured, not estimated (`{{ClinicName}}` = `VIP Medical Group`, 17 chars; `{{FirstName}}` at the renderer's own `_NAME_MAX_LEN` truncation point of 20 chars, which is the real worst case):

  | | raw | rendered @20-char name | rendered @"Maria" |
  |---|---|---|---|
  | Pain Management (approved) | 125 | **135** ✅ | 120 |
  | Vein (approved) | 158 | **168** ❌ | 153 |

  So Task 5's validator **rejects the approved Vein copy as written**. Three options, all measured:

  1. **Shorten the copy** — needs Sebastian's sign-off since it is his wording. Four candidates, each measured at the 20-char worst case, with the exact resulting edit so he is approving a string and not a description:

     | edit | resulting fragment | rendered @20 |
     |---|---|---|
     | `give you a quick call` → `call you` | `We're about to call you regarding your vein consultation request.` | **155** ✅ |
     | delete `give you a quick ` | `We're about to call regarding your vein consultation request.` | **151** ✅ |
     | closing sentence → `Look out for our call!` | — | **153** ✅ |
     | delete the closing sentence entirely | — | **130** ✅ |

     Two near-misses worth recording so nobody retries them: deleting only ` quick` gives **162**, and borrowing Pain's shorter opener (`{{ClinicName}} here.` instead of `This is {{ClinicName}}.`) gives **165**. Both are still over — the opener is not where the length is.
  2. **Lower `_NAME_MAX_LEN` from 20 to 12.** Vein then renders to **exactly 160** — zero headroom, and any future copy edit or a longer clinic name breaks it again. Also truncates real first names at 12 characters for every message, not just this one.
  3. **Raise `_MAX_SMS_CHARS` from 160 to 320.** Two GSM-7 segments, so **2× the per-message cost** on every send, to accommodate one template that is 8 characters over. Also removes the pressure that is currently keeping the copy short.

  Recommendation: option 1. But it is his copy, so it is his call. Task 5's `test_approved_vein_copy_length_is_pinned_so_the_conflict_cannot_be_lost` deliberately asserts the **measurement** (168) and not a verdict, so whichever option is chosen, that test is the thing that changes and the conflict cannot be quietly lost in a refactor.
- **OQ-10 — should the approved copy carry an opt-out instruction? Must be answered together with OQ-9.** Neither approved template says "Reply STOP". An earlier draft of this plan had added that tail; **it was this plan's invention, not Sebastian's, and it has been removed** rather than left in his wording. The case for adding it is in Task 5, point (3): the deployed opt-out path only ever populates from inbound `STOP`, so without an instruction the mechanism exists but is never exercised, and an unsolicited-looking "we're about to call you" text with no opt-out affordance is the TCPA fact pattern plaintiffs' counsel look for. The cost, measured: ` Reply STOP to opt out.` (23 chars) puts Pain at **158** (fits, max first name 22) and Vein at **191** — over the ceiling **even with a zero-length name** (171). So "add STOP" and "keep Vein as written" are mutually exclusive, which is why these two questions are one decision.
- **OQ-11 — how Connect encodes "closed on this day" is unverified.** Botocore proves both `"SUNDAY": []` and omitting the key are *syntactically* valid (`DailyHours` has no required keys, `TimeRangeList` has no minimum length). Which one Connect *interprets* as "never contact" is undocumented and **cannot be verified without calling `CreateCampaign`**, which a planning pass must not do. Task 2 chooses omission and says so plainly; Task 7 Step 4 checks the `describe-campaign` echo and Task 7 Step 6 proves it with a real Sunday dial attempt. **This is the single most likely thing in Phase I to behave differently from the plan.** If omission turns out to be wrong and `"SUNDAY": []` is too, the day axis has to move into our own code for the voice channel — a design change, not a fix.

### Resolved (kept for the audit trail; numbering preserved so cross-references still hold)

- **OQ-1 — is a 1–6 minute lead enough? RESOLVED 2026-09-10: yes, accepted as-is.** Sebastian **explicitly accepted** the minutes-scale lead rather than it being defaulted into: the SMS is enqueued at bucket activation and Connect's campaign `startTime` is warm-time + 6 min (`executor.py:4136`), giving a real 1–6 minute gap. Guaranteed *ordering* was the requirement; lead time was not. **No Task 4 design change** — neither raising `_PRESTART_MINUTES` nor adding a separate earlier SMS-only plan is in scope.
- **OQ-2 — `phonenumbers` is a brand-new dependency. RESOLVED 2026-09-10: accepted as-is, contingency included.** Confirmed absent from this repo and from `Connect-batch-redis-refactor`, `connect-campaigns-webapp`, and `rcm-sms-inbox`. Sebastian accepted **both** the new dependency **and** Task 1 Step 4's contingency as written: if the layer budget check fails, **stop and report** rather than improvising, and a hand-rolled area-code→timezone map is the documented fallback requiring its own decision. That contingency was explicitly accepted, not merely defaulted to — implementers should follow it literally and not treat "just hand-roll the map" as a shortcut available without asking.
- **OQ-3 — the contact window. RESOLVED 2026-09-10: 08:00–21:00 recipient-local, Monday through Saturday, no Sunday.** The **full statutory TCPA window** on the hours axis (not the tighter 08:00–20:00 this plan originally proposed) and **stricter than statute on the day axis** (TCPA does not exempt Sunday; excluding it is a VIP business choice). This is **not** a two-constant change: the code had no day-of-week concept at all, so Tasks 1 and 2 both add real day logic, and the day must be evaluated in the **recipient's** timezone — 2026-06-15 03:00 UTC is Monday in UTC but Sunday 20:00 Pacific, which is inside the hour window, so only the day axis catches it. Task 1 keeps all three values as env vars (`QUIET_HOURS_START`/`END`/`DAYS`), so widening back to seven days needs no deploy; Task 2 hardcodes them in the builders, which does.
- **OQ-4 — is `FirstName` actually populated in Customer Profiles? RESOLVED 2026-09-10.** Coverage is **100%**, **confirmed directly by Sebastian, not independently measured** — Customer Profiles has no cheap existence-filter API for this, so no aggregate query was run and none is required before Task 3. CP also exposes the field in code (`services/api-profiles/src/handlers/profiles.py:140-141`; `test_profiles_handler.py:38`). Task 3 proceeds on this basis with **no coverage caveat**: personalization is expected to land for every recipient. The renderer's `"there"` fallback and junk-name rejection (`test_missing_first_name_uses_neutral_fallback`, `test_blank_first_name_uses_fallback`) stay in as defensive code against a single bad profile record — they are **not** a hedge against poor coverage.
- **OQ-5 — which origination number should the pre-call SMS use? RESOLVED 2026-09-10: `+16106009752`.** ARN `arn:aws:sms-voice:us-east-1:165505826690:phone-number/phone-ba711707215947e3a0e5112c0872014b` — ACTIVE, `TEN_DLC`, SMS capability, no `TwoWayChannelArn`, and unclaimed by any code (zero hits across all four repos searched). Task 5's "Origination number" section records the two caveats that are **notes, not blockers**: its `MessageType` is `TRANSACTIONAL`, and it shares a `RegistrationId` with the CloudHesive-owned `+19378702788` — **do not attempt to change that registration.** The earlier candidate `+18554810365` (Connect, `Capabilities: null`) was the wrong kind of number entirely; `+18443527135` and `+18444152389` are **already claimed by the separate `rcm-sms-inbox` application** and must not be reused here.
- **OQ-6 — UI for `precallSms`. RESOLVED 2026-09-10: required, not deferred — it is now Task 6.** An operator-follows-the-runbook workflow was rejected. Task 6 adds a real authoring panel to the existing plan editor (`frontend/src/pages/PlanNew.tsx`'s `CampaignCard`), following that file's own conventions, and Task 7 Step 2 configures the verification campaign **through it**. The panel must not be able to author anything the backend rejects, which is why `precallSmsAvailability()` disables it for `dependsOn` campaigns and the length counter measures the **rendered** worst case rather than the raw string.
- **OQ-7 — `{{LastName}}`. RESOLVED 2026-09-10: stays excluded, and the copy is now real.** A surname adds identifiability with no engagement benefit, so it remains denied by omission and nobody should expect `Hi Maria Gomez!`. The same decision replaced this plan's drafted copy with Sebastian's business-approved strings for Vein and Pain Management, which had two knock-on effects worth reading before implementing: `{{Specialty}}` is **no longer allowlisted** (the approved copy bakes the specialty into the sentence — Task 5, point 1, lists the five reversal sites), and the approved copy raised **OQ-9** and **OQ-10** above.

---

## Appendix A: CloudHesive flow findings (no longer work items — handoff only)

An earlier revision of this plan proposed cloning `*PreCallSMSFlow`. That is withdrawn, but the defects found while reading it are recorded so CloudHesive can fix their own copy and nobody re-derives them. **No task in this plan acts on any of these.**

In `*PreCallSMSFlow` (`f85f1cd2-91b0-4252-9844-05b8b1c72967`, CAMPAIGN, SAVED):
1. The `Compare` block reads `$.FlowAttributes.campaign`, but the preceding `UpdateContactAttributes` writes with `TargetContact: "Current"` → **contact** attributes. The condition never matches, so every lead falls through `NoMatchingCondition` and is classified `General` regardless of specialty.
2. The Lambda's resolved text is stored via `UpdateFlowAttributes` as a **flow** attribute `smsText`, but the Wisdom template body is `{{Attributes.Customer.Attributes.smsText}}` — a Customer-Profile reference. The interpolation cannot resolve; the lookup is wasted.
3. `SendSMS` sends on an existing chat/SMS contact, but a CAMPAIGN flow runs in a voice dial-request context with no SMS contact. `StartOutboundChatContact` with `ContactSubtype: "connect:SMS"` is the correct construction.
4. The flow contains no `PutDialRequest`, so it would never place the call.

In `*PreCallSMSFlow-test` (`412a4278-75ba-4495-b670-cd97846e3b63`, CONTACT_FLOW, PUBLISHED — fixes 1-3, ownership unknown):
5. `EvaluateDataTableValues` has `"DataTableId": "PLACEHOLDER_DATA_TABLE_UUID"`. The real id is `4ecc384d-c8b3-44f1-ada7-9c0962165d7f`.
6. Its query passes four `PrimaryValues` (`stage`, `funnel_touchpoint`, `campaign`, `specialty`), but that Data Table has exactly **one** primary attribute, `message_key`, holding the whole composite string. The query fails and the error branch sends an SMS with `smsText` unset.
7. Every `text_prompt` row contains literal `[FirstName]`/`[First_Name]`/`[Staff_Name]` and nothing interpolates them, so patients would receive the brackets verbatim. (Our design solves the same problem properly in Task 3.)

Also for the handoff: version 1 of the `PreCallSMS` Wisdom template was created and activated 2026-09-08 by `sebastian.valdenebro@medwork.io`, and `cloudhesive-integration-connectcampaign_sms_lookup` was added to the Connect instance's classic Lambda association list. Both additive; CloudHesive's flow already pins `...:1`.

---

## Explicitly Out of Scope (do not implement as part of this plan)

- **Cloning, editing, or depending on any Connect contact flow.** Withdrawn by design decision. Do not "helpfully" fix `*PreCallSMSFlow` or `*PreCallSMSFlow-test` while here.
- **Sequencing via `dependsOn`, and the machinery it would have required.** Specifically not being built: `reuseSegmentFromCampaignId`, `startDelayMinutesAfterDeps`, changes to the four `_create_segment` call sites, changes to the five segment-cleanup guards, and the `_bucket_has_only_legitimate_waits` alarm-predicate fix. All were needed only by the rejected design. **If a future task reintroduces `dependsOn` as a pre-call ordering device, it must also reintroduce all of them, plus accept the loss of pre-warming** (`executor.py:2180`, `2571-2573`).
- **Raising `_STUCK_RUN_HOURS` or `_NO_ACTIVE_CAMPAIGN_MINUTES`.** Both protect real alarms and this design does not need either relaxed.
- **`JOURNEY_FLOW_ARN` and the `Test-Journey-Flow` placeholder.** `services/api-plans/src/builders.py:324` hardcodes `_JOURNEY_FLOW_NAME = "Test-Journey-Flow"` and no CDK stack sets `JOURNEY_FLOW_ARN`. A real latent gap for `deliveryType: 'journey'` users. Track separately — this plan does not fix it. Note the distinction: pre-call SMS itself **is** deliberately available on journey campaigns (`precallSmsAvailability` returns `available` for both `campaign` and `journey`, and Task 6 unit-tests both), because the firing hook lives in the bucket-activation transition and is indifferent to `deliveryType`. What is out of scope is *exercising* journeys — Task 7's end-to-end gate uses a plain voice `campaign` only, so pre-call SMS on a journey is enabled-but-unverified until someone runs it.
- **The `VIP_SMS_Journey_Texts` stores** (CloudHesive's DynamoDB table and the Connect Data Table) and the **`PreCallSMS` Wisdom template.** Unused under this design; do not add pre-call rows to either.
- **A new DynamoDB message-template table.** Deliberately rejected: the repo's convention is inline validated config, and a new store would duplicate the PHI guard. Revisit only if copy must change without a plan edit.
- **Widening the placeholder allowlist** beyond `{{FirstName}}` and `{{ClinicName}}` — including `{{LastName}}` (**OQ-7**), `{{Specialty}}` (dropped because the approved copy bakes the specialty into the sentence; re-adding it is additive at the five sites Task 5 lists), dates, appointment details, or free-text fields. That is a PHI decision requiring Sebastian and counsel, not an implementation detail. `${...}` stays banned outright.
- **Removing or bypassing any of the other nine `_PHI_PATTERNS` entries.** The list has exactly **ten** entries (`plans.py:520-534`, counted: two SSN forms, email, two date forms, long numeric ID, URL, `{{...}}`, `${...}`, clinical terms). Task 3 narrows exactly one — the `{{...}}` entry at line 528 — and keeps the remaining nine byte-identical.
- **Renaming the `messageTemplate` SQS message key.** Would strand in-flight messages across the deploy boundary.
- **Deleting or reworking the COT staffing checks.** Task 1 Step 6 changes comments only.
- **A new page, route, or standalone form for pre-call SMS.** The UI is in scope (Task 6, resolving **OQ-6**) but only as a panel inside the existing `CampaignCard` in `frontend/src/pages/PlanNew.tsx`. Do not add a route, do not extend the read-only `PlanDetail.tsx`/`CampaignDetail.tsx` views, do not touch the unrelated `CampaignNew.tsx`, and do not introduce `react-hook-form`, `zod`, a component library, or `ui.tsx` primitives into a file that uses none of them.
- **Reimplementing the `_PHI_PATTERNS` regexes in TypeScript.** Task 6's client-side validator deliberately mirrors only the cheap, stable rules (required fields, allowlist, rendered length, `dependsOn` conflict). The server stays the single source of truth for the PHI screen; two copies of a compliance rule drift.
- **`ZIP_CODE` timezone detection or `localTimeZoneDetectionScope`.** `AREA_CODE` chosen because it needs no Customer Profiles address.
- **CloudHesive's `callback-tz-TimezoneCheck` / `callback-tz-RequeueCallbacks` and the `outbound_extractareanumber` Lambda.** All three touch timezone/area-code logic. If any duplicates `quiet_hours.py`, consolidating is separate scope — ask first.
- **Phase II and Phase III.** No event-driven post-call SMS, no Lex triage, no Luma Health handoff.
- **Medwork write-back of pre-call SMS status.** No task calls `api-leads.medwork.io`.
