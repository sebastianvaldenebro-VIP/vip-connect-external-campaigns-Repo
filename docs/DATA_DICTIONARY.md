# Data Dictionary

Complete reference for every persisted record in the **VIP Connect External Campaigns** platform.

---

## 1. DynamoDB Table: `VipAdminPlans`

Single-table design. Two record kinds share the same partition.

| Attribute | Type | Purpose |
|---|---|---|
| `pk` | String | Partition key. Always `PLAN#{planId}`. |
| `sk` | String | Sort key. `META` for plan, `RUN#{epoch_ms}-{uuid8}` for run. |

Sort key shape `RUN#{epoch_ms}-{uuid8}` gives chronological lexicographic ordering — `ScanIndexForward=False` returns most recent run first.

---

### 1.1 Plan META record (`pk=PLAN#{planId}`, `sk=META`)

| Field | Type | Required | Description | PHI |
|---|---|---|---|---|
| `planId` | String | Yes | UUID4 | No |
| `name` | String | Yes | Operator-visible name | No |
| `description` | String | No | Optional notes | No |
| `trigger` | Map | Yes | `{type, ...}` — see below | No |
| `loop` | Map | No | `{startTime?, endTime}` (COT HH:MM) | No |
| `workingHours` | Map | No | `{days:[MON,...], startTime, endTime}` | No |
| `buckets` | List | Yes | Array of `BucketDef` | No |
| `isTemplate` | Boolean | No | Hides plan from active runs queue if true | No |
| `isDefault` | Boolean | No | Operator-facing default selection flag | No |
| `pendingWarmup` | Map | No | `{campaigns:[{campaignId, connectCampaignId, segmentName, segmentArn, warmupStarted}]}` — cleared on consumption | No |
| `runLock` | String | No | `runId` of an active run; blocks concurrent triggers | No |
| `createdAt` | String | Yes | ISO 8601 UTC | No |
| `updatedAt` | String | Yes | ISO 8601 UTC | No |

#### `trigger` shapes

| Type | Fields |
|---|---|
| `manual` | none |
| `time` | `time:"HH:MM"` COT, `repeat?:bool` |
| `on_plan_complete` | `planId:string`, `repeat:bool`, `afterBucket?:int`, `afterCampaign?:string` |

#### `BucketDef`

| Field | Type | Description |
|---|---|---|
| `id` | String | UUID4 |
| `name` | String | Display name |
| `run_mode` | String | `time_based` (uses `run_duration_minutes`) or `status_based` (advances when all campaigns terminal) |
| `prestart_next` | Boolean | Whether to pre-warm next bucket (default `true`) |
| `parallel` | Boolean | If `true`, starts in parallel with previous bucket |
| `cleanup` | Boolean | Whether the bucket cleans up created segments on exit |
| `duration_minutes` | Integer | Soft / display value |
| `run_duration_minutes` | Integer | Hard time cap; triggers `_expire_bucket` |
| `reconcileRetryLimit` | Integer | Max empty-segment retries (default 5; legacy plans may carry 1 — see TD-005) |
| `onReconcileExhausted` | String | `continue` or `fail` |
| `campaignConfig` | Map | Default Connect config for all campaigns in this bucket |
| `campaigns` | List | Array of `CampaignDef` |

#### `CampaignDef`

| Field | Type | Description |
|---|---|---|
| `id` | String | UUID4 |
| `name` | String | Display name |
| `states` | List[String] | Geographic codes: `NY`, `LI`, `NJ`, `CT`, `MD`, `TX`, `NCA`, `SCA` |
| `groups` | List[String] | Segment group labels, e.g. `"Cancellation / 1st attempt"` |
| `run_type` | String | `full` (until daily 19:00 cutoff) or `custom` (use `run_duration_minutes`) |
| `run_duration_minutes` | Integer | Used when `run_type=custom` |
| `dependsOn` | List[String] | Campaign IDs this must wait for. Empty = stage-1. Cross-bucket allowed (AND semantics). |
| `deliveryType` | String | `campaign` (Connect MANAGED) or `journey` (Connect JOURNEY) |
| `campaignConfig` | Map | Overrides bucket-level config when present |
| `pinnedSegmentArn` | String | If set, skip Redis lookup; use this segment ARN directly |

