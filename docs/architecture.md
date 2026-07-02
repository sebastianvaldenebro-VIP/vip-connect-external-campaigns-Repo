# Architecture — VIP Connect Admin UI

End-to-end design for the admin UI that manages Customer Profiles segments and Outbound Campaigns from a single centralized interface, with a Plans orchestration engine for multi-bucket, multi-campaign automated workflows.

## 1. Problem & scope

VIP Medical Group's contact-center operators currently split their workflow between two interfaces:

1. **Amazon Connect console** — to create Customer Profiles segments and Outbound Campaigns
2. **CSV exports + manual reconciliation** — to understand how many profiles match a segment and to audit changes

Three concrete pain points drive this system:

| Pain point | Today's workaround | Business impact |
|---|---|---|
| Segment `lastComputedAt` lag of 24h+ | Operators manually re-create or snapshot-refresh segments | Campaigns dial leads already contacted / no longer qualified |
| No preview of segment member count pre-launch | Export CSV or launch a test campaign | Wasted agent time on empty segments or over-dialing |
| No centralized audit trail | AWS CloudTrail has events but not business context | HIPAA audit requirements force manual reconciliation |

This project delivers a React admin UI backed by AWS-native APIs that removes all three, plus a **Plans Orchestrator** that automates the daily campaign wave sequences that operators previously coordinated manually.

---

## 2. System context

```
                         ┌──────────────────────────┐
                         │   VIP CRM (source data)  │
                         └────────────┬─────────────┘
                                      │ periodic rebuild
                                      ▼
                         ┌──────────────────────────┐
                         │  ElastiCache Redis       │
                         │  wait_list:{team}:list   │
                         │  ingestion_dedup:{team}  │
                         └────────────┬─────────────┘
                            ┌─────────┴──────────┐
                            │ existing           │ new
                            ▼                    ▼
               ┌────────────────────┐   ┌─────────────────────────────┐
               │ connectcampaign    │   │  vip-external-campaigns-    │
               │ RedisSub.py        │   │  feeder Lambda              │
               │ Redis → Customer   │   │  Redis → StartOutbound      │
               │ Profiles (profiles)│   │  VoiceContact (direct dial) │
               └────────────┬───────┘   └─────────────────────────────┘
                            │
                            ▼
   ┌────────────────────────────────────────────────────────────────┐
   │         Amazon Connect Customer Profiles Domain                 │
   │         (amazon-connect-vipmedicalgroup)                        │
   │                                                                 │
   │  Profiles  →  Segment definitions  →  Outbound Campaigns (V2)  │
   └──────────────────────────────────────┬──────────────────────────┘
                                          │ manage via API
                                          ▼
                        ┌─────────────────────────────────────────┐
                        │        VIP Connect Admin UI             │
                        │                                         │
                        │  React SPA  (CloudFront + S3)           │
                        │      │                                  │
                        │      ▼                                  │
                        │  API Gateway HTTP API + Cognito JWT     │
                        │      │                                  │
                        │      ▼                                  │
                        │  6 Lambda services:                     │
                        │   - api-plans (Plans Orchestrator)      │
                        │   - api-segments                        │
                        │   - api-profiles                        │
                        │   - api-campaigns                       │
                        │   - api-metrics                         │
                        │   - feeder (no API Gateway)             │
                        └─────────────────────────────────────────┘
```

---

## 3. Runtime components

### 3.1 Existing (do not modify)

| Component | Location | Purpose |
|---|---|---|
| `connectcampaignRedisSub.py` | `projects/Connect-batch-redis-refactor/` | Consumes Redis list, writes to Customer Profiles via PutProfileObject |
| AWS Valkey | `master.prod-medwork-api.jrdc0s.use1.cache.amazonaws.com:6379` | Source of truth for lead lists, populated by CRM rebuild |
| Customer Profiles domain | `amazon-connect-vipmedicalgroup` | Profile storage with custom object type `leads-data-mapping` |
| Connect instance | `6b3f17ba-68a4-472a-9b20-db1991507009` | Voice routing, agent queues, outbound campaigns engine |

