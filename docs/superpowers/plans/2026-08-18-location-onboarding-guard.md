# Location Onboarding Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Alarm operators via SNS when a brand-new state appears in `VipLocationMapping` with no canonical phone number configured, and stop `EnableCampaignModal.tsx`'s ad-hoc "Enable Campaign" flow from guessing campaign flows client-side — have it call the backend's existing, already-deployed `resolve_campaign_flow_arn()` (which auto-creates the canonical `campaign-<STATE>` Connect flow if missing) instead.

**Architecture:** (1) A one-off backfill populates `canonicalPhone`/`areaCodes` attributes on all 76 existing `VipLocationMapping` items, using the exact values already live in `frontend/src/lib/areaCodeMap.ts`. (2) DynamoDB Streams is enabled on `VipLocationMapping` (CLI — table is CFN-exec-role-boundary-imported, not CDK-owned). (3) A new lightweight Lambda (`location_onboarding_guard.py`, no VPC, no Connect permissions) is added to `api-plans-stack.ts`, triggered on `INSERT` events; it checks whether the inserted item's `stateCode` is genuinely new (no other item shares it) and whether `canonicalPhone` is missing, and if so publishes to the existing `vip-plans-alerts` SNS topic using the same `Subject`/`Message`/`MessageAttributes` shape `executor.py`'s `_notify_sns()` already uses. (4) A new `POST /plans/resolve-campaign-flow` endpoint exposes the existing `builders.resolve_campaign_flow_arn(state_codes, connect_instance_id)` (already deployed, already has `connect:CreateContactFlow`+`connect:TagResource` IAM, already used by the Plans/executor system) to the frontend. (5) `EnableCampaignModal.tsx` calls that endpoint instead of running its own `suggestCampaignFlow()` substring search.

**Tech Stack:** AWS CDK (TypeScript), Python 3.12 Lambda (`services/api-plans`), DynamoDB Streams + Lambda Event Source, boto3, React/TypeScript frontend, Vitest, pytest.

**Spec:** No separate spec doc — this plan is its own spec, argued from investigation performed live against the production AWS account (165505826690, Connect instance `vipmedicalgroup` / `6b3f17ba-68a4-472a-9b20-db1991507009`) and the current repo state on `main`, immediately before this plan was written. Two design decisions were confirmed by the user via AskUserQuestion before writing this plan:
1. Give the backend a real source of truth for "canonical phone per state" by adding `canonicalPhone`/`areaCodes` to `VipLocationMapping` (not a heuristic, not a duplicate hardcoded backend map).
2. Wire `EnableCampaignModal.tsx` to the backend's existing `resolve_campaign_flow_arn()` instead of leaving the frontend's own substring-search heuristic as the only path.

## Global Constraints

- Never invent a "canonical phone" or "area codes" value for any state. Every value used in the Task 1 backfill must be copied verbatim from the CURRENT content of `frontend/src/lib/areaCodeMap.ts` (reproduced in Task 1 below) — do not guess or derive new ones.
- The new Lambda (`location_onboarding_guard.py`) must NOT have any Connect IAM permissions — its job is detection + SNS publish only, nothing else. Flow auto-creation is already handled elsewhere (`builders.resolve_campaign_flow_arn`); duplicating that here is out of scope and forbidden by this plan.
- Follow the existing "table pre-created via CLI, CDK imports by name/ARN" pattern already used for `VipLocationMapping`/`VipProgressiveCampaignQueue` elsewhere in this repo (see `infra/lib/stacks/api-progressive-dialer-stack.ts:26-32,59-65` for the exact precedent). Do not attempt to have CDK create or manage `VipLocationMapping`'s stream — enable it via AWS CLI, then hardcode the resulting stream ARN as a literal string in `infra/bin/app.ts`, exactly like `campaignQueueStreamArn` at `infra/bin/app.ts:142`.
- SNS alerts from the new Lambda MUST use the exact same message shape as `executor.py`'s `_notify_sns()` (`services/api-plans/src/executor.py:4301-4319`): `Subject` (truncated to 100 chars), `Message` (free text), `MessageAttributes` (flat string key/value pairs) — so existing operator tooling/filters built around `vip-plans-alerts` keep working unchanged.
- No new IAM roles/policies beyond what CDK creates automatically for the new Lambda's own execution role — this is a normal `lambda.Function` + auto-generated role + `role.addToPolicy()`/`grantXxx()` calls, not subject to the `EngineeringPermissionBoundary` issue (that issue blocks creating new DynamoDB tables/SNS topics directly via CFN, not adding policy statements to a CDK-managed Lambda role — this repo's stacks already do this routinely, e.g. `api-plans-stack.ts:105-382`).
- Any production AWS mutation (the Task 1 backfill write, the Task 2 `update-table` Streams enable, any `cdk deploy`) must be flagged clearly in the SDD ledger with `AWS_PROFILE=production` visible in the exact command run, and confirmed against a `describe`/`scan` read-back immediately after — never trust a "success" API response alone.
- Do not touch `EnableCampaignModal.tsx`'s existing `STATE_FLOW_PATTERNS`/`suggestCampaignFlow` (fixed earlier today, has its own regression tests) beyond what Task 6 requires — keep it as the fallback path if the new backend call fails, not delete it.

---

### Task 1: Backfill `canonicalPhone`/`areaCodes` onto every existing `VipLocationMapping` item

**Files:**
- Create: `infra/scripts/backfill-location-canonical-phone.py`
- Test: none (one-off ops script, not part of any deployed Lambda; verified by a `--dry-run` mode plus a live `describe`/`scan` read-back after running for real)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: every item in the live `VipLocationMapping` DynamoDB table gains two new attributes: `canonicalPhone` (String) and `areaCodes` (String Set). Task 3's Lambda reads these two attributes by name — they must match exactly.

