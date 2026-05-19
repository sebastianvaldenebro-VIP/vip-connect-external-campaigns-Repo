# Data Model — VIP Connect Admin UI

---

## 1. DynamoDB tables

### 1.1 `AdminAuditLog`

Immutable audit trail for all operator actions.

```
PK  entity_id     STRING   "plan/<uuid>", "segment/<uuid>", "campaign/<uuid>"
SK  timestamp     STRING   ISO 8601 (sortable)

Attributes:
  action         STRING   create | update | delete | start | stop | pause | resume | abort |
                          estimate | clone_template | …
  actor_sub      STRING   Cognito user sub (immutable)
  actor_email    STRING   Cognito email (display only)
  ip_address     STRING   API Gateway source IP
  user_agent     STRING
  before         MAP      entity state before (null on create)
  after          MAP      entity state after (null on delete)
  ttl            NUMBER   Unix epoch 6 years ahead (DynamoDB TTL)
```

Encryption: SSE-KMS, CMK `alias/prod/external-campaigns/data`.
Deletion protection: enabled. PITR: enabled.

---

### 1.2 `VipAdminPlans`

Single-table design for Plans and Runs.

```
PK   pk    STRING   "PLAN#<planId>"
SK   sk    STRING   "META"                    → Plan definition item
                    "RUN#<epochMs>-<uuid8>"   → Run record item
```

Encryption: SSE-KMS, same CMK. PITR: enabled.

---

## 2. Plan definition item (`sk = "META"`)

```json
{
  "pk": "PLAN#a1b2c3d4",
  "sk": "META",
  "planId": "a1b2c3d4",
  "name": "NY Morning Wave",
  "description": "Daily morning calls for NY leads",
  "trigger": {
    "type": "time",
    "time": "07:55"
  },
  "loop": {
    "endTime": "19:00"
  },
  "isTemplate": false,
  "isDefault": false,
  "isLocked": false,
  "pendingWarmup": null,
  "buckets": [ ... ],
  "createdAt": "2026-05-01T12:00:00Z",
  "updatedAt": "2026-05-08T09:30:00Z"
}
```

### `trigger` shapes

```json
{ "type": "manual" }

{ "type": "time", "time": "07:55" }

{
  "type": "on_plan_complete",
  "planId": "upstream-plan-id",
  "repeat": true
}

{
  "type": "on_plan_complete",
  "planId": "upstream-plan-id",
  "repeat": false,
  "afterBucket": 1
}

{
  "type": "on_plan_complete",
  "planId": "upstream-plan-id",
  "repeat": false,
  "afterBucket": 1,
  "afterCampaign": "c2"
}
```

- `time` values are in **COT (UTC-5, no DST)** year-round. Do not use Eastern time.
- `afterBucket`: fire only after the specified bucket index of the upstream plan completes.
- `afterCampaign`: fire only after the specified campaign ID completes within a bucket. Takes precedence over `afterBucket` for matching; `afterBucket` is still stored for display purposes.
- Cycle detection runs on save: BFS from `trigger.planId`; rejects with 400 if a cycle is found.

### `loop` field

```json
{ "endTime": "19:00" }
```

`endTime` is in COT. When `loop` is present and `endTime` is set, the tick checks COT time on every invocation. When `now_cot >= endTime`, the run is force-finished without starting a new loop cycle. `_maybe_loop` also uses `endTime` to decide whether to re-trigger the plan after a natural completion.

### `pendingWarmup` field

Stores pre-created Connect campaign data written by the warmup system before the plan's first bucket starts.

```json
{
  "campaigns": [
    {
      "campaignId": "c1",
      "connectCampaignId": "cmp_xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
      "segmentArn": "arn:aws:profile:us-east-1:165505826690:domains/...",
      "leadCount": 412
    }
  ]
}
```

`pendingWarmup` is `null` when no warmup is pending. It is written by `_prestart_plan` and consumed (then cleared to `null`) by `start_run`.

### `isLocked` field

Set to `true` by `start_run` at the start of execution; cleared when the run completes or is aborted. Guards against double-starts when multiple EventBridge events fire concurrently.

