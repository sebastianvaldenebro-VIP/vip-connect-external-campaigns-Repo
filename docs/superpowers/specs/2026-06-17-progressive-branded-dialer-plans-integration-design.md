# Progressive Branded Dialer — Plans Integration Design

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Integrate the Progressive Branded Dialer into the existing Plans system so that a campaign within a Plan bucket can use the branded dialing channel — with the same UI, bucket/time-expiry behavior, and Redis-leads-as-primary-segment-source that existing campaigns use today.

**Status:** Design approved. Pre-deploy fixes required on the standalone `ApiProgressiveDialerStack` before this integration is deployed (see Section 6).

---

## Section 1: Architecture

### Current state

The Plans system (`api-plans`) orchestrates Plan → Buckets → Campaigns via `executor.py`. When a bucket becomes active, `_start_one_campaign()` calls the Connect Campaigns V2 API (`outbound_campaigns_client`) to create and start outbound dialing. The executor polls campaign state via Connect V2 and advances buckets when all campaigns are terminal.

The Progressive Branded Dialer (`api-progressive-dialer`) runs as an independent stack:
- **Seeder Lambda** (`handler_seeder.py`): seeds `VipProgressiveCampaignQueue` from a Customer Profiles segment.
- **Consumer Lambda** (`handler_consumer.py`): Kinesis-triggered on agent `STATE_CHANGE`; dequeues a contact, pushes First Orion INFORM, enqueues to SQS with 22s delay.
- **Caller Lambda** (`handler_caller.py`): SQS-triggered; fires `StartOutboundVoiceContact`.

### Integration design

The executor becomes the **single orchestrator** for both channel types. A new campaign variant — identified by `deliveryType: "branded"` — bifurcates the executor flow:

```
executor._start_one_campaign()
  ├─ deliveryType == "branded"  → invoke Seeder + write VipActiveBrandedCampaigns
  └─ (default)                  → Connect Campaigns V2 (unchanged)

executor.tick()
  ├─ connectCampaignId present  → poll Connect V2 (unchanged)
  └─ brandedCampaignId present  → poll VipProgressiveCampaignQueue count

All abort/stop/expire paths
  ├─ connectCampaignId present  → _safe_stop/_safe_delete Connect (unchanged)
  └─ brandedCampaignId present  → _stop_branded_campaign()
```

**New DynamoDB table** (`VipActiveBrandedCampaigns`): tracks active branded campaigns per agent queue. One-to-many design (PK=`QUEUE#{queueArn}`) allows multiple concurrent branded campaigns on the same queue, matching Connect V2 `bandwidthAllocation` behavior.

**Consumer change**: replaces static `ACTIVE_CAMPAIGN_ID` env var with a dynamic `Query` on `VipActiveBrandedCampaigns` GSI (`queueArn-index`), selecting campaigns by `priority ASC, createdAt ASC`.

### Invariants

- Existing `predictive`/`progressive`/`agentless` Connect V2 campaigns are **completely unaffected**. All bifurcations are additive `if _is_branded()` branches; the existing path has no changes.
- `dialerType` inside `campaignConfig` continues to mean the Connect V2 dialer mode. It is **not** the branded discriminator (doing so would inject `"PROGRESSIVE_BRANDED"` as a literal key into the Connect V2 `outboundMode` JSON, causing `ValidationException`).
- The executor does NOT prevent multiple branded campaigns sharing the same queue. The consumer resolves priority.

---

## Section 2: Data Model

### New field on campaign (plan store)

```json
{
  "deliveryType": "branded",
  "campaignConfig": {
    "dialerType": "progressive",
    "queueArn": "arn:aws:connect:us-east-1:165505826690:instance/.../queue/...",
    "contactFlowId": "3d24320b-c1e3-40f3-90a2-b6867ef70c85",
    "sourcePhone": "+19174105649",
    "bandwidthAllocation": 1.0
  }
}
```

`deliveryType` lives at the same level as `campaignConfig`. When absent, defaults to `"connect"` (existing behavior). Required validation: if `deliveryType == "branded"`, then `queueArn`, `contactFlowId`, and `sourcePhone` are required inside `campaignConfig`.

### New field on campaign state (`_initial_campaign_state`)

```python
"brandedCampaignId": None,   # set when branded campaign starts
"queueArn":          None,   # copied from campaignConfig on start (needed for cleanup)
```

`connectCampaignId` remains `None` for branded campaigns throughout their lifecycle.

### New DynamoDB table: `VipActiveBrandedCampaigns`

```
PK  = "QUEUE#{queueArn}"          (string)
SK  = "CAMPAIGN#{campaignId}"     (string)

Attributes:
  campaignId      string   — matches VipProgressiveCampaignQueue partition key
  planId          string
  runId           string
  contactFlowId   string
  sourcePhone     string
  priority        number   — from campaign stage order (0-indexed)
  createdAt       string   — ISO-8601
  ttl             number   — Unix epoch: bucket endTime + 30min buffer

GSI: "queueArn-index"
  PK  = queueArn    (string)
  SK  = createdAt   (string)
```