**Context:** `VipLocationMapping` has PK=`location` (String), 76 items today, one item per physical location string (e.g. `"NJ - Hoboken"`), each already carrying `stateCode`/`slug`/`stateName`/`stateSortOrder`. Multiple items share the same `stateCode` (e.g. 15 items have `stateCode=NJ`). This backfill denormalizes `canonicalPhone`/`areaCodes` onto every item of a given state — the same denormalization pattern already used for `stateName`/`stateSortOrder` (every item of a state repeats the same values). These are the EXACT values currently live in `frontend/src/lib/areaCodeMap.ts` as of this plan's writing — copy them verbatim, do not recompute or guess:

```python
CANONICAL_PHONES = {
    "NY":  "+19174105649",
    "LI":  "+16314497507",
    "NJ":  "+19734949660",
    "MD":  "+13018594566",
    "CT":  "+14753656590",
    "TX":  "+15126508970",
    "SCA": "+18588686651",
    "NCA": "+16694674988",
    "PA":  "+12154009167",
}

AREA_CODES = {
    "NY":  ["212", "646", "332", "718", "347", "929", "516", "631", "914", "845", "585", "716", "315", "680", "607", "518", "838"],
    "NCA": ["415", "628", "510", "341", "925", "669", "408", "650", "209", "559", "916", "279", "530", "707", "369"],
    "SCA": ["213", "323", "747", "818", "310", "424", "562", "626", "661", "714", "657", "949", "805", "619", "858", "760", "442", "951", "909"],
    "NJ":  ["201", "551", "732", "848", "609", "640", "856", "862", "973", "908"],
    "TX":  ["214", "469", "972", "945", "832", "713", "281", "346", "210", "726", "512", "737", "254", "325", "361", "409", "430", "432", "806", "830", "903", "915", "936", "940", "956", "979"],
    "CT":  ["203", "475", "860", "959"],
    "MD":  ["240", "301", "410", "443", "667"],
    "LI":  ["516", "631"],
    "PA":  ["215"],
}
```

- [ ] **Step 1: Write the backfill script**

```python
#!/usr/bin/env python3
"""One-off backfill: add canonicalPhone/areaCodes to every VipLocationMapping item.

Source of truth for the values: frontend/src/lib/areaCodeMap.ts as of 2026-08-18.
Run once. Safe to re-run (idempotent — always overwrites with the same values).

Usage:
    AWS_PROFILE=production python3 backfill-location-canonical-phone.py --dry-run
    AWS_PROFILE=production python3 backfill-location-canonical-phone.py
"""
import argparse
import sys

import boto3

TABLE_NAME = "VipLocationMapping"

CANONICAL_PHONES = {
    "NY":  "+19174105649",
    "LI":  "+16314497507",
    "NJ":  "+19734949660",
    "MD":  "+13018594566",
    "CT":  "+14753656590",
    "TX":  "+15126508970",
    "SCA": "+18588686651",
    "NCA": "+16694674988",
    "PA":  "+12154009167",
}

AREA_CODES = {
    "NY":  ["212", "646", "332", "718", "347", "929", "516", "631", "914", "845", "585", "716", "315", "680", "607", "518", "838"],
    "NCA": ["415", "628", "510", "341", "925", "669", "408", "650", "209", "559", "916", "279", "530", "707", "369"],
    "SCA": ["213", "323", "747", "818", "310", "424", "562", "626", "661", "714", "657", "949", "805", "619", "858", "760", "442", "951", "909"],
    "NJ":  ["201", "551", "732", "848", "609", "640", "856", "862", "973", "908"],
    "TX":  ["214", "469", "972", "945", "832", "713", "281", "346", "210", "726", "512", "737", "254", "325", "361", "409", "430", "432", "806", "830", "903", "915", "936", "940", "956", "979"],
    "CT":  ["203", "475", "860", "959"],
    "MD":  ["240", "301", "410", "443", "667"],
    "LI":  ["516", "631"],
    "PA":  ["215"],
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    table = boto3.resource("dynamodb").Table(TABLE_NAME)

    scanned = []
    resp = table.scan()
    scanned.extend(resp["Items"])
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        scanned.extend(resp["Items"])

    print(f"Scanned {len(scanned)} items from {TABLE_NAME}")

    missing_codes = {item["stateCode"] for item in scanned} - set(CANONICAL_PHONES)
    if missing_codes:
        print(f"ERROR: no canonical phone defined for state codes: {sorted(missing_codes)}")
        print("Add them to CANONICAL_PHONES/AREA_CODES before running — do not guess.")
        return 1

    updated = 0
    for item in scanned:
        code = item["stateCode"]
        location = item["location"]
        phone = CANONICAL_PHONES[code]
        codes = AREA_CODES[code]
        if args.dry_run:
            print(f"[dry-run] {location} ({code}) -> canonicalPhone={phone}, areaCodes={codes}")
            continue
        table.update_item(
            Key={"location": location},
            UpdateExpression="SET canonicalPhone = :p, areaCodes = :a",
            ExpressionAttributeValues={
                ":p": phone,
                ":a": set(codes),
            },
        )
        updated += 1

    if args.dry_run:
        print(f"[dry-run] would update {len(scanned)} items — no writes performed")
    else:
        print(f"Updated {updated} items")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Dry-run against production and inspect output**

```bash
cd infra/scripts
AWS_PROFILE=production python3 backfill-location-canonical-phone.py --dry-run
```

Expected: 76 lines, one per item, no `ERROR:` line, ending with `[dry-run] would update 76 items — no writes performed`.

- [ ] **Step 3: STOP — confirm with the user before writing to production**, then run for real

```bash
AWS_PROFILE=production python3 backfill-location-canonical-phone.py
```

Expected: `Updated 76 items`.

- [ ] **Step 4: Read back and verify against live data**

```bash
AWS_PROFILE=production aws dynamodb scan --table-name VipLocationMapping \
  --query 'Items[?location.S==`PA - Vein Leads`]' --output json