### Bucket definition (`BucketDef`)

```json
{
  "id": "b1",
  "name": "NY Morning",
  "run_mode": "time_based",
  "duration_minutes": 45,
  "cleanup": true,
  "prestart_next": true,
  "parallel": false,
  "campaignConfig": {
    "queueId": "fc5e3102-44f1-4986-baaa-055ee92e0a98",
    "sourcePhoneNumber": "+19734949660",
    "dialerType": "progressive",
    "contactFlowId": "3d24320b-c1e3-40f3-90a2-b6867ef70c85",
    "campaignFlowArn": "arn:aws:connect:us-east-1:165505826690:instance/.../contact-flow/..."
  },
  "campaigns": [ ... ]
}
```

| Field | Values | Notes |
|---|---|---|
| `run_mode` | `time_based`, `status_based` | Aliases `time-based`/`status-based` accepted on write |
| `duration_minutes` | integer | Only meaningful for `time_based` |
| `cleanup` | bool | Delete Connect campaigns + CP segments when bucket completes |
| `prestart_next` | bool | Pre-create next bucket's stage-0 campaigns 5 min before expiry (`time_based` only) |
| `parallel` | bool | Start this bucket simultaneously with previous bucket (not after it completes) |

### Campaign definition (`CampaignDef`)

```json
{
  "id": "c1",
  "name": "NY New Lead 1",
  "states": ["NY"],
  "group": "New lead",
  "attempts": [1],
  "run_type": "full",
  "dependsOn": []
}
```

| Field | Values | Notes |
|---|---|---|
| `states` | `NY`, `NJ`, `LI`, `CT`, `MD`, `TX`, `NCA`, `SCA` | Multi-select; filters by `location` attribute in Customer Profiles |
| `group` | `New lead`, `Cancellation`, `No show`, `Follow-up`, `Reschedule` | Maps to `groups` attribute |
| `attempts` | `[1]`, `[2, 4]`, etc. | Maps to `groups` filter: `"New Lead / 1st Attempt"` etc. |
| `run_type` | `full`, `time_45`, `time_90`, `time_120`, `time_180` | Determines campaign `endTime` override (`full` = bucket end) |
| `dependsOn` | `["c1", "c2"]` | Campaign IDs (same or earlier bucket only); AND semantics |

---

## 3. Run record item (`sk = "RUN#..."`)

```json
{
  "pk": "PLAN#a1b2c3d4",
  "sk": "RUN#1746700800000-ab12cd34",
  "planId": "a1b2c3d4",
  "runId": "run-ab12cd34",
  "status": "running",
  "triggeredBy": "manual",
  "startBucketIndex": 0,
  "planSnapshot": { /* full Plan definition at trigger time — immutable */ },
  "currentBucketIndex": 0,
  "bucketStates": [ ... ],
  "startedAt": "2026-05-08T13:00:00Z",
  "completedAt": null
}
```

`triggeredBy` values: `"manual"`, `"chained"`, `"scheduled"`, `"loop"`.

`startBucketIndex`: the bucket index the run started at. Buckets before this index have all campaigns set to `status: "cancelled"` with `exitReason: "skipped"`.

`planSnapshot` is written once at trigger time and never updated. The executor reads exclusively from `planSnapshot` during a run, so editing the live plan definition does not affect an in-progress run.

### Bucket state

```json
{
  "bucketId": "b1",
  "name": "NY Morning",
  "status": "running",
  "scheduleName": "vip-plan-a1b2c3d4-run-ab12cd34-b0",
  "startedAt": "2026-05-08T13:00:00Z",
  "completedAt": null,
  "campaignStates": [ ... ]
}
```

Bucket status values: `queued` → `warming` → `running` → `completed`

`scheduleName` is the EventBridge Scheduler schedule name for the `rate(1 min)` tick rule. Deleted when the bucket completes or the run is aborted.

### Campaign state