TTL ensures entries auto-expire even if `_stop_branded_campaign` is never called (e.g., executor crash). Consumer uses the GSI to find all active campaigns for a given queue ARN at agent-available time.

---

## Section 3: E2E Data Flow

### Phase 1 — Plan creation

User creates a Plan with a campaign having `deliveryType: "branded"`. `handlers/plans.py` validates: when `deliveryType == "branded"`, `queueArn` / `contactFlowId` / `sourcePhone` are required. `store.py` persists without schema changes — the new fields round-trip as-is.

### Phase 2 — Campaign start (`_start_one_campaign`)

```
_is_branded(campaign) → campaign.get("deliveryType") == "branded"

1. _create_segment(campaign)
     └─ Redis leads as primary source (same as existing plans)
     └─ CP segment as alternative (same flag/config)

2. lambda.invoke("vip-admin-progressive-dialer-seeder",
       payload={
         "campaignId": campaignId,        # direct invocation shape (see §3a)
         "segmentName": segmentName,
         "contactFlowId": cfg["contactFlowId"],
         "sourcePhone":   cfg["sourcePhone"],
       })

   if result["seeded"] == 0:
       cs["status"] = "completed"
       return                              # immediate terminal, no DDB write

3. dynamodb.put_item("VipActiveBrandedCampaigns", {
       pk:            f"QUEUE#{queueArn}",
       sk:            f"CAMPAIGN#{campaignId}",
       campaignId:    campaignId,
       planId:        planId,
       runId:         runId,
       contactFlowId: cfg["contactFlowId"],
       sourcePhone:   cfg["sourcePhone"],
       priority:      stage_index,
       createdAt:     datetime.utcnow().isoformat(),
       ttl:           bucket_end_epoch + 1800,
   })

4. cs["brandedCampaignId"] = campaignId
   cs["connectCampaignId"] = None
   cs["queueArn"]          = queueArn
   cs["status"]            = "running"
```

#### §3a — Seeder direct invocation shape

`handler_seeder.py` currently reads `event["pathParameters"]["id"]` (HTTP shape). Add a fallback for direct Lambda invocation:

```python
if "pathParameters" in event:          # HTTP API Gateway (existing)
    campaign_id = event["pathParameters"]["id"]
    body = json.loads(event["body"])
else:                                   # Direct Lambda invocation (new)
    campaign_id = event["campaignId"]
    body = event
```

### Phase 3 — Active dialing (consumer-driven, no executor involvement)

```
Agent goes Available
  → Kinesis → consumer Lambda
    → Query VipActiveBrandedCampaigns (GSI: queueArn)
       → sort: priority ASC, createdAt ASC
       → loop campaigns:
           → try dequeue from VipProgressiveCampaignQueue (campaignId)
           → if contact found: push First Orion + enqueue SQS
           → if queue empty: continue to next campaign
```

Consumer replaces `ACTIVE_CAMPAIGN_ID` env var with:

```python
# env var: ACTIVE_CAMPAIGNS_TABLE = "VipActiveBrandedCampaigns"
# env var: ACTIVE_CAMPAIGNS_GSI   = "queueArn-index"

items = ddb.query(
    TableName=ACTIVE_CAMPAIGNS_TABLE,
    IndexName=ACTIVE_CAMPAIGNS_GSI,
    KeyConditionExpression="queueArn = :q",
    ExpressionAttributeValues={":q": agent_queue_arn},
).get("Items", [])

campaigns = sorted(items,
    key=lambda x: (int(x["priority"]), x["createdAt"]))
```

### Phase 4 — Poll completion (`tick()`)

After the existing Connect V2 poll block, add:

```python
elif cs.get("brandedCampaignId") and cs["status"] == "running":
    count = _count_branded_queue(cs["brandedCampaignId"])
    if count == 0:
        cs["status"] = "completed"
        _stop_branded_campaign(cs)
    elif _bucket_time_expired(run, bucket_idx):
        cs["status"] = "expired"
        _stop_branded_campaign(cs)
```

`_count_branded_queue(campaignId)`: DynamoDB Query on `VipProgressiveCampaignQueue` with `FilterExpression=Attr("status").is_in(["PENDING", "DISPATCHING"])`. Returns count. Eventual consistency is acceptable — worst case is one extra tick before completing.

### Phase 5 — Abort / stop / expire (unified cleanup)

New helper `_stop_branded_campaign(cs)`:

```python
def _stop_branded_campaign(cs):
    campaign_id = cs.get("brandedCampaignId")
    queue_arn   = cs.get("queueArn")
    if not campaign_id:
        return
    try:
        ddb.delete_item(
            TableName="VipActiveBrandedCampaigns",
            Key={"pk": f"QUEUE#{queue_arn}", "sk": f"CAMPAIGN#{campaign_id}"},
        )
    except Exception as e:
        logger.error("stop_branded_campaign_delete_failed", extra={
            "campaignId": campaign_id, "error": str(e)
        })
    try:
        _expire_queue_items(campaign_id)   # batch-write ttl=now on PENDING/DISPATCHING items
    except Exception as e:
        logger.error("stop_branded_campaign_expire_failed", extra={
            "campaignId": campaign_id, "error": str(e)
        })
```

Called from: `abort_run`, `_force_finish_internal`, `_expire_bucket`, `_advance_bucket` cleanup, `force_stop_campaign`, `skip_campaign`, `force_start_campaign` Phase-2 (before re-seed).

### Phase 6 — Prewarm exclusion

In `_prestart_next_bucket` and `_prestart_plan`:

```python
if _is_branded(campaign):
    continue   # no Connect V2 warmup for branded — starts directly in _start_one_campaign
```

---

## Section 4: Error Handling

| Scenario | Behavior |
|---|---|
| Seeder Lambda errors (timeout, CP API failure) | `cs["status"] = "error"`. No DDB write. Queue TTL cleans items. Bucket advances. |
| Seeder succeeds; `VipActiveBrandedCampaigns` write fails | `cs["status"] = "error"`. Unseeded contacts expire via TTL (24h). No dispatch. |
| Segment empty (seeded == 0) | `cs["status"] = "completed"` immediately. No DDB write. No consumer dispatch. |
| `_count_branded_queue` DDB error in `tick()` | Silently skip (no transition). Next tick retries. StuckRun alarm (4h) is the backstop. |
| `_stop_branded_campaign` delete fails on abort | Log error with `campaignId`. Do NOT re-raise. Abort continues. Entry expires via TTL. |
| `_expire_queue_items` fails on abort | Log error. Do NOT re-raise. Items expire via natural TTL (24h). |
| `force_start_campaign` recovery for branded | Call `_stop_branded_campaign(old_cs)` first (log if fails), then re-invoke seeder. |
| Consumer queries GSI but entry not yet visible (eventual consistency) | No campaigns found → no dispatch. Next agent-available event retries. |

---

## Section 5: Testing Strategy

### New unit tests (executor.py)

```python
# Discriminator
test_is_branded_returns_true_for_branded_delivery_type()
test_is_branded_returns_false_for_connect_campaigns()
test_is_branded_returns_false_when_delivery_type_absent()

# _start_one_campaign — branded path
test_start_branded_campaign_invokes_seeder_with_correct_payload()
test_start_branded_campaign_writes_vip_active_branded_campaigns()
test_start_branded_campaign_sets_status_running_and_campaign_id()
test_start_branded_campaign_sets_queue_arn_in_campaign_state()
test_start_branded_campaign_empty_segment_sets_completed_immediately()
test_start_branded_campaign_seeder_error_sets_error_status_no_ddb_write()
test_start_branded_campaign_does_NOT_call_create_and_start_campaign()
test_start_branded_campaign_does_NOT_enter_warmed_path()

# tick() — branded poll
test_tick_branded_campaign_completes_when_queue_empty()
test_tick_branded_campaign_expires_when_bucket_time_exceeded()
test_tick_branded_campaign_does_NOT_poll_connect_v2()
test_tick_branded_count_error_does_not_transition_status()
test_tick_branded_all_terminal_advances_bucket()

# _stop_branded_campaign
test_stop_branded_deletes_from_vip_active_branded_campaigns()
test_stop_branded_expires_queue_items()
test_stop_branded_noop_when_no_branded_campaign_id()
test_stop_branded_delete_failure_does_not_raise()
test_stop_branded_expire_failure_does_not_raise()

# Abort / stop integration
test_abort_run_calls_stop_branded_for_branded_campaigns()
test_expire_bucket_calls_stop_branded_for_branded_campaigns()
test_force_stop_campaign_calls_stop_branded()
test_force_start_campaign_calls_stop_branded_before_reseed()

# Prewarm exclusion
test_prestart_next_bucket_skips_branded_campaigns()
test_prestart_plan_skips_branded_campaigns()
```

### Regression requirement

All 152 existing tests in `test_executor_v2.py`, `test_store.py`, `test_builders.py` must pass without modification. The branded bifurcation (`_is_branded()` guard) is the first evaluation in each modified function — if it returns `False`, the existing code path executes identically.

### New integration tests (seeder + consumer)

```python
test_seeder_accepts_direct_payload_without_path_parameters()
test_seeder_fallback_extracts_campaign_id_and_body_correctly()
test_consumer_queries_active_branded_campaigns_by_queue_arn()
test_consumer_picks_highest_priority_campaign_first()
test_consumer_picks_oldest_campaign_on_same_priority_when_tied()
```