```

Expected: the item now has `"canonicalPhone": {"S": "+12154009167"}` and `"areaCodes": {"SS": ["215"]}` alongside its existing attributes. Spot-check one more state (e.g. `NJ - Hoboken`) the same way.

- [ ] **Step 5: Commit the script**

```bash
git add infra/scripts/backfill-location-canonical-phone.py
git commit -m "feat(infra): backfill canonicalPhone/areaCodes onto VipLocationMapping

One-off script establishing a real backend source of truth for
per-state canonical phone numbers, so the new Location Onboarding
Guard Lambda (next task) can alarm on missing data instead of
guessing. Values copied verbatim from frontend/src/lib/areaCodeMap.ts."
```

---

### Task 2: Enable DynamoDB Streams on `VipLocationMapping`

**Files:** none (infrastructure-only, no repo file changes except recording the resulting ARN for Task 3 to consume)

**Interfaces:**
- Consumes: nothing.
- Produces: a DynamoDB Streams ARN, of the form `arn:aws:dynamodb:us-east-1:165505826690:table/VipLocationMapping/stream/<timestamp>`. Task 3 hardcodes this exact string into `infra/bin/app.ts`, mirroring `campaignQueueStreamArn` at `infra/bin/app.ts:142`.

- [ ] **Step 1: Enable the stream**

```bash
AWS_PROFILE=production aws dynamodb update-table \
  --table-name VipLocationMapping \
  --stream-specification StreamEnabled=true,StreamViewType=NEW_IMAGE
```

- [ ] **Step 2: Read back the resulting stream ARN**

```bash
AWS_PROFILE=production aws dynamodb describe-table --table-name VipLocationMapping \
  --query 'Table.LatestStreamArn' --output text
```

Record this exact ARN — Task 3 needs it verbatim.

- [ ] **Step 3: No commit** — this step has no repo file changes. Record the ARN in the SDD ledger for Task 3 to consume.

---

### Task 3: CDK — new Lambda, IAM, and DynamoDB Streams event source in `api-plans-stack.ts`

**Files:**
- Modify: `infra/lib/stacks/api-plans-stack.ts`
- Modify: `infra/bin/app.ts`
- Test: `infra/test/api-plans-stack.test.ts` (create if no such CDK snapshot/assertion test file exists for this stack — check first; if the repo has no CDK unit tests anywhere, skip this file and rely on `cdk synth` succeeding as the verification step instead — do not invent a testing convention that doesn't exist in this repo)

**Interfaces:**
- Consumes: the stream ARN from Task 2 (hardcoded literal string).
- Produces: a new Lambda function (logical id `LocationOnboardingGuardFunction`, `functionName: 'vip-location-onboarding-guard'`) that Task 4's Python code runs inside. Its role has: DynamoDB Streams read on `VipLocationMapping`'s stream, `dynamodb:Scan`/`dynamodb:GetItem` on `VipLocationMapping` itself, and `sns:Publish` on the existing `vip-plans-alerts` topic. Environment variable `SNS_ALERTS_TOPIC_ARN` (same name Task 4 reads) and `LOCATION_MAPPING_TABLE` (table name).

**Context:** `api-plans-stack.ts` already imports `VipLocationMapping` read-only via `dynamodb.Table.fromTableName(this, 'LocationMappingTable', 'VipLocationMapping')` at line 313-317 (used by `builders.py` for segment building) and grants it `grantReadData(role)` on the MAIN Lambda's role — leave that import and grant exactly as-is; it is a separate `role` (the main Lambda's) from the new Lambda's own role this task creates. This task adds a SECOND import via `fromTableAttributes` (which exposes `tableStreamArn`, unlike `fromTableName`) specifically for the new Lambda's event source — having two CDK constructs referencing the same physical table by different `fromTableXxx` calls is fine and already how this repo's `campaignQueueTable` pattern works across stacks.

- [ ] **Step 1: Add the optional stream-ARN prop**

In `infra/lib/stacks/api-plans-stack.ts`, add to `ApiPlansStackProps` (near the other optional props, after `smsSenderFunctionArn`):

```typescript
  // Location Onboarding Guard — DynamoDB stream ARN for VipLocationMapping.
  // Enable with: aws dynamodb update-table --table-name VipLocationMapping
  //   --stream-specification StreamEnabled=true,StreamViewType=NEW_IMAGE
  // Omit to deploy without the guard Lambda's event source wired.
  readonly locationMappingStreamArn?: string;
```

- [ ] **Step 2: Add a second table reference with stream ARN**

Immediately after the existing `locationMappingTable.grantReadData(role);` line (line 318), add:

```typescript
    // ── Location Onboarding Guard — same physical table, second CDK
    // reference so we can expose its stream ARN (fromTableName can't).
    const locationMappingTableWithStream = props.locationMappingStreamArn
      ? dynamodb.Table.fromTableAttributes(this, 'LocationMappingTableStream', {
          tableArn: `arn:aws:dynamodb:${this.region}:${this.account}:table/VipLocationMapping`,
          tableStreamArn: props.locationMappingStreamArn,
        })
      : undefined;