### 3.2 New (this project)

| Component | Tech | Purpose |
|---|---|---|
| Frontend SPA | React 18 + Vite + TS + Shadcn/ui + Tailwind + TanStack Query | Admin UI — segments, campaigns, plans, profiles, analytics |
| Hosting | S3 + CloudFront | Static SPA hosting; deploy via `aws s3 sync` + CloudFront invalidation |
| Auth | Cognito User Pool + Hosted UI + TOTP MFA | HIPAA-compliant auth, 15-min session timeout |
| API Gateway | HTTP API + Cognito JWT Authorizer | REST-ish endpoints for all domains |
| `api-plans` Lambda | Python 3.12 + VPC | Plans Orchestrator: DAG dispatch, bucket sequencing, warmup, chaining |
| `api-segments` Lambda | Python 3.12 + VPC | CRUD segments + `CreateSegmentEstimate` for on-demand recompute |
| `api-profiles` Lambda | Python 3.12 | `SearchProfiles` + `BatchGetProfile` read-only |
| `api-campaigns` Lambda | Python 3.12 | CRUD campaigns V2 + lifecycle (start/stop/pause/resume) |
| `api-metrics` Lambda | Python 3.12 | CloudWatch `AWS/Connect/Campaigns` + audit log queries |
| `feeder` Lambda | Python 3.12 + VPC | Redis → `StartOutboundVoiceContact` for real-time dial injection |
| DynamoDB `VipAdminPlans` | On-demand + KMS CMK + PITR | Plans definitions + run records (single-table design) |
| DynamoDB `AdminAuditLog` | On-demand + KMS CMK + TTL 6yr | Immutable audit trail for all operator actions |
| DynamoDB `SegmentFilterConfig` | On-demand + KMS CMK | Segment filter configuration presets |
| KMS CMK | `alias/prod/external-campaigns/data` | Encryption at rest for all new tables + CloudWatch Log Groups |
| SharedLayer | `vip-admin-shared` (CDK-managed) | Shared Python layer: `vip_shared` source + pip deps (redis, etc.) |
| SNS topic | `vip-admin-alerts` | Operational alerts → email to ops team |
| CloudWatch alarms | 14 alarms | Automated alerting for all critical failure modes |
| CloudWatch dashboard | `VipConnect-Admin-UI` | Operational visibility across all services |
| EventBridge rule | `vip-plans-prestart-check` | `rate(1 min)` rule that fires `{"action":"prestart_check"}` into api-plans |

---

## 4. Data flow — segment lifecycle

```
Operator action                AWS API called                    Storage
═══════════════════            ══════════════════                ═══════════

[Create segment]
  Form submit
  API POST /segments  ───────▶ CreateSegmentDefinition ────────▶ Customer Profiles
                                (segment type: CLASSIC)          Domain index

[Preview count]
  Click "Refresh"
  API POST                     CreateSegmentEstimate
  /segments/{id}/estimate      (async job) ────────────────────▶ internal store
  Poll every 2s                GetSegmentEstimate
                               → status: SUCCEEDED
                               → estimate.totalCount
  UI renders count

[Browse members]
  Click "View sample"
  API POST                     CreateSegmentSnapshot
  /segments/{id}/snapshot      (async export) ─────────────────▶ S3 bucket
  Poll                         GetSegmentSnapshot
                               → status: COMPLETED → object key
  Download first 20 rows

[Delete segment]
  API DELETE /segments/{id}    DeleteSegmentDefinition ─────────▶ gone
```

**Lag mitigation:** `CreateSegmentEstimate` forces the segment to recompute against live Profile Attributes on demand, bypassing the ~24h lag that Connect console shows.

---

## 5. Data flow — campaign lifecycle