`campaignConfig`:
```
{
  queueId: string,
  contactFlowId?: string,           // optional override; otherwise resolved by name
  sourcePhoneNumber: string,
  dialerType: "PREDICTIVE" | "PROGRESSIVE",
  bandwidthAllocation: float,        // 0.0 .. 1.0
  dialingCapacity: number,
  amdEnabled: boolean,
  amdAwaitPrompt: boolean,
  ringTimeout?: number               // seconds
}
```

---

### 1.2 Run record (`pk=PLAN#{planId}`, `sk=RUN#{runId}`)

| Field | Type | Description |
|---|---|---|
| `runId` | String | `{epoch_ms}-{uuid8}` |
| `planId` | String | Parent plan id |
| `status` | String | `running`, `completed`, `cancelled`, `failed` |
| `startedAt` | String | ISO 8601 UTC |
| `completedAt` | String | ISO 8601 UTC |
| `triggeredBy` | String | `scheduled`, `manual`, `chained` |
| `currentBucketIndex` | Integer | Index of the bucket currently advancing |
| `planSnapshot` | Map | Frozen copy of the Plan at run start; executor reads this, not the live plan |
| `bucketStates` | List | Array of `BucketState` |
| `error` | String | Free-text reason if `status=failed` |
| `scheduleName` | String | Legacy run-level rule name (migrated to per-bucket on read) |
| `_version` | Integer | Optimistic locking counter, increments on every save |

#### `BucketState`

| Field | Type | Description |
|---|---|---|
| `bucketId` | String | Matches `BucketDef.id` |
| `name` | String | Cached display name |
| `status` | String | `queued`, `warming`, `running`, `completed`, `cancelled`, `expired`, `error` |
| `startedAt` | String | Set at activation time (NOT pre-warm time) |
| `completedAt` | String | ISO 8601 |
| `scheduleName` | String | EventBridge rule `vip-plan-{runId}-{idx}` |
| `campaignStates` | List | Array of `CampaignState` |

#### `CampaignState`

| Field | Type | Description |
|---|---|---|
| `campaignId` | String | Matches `CampaignDef.id` |
| `name` | String | Cached display name |
| `status` | String | `queued`, `warming`, `creating`, `running`, `completed`, `cancelled`, `expired`, `error` |
| `connectCampaignId` | String | Amazon Connect campaign UUID |
| `segmentName` | String | Customer Profiles segment definition name (timestamp-encoded) |
| `segmentArn` | String | ARN of the segment definition |
| `leadCount` | Integer | Estimated lead count when known |
| `startedAt` | String | ISO 8601 |
| `completedAt` | String | ISO 8601 |
| `exitReason` | String | See enum below |
| `errorDetail` | String | Human-readable error if `exitReason=error` |
| `reconcileRetries` | Integer | Count of empty-segment retries |
| `warmupStarted` | Boolean | True while pre-warmed; removed after activation |
| `creatingAt` | String | Set when status enters `creating`; used to time out stale claims after 300s |

#### `exitReason` enum

| Value | Meaning |
|---|---|
| `completed` | Connect reported COMPLETED |
| `stopped` | StopCampaign called (operator / 19:00 cutoff) |
| `expired` | Campaign `run_duration_minutes` elapsed |
| `bucket_expired` | Bucket time cap reached |
| `error` | Unrecoverable error; see `errorDetail` |
| `skipped_empty` | Reconcile exhausted; segment had no members |
| `reconcile_failed` | Segment did not materialise within retries |
| `creation_failed` | CreateCampaign or StartCampaign threw |
| `cancelled` | Abort by operator |
| `parent_cancelled` | Parent campaign cancelled and dependent treated cancelled |
| `aborted` | Run aborted before campaign could start |
| `cutoff_too_close` | Less than 6 minutes to 19:00 COT; refused to start |

---

## 2. PHI Data Map