```

- [ ] **Step 3: Add the new Lambda, its role, and the event source**

Add near the end of the constructor, after the main `this.lambdaFunction` definition and its `addEnvironment` calls (after line ~454, before the closing of the constructor):

```typescript
    // ── Location Onboarding Guard — detects a brand-new state appearing in
    // VipLocationMapping with no canonicalPhone set, alarms via SNS.
    // No Connect permissions — flow auto-creation is handled elsewhere
    // (builders.resolve_campaign_flow_arn, called from executor.py).
    if (locationMappingTableWithStream) {
      const guardRole = new iam.Role(this, 'LocationOnboardingGuardRole', {
        assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
        managedPolicies: [
          iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
        ],
        ...(props.permissionsBoundaryName
          ? { permissionsBoundary: iam.ManagedPolicy.fromManagedPolicyName(this, 'GuardBoundary', props.permissionsBoundaryName) }
          : {}),
      });
      locationMappingTableWithStream.grantStreamRead(guardRole);
      locationMappingTableWithStream.grantReadData(guardRole);
      alertsTopic.grantPublish(guardRole);

      const guardLogGroup = new logs.LogGroup(this, 'LocationOnboardingGuardLogs', {
        logGroupName: '/aws/lambda/vip-location-onboarding-guard',
        retention: logs.RetentionDays.ONE_YEAR,
        encryptionKey: props.dataKey,
        removalPolicy: cdk.RemovalPolicy.RETAIN,
      });

      const guardFunction = new lambda.Function(this, 'LocationOnboardingGuardFunction', {
        functionName: 'vip-location-onboarding-guard',
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: 'location_onboarding_guard.lambda_handler',
        code: lambda.Code.fromAsset(
          path.join(__dirname, '../../../services/api-plans/src'),
        ),
        memorySize: 256,
        timeout: cdk.Duration.seconds(30),
        role: guardRole,
        logGroup: guardLogGroup,
        reservedConcurrentExecutions: 1,
        environment: {
          SNS_ALERTS_TOPIC_ARN: alertsTopic.topicArn,
          LOCATION_MAPPING_TABLE: 'VipLocationMapping',
          LOG_LEVEL: 'INFO',
        },
      });

      guardFunction.addEventSource(
        new DynamoEventSource(locationMappingTableWithStream, {
          startingPosition: lambda.StartingPosition.LATEST,
          batchSize: 10,
          retryAttempts: 2,
          filters: [
            lambda.FilterCriteria.filter({
              eventName: lambda.FilterRule.isEqual('INSERT'),
            }),
          ],
        }),
      );
    }
```

Add this import to the existing import block at the top of the file (alongside the other `aws-cdk-lib/aws-*` imports):

```typescript
import { DynamoEventSource } from 'aws-cdk-lib/aws-lambda-event-sources';
```

- [ ] **Step 4: Wire the prop in `infra/bin/app.ts`**

In `infra/bin/app.ts`, find the `new ApiPlansStack(app, 'VipAdminApiPlansStack', { ... })` call (around line 179) and add, alongside the other optional stream-ARN props:

```typescript
  locationMappingStreamArn: 'PASTE_THE_EXACT_ARN_FROM_TASK_2_STEP_2_HERE',
```

Use the literal ARN string recorded in Task 2 — do not leave a placeholder in the committed code.

- [ ] **Step 5: Verify with `cdk synth`**

```bash
cd infra
npx cdk synth VipAdminApiPlansStack > /dev/null
echo "exit code: $?"
```

Expected: exit code 0, no synth errors. If a CDK unit-test file already exists for other stacks in `infra/test/`, follow that exact existing convention for a matching test on this stack; if none exists anywhere in the repo, `cdk synth` succeeding is the verification for this task — do not invent a test framework that isn't already in use.

- [ ] **Step 6: Commit**

```bash
git add infra/lib/stacks/api-plans-stack.ts infra/bin/app.ts
git commit -m "feat(infra): add Location Onboarding Guard Lambda + DynamoDB Streams wiring

New Lambda triggered on VipLocationMapping INSERT events, scoped to
detection + SNS alerting only (no Connect permissions — flow
auto-creation already exists via builders.resolve_campaign_flow_arn).
Streams enabled on the table via CLI per Task 2; ARN hardcoded here
following the same pattern as campaignQueueStreamArn."
```

---

### Task 4: `location_onboarding_guard.py` Lambda handler

**Files:**
- Create: `services/api-plans/src/location_onboarding_guard.py`
- Test: `services/api-plans/tests/unit/test_location_onboarding_guard.py`

**Interfaces:**
- Consumes: DynamoDB Streams event shape (`event["Records"]`, each with `eventName`, `dynamodb.NewImage`), env vars `SNS_ALERTS_TOPIC_ARN` and `LOCATION_MAPPING_TABLE` (set by Task 3's CDK).
- Produces: nothing consumed by other tasks — this is a leaf Lambda.

**Context:** DynamoDB Streams records arrive in the low-level `AttributeValue` JSON shape (`{"S": "..."}`, `{"N": "..."}`), not plain Python types — use `boto3.dynamodb.types.TypeDeserializer` to convert, matching how any other Streams-consuming code in this repo would (check `services/api-progressive-dialer/src` for a Streams handler to confirm the exact deserialization idiom used elsewhere before writing this — if one exists, mirror it; if none exists, `TypeDeserializer().deserialize(av)` is the standard boto3 approach). "Genuinely new state" is determined by scanning the table for other items sharing the same `stateCode` and checking whether the just-inserted item is the ONLY one — do this with a `Scan` + `FilterExpression` (table is small, ~76-90 items, a full scan per invocation is cheap and this Lambda only runs on rare inserts).

- [ ] **Step 1: Write the failing test**

```python
# services/api-plans/tests/unit/test_location_onboarding_guard.py
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