```
Operator action                AWS API called                        Connect side effect
═══════════════════            ══════════════════                    ══════════════════════

[Create campaign]
  Form submit
  API POST /campaigns  ──────▶ CreateCampaign V2                   campaign = INITIALIZED
                               source.customerProfilesSegmentArn
                               channelSubtypeConfig.telephony
                               schedule, communicationTimeConfig

[Start]                        StartCampaign ─────────────────────▶ campaign = RUNNING
                                                                      → snapshot of segment
                                                                      → feeds ML predictive dialer

[Monitor]
  Dashboard auto-refresh       GetCampaignState + CloudWatch metrics
                               AWS/Connect/Campaigns namespace

[Pause]                        PauseCampaign ─────────────────────▶ campaign = PAUSED
[Resume]                       ResumeCampaign ────────────────────▶ campaign = RUNNING
[Stop]                         StopCampaign ──────────────────────▶ campaign = STOPPED
[Delete]                       DeleteCampaign (requires STOPPED) ─▶ gone
```

---

## 6. Plans Orchestrator

The Plans Orchestrator is the core scheduling system. It automates the daily call waves that operators previously coordinated manually across dozens of segments and campaigns.

### 6.1 Concepts

| Term | Definition |
|---|---|
| **Plan** | Named sequence of buckets, with a trigger (manual / time / on_plan_complete / loop) |
| **Bucket** | Group of campaigns that share queue/flow settings and run as a unit |
| **Campaign (def)** | A DAG node: states + group + attempts + `dependsOn` edges |
| **Run** | One execution instance of a plan; stores an immutable `planSnapshot` |
| **Tick** | EventBridge Scheduler fires `rate(1 min)` → Lambda polls running campaigns and dispatches newly unblocked ones |
| **Warmup** | Pre-creating a bucket's stage-0 campaigns ~5 min before the bucket starts, to avoid the ~5-min Connect dialer initialization lag |

### 6.2 Lambda event sources

```
API Gateway HTTP API
  routeKey → router.resolve() → handler function

EventBridge Scheduler (rate 1 min, one per active bucket)
  {"action": "tick", "planId": "...", "runId": "...", "bucketIndex": 0}

EventBridge rule vip-plans-prestart-check (rate 1 min, global)
  {"action": "prestart_check"}
  → scans all time-triggered plans, warms those starting within 4-6 min

EventBridge (chain trigger)
  {"action": "chain_trigger", "planId": "..."}

EventBridge Scheduler (daily scheduled run)
  {"action": "scheduled_run", "planId": "..."}
```

### 6.3 Pre-start warmup system

Connect campaigns require ~5 minutes to initialize the dialer. The warmup system pre-creates campaigns before their bucket becomes active, eliminating the cold-start lag.

**Within-plan warmup (`prestart_next: true`):** When a time-based bucket is within 5 min of expiry, the tick pre-creates stage-0 campaigns for the next bucket. Results are stored in `pendingWarmup` on the plan's META item.

**Cross-plan warmup:** When a plan nears completion, `_prestart_chained_runs` pre-warms the first bucket of any downstream `on_plan_complete`-triggered plans, and also pre-warms the same plan's first bucket if a loop cycle will follow.

**Time-triggered warmup (`prestart_check`):** The global `rate(1 min)` EventBridge rule fires `prestart_check` into api-plans every minute. The handler scans all `time`-triggered plans and warms any whose `trigger.time` is 4–6 minutes away (COT, UTC-5).

**Warmup consumption:** When `start_run` fires, if `pendingWarmup` is present, the pre-created campaigns are injected into the run's campaign states as `status: "warming"`, then `_activate_warming_bucket` transitions them to `running` directly.

**Error recovery:** If a warmup campaign ended up in `error` state (e.g., Redis was rebuilding), `_activate_warming_bucket` detects campaigns with `status: "error"` and no `connectCampaignId`, resets them to `"queued"`, and the normal dispatch loop starts them as cold campaigns.

### 6.4 Bucket run modes

| run_mode | Advance condition | Pre-start |
|---|---|---|
| `status_based` | All campaigns reach terminal status | n/a |
| `time_based` | `duration_minutes` elapsed from bucket start | Yes — 5 min before expiry, pre-create next bucket's stage-0 campaigns |