---

## Section 6: CDK Changes

### `api-plans-stack.ts` — new permissions for executor Lambda

```typescript
// VipProgressiveCampaignQueue — count PENDING/DISPATCHING + expire items
progressiveCampaignQueueTable.grantReadWriteData(plans.lambdaRole);

// VipActiveBrandedCampaigns — put/delete/query (new table)
activeBrandedCampaignsTable.grantReadWriteData(plans.lambdaRole);

// Seeder Lambda — direct invocation
plans.lambdaRole.addToPolicy(new iam.PolicyStatement({
  actions: ['lambda:InvokeFunction'],
  resources: [seederFn.functionArn],
}));
```

New env vars on executor Lambda: `ACTIVE_BRANDED_CAMPAIGNS_TABLE`, `CAMPAIGN_QUEUE_TABLE_BRANDED` (or reuse existing output), `PROGRESSIVE_DIALER_SEEDER_ARN`.

### `api-progressive-dialer-stack.ts` — consumer changes

- Remove `ACTIVE_CAMPAIGN_ID` env var
- Add env vars: `ACTIVE_CAMPAIGNS_TABLE`, `ACTIVE_CAMPAIGNS_GSI`
- Add `dynamodb:Query` on `VipActiveBrandedCampaigns` GSI to consumer role
- Add `VipActiveBrandedCampaigns` table creation with GSI `queueArn-index`

### New table definition (can live in either stack — recommend `api-progressive-dialer-stack.ts` as owner)

```typescript
const activeBrandedCampaignsTable = new dynamodb.Table(this, 'ActiveBrandedCampaigns', {
  tableName: 'VipActiveBrandedCampaigns',
  partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
  sortKey:      { name: 'sk', type: dynamodb.AttributeType.STRING },
  billingMode:  dynamodb.BillingMode.PAY_PER_REQUEST,
  encryption:   dynamodb.TableEncryption.CUSTOMER_MANAGED,
  encryptionKey: dataKey,
  timeToLiveAttribute: 'ttl',
  removalPolicy: cdk.RemovalPolicy.RETAIN,
});
activeBrandedCampaignsTable.addGlobalSecondaryIndex({
  indexName:            'queueArn-index',
  partitionKey: { name: 'queueArn', type: dynamodb.AttributeType.STRING },
  sortKey:      { name: 'createdAt', type: dynamodb.AttributeType.STRING },
});
```

---

## Section 7: Pre-Deploy Fixes (standalone `ApiProgressiveDialerStack`)

Before implementing or deploying the Plans integration, the following show-stoppers on the standalone branded dialer stack must be resolved:

| # | File | Fix |
|---|------|-----|
| 1 | `infra/bin/app.ts:155-162` | Eliminate placeholder fallbacks; `throw new Error(...)` in synth if context missing. Verify real ARNs in `cdk.json`. |
| 2 | Entire stack | Add CloudWatch alarms: DLQ visible messages > 0, Lambda error rate, FirstOrion push failures (custom metric), Connect throttle. |
| 3 | `handler_consumer.py:104` + `handler_caller.py` | Unify `correlationId` as UUID generated at start of `_process_record`, propagated in SQS message body, used in all consumer + caller log lines. |
| 4 | `handler_caller.py` | Release agent lock after `mark_dialed()` in success path. |
| 5 | `handler_caller.py:59-65` | On `get_phone` returning `None`: release lock + `reset_to_pending()` before returning. |
| 6 | `handler_caller.py:99` + `first_orion_client.py` | On throttle retry (SQS visibility redelivery): re-dispatch First Orion INFORM push before `StartOutboundVoiceContact`. |
| 7 | `infra/lib/stacks/api-progressive-dialer-stack.ts` | Implement `reportBatchItemFailures` on SQS ESM + return `{batchItemFailures:[...]}` from caller handler. Add `visibilityTimeout: Duration.seconds(180)`. |

---

## Open Decisions

- **Segment source**: Redis leads as primary (same as existing plans). CP segment as opt-in alternative via existing `campaignConfig` flags. No new config required.
- **`dialerType` in `campaignConfig`**: remains `"progressive"` for any future Connect V2 fallback path. `deliveryType: "branded"` is the sole discriminator for the branded channel.
- **`bandwidthAllocation`**: not applicable to branded campaigns (no Connect V2). Field can be present in `campaignConfig` but is ignored.
- **Multiple branded campaigns on same queue**: supported (one-to-many DDB design). Consumer selects by `priority ASC, createdAt ASC`.

---

*Design approved: 2026-06-17. Sections 1–5 reviewed and approved iteratively. Pre-deploy review findings incorporated in Section 7.*