os.environ["SNS_ALERTS_TOPIC_ARN"] = "arn:aws:sns:us-east-1:165505826690:vip-plans-alerts"
os.environ["LOCATION_MAPPING_TABLE"] = "VipLocationMapping"

import location_onboarding_guard as guard  # noqa: E402


def _insert_record(location: str, state_code: str, canonical_phone: str | None) -> dict:
    new_image = {
        "location": {"S": location},
        "stateCode": {"S": state_code},
    }
    if canonical_phone is not None:
        new_image["canonicalPhone"] = {"S": canonical_phone}
    return {
        "eventName": "INSERT",
        "dynamodb": {"NewImage": new_image},
    }


def test_alarms_when_new_state_has_no_canonical_phone(monkeypatch):
    published = []

    class FakeSns:
        def publish(self, **kwargs):
            published.append(kwargs)

    monkeypatch.setattr(guard, "_sns_client", lambda: FakeSns())
    monkeypatch.setattr(guard, "_is_first_occurrence_of_state", lambda code, exclude_location: True)

    event = {"Records": [_insert_record("WA - Seattle", "WA", None)]}
    guard.lambda_handler(event, None)

    assert len(published) == 1
    assert published[0]["TopicArn"] == "arn:aws:sns:us-east-1:165505826690:vip-plans-alerts"
    assert "WA" in published[0]["Subject"]
    assert published[0]["MessageAttributes"]["stateCode"]["StringValue"] == "WA"


def test_no_alarm_when_new_state_has_canonical_phone(monkeypatch):
    published = []

    class FakeSns:
        def publish(self, **kwargs):
            published.append(kwargs)

    monkeypatch.setattr(guard, "_sns_client", lambda: FakeSns())
    monkeypatch.setattr(guard, "_is_first_occurrence_of_state", lambda code, exclude_location: True)

    event = {"Records": [_insert_record("WA - Seattle", "WA", "+12065551234")]}
    guard.lambda_handler(event, None)

    assert published == []


def test_no_alarm_when_state_already_existed(monkeypatch):
    published = []

    class FakeSns:
        def publish(self, **kwargs):
            published.append(kwargs)

    monkeypatch.setattr(guard, "_sns_client", lambda: FakeSns())
    monkeypatch.setattr(guard, "_is_first_occurrence_of_state", lambda code, exclude_location: False)

    event = {"Records": [_insert_record("NJ - New Town", "NJ", None)]}
    guard.lambda_handler(event, None)

    assert published == []


def test_ignores_non_insert_events(monkeypatch):
    published = []

    class FakeSns:
        def publish(self, **kwargs):
            published.append(kwargs)

    monkeypatch.setattr(guard, "_sns_client", lambda: FakeSns())

    event = {
        "Records": [
            {
                "eventName": "MODIFY",
                "dynamodb": {"NewImage": {"location": {"S": "NJ - Hoboken"}, "stateCode": {"S": "NJ"}}},
            }
        ]
    }
    guard.lambda_handler(event, None)

    assert published == []
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd services/api-plans
python3 -m pytest tests/unit/test_location_onboarding_guard.py -v
```

Expected: `ModuleNotFoundError: No module named 'location_onboarding_guard'`.

- [ ] **Step 3: Write the implementation**

```python
# services/api-plans/src/location_onboarding_guard.py
"""Alarm when a brand-new state appears in VipLocationMapping with no
canonical phone number configured.

Triggered by DynamoDB Streams INSERT events on VipLocationMapping.
Flow auto-creation for new states is handled elsewhere
(builders.resolve_campaign_flow_arn, called from executor.py) — this
Lambda does detection + SNS alerting ONLY, no Connect permissions.
"""
import json
import logging
import os

import boto3
from boto3.dynamodb.types import TypeDeserializer

_LOG = logging.getLogger(__name__)
_LOG.setLevel(logging.INFO)

_SNS_ALERTS_TOPIC_ARN = os.environ["SNS_ALERTS_TOPIC_ARN"]
_LOCATION_MAPPING_TABLE = os.environ["LOCATION_MAPPING_TABLE"]

_deserializer = TypeDeserializer()
_sns = None
_ddb_table = None


def _sns_client():
    global _sns
    if _sns is None:
        _sns = boto3.client("sns")
    return _sns


def _table():
    global _ddb_table
    if _ddb_table is None:
        _ddb_table = boto3.resource("dynamodb").Table(_LOCATION_MAPPING_TABLE)
    return _ddb_table


def _is_first_occurrence_of_state(state_code: str, exclude_location: str) -> bool:
    """True if no OTHER item in the table already has this stateCode."""
    resp = _table().scan(
        FilterExpression="stateCode = :code AND #loc <> :loc",
        ExpressionAttributeNames={"#loc": "location"},
        ExpressionAttributeValues={":code": state_code, ":loc": exclude_location},
        ProjectionExpression="#loc",
    )
    return len(resp.get("Items", [])) == 0


def _notify_missing_phone(state_code: str, location: str) -> None:
    subject = f"New state detected with no canonical phone: {state_code}"[:100]
    message = (
        f"A new location ({location}) introduced state code '{state_code}' to "
        f"VipLocationMapping, but no canonicalPhone attribute is set for it.\n\n"
        f"Set canonicalPhone (and areaCodes) on this state's VipLocationMapping "
        f"items before enabling campaigns for it — see "
        f"infra/scripts/backfill-location-canonical-phone.py for the pattern."
    )
    try:
        _sns_client().publish(
            TopicArn=_SNS_ALERTS_TOPIC_ARN,
            Subject=subject,
            Message=message,
            MessageAttributes={
                "stateCode": {"DataType": "String", "StringValue": state_code},
                "event": {"DataType": "String", "StringValue": "location_onboarding_missing_phone"},
            },
        )
    except Exception as exc:
        _LOG.warning(
            "location_onboarding_guard: SNS publish failed (topic=%s): %s",
            _SNS_ALERTS_TOPIC_ARN,
            exc,
        )