### 6.5 DAG dispatch rules

- Stage-0 campaigns (`dependsOn: []`) start as soon as their bucket is active.
- Stage-N campaigns start when **all** listed parents reach `completed` status.
- If any parent reaches `cancelled`, `error`, or `expired` **and does not have `exitReason: "skipped"`**, the child is immediately set to `cancelled` with `exitReason: "parent_cancelled"` (cascade).
- **Skipped campaigns** (`exitReason: "skipped"`) are set when a run starts with `startBucketIndex > 0`. Skipped campaigns are transparent — they neither trigger cascade-cancel nor block the all-parents-completed check.
- Cross-bucket `dependsOn` is supported: a campaign in bucket 1 may depend on a campaign in bucket 0.
- A fixed-point loop re-dispatches after every cascade event so newly unblocked campaigns start in the same tick.

### 6.6 Execution flow

```text
start_run(planId, startBucketIndex=0)
  ↓ load plan → validate (not template, trigger allowed, not locked)
  ↓ if pendingWarmup: inject pre-created campaigns → _activate_warming_bucket(0)
  ↓ else: _start_bucket(run, 0)  [earlier buckets set to cancelled+skipped]
  ↓ create EventBridge Scheduler tick rule (rate 1 min) for bucket 0
  ↓ save_run

tick(planId, runId, bucketIndex)          ← fires every minute
  ↓ acquire run lock (optimistic)
  ↓ for each running campaign → poll Connect → update status
  ↓ if loop.endTime defined: check COT time ≥ endTime → force_finish
  ↓ if time_based: check pre-start window (remaining ≤ 5 min) → prestart_next_bucket
  ↓ if time_based: check duration elapsed → _expire_bucket
  ↓ detect newly completed campaigns → _fire_campaign_chains (afterCampaign triggers)
  ↓ _dispatch_ready_campaigns (fixed-point: cascade + start unblocked)
  ↓ if all campaigns terminal → _advance_bucket
     → delete EventBridge Scheduler tick rule
     → if cleanup: delete Connect campaigns + CP segments
     → if next bucket has pendingWarmup → _activate_warming_bucket
     → else _start_bucket(run, bucketIndex+1)
     → if last bucket: run.status = "completed"
        → _prestart_chained_runs (warm downstream + loop self-restart)
        → start_run_chained (fire on_plan_complete chains)
  ↓ save_run, release lock

abort_run(planId, runId)
  ↓ stop all running campaigns in Connect
  ↓ delete all active EventBridge Scheduler tick rules
  ↓ cascade-cancel all queued/warming campaigns
  ↓ run.status = "aborted"
```

### 6.7 Trigger types

| type | Fires when | config fields |
|---|---|---|
| `manual` | Operator clicks "Run Now" in UI | — |
| `time` | Daily at specified COT (UTC-5) time | `time: "07:55"` |
| `on_plan_complete` | Upstream plan's last bucket completes | `planId`, `repeat: bool`, `afterBucket?: number`, `afterCampaign?: string` |

`afterBucket`: fire only after a specific bucket index completes (not whole-plan completion).
`afterCampaign`: fire only after a specific campaign ID completes within a bucket (overrides `afterBucket` for matching; `afterBucket` still stored for display).

`loop.endTime`: if set on a plan, the tick checks current COT time against `endTime`. When COT ≥ endTime, the run is force-finished without looping. Loop is COT (UTC-5, no DST) year-round.

Cycle detection runs on save: BFS from `trigger.planId` through the plan graph; rejects with 400 if a cycle is found.

### 6.8 API routes (api-plans)