```json
{
  "campaignId": "c1",
  "name": "NY New Lead 1",
  "status": "queued",
  "connectCampaignId": null,
  "segmentName": "4-5-NY-NL_1-0942",
  "segmentArn": "arn:aws:profile:...",
  "leadCount": null,
  "startedAt": null,
  "completedAt": null,
  "exitReason": null,
  "errorDetail": null
}
```

### Campaign status state machine

```text
                              ┌──────────────────────────────────────────┐
                              │ [set by start_run when startBucketIndex  │
                              │  > bucket's index]                       │
         queued ──────────────▶ cancelled  (exitReason: "skipped")       │
           │                  │  transparent to downstream dependsOn     │
           │                  └──────────────────────────────────────────┘
           │
           ├─── (dependsOn empty OR all parents completed/skipped,
           │     bucket active) ─────────────────────────────────▶ running
           │                                                          │
           ├─── (pre-start window, dependsOn empty) ──▶ warming       ├── Connect COMPLETED ──▶ completed
           │         │                                                 │
           │         └── (bucket activates) ─────────▶ running        ├── Connect STOPPED/FAILED ──▶ error
           │                                                           │
           ├─── (any parent cancelled/error/expired                    └── (time_based expires) ──▶ expired
           │     AND exitReason != "skipped") ──▶ cancelled
           │         exitReason: "parent_cancelled"
           │
           └─── (bucket expires while queued) ──▶ cancelled
                 exitReason: "bucket_expired"
```

**Terminal statuses:** `completed`, `cancelled`, `error`, `expired`

**`exitReason` values:**

| exitReason | Set when |
|---|---|
| `"skipped"` | `start_run` was called with `startBucketIndex > bucket_index` |
| `"parent_cancelled"` | A parent campaign reached a cancel-family status (and was not itself skipped) |
| `"bucket_expired"` | The bucket's `duration_minutes` elapsed while this campaign was still `queued` |
| `"redis_rebuilding"` | Redis list was empty after 3 retry attempts during pre-warming (transient failure) |

---

## 4. Segment naming convention

Segment names are constructed by `builders.build_segment_name(bucket, campaign)`.

Format: `<weekday><hour>-<states>-<group_abbr>_<attempt_str>-<suffix>`

Examples:

| Campaign | Segment name |
|---|---|
| States=NY, group=New lead, attempts=[1] | `4-5-NY-NL_1-0942` |
| States=NJ+CT, group=Cancellation, attempts=[2,4] | `4-5-NJ_CT-CL_2_4-0942` |
| States=LI, group=No show, attempts=[1] | `4-5-LI-NS_1-0942` |

Group abbreviations:

| Group | Abbreviation |
|---|---|
| New lead | `NL` |
| Cancellation | `CL` |
| No show | `NS` |
| Follow-up | `FU` |
| Reschedule | `RS` |

The suffix is a 4-digit COT timestamp (`HHmm`) appended at creation time to ensure uniqueness when the same segment is created multiple times on the same day.

---

## 5. Segment filter mapping

`builders.campaign_to_segment_filters(campaign)` translates a `CampaignDef` into the Customer Profiles segment filter structure:

```python
{
  "state": campaign["states"],         # ["NY", "NJ"]
  "groups": [                          # e.g. ["New Lead / 1st Attempt", "New Lead / 2nd Attempt"]
    f"{group_label} / {ordinal} Attempt"
    for n in campaign["attempts"]
  ],
  "attempts": [],
  "available": campaign.get("available", ""),
}
```

Group label map (canonical form used in Customer Profiles):

| Input (`group`) | Canonical label |
|---|---|
| `New lead` | `New Lead` |
| `Cancellation` | `Cancellation` |
| `No show` | `No Show` |
| `Follow-up` | `Follow Up` |
| `Reschedule` | `Reschedule` |

---

## 6. Backward compatibility

Plans created before the v2 schema (single `segmentFilters` + `campaignConfig` per bucket, no `campaigns` array) are still readable. On read, `store._plan_from_item` detects the absence of a `campaigns` field on a bucket and synthesizes a single-element `campaigns` array from the legacy fields, so old plans can still be triggered and executed.

New plans always write the `campaigns` array explicitly.