def lambda_handler(event: dict, _context) -> None:
    for record in event.get("Records", []):
        if record.get("eventName") != "INSERT":
            continue

        raw_image = record.get("dynamodb", {}).get("NewImage", {})
        image = {k: _deserializer.deserialize(v) for k, v in raw_image.items()}

        state_code = image.get("stateCode")
        location = image.get("location")
        if not state_code or not location:
            continue

        canonical_phone = image.get("canonicalPhone")

        if canonical_phone:
            continue

        if not _is_first_occurrence_of_state(state_code, location):
            continue

        _LOG.info(json.dumps({
            "event": "location_onboarding_missing_phone_detected",
            "state_code": state_code,
        }))
        _notify_missing_phone(state_code, location)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd services/api-plans
python3 -m pytest tests/unit/test_location_onboarding_guard.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add services/api-plans/src/location_onboarding_guard.py services/api-plans/tests/unit/test_location_onboarding_guard.py
git commit -m "feat(api-plans): add location_onboarding_guard Lambda handler

Detects a brand-new stateCode inserted into VipLocationMapping with
no canonicalPhone set, publishes to vip-plans-alerts using the same
Subject/Message/MessageAttributes shape as executor.py's
_notify_sns(). No Connect permissions needed or granted."
```

---

### Task 5: Backend HTTP endpoint — `POST /plans/resolve-campaign-flow`

**Files:**
- Modify: `services/api-plans/src/router.py`
- Modify: `services/api-plans/src/handlers/plans.py`
- Modify: `infra/lib/stacks/api-stack.ts`
- Test: `services/api-plans/tests/unit/test_handlers_plans.py` (check this exact filename exists first — if the existing test file for `handlers/plans.py` has a different name, add the new test to that file instead of creating a duplicate)

**Interfaces:**
- Consumes: `builders.resolve_campaign_flow_arn(state_codes: list[str], connect_instance_id: str) -> str | None` (already exists, `services/api-plans/src/builders.py:371-422`, unchanged by this task).
- Produces: `POST /plans/resolve-campaign-flow` with body `{"states": ["PA"]}` → `{"arn": "arn:aws:connect:...:contact-flow/..."}` or `{"arn": null}`. Task 6's frontend client calls this exact path/shape.

**Context:** `services/api-plans/src/handlers/plans.py` already has `get_location_mapping` (line 19-22) as the simplest possible handler to mirror. `CONNECT_INSTANCE_ID` is available as an env var in this Lambda already (used elsewhere in `executor.py`/`builders.py` — check the exact env var name via `grep -n "CONNECT_INSTANCE_ID" services/api-plans/src/builders.py` before writing this handler, and import it the same way). `parse_body`/`json_response` come from `vip_shared.application.http`, already imported at the top of `handlers/plans.py`.

- [ ] **Step 1: Write the failing test**

First check the existing test file name for `handlers/plans.py`:

```bash
grep -rl "get_location_mapping\|handlers.plans\|handlers import plans" services/api-plans/tests/unit/
```

Add to whichever file that command finds (or create `test_handlers_plans.py` if none exists):

```python
def test_resolve_campaign_flow_returns_arn(monkeypatch):
    import handlers.plans as plans_handler

    monkeypatch.setattr(
        plans_handler.builders,
        "resolve_campaign_flow_arn",
        lambda states, instance_id: "arn:aws:connect:us-east-1:165505826690:instance/x/contact-flow/y",
    )
    event = {"body": '{"states": ["PA"]}'}
    resp = plans_handler.resolve_campaign_flow(event, {})
    assert resp["statusCode"] == 200
    import json
    body = json.loads(resp["body"])
    assert body["arn"] == "arn:aws:connect:us-east-1:165505826690:instance/x/contact-flow/y"


def test_resolve_campaign_flow_returns_null_when_not_found(monkeypatch):
    import handlers.plans as plans_handler

    monkeypatch.setattr(
        plans_handler.builders,
        "resolve_campaign_flow_arn",
        lambda states, instance_id: None,
    )
    event = {"body": '{"states": ["ZZ"]}'}
    resp = plans_handler.resolve_campaign_flow(event, {})
    assert resp["statusCode"] == 200
    import json
    body = json.loads(resp["body"])
    assert body["arn"] is None


def test_resolve_campaign_flow_requires_states(monkeypatch):
    import handlers.plans as plans_handler

    event = {"body": '{"states": []}'}
    resp = plans_handler.resolve_campaign_flow(event, {})
    assert resp["statusCode"] == 400
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd services/api-plans
python3 -m pytest tests/unit/ -k resolve_campaign_flow -v
```

Expected: `AttributeError: module 'handlers.plans' has no attribute 'resolve_campaign_flow'`.

- [ ] **Step 3: Check the exact `CONNECT_INSTANCE_ID` env var convention**

```bash
grep -n "CONNECT_INSTANCE_ID" services/api-plans/src/builders.py services/api-plans/src/executor.py
```

Use whatever exact module-level constant name that shows (e.g. `executor.CONNECT_INSTANCE_ID`) — import it the same way in `handlers/plans.py` rather than reading `os.environ` a second time with a different name.

- [ ] **Step 4: Write the implementation**

Add to `services/api-plans/src/handlers/plans.py`, after `get_location_mapping`:

```python
def resolve_campaign_flow(event: dict, _path_params: dict) -> dict:
    """Resolve (and auto-create if missing) the canonical campaign flow ARN
    for the given states. Thin wrapper over builders.resolve_campaign_flow_arn,
    which already has connect:CreateContactFlow — this just exposes it over HTTP
    so the frontend can stop guessing flow names client-side."""
    body = parse_body(event)
    states = body.get("states") or []
    if not states:
        return json_response(400, {"error": {"code": "BAD_REQUEST", "message": "states is required and must be non-empty"}})

    arn = builders.resolve_campaign_flow_arn(states, executor.CONNECT_INSTANCE_ID)
    return json_response(200, {"arn": arn})