| Method | Path | Handler |
|---|---|---|
| GET | `/plans` | `list_plans` |
| POST | `/plans` | `create_plan` |
| GET | `/plans/{id}` | `get_plan` |
| PUT | `/plans/{id}` | `update_plan` |
| DELETE | `/plans/{id}` | `delete_plan` |
| GET | `/templates` | `list_templates` |
| POST | `/plans/from-template/{tid}` | `clone_from_template` |
| POST | `/plans/{id}/runs` | `trigger_run` |
| GET | `/plans/{id}/runs` | `list_runs` |
| GET | `/plans/{id}/runs/{runId}` | `get_run` |
| POST | `/plans/{id}/runs/{runId}/abort` | `abort_run` |

Internal event actions (not HTTP): `tick`, `scheduled_run`, `chain_trigger`, `prestart_check`.

---

## 7. Authentication & authorization flow

```
Browser                    CloudFront/S3           Cognito              API Gateway           Lambda
═════════                  ═════════════           ═══════              ═══════════           ═══════

GET /                      ◄── index.html ──
                           (SPA loads)

                           Check session
                           (no token) ──▶ Redirect to Cognito Hosted UI

Login + MFA code ─────────────────────▶ Authenticate
                                        ◄── ID/Access/Refresh tokens ──

GET /api/segments
  Authorization: Bearer <access_token>
                                                     JWT Authorizer
                                                     validates against
                                                     Cognito JWKS
                                                     ◄── 401 or pass ──

                                                     (if valid) ──────▶ Lambda handler
                                                                          Extract user sub
                                                                          Business logic
                                                                          Audit log insert
                                                                          ◄── response ──
```

**Session timeout:** Cognito access tokens expire in 1h. The UI forces re-auth after 15 min idle (HIPAA requirement). Refresh token valid 24h.

---

## 8. HIPAA-relevant design choices

| Requirement | Implementation |
|---|---|
| Encryption at rest | All DynamoDB tables SSE-KMS with CMK `alias/prod/external-campaigns/data` (annual rotation enabled) |
| Encryption in transit | API Gateway enforces TLS 1.2+; Cognito tokens over HTTPS |
| Unique user ID + MFA | Cognito User Pool, `mfa_configuration = "ON"`, TOTP required |
| Session timeout 15 min | UI checks `lastActivity` on every route; forces re-login after 900s idle |
| No PHI in logs | `StructuredLogger` (shared layer) SHA-256-hashes phone/name/email/SSN fields |
| Audit trail ≥ 6 years | `AdminAuditLog` DynamoDB table, `ttl` = 6 years ahead, deletion protection ON |
| Least-privilege IAM | Each Lambda has a scoped execution role; all roles have `EngineeringPermissionBoundary` |
| VPC isolation | `api-plans`, `api-segments`, and `feeder` are VPC-attached (need Redis access). `api-campaigns`, `api-profiles`, `api-metrics` call AWS APIs only — no VPC required. |
| Dedicated account for PHI | Account `165505826690` already holds the Customer Profiles domain; no cross-account data movement |
| Breach readiness | CloudTrail all-regions; GuardDuty at org level; SNS alerts on Lambda errors and DynamoDB system errors |

---

## 9. Non-functional targets

| Dimension | Target | Notes |
|---|---|---|
| UI load time | < 3s first load (including Cognito round-trip) | CloudFront edge caching |
| Segment estimate latency | p95 < 90s click-to-count | `CreateSegmentEstimate` is async — UI polls every 2s |
| Campaign create/start | < 10s end-to-end | Single Lambda call |
| Metrics dashboard refresh | 60s auto-refresh, < 5s render | Cached GetMetricStatistics responses |
| Plans tick latency | < 30s from campaign completion to next campaign start | EventBridge Scheduler fires every 60s; dispatch is in-tick |
| Concurrency | Up to 10 operators simultaneously | Cognito + API GW scale trivially |
| HIPAA audit | 6-year retention, immutable records | DynamoDB TTL + deletion protection |

---

## 10. What's out of scope

- Email / SMS channels in campaigns (voice only for now)
- Multi-tenant / org-level RBAC (single admin group currently)
- Custom dashboards beyond the fixed Analytics screen
- Write operations to Customer Profiles (profiles are read-only from this UI; populated only by `connectcampaignRedisSub`)