| Field | Where stored | Where logged | Notes |
|---|---|---|---|
| Patient phone number | ElastiCache Redis (`wait_list:{team}:list`) and within Customer Profiles | **Never** in CloudWatch | `StructuredLogger._scrub` replaces `phone`, `phone_number` with `*_hash` (SHA-256 / first 12 chars) |
| Patient first/last name | Customer Profiles only | **Never** | `first_name`, `last_name`, `fullname` are scrubbed |
| `lead_id` | Internally referenced | Logged as `lead_id_hash` + 8-char `lead_id_prefix` | Hash + prefix only |
| Segment definition name | DynamoDB (`segmentName`) | Yes (operator name + timestamp, no PHI) | Names are constructed from state code + group + timestamp |
| Connect campaign ID | DynamoDB (`connectCampaignId`) | Yes (UUID, no PHI) | OK to log |

PHI is **never** in DynamoDB. Names and phone numbers stay inside Connect + Customer Profiles only.

---

## 3. Lambda × Table × Operations Matrix

| Lambda | `VipAdminPlans` | `VipAdminAudit` | Redis | Customer Profiles | Connect |
|---|---|---|---|---|---|
| `api-plans` | Read+Write (plans, runs) | Write (audit) | Read | Create + Get + Delete + List segments | Create + Start + Stop + Get + Describe + List campaigns; List/Create contact flows |
| `api-campaigns` | — | Write | — | Get + List segments | Full CRUD on campaigns |
| `api-metrics` | Read | Read + Write | — | — | — |
| `api-profiles` | — | Write | — | CRUD on profiles + segments | — |
| `api-segments` | Read | Write | Read | CRUD on segments | — |

---

## 4. Access Patterns

| Pattern | Operation | Cost notes |
|---|---|---|
| Get plan by id | `GetItem pk=PLAN#{id} sk=META` | 1 RCU |
| List plans | `Scan FilterExpression sk=META` | One scan per call; plan table is < 200 items so acceptable |
| Get run by id | `GetItem pk=PLAN#{id} sk=RUN#{runId}` | 1 RCU |
| List recent runs of a plan | `Query pk=PLAN#{id} sk begins_with(RUN#) ScanIndexForward=False Limit=20` | Cheap |
| Latest run of plan | Same as above with `Limit=1` | Cheap |
| Find plans triggered by another plan | `Scan` then filter on `trigger.type=on_plan_complete AND trigger.planId=…` | Scan; plan table small |
| Optimistic write on run | `PutItem ConditionExpression="attribute_not_exists(_version) OR _version = :v"` | Throws `ConcurrentWriteError` on conflict |
| Set/clear `pendingWarmup` | `UpdateItem SET pendingWarmup` or `REMOVE pendingWarmup` | Single update |
| Lock plan against concurrent runs | `UpdateItem SET runLock=:r ConditionExpression="attribute_not_exists(runLock)"` | Throws `ValueError` if already locked |

---

## 5. Redis Data

| Key pattern | Type | Purpose | PHI |
|---|---|---|---|
| `wait_list:{team}:list` | Hash | Map of `lead_id` → JSON payload containing `phone_number`, `first_name`, `last_name`, segment group labels, source state location | **YES (phones + names)** |

- Populated by an **external feeder pipeline** (out of scope for this repo).
- `RedisLeadSource.fetch_for_filters(state_codes, groups)` performs server-side filtering through batched `HSCAN`.
- Lead lists are mid-day refreshable; reconcile retries handle the race where the segment is queried before the feeder catches up.

---

## 6. VipAdminAudit Table

Owned by other services but produced by api-plans through `vip_shared.infrastructure.persistence.audit`:

| Field | Type | Notes |
|---|---|---|
| `pk` | String | `AUDIT#{entityType}#{entityId}` |
| `sk` | String | `{timestamp}#{uuid}` |
| `actorSub` | String | Cognito sub |
| `actorEmail` | String | Cognito email |
| `action` | String | e.g. `start`, `abort`, `create` |
| `before` | Map | Optional pre-image |
| `after` | Map | Optional post-image |
| `ipAddress` | String | From API Gateway |
| `userAgent` | String | From API Gateway |
| `timestamp` | String | ISO 8601 |

PHI is never written to audit records.