```

Add `import executor` to the top of `handlers/plans.py` alongside the existing `import builders` / `import scheduler_manager` / `import store` block if it isn't already imported (check first — `executor.py` may already be imported elsewhere in this file's sibling handlers; if `CONNECT_INSTANCE_ID` actually lives on `builders` instead of `executor` per Step 3's grep result, import from wherever it actually is instead).

- [ ] **Step 5: Register the route**

In `services/api-plans/src/router.py`, add to the `ROUTES` dict, next to the existing `"GET /location-mapping"` entry:

```python
    "POST /plans/resolve-campaign-flow": plans_handler.resolve_campaign_flow,
```

- [ ] **Step 6: Wire the API Gateway route**

In `infra/lib/stacks/api-stack.ts`, find the existing `addRoutes` call(s) using `plansIntegration` (e.g. the one for `/plans`) and add a new one:

```typescript
    this.httpApi.addRoutes({
      path: '/plans/resolve-campaign-flow',
      methods: [apigatewayv2.HttpMethod.POST],
      integration: plansIntegration,
    });
```

Match the exact `authorizer`/`authorizationScopes` props the neighboring `/plans` route uses (read that route's full block first — do not omit auth if the existing routes have it).

- [ ] **Step 7: Run tests to verify they pass**

```bash
cd services/api-plans
python3 -m pytest tests/unit/ -k resolve_campaign_flow -v
```

Expected: 3 passed.

- [ ] **Step 8: Verify CDK synth**

```bash
cd infra
npx cdk synth VipAdminApiStack > /dev/null
echo "exit code: $?"
```

Expected: exit code 0. (Confirm the actual logical stack id for `api-stack.ts` via `grep -n "new ApiStack\|new cdk.Stack" infra/bin/app.ts` if `VipAdminApiStack` isn't the right name — use whatever name that grep shows.)

- [ ] **Step 9: Commit**

```bash
git add services/api-plans/src/handlers/plans.py services/api-plans/src/router.py infra/lib/stacks/api-stack.ts services/api-plans/tests/unit/
git commit -m "feat(api-plans): expose resolve_campaign_flow_arn over HTTP

New POST /plans/resolve-campaign-flow endpoint — thin wrapper over the
existing builders.resolve_campaign_flow_arn (already deployed, already
has connect:CreateContactFlow IAM, already used by the Plans executor).
Lets the frontend stop guessing campaign flow names client-side."
```

---

### Task 6: Frontend — wire `EnableCampaignModal.tsx` to the new endpoint

**Files:**
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/src/components/EnableCampaignModal.tsx`
- Modify: `frontend/src/components/EnableCampaignModal.test.ts`

**Interfaces:**
- Consumes: `POST /plans/resolve-campaign-flow` (Task 5), request `{"states": string[]}`, response `{"arn": string | null}`.
- Produces: nothing consumed by other tasks — this is the final task in the plan.

**Context:** `EnableCampaignModal.tsx` currently resolves `resolved.campaignFlow` via `suggestCampaignFlow(campaignFlows, segmentStates)` inside the `useMemo` at lines 107-121 (fixed earlier today for the CAMPAIGN/PA substring bug — keep `suggestCampaignFlow`/`STATE_FLOW_PATTERNS` exactly as they are, do not delete them). This task ADDS a backend call as the primary path and keeps the existing client-side heuristic as a fallback if the backend call fails (network error, auth error, etc.) — never regress to a worse UX if the endpoint is briefly unavailable. Use a `useQuery` the same way `queues`/`flows`/`phones` already do (`enabled: open`), so the endpoint call also only fires when the modal is actually opened.

- [ ] **Step 1: Add the frontend API client method**

In `frontend/src/lib/api.ts`, add to the `plans` object, next to `getLocationMapping`:

```typescript
    resolveCampaignFlow: (states: string[]) =>
      request<{ arn: string | null }>('/plans/resolve-campaign-flow', {
        method: 'POST',
        body: { states },
      }),
```

- [ ] **Step 2: Write the failing test for the new resolution priority**

Add to `frontend/src/components/EnableCampaignModal.test.ts`:

```typescript
import { describe, expect, it, vi } from 'vitest';

// ... (keep existing imports and tests for suggestCampaignFlow — add below them)

describe('resolveCampaignFlowArn (backend-first, client-heuristic fallback)', () => {
  it('prefers the backend-resolved ARN when available', async () => {
    const { resolveCampaignFlowArn } = await import('./EnableCampaignModal');
    const flows = [flow('campaign-CT')];
    const backendArn = 'arn:aws:connect:us-east-1:165505826690:instance/x/contact-flow/backend-resolved';
    const result = await resolveCampaignFlowArn(
      flows,
      ['PA'],
      async () => ({ arn: backendArn }),
    );
    expect(result).toBe(backendArn);
  });

  it('falls back to the client-side heuristic when the backend call throws', async () => {
    const { resolveCampaignFlowArn } = await import('./EnableCampaignModal');
    const flows = [flow('campaign-NY')];
    const result = await resolveCampaignFlowArn(
      flows,
      ['NY'],
      async () => {
        throw new Error('network error');
      },
    );
    expect(result).toBe(flows[0].arn);
  });

  it('falls back to the client-side heuristic when the backend returns null', async () => {
    const { resolveCampaignFlowArn } = await import('./EnableCampaignModal');
    const flows = [flow('campaign-NY')];
    const result = await resolveCampaignFlowArn(
      flows,
      ['NY'],
      async () => ({ arn: null }),
    );
    expect(result).toBe(flows[0].arn);
  });
});
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd frontend
npx vitest run src/components/EnableCampaignModal.test.ts
```

Expected: fails — `resolveCampaignFlowArn` is not exported.

- [ ] **Step 4: Write the implementation**

In `frontend/src/components/EnableCampaignModal.tsx`, add after `suggestCampaignFlow`:

```typescript
/**
 * Resolve the campaign flow ARN for the given states, preferring the
 * backend's resolve_campaign_flow_arn (which auto-creates the canonical
 * flow if missing) over the client-side name-search heuristic. Falls back
 * to the client-side result if the backend call fails or finds nothing —
 * never regress below what suggestCampaignFlow alone already provided.
 */
export async function resolveCampaignFlowArn(
  campaignFlows: ContactFlow[],
  states: string[],
  callBackend: (states: string[]) => Promise<{ arn: string | null }>,
): Promise<string | undefined> {
  const clientArn = suggestCampaignFlow(campaignFlows, states)?.arn;
  try {
    const { arn } = await callBackend(states);
    if (arn) return arn;
  } catch {
    // fall through to the client-side result
  }
  return clientArn;
}
```

Replace the `resolved` `useMemo` block's campaign-flow line. Currently:

```typescript
    const campaignFlow = suggestCampaignFlow(campaignFlows, segmentStates);
    return { queue, flow, phone, campaignFlow, isHighPriority };
```

This needs to become async-aware. Since `useMemo` can't be async, introduce a small `useEffect` + `useState` pair for the resolved ARN, keeping `resolved.campaignFlow` (the object, for display name) from the synchronous client-side heuristic as before, and adding a separate `effectiveCampaignFlowArn` that prefers the async-resolved backend ARN once it lands:

```typescript
  const [backendResolvedArn, setBackendResolvedArn] = useState<string | undefined>(undefined);

  useEffect(() => {
    if (!open || segmentStates.length === 0) return;
    let cancelled = false;
    resolveCampaignFlowArn(
      (flows.data?.contactFlows ?? []).filter((f) => f.contactFlowType === 'CAMPAIGN'),
      segmentStates,
      (states) => api.plans.resolveCampaignFlow(states),
    ).then((arn) => {
      if (!cancelled) setBackendResolvedArn(arn);
    });
    return () => {
      cancelled = true;
    };
  }, [open, flows.data, segmentStates]);
```

Then change the existing line:

```typescript
  const effectiveCampaignFlowArn = resolved.campaignFlow?.arn ?? '';
```

to:

```typescript
  const effectiveCampaignFlowArn = backendResolvedArn ?? resolved.campaignFlow?.arn ?? '';
```

Add `useEffect` to the existing `import { useMemo, useState, type ReactNode } from 'react';` line, and add `api` is already imported — confirm `resolveCampaignFlowArn` and `api.plans.resolveCampaignFlow` are both in scope.

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd frontend
npx tsc --noEmit
npx vitest run
```

Expected: `tsc` clean; full suite passes except the same pre-existing `chainMap.test.ts` baseline (3 failures) already known from earlier today — no new failures.

- [ ] **Step 6: Manual verification note**

This sandbox has no browser — per the established practice from earlier tasks today, a static trace substitutes for manual browser verification: re-read the final `EnableCampaignModal.tsx` diff and confirm (a) `effectiveCampaignFlowArn`'s dependents (the `errors` array check, `body` construction) still reference the same variable name unchanged, and (b) the modal never regresses to fewer capabilities than before this task (client-side fallback still fires on backend failure).

- [ ] **Step 7: Commit**

```bash
git add frontend/src/lib/api.ts frontend/src/components/EnableCampaignModal.tsx frontend/src/components/EnableCampaignModal.test.ts
git commit -m "feat(frontend): prefer backend resolve-campaign-flow over client heuristic

EnableCampaignModal now calls POST /plans/resolve-campaign-flow first
(auto-creates the canonical flow server-side if missing), falling back
to the existing suggestCampaignFlow substring search only if the
backend call fails or finds nothing. Closes the loop so future new
states never need a manual Connect console flow creation."
```

---

## Self-review notes (for the plan author, not a task)

- Task 3 Step 5's `cdk synth` and Task 5 Step 8's `cdk synth` both mutate no AWS resources — safe to run freely during implementation.
- Task 1's actual production write (Step 3) and Task 2's `update-table` are the only two genuinely irreversible-ish production mutations in this plan (Task 1's is fully reversible by re-running with different values; Task 2's Streams enable has no real downside and is not disableable-with-consequence). Both are called out explicitly in Global Constraints as needing ledger visibility and a read-back check — flag them to the user at the point of execution per this session's established practice, do not silently run them.
- No task deploys the new Lambda or the frontend to production — `cdk deploy`/`npm run build && s3 sync && cloudfront invalidate` are deliberately left out of this plan's tasks. Deploying is a separate, explicit step after the whole branch review, same as every other deploy today, and needs the user's go-ahead.