# Progressive Branded Dialer — Plans Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate the Progressive Branded Dialer into the Plans system so a campaign can use `deliveryType: "branded"` to drive agent-first outbound dialing instead of Connect Campaigns V2 — with identical bucket/time-expiry behavior and no regressions on existing plans.

**Architecture:** The executor bifurcates on `_is_branded(campaign)` at every control point (`_start_one_campaign`, `tick`, abort/stop/expire paths, prewarm exclusion). A new DynamoDB table `VipActiveBrandedCampaigns` acts as the handoff between executor and consumer; the consumer replaces its static `ACTIVE_CAMPAIGN_ID` env var with a live GSI query. All existing predictive/progressive/agentless Connect V2 flows are unaffected — every branch is additive.

**Tech Stack:** Python 3.12 (executor, seeder, consumer), AWS CDK v2 TypeScript, DynamoDB (PAY_PER_REQUEST, KMS CMK), Lambda (boto3 direct invoke), pytest + moto/unittest.mock.

## Global Constraints

- **Discriminator field:** `campaign.get("deliveryType") == "branded"` — NOT `dialerType` (that key goes into Connect V2 JSON verbatim and would cause `ValidationException`).
- **`dialerType` in `campaignConfig`** remains `"progressive"` — it is only the Connect V2 mode selector and is ignored for branded campaigns.
- **PHI rule:** phone numbers must never appear in logs, SQS body, or error messages. Use `type(e).__name__` for exception strings, `correlation_id` (UUID) for tracing.
- **Regression rule:** all 152 existing tests in `test_executor_v2.py` / `test_store.py` / `test_builders.py` must pass without modification after every task.
- **Env var pattern:** executor reads all config from `os.environ[...]` at module level (fail-fast on missing key). New vars: `ACTIVE_BRANDED_CAMPAIGNS_TABLE`, `CAMPAIGN_QUEUE_TABLE_BRANDED`, `PROGRESSIVE_DIALER_SEEDER_ARN`.
- **DynamoDB write pattern:** all new DDB writes in executor use `boto3` client directly (not resource) to match the project's existing pattern in `store.py`.
- **Error handling:** `_stop_branded_campaign` must NEVER raise — log and continue. `tick` poll failures must NEVER transition campaign status — log and let the next tick retry.
- **Test command (plans):** `cd services/api-plans && python3 -m pytest tests/unit/ -v`
- **Test command (dialer):** `cd services/api-progressive-dialer && python3 -m pytest tests/unit/ -v`
- **TypeScript check:** `cd infra && npx tsc --noEmit`

---

## File Map

| File | Action | Purpose |
|------|--------|---------|
| `services/api-plans/src/store.py` | Modify `:322` | Add `brandedCampaignId`, `queueArn` to `_initial_campaign_state` |
| `services/api-plans/src/executor.py` | Modify multiple | `_is_branded`, `_start_one_campaign` bifurcation, `tick` poll, `_stop_branded_campaign`, abort/stop/expire paths, prewarm exclusion |
| `services/api-plans/src/handlers/plans.py` | Modify | Validate `deliveryType: "branded"` required fields |
| `services/api-plans/tests/unit/test_executor_v2.py` | Modify | Add 25 branded tests |
| `services/api-progressive-dialer/src/handler_seeder.py` | Modify `:84` | Add direct Lambda invocation fallback |
| `services/api-progressive-dialer/src/handler_consumer.py` | Modify | Replace static `ACTIVE_CAMPAIGN_ID` with GSI query on `VipActiveBrandedCampaigns` |
| `services/api-progressive-dialer/tests/unit/test_handler_consumer.py` | Modify | Add GSI query tests |
| `services/api-progressive-dialer/tests/unit/test_handler_seeder.py` | Modify | Add direct invocation tests |
| `infra/lib/stacks/api-progressive-dialer-stack.ts` | Modify | Create `VipActiveBrandedCampaigns` table + GSI; expose as public property; update consumer env |
| `infra/lib/stacks/api-plans-stack.ts` | Modify | Accept new props; grant executor permissions; add env vars |
| `infra/bin/app.ts` | Modify | Wire table refs + seeder ARN from progressive stack to plans stack |

---

### Task 1: Foundation — `_is_branded()` discriminator + campaign state fields

**Files:**
- Modify: `services/api-plans/src/executor.py` (add helper near top of file, after imports)
- Modify: `services/api-plans/src/store.py:322`
- Test: `services/api-plans/tests/unit/test_executor_v2.py`

**Interfaces:**
- Produces: `_is_branded(campaign: dict) -> bool` — used by Tasks 5, 6, 7, 8
- Produces: `_initial_campaign_state` with keys `"brandedCampaignId": None` and `"queueArn": None`

- [ ] **Step 1: Write failing tests**

In `services/api-plans/tests/unit/test_executor_v2.py`, add at the end of the file:

```python
# ── _is_branded ──────────────────────────────────────────────────────────────

class TestIsBranded:
    def test_returns_true_for_branded_delivery_type(self):
        from executor import _is_branded
        assert _is_branded({"deliveryType": "branded"}) is True

    def test_returns_false_for_connect_campaign(self):
        from executor import _is_branded
        assert _is_branded({"campaignConfig": {"dialerType": "progressive"}}) is False

    def test_returns_false_when_delivery_type_absent(self):
        from executor import _is_branded
        assert _is_branded({}) is False

    def test_returns_false_for_other_delivery_type(self):
        from executor import _is_branded
        assert _is_branded({"deliveryType": "journey"}) is False


class TestInitialCampaignStateFields:
    def test_branded_campaign_id_initialized_to_none(self):
        from store import _initial_campaign_state
        cs = _initial_campaign_state({"id": "c-1"})
        assert "brandedCampaignId" in cs
        assert cs["brandedCampaignId"] is None

    def test_queue_arn_initialized_to_none(self):
        from store import _initial_campaign_state
        cs = _initial_campaign_state({"id": "c-1"})
        assert "queueArn" in cs
        assert cs["queueArn"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-plans
python3 -m pytest tests/unit/test_executor_v2.py::TestIsBranded tests/unit/test_executor_v2.py::TestInitialCampaignStateFields -v 2>&1
```

Expected: `ImportError: cannot import name '_is_branded'` and assertion failures on missing keys.

- [ ] **Step 3: Add `_is_branded` to executor.py**

In `services/api-plans/src/executor.py`, find the block of module-level constants (after the imports, before the first `def`). Add:

```python
def _is_branded(campaign: dict) -> bool:
    """Return True if this campaign uses the Progressive Branded Dialer channel.

    Discriminator is deliveryType='branded', NOT dialerType — dialerType is injected
    verbatim as a Connect V2 JSON key and would cause ValidationException if set to
    'branded'.
    """
    return campaign.get("deliveryType") == "branded"
```

- [ ] **Step 4: Add fields to `_initial_campaign_state` in store.py**

In `services/api-plans/src/store.py`, find `_initial_campaign_state` at line 322. Add two keys after `"connectCampaignId": None`:

```python
def _initial_campaign_state(campaign: dict) -> dict:
    return {
        "campaignId": campaign.get("id")
        or campaign.get("campaignId")
        or str(uuid.uuid4()),
        "name": campaign.get("name", ""),
        "status": "queued",
        "connectCampaignId": None,
        "brandedCampaignId": None,   # set when branded campaign starts
        "queueArn": None,            # copied from campaignConfig on branded start
        "segmentName": None,
        "segmentArn": None,
        "leadCount": None,
        "startedAt": None,
        "completedAt": None,
        "exitReason": None,
        "errorDetail": None,
    }
```

- [ ] **Step 5: Run all tests to verify pass + no regressions**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-plans
python3 -m pytest tests/unit/ -v 2>&1
```

Expected: all existing tests + 6 new tests pass.

- [ ] **Step 6: Commit**

```bash
git add services/api-plans/src/executor.py \
        services/api-plans/src/store.py \
        services/api-plans/tests/unit/test_executor_v2.py
git commit -m "feat(plans): _is_branded() discriminator + branded fields in campaign state

Add _is_branded(campaign) helper that reads deliveryType='branded' — NOT
dialerType, which is a Connect V2 JSON key and would cause ValidationException.
Add brandedCampaignId and queueArn to _initial_campaign_state so the fields
exist from run creation rather than appearing only after branded start."
```

---

### Task 2: Seeder direct Lambda invocation support

**Files:**
- Modify: `services/api-progressive-dialer/src/handler_seeder.py:84`
- Test: `services/api-progressive-dialer/tests/unit/test_handler_seeder.py`

**Interfaces:**
- Produces: `lambda_handler(event, ctx)` now accepts both HTTP shape (`event["pathParameters"]["id"]`) and direct shape (`event["campaignId"]`). Return value: `{"statusCode": 200, "body": '{"seeded": N}'}` for HTTP; `{"seeded": N}` for direct.
- Consumed by: Task 5 (`_invoke_seeder` in executor)

- [ ] **Step 1: Write failing test**

Add to `services/api-progressive-dialer/tests/unit/test_handler_seeder.py`:

```python
class TestDirectInvocation:
    """Seeder invoked directly from executor Lambda (not via API Gateway)."""

    def test_direct_payload_extracts_campaign_id(self, mocker):
        mocker.patch("handler_seeder._get_cp")
        mocker.patch("handler_seeder._get_table_writer", return_value=MagicMock())
        mocker.patch("handler_seeder._get_phones_from_segment", return_value=["+15551234567"])

        event = {
            "campaignId": "camp-direct-001",
            "segmentName": "test-segment",
            "contactFlowId": "flow-abc",
            "sourcePhone": "+19174105649",
        }
        result = lambda_handler(event, None)
        assert result == {"seeded": 1}

    def test_direct_payload_missing_campaign_id_returns_400_compatible(self, mocker):
        event = {"segmentName": "test-segment"}
        result = lambda_handler(event, None)
        # Direct invocations return dict, not HTTP response — raise on missing id
        assert "error" in result or result.get("statusCode") == 400
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-progressive-dialer
python3 -m pytest tests/unit/test_handler_seeder.py::TestDirectInvocation -v 2>&1
```

Expected: failures — current handler reads `pathParameters` only.

- [ ] **Step 3: Add direct invocation fallback to handler_seeder.py**

In `services/api-progressive-dialer/src/handler_seeder.py`, replace the opening of `lambda_handler` (line 84):

```python
def lambda_handler(event: dict, _context) -> dict:
    # Support both HTTP API Gateway shape and direct Lambda invocation from executor.
    if "pathParameters" in event:                    # HTTP shape (API Gateway)
        path_params = event.get("pathParameters") or {}
        campaign_id = path_params.get("id")
        if not campaign_id:
            return {"statusCode": 400, "body": json.dumps({"error": "missing campaign id"})}
        body: dict = {}
        raw = event.get("body")
        if raw:
            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return {"statusCode": 400, "body": json.dumps({"error": "invalid JSON body"})}
        _http_mode = True
    else:                                            # Direct Lambda invocation shape
        campaign_id = event.get("campaignId")
        if not campaign_id:
            return {"statusCode": 400, "body": json.dumps({"error": "missing campaign id"})}
        body = event
        _http_mode = False

    segment_name = body.get("segmentName")
    if not segment_name:
        err = {"error": "missing segmentName"}
        return {"statusCode": 400, "body": json.dumps(err)} if _http_mode else err
```

At the end of the function, replace the return statement to handle both modes. Find the final `return {"statusCode": 200, "body": json.dumps({...})}` and change to:

```python
    result = {"seeded": len(phones)}
    if _http_mode:
        return {"statusCode": 200, "body": json.dumps(result)}
    return result
```

- [ ] **Step 4: Run all dialer tests**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-progressive-dialer
python3 -m pytest tests/unit/ -v 2>&1
```

Expected: all existing + 2 new tests pass.

- [ ] **Step 5: Commit**

```bash
git add services/api-progressive-dialer/src/handler_seeder.py \
        services/api-progressive-dialer/tests/unit/test_handler_seeder.py
git commit -m "feat(progressive-dialer): seeder accepts direct Lambda invocation

Add fallback in lambda_handler for non-HTTP events (campaignId at top level
instead of pathParameters). Returns plain dict instead of HTTP response when
invoked directly from executor, matching boto3 lambda.invoke() expectations."
```

---

### Task 3: `VipActiveBrandedCampaigns` DynamoDB table + CDK

**Files:**
- Modify: `infra/lib/stacks/api-progressive-dialer-stack.ts`

**Interfaces:**
- Produces: `ApiProgressiveDialerStack.activeBrandedCampaignsTable` (public `dynamodb.Table`)
- Produces: `ApiProgressiveDialerStack.campaignQueueTable` (make existing table public)
- Consumed by: Task 9 (api-plans-stack permissions), Task 4 (consumer env), `infra/bin/app.ts`

- [ ] **Step 1: Add public properties and new table to the stack**

In `infra/lib/stacks/api-progressive-dialer-stack.ts`, add two public properties after `seederFunction`:

```typescript
export class ApiProgressiveDialerStack extends cdk.Stack {
  public readonly seederFunction: lambda.Function;
  public readonly campaignQueueTable: dynamodb.Table;       // expose existing table
  public readonly activeBrandedCampaignsTable: dynamodb.Table;  // new
```

In the constructor, after `campaignQueueTable` is created (line ~65), assign:
```typescript
this.campaignQueueTable = campaignQueueTable;
```

After the existing tables, add the new table (before the SQS section):

```typescript
// ── DynamoDB: Active Branded Campaigns ───────────────────────────
// One-to-many: PK=QUEUE#{queueArn}, SK=CAMPAIGN#{campaignId}
// GSI queueArn-index used by consumer to find campaigns by queue ARN.
const activeBrandedCampaignsTable = new dynamodb.Table(
  this,
  'ActiveBrandedCampaignsTable',
  {
    tableName: 'VipActiveBrandedCampaigns',
    partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
    sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
    billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
    encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
    encryptionKey: dataKey,
    timeToLiveAttribute: 'ttl',
    removalPolicy: cdk.RemovalPolicy.RETAIN,
  },
);
activeBrandedCampaignsTable.addGlobalSecondaryIndex({
  indexName: 'queueArn-index',
  partitionKey: { name: 'queueArn', type: dynamodb.AttributeType.STRING },
  sortKey: { name: 'createdAt', type: dynamodb.AttributeType.STRING },
  projectionType: dynamodb.ProjectionType.ALL,
});
this.activeBrandedCampaignsTable = activeBrandedCampaignsTable;
```

Add to Outputs section:
```typescript
new cdk.CfnOutput(this, 'ActiveBrandedCampaignsTableName', {
  value: activeBrandedCampaignsTable.tableName,
});
new cdk.CfnOutput(this, 'ActiveBrandedCampaignsTableArn', {
  value: activeBrandedCampaignsTable.tableArn,
});
```

- [ ] **Step 2: Verify TypeScript compiles**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/infra
npx tsc --noEmit 2>&1
```

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add infra/lib/stacks/api-progressive-dialer-stack.ts
git commit -m "feat(infra): VipActiveBrandedCampaigns DynamoDB table with queueArn GSI

One-to-many table: PK=QUEUE#{queueArn}, SK=CAMPAIGN#{campaignId}.
GSI queueArn-index allows consumer to find all active branded campaigns
for a given agent queue ARN in O(1) instead of scanning the whole table.
Expose campaignQueueTable and activeBrandedCampaignsTable as public props
for cross-stack wiring in app.ts."
```

---

### Task 4: Consumer — replace static `ACTIVE_CAMPAIGN_ID` with GSI query

**Files:**
- Modify: `services/api-progressive-dialer/src/handler_consumer.py`
- Modify: `infra/lib/stacks/api-progressive-dialer-stack.ts` (consumer env vars + IAM)
- Test: `services/api-progressive-dialer/tests/unit/test_handler_consumer.py`

**Interfaces:**
- Consumes: `VipActiveBrandedCampaigns` GSI `queueArn-index` (Task 3)
- Produces: consumer selects campaigns by priority ASC, createdAt ASC; tries each queue in order until a contact is found

- [ ] **Step 1: Write failing tests**

Add to `services/api-progressive-dialer/tests/unit/test_handler_consumer.py`:

```python
class TestGsiCampaignLookup:
    """Consumer queries VipActiveBrandedCampaigns by queueArn instead of static env."""

    def _make_campaign_item(self, campaign_id, priority=0, created_at="2026-06-18T10:00:00"):
        return {
            "campaignId": {"S": campaign_id},
            "contactFlowId": {"S": "flow-abc"},
            "sourcePhone": {"S": "+19174105649"},
            "priority": {"N": str(priority)},
            "createdAt": {"S": created_at},
        }

    def test_queries_active_campaigns_by_queue_arn(self, mocker):
        ddb = mocker.patch("handler_consumer._get_ddb")
        ddb.return_value.query.return_value = {"Items": []}
        mocker.patch("handler_consumer.is_agent_available", return_value=True)
        mocker.patch("handler_consumer.extract_agent_info", return_value={
            "agent_arn": "arn:aws:connect:::agent/a1",
            "queue_arn": "arn:aws:connect:::queue/q1",
        })
        mocker.patch("handler_consumer.is_queue_allowed", return_value=True)

        import handler_consumer, base64, json
        record = {"kinesis": {"data": base64.b64encode(json.dumps({}).encode()).decode()}}
        handler_consumer._process_record(record)

        ddb.return_value.query.assert_called_once_with(
            TableName="VipActiveBrandedCampaigns",
            IndexName="queueArn-index",
            KeyConditionExpression="queueArn = :q",
            ExpressionAttributeValues={":q": {"S": "arn:aws:connect:::queue/q1"}},
        )

    def test_picks_lowest_priority_campaign_first(self, mocker):
        items = [
            self._make_campaign_item("camp-low",  priority=1, created_at="2026-06-18T10:00:00"),
            self._make_campaign_item("camp-high", priority=0, created_at="2026-06-18T10:05:00"),
        ]
        mocker.patch("handler_consumer._get_ddb").return_value.query.return_value = {"Items": items}
        mocker.patch("handler_consumer.is_agent_available", return_value=True)
        mocker.patch("handler_consumer.extract_agent_info", return_value={
            "agent_arn": "arn::agent/a1", "queue_arn": "arn::queue/q1",
        })
        mocker.patch("handler_consumer.is_queue_allowed", return_value=True)
        lock = mocker.patch("handler_consumer._get_lock").return_value
        lock.acquire.return_value = True
        queue = mocker.patch("handler_consumer._get_queue").return_value
        queue.dequeue.return_value = None  # both empty — just verify order

        import handler_consumer, base64, json
        record = {"kinesis": {"data": base64.b64encode(json.dumps({}).encode()).decode()}}
        handler_consumer._process_record(record)

        calls = [c.args[0] for c in queue.dequeue.call_args_list]
        assert calls == ["camp-high", "camp-low"]  # priority 0 first

    def test_no_active_campaigns_skips_dispatch(self, mocker):
        mocker.patch("handler_consumer._get_ddb").return_value.query.return_value = {"Items": []}
        mocker.patch("handler_consumer.is_agent_available", return_value=True)
        mocker.patch("handler_consumer.extract_agent_info", return_value={
            "agent_arn": "arn::agent/a1", "queue_arn": "arn::queue/q1",
        })
        mocker.patch("handler_consumer.is_queue_allowed", return_value=True)
        lock = mocker.patch("handler_consumer._get_lock").return_value

        import handler_consumer, base64, json
        record = {"kinesis": {"data": base64.b64encode(json.dumps({}).encode()).decode()}}
        handler_consumer._process_record(record)

        lock.acquire.assert_not_called()
```

- [ ] **Step 2: Run to verify failures**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-progressive-dialer
python3 -m pytest tests/unit/test_handler_consumer.py::TestGsiCampaignLookup -v 2>&1
```

Expected: failures — `_get_ddb` does not exist yet.

- [ ] **Step 3: Rewrite consumer `_process_record` with GSI lookup**

In `services/api-progressive-dialer/src/handler_consumer.py`:

Replace line 36 (`_ACTIVE_CAMPAIGN_ID = os.environ["ACTIVE_CAMPAIGN_ID"]`) and lines 34-35 (`_CONTACT_FLOW_ID`, `_SOURCE_PHONE`) — these become per-campaign from DDB. Add new env vars:

```python
_ACTIVE_CAMPAIGNS_TABLE = os.environ["ACTIVE_CAMPAIGNS_TABLE"]
_ACTIVE_CAMPAIGNS_GSI   = os.environ.get("ACTIVE_CAMPAIGNS_GSI", "queueArn-index")
# Keep CONNECT_INSTANCE_ID, FIRSTORION_SECRET_NAME, SQS_QUEUE_URL, CAMPAIGN_QUEUE_TABLE,
# AGENT_LOCK_TABLE, ALLOWED_QUEUE_ARNS — remove ACTIVE_CAMPAIGN_ID, CONTACT_FLOW_ID,
# SOURCE_PHONE (now come from VipActiveBrandedCampaigns per campaign)
```

Add `_ddb_client = None` to singletons and a getter:

```python
_ddb_client = None

def _get_ddb():
    global _ddb_client
    if _ddb_client is None:
        _ddb_client = boto3.client("dynamodb")
    return _ddb_client
```

Add `_get_active_campaigns` helper:

```python
def _get_active_campaigns(queue_arn: str) -> list[dict]:
    """Query VipActiveBrandedCampaigns GSI for all active campaigns on this queue.

    Returns items sorted by priority ASC then createdAt ASC (oldest high-priority first).
    """
    resp = _get_ddb().query(
        TableName=_ACTIVE_CAMPAIGNS_TABLE,
        IndexName=_ACTIVE_CAMPAIGNS_GSI,
        KeyConditionExpression="queueArn = :q",
        ExpressionAttributeValues={":q": {"S": queue_arn}},
    )
    items = resp.get("Items", [])
    return sorted(
        items,
        key=lambda x: (int(x["priority"]["N"]), x["createdAt"]["S"]),
    )
```

Rewrite `_process_record` from the lock-acquire point onward:

```python
def _process_record(record: dict) -> None:
    raw = base64.b64decode(record["kinesis"]["data"]).decode("utf-8")
    agent_event = json.loads(raw)

    if not is_agent_available(agent_event):
        return

    info = extract_agent_info(agent_event)
    agent_arn = info["agent_arn"]
    queue_arn = info["queue_arn"]

    if not agent_arn or not queue_arn:
        logger.warning("Agent event missing ARN or queue_arn — skipping")
        return

    if not is_queue_allowed(queue_arn, _ALLOWED_QUEUE_ARNS):
        return

    campaigns = _get_active_campaigns(queue_arn)
    if not campaigns:
        return  # no active branded campaigns for this queue

    # Acquire agent lock using the first (highest-priority) campaign id as metadata
    first_campaign_id = campaigns[0]["campaignId"]["S"]
    lock = _get_lock()
    if not lock.acquire(agent_arn, campaign_id=first_campaign_id):
        logger.info("Lock already held for agent — skipping dispatch")
        return

    # Try each campaign queue in priority order until a contact is found
    contact = None
    campaign_id = None
    contact_flow_id = None
    source_phone = None

    for camp in campaigns:
        c_id = camp["campaignId"]["S"]
        c = _get_queue().dequeue(c_id)
        if c is not None:
            contact = c
            campaign_id = c_id
            contact_flow_id = camp["contactFlowId"]["S"]
            source_phone = camp["sourcePhone"]["S"]
            break

    if contact is None:
        lock.release(agent_arn)
        logger.info("All campaign queues empty — releasing lock")
        return

    correlation_id = str(uuid.uuid4())[:8]

    pushed = _get_fo().push(a_number=source_phone, b_number=contact.phone)
    if not pushed:
        logger.warning(
            "First Orion push failed — will retry via SQS caller correlation_id=%s",
            correlation_id,
        )
        _emit_metric("FirstOrionPushFailed")

    message = {
        "agentArn": agent_arn,
        "queueArn": queue_arn,
        "campaignId": campaign_id,
        "contactSk": contact.sk,
        "sourcePhone": source_phone,
        "contactFlowId": contact_flow_id,
        "instanceId": _CONNECT_INSTANCE_ID,
        "correlationId": correlation_id,
    }
    _get_sqs().send_message(
        QueueUrl=_SQS_QUEUE_URL,
        MessageBody=json.dumps(message),
        DelaySeconds=_SQS_DELAY_SECONDS,
    )
    logger.info(
        "SQS message enqueued correlation_id=%s campaign_id=%s",
        correlation_id,
        campaign_id,
    )
```

- [ ] **Step 4: Update consumer env vars in CDK stack**

In `infra/lib/stacks/api-progressive-dialer-stack.ts`, in the consumer Lambda `environment` block, replace:

```typescript
// REMOVE:
ACTIVE_CAMPAIGN_ID: props.activeCampaignId,
CONTACT_FLOW_ID,
SOURCE_PHONE: props.sourcePhonenumber,

// ADD:
ACTIVE_CAMPAIGNS_TABLE: activeBrandedCampaignsTable.tableName,
ACTIVE_CAMPAIGNS_GSI: 'queueArn-index',
```

Keep: `CAMPAIGN_QUEUE_TABLE`, `AGENT_LOCK_TABLE`, `SQS_QUEUE_URL`, `CONNECT_INSTANCE_ID`, `FIRSTORION_SECRET_NAME`, `ALLOWED_QUEUE_ARNS`.

Grant consumer access to new table:
```typescript
activeBrandedCampaignsTable.grantReadData(consumerRole);
```

- [ ] **Step 5: Remove `activeCampaignId` prop from stack interface and `app.ts`**

In `infra/lib/stacks/api-progressive-dialer-stack.ts`, remove `readonly activeCampaignId: string` from `ApiProgressiveDialerStackProps` and the `CONTACT_FLOW_ID` constant.

In `infra/bin/app.ts`, remove `activeCampaignId` from the `ApiProgressiveDialerStack` constructor call and the `requireContext('activeCampaignId')` line. Remove the `const activeCampaignId` variable entirely.

Also remove `sourcePhonenumber` from the stack props and from `app.ts` (it was only needed for the static env var; now it's per-campaign in DDB).

- [ ] **Step 6: Run all tests**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-progressive-dialer
python3 -m pytest tests/unit/ -v 2>&1
```

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/infra
npx tsc --noEmit 2>&1
```

Expected: all tests pass, tsc clean.

- [ ] **Step 7: Commit**

```bash
git add services/api-progressive-dialer/src/handler_consumer.py \
        services/api-progressive-dialer/tests/unit/test_handler_consumer.py \
        infra/lib/stacks/api-progressive-dialer-stack.ts \
        infra/bin/app.ts
git commit -m "feat(progressive-dialer): consumer queries VipActiveBrandedCampaigns GSI

Replace static ACTIVE_CAMPAIGN_ID env var with dynamic DynamoDB query on
queueArn-index GSI. Consumer tries campaigns in priority ASC, createdAt ASC
order, dequeuing from the first non-empty queue. contactFlowId and sourcePhone
now come per-campaign from the table instead of fixed env vars."
```

---

### Task 5: `_start_one_campaign` branded path

**Files:**
- Modify: `services/api-plans/src/executor.py:2205`
- Test: `services/api-plans/tests/unit/test_executor_v2.py`

**Interfaces:**
- Consumes: `_is_branded()` (Task 1), seeder direct invocation (Task 2), `VipActiveBrandedCampaigns` table name from env `ACTIVE_BRANDED_CAMPAIGNS_TABLE`
- Produces: on success, `cs["brandedCampaignId"]`, `cs["queueArn"]`, `cs["status"] = "running"`. On seeder error: `cs["status"] = "error"`. On empty segment: `cs["status"] = "completed"`.

- [ ] **Step 1: Write failing tests**

Add to `services/api-plans/tests/unit/test_executor_v2.py`:

```python
class TestStartBrandedCampaign:
    """_start_one_campaign with deliveryType='branded'."""

    def _branded_campaign(self, campaign_id="bc-1", queue_arn="arn:aws:connect:::queue/q1"):
        return {
            "id": campaign_id,
            "name": "Branded Test",
            "deliveryType": "branded",
            "campaignConfig": {
                "dialerType": "progressive",
                "queueArn": queue_arn,
                "contactFlowId": "flow-abc",
                "sourcePhone": "+19174105649",
            },
        }

    def _make_run_with_branded(self, campaign_id="bc-1"):
        bucket = _bucket_def(campaigns=[self._branded_campaign(campaign_id)])
        plan = {"planId": "p-1", "buckets": [bucket]}
        cs = _campaign_state(campaign_id=campaign_id, status="queued")
        run = {
            "planId": "p-1", "runId": "r-1",
            "bucketStates": [{"status": "running", "campaignStates": [cs]}],
        }
        return run, plan

    def test_invokes_seeder_lambda(self, mocker):
        run, plan = self._make_run_with_branded()
        mocker.patch("executor._create_segment", return_value=("seg-name", "seg-arn"))
        lam = mocker.patch("executor._get_lambda_client").return_value
        lam.invoke.return_value = {
            "Payload": MagicMock(read=lambda: json.dumps({"seeded": 5}).encode())
        }
        mocker.patch("executor._get_ddb_client").return_value.put_item.return_value = {}

        from executor import _start_one_campaign
        _start_one_campaign(run, plan, 0, 0)

        lam.invoke.assert_called_once()
        call_kwargs = lam.invoke.call_args[1] if lam.invoke.call_args[1] else lam.invoke.call_args[0][0]
        payload = json.loads(call_kwargs.get("Payload") or lam.invoke.call_args.kwargs["Payload"])
        assert payload["campaignId"] == "bc-1"
        assert payload["segmentName"] == "seg-name"

    def test_writes_vip_active_branded_campaigns(self, mocker):
        run, plan = self._make_run_with_branded()
        mocker.patch("executor._create_segment", return_value=("seg-name", "seg-arn"))
        lam = mocker.patch("executor._get_lambda_client").return_value
        lam.invoke.return_value = {
            "Payload": MagicMock(read=lambda: json.dumps({"seeded": 3}).encode())
        }
        ddb = mocker.patch("executor._get_ddb_client").return_value

        from executor import _start_one_campaign
        _start_one_campaign(run, plan, 0, 0)

        ddb.put_item.assert_called_once()
        item = ddb.put_item.call_args.kwargs["Item"]
        assert item["pk"]["S"] == "QUEUE#arn:aws:connect:::queue/q1"
        assert "CAMPAIGN#bc-1" in item["sk"]["S"]
        assert item["campaignId"]["S"] == "bc-1"

    def test_sets_status_running_and_branded_campaign_id(self, mocker):
        run, plan = self._make_run_with_branded()
        mocker.patch("executor._create_segment", return_value=("seg-name", "seg-arn"))
        lam = mocker.patch("executor._get_lambda_client").return_value
        lam.invoke.return_value = {
            "Payload": MagicMock(read=lambda: json.dumps({"seeded": 2}).encode())
        }
        mocker.patch("executor._get_ddb_client").return_value.put_item.return_value = {}

        from executor import _start_one_campaign
        cs = run["bucketStates"][0]["campaignStates"][0]
        _start_one_campaign(run, plan, 0, 0)

        assert cs["status"] == "running"
        assert cs["brandedCampaignId"] == "bc-1"
        assert cs["queueArn"] == "arn:aws:connect:::queue/q1"
        assert cs["connectCampaignId"] is None

    def test_empty_segment_sets_completed_immediately(self, mocker):
        run, plan = self._make_run_with_branded()
        mocker.patch("executor._create_segment", return_value=("seg-name", "seg-arn"))
        lam = mocker.patch("executor._get_lambda_client").return_value
        lam.invoke.return_value = {
            "Payload": MagicMock(read=lambda: json.dumps({"seeded": 0}).encode())
        }
        ddb = mocker.patch("executor._get_ddb_client").return_value

        from executor import _start_one_campaign
        cs = run["bucketStates"][0]["campaignStates"][0]
        _start_one_campaign(run, plan, 0, 0)

        assert cs["status"] == "completed"
        ddb.put_item.assert_not_called()  # no entry written when empty

    def test_seeder_error_sets_error_status(self, mocker):
        run, plan = self._make_run_with_branded()
        mocker.patch("executor._create_segment", return_value=("seg-name", "seg-arn"))
        lam = mocker.patch("executor._get_lambda_client").return_value
        lam.invoke.side_effect = Exception("Lambda timeout")
        ddb = mocker.patch("executor._get_ddb_client").return_value

        from executor import _start_one_campaign
        cs = run["bucketStates"][0]["campaignStates"][0]
        _start_one_campaign(run, plan, 0, 0)

        assert cs["status"] == "error"
        ddb.put_item.assert_not_called()

    def test_does_not_call_create_and_start_campaign(self, mocker):
        run, plan = self._make_run_with_branded()
        mocker.patch("executor._create_segment", return_value=("seg-name", "seg-arn"))
        lam = mocker.patch("executor._get_lambda_client").return_value
        lam.invoke.return_value = {
            "Payload": MagicMock(read=lambda: json.dumps({"seeded": 1}).encode())
        }
        mocker.patch("executor._get_ddb_client").return_value.put_item.return_value = {}
        create_fn = mocker.patch("executor._create_and_start_campaign")

        from executor import _start_one_campaign
        _start_one_campaign(run, plan, 0, 0)

        create_fn.assert_not_called()
```

- [ ] **Step 2: Run to verify failures**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-plans
python3 -m pytest tests/unit/test_executor_v2.py::TestStartBrandedCampaign -v 2>&1
```

Expected: 6 failures.

- [ ] **Step 3: Add `_get_lambda_client`, `_get_ddb_client`, and `_invoke_seeder` helpers to executor.py**

In `executor.py`, near the other boto3 singletons (search for `boto3` imports), add:

```python
_lambda_client = None
_ddb_client_branded = None

_PROGRESSIVE_DIALER_SEEDER_ARN = os.environ.get("PROGRESSIVE_DIALER_SEEDER_ARN", "")
_ACTIVE_BRANDED_CAMPAIGNS_TABLE = os.environ.get("ACTIVE_BRANDED_CAMPAIGNS_TABLE", "")
_CAMPAIGN_QUEUE_TABLE_BRANDED = os.environ.get("CAMPAIGN_QUEUE_TABLE_BRANDED", "")


def _get_lambda_client():
    global _lambda_client
    if _lambda_client is None:
        import boto3 as _boto3
        _lambda_client = _boto3.client("lambda")
    return _lambda_client


def _get_ddb_client():
    global _ddb_client_branded
    if _ddb_client_branded is None:
        import boto3 as _boto3
        _ddb_client_branded = _boto3.client("dynamodb")
    return _ddb_client_branded


def _invoke_seeder(campaign_id: str, segment_name: str, contact_flow_id: str, source_phone: str) -> int:
    """Invoke the progressive dialer seeder Lambda directly. Returns number of contacts seeded."""
    payload = {
        "campaignId": campaign_id,
        "segmentName": segment_name,
        "contactFlowId": contact_flow_id,
        "sourcePhone": source_phone,
    }
    response = _get_lambda_client().invoke(
        FunctionName=_PROGRESSIVE_DIALER_SEEDER_ARN,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode(),
    )
    result = json.loads(response["Payload"].read())
    return int(result.get("seeded", 0))
```

- [ ] **Step 4: Add branded path at the top of `_start_one_campaign`**

In `executor.py` at line 2205, find `def _start_one_campaign`. Add the branded branch as the FIRST thing after loading `campaign` and `cs`:

```python
def _start_one_campaign(
    run: dict, plan: dict, bucket_index: int, campaign_index: int
) -> None:
    """Start a single campaign (creating segment + Connect campaign if not already warmed)."""
    bucket = plan["buckets"][bucket_index]
    campaigns = bucket.get("campaigns", [])
    campaign = campaigns[campaign_index]
    cs = run["bucketStates"][bucket_index]["campaignStates"][campaign_index]
    now = _now_utc()
    now_iso = now.isoformat()

    cs["startedAt"] = now_iso

    # ── Branded path — bypass Connect V2 entirely ─────────────────────────
    if _is_branded(campaign):
        cfg = campaign.get("campaignConfig", {})
        queue_arn = cfg.get("queueArn", "")
        campaign_id = cs["campaignId"]
        try:
            seg_name, _ = _create_segment(campaign, run)
            seeded = _invoke_seeder(
                campaign_id=campaign_id,
                segment_name=seg_name,
                contact_flow_id=cfg["contactFlowId"],
                source_phone=cfg["sourcePhone"],
            )
        except Exception as exc:
            logger.error(
                "_start_one_campaign[%d/%d]: branded seeder failed: %s",
                bucket_index, campaign_index, type(exc).__name__,
            )
            cs["status"] = "error"
            cs["errorDetail"] = type(exc).__name__
            return

        if seeded == 0:
            cs["status"] = "completed"
            cs["exitReason"] = "empty_segment"
            cs["completedAt"] = now_iso
            return

        # Write entry to VipActiveBrandedCampaigns
        import time as _time
        bucket_end_epoch = int(
            (now + timedelta(hours=4)).timestamp()  # fallback TTL if no bucket endTime
        )
        try:
            _get_ddb_client().put_item(
                TableName=_ACTIVE_BRANDED_CAMPAIGNS_TABLE,
                Item={
                    "pk":            {"S": f"QUEUE#{queue_arn}"},
                    "sk":            {"S": f"CAMPAIGN#{campaign_id}"},
                    "queueArn":      {"S": queue_arn},
                    "campaignId":    {"S": campaign_id},
                    "planId":        {"S": run["planId"]},
                    "runId":         {"S": run["runId"]},
                    "contactFlowId": {"S": cfg["contactFlowId"]},
                    "sourcePhone":   {"S": cfg["sourcePhone"]},
                    "priority":      {"N": str(campaign_index)},
                    "createdAt":     {"S": now_iso},
                    "ttl":           {"N": str(bucket_end_epoch + 1800)},
                },
            )
        except Exception as exc:
            logger.error(
                "_start_one_campaign[%d/%d]: branded DDB write failed: %s",
                bucket_index, campaign_index, type(exc).__name__,
            )
            cs["status"] = "error"
            cs["errorDetail"] = type(exc).__name__
            return

        cs["brandedCampaignId"] = campaign_id
        cs["queueArn"] = queue_arn
        cs["connectCampaignId"] = None
        cs["status"] = "running"
        return
    # ── End branded path ─────────────────────────────────────────────────

    # Existing Connect V2 path continues unchanged below...
    if cs.get("connectCampaignId"):
```

- [ ] **Step 5: Run all plans tests**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-plans
python3 -m pytest tests/unit/ -v 2>&1
```

Expected: all 152 existing + 6 new tests pass.

- [ ] **Step 6: Commit**

```bash
git add services/api-plans/src/executor.py \
        services/api-plans/tests/unit/test_executor_v2.py
git commit -m "feat(plans): _start_one_campaign branded path — seeder + VipActiveBrandedCampaigns

When campaign.deliveryType=='branded', bypass Connect V2 entirely:
create segment, invoke progressive dialer seeder Lambda directly, write
entry to VipActiveBrandedCampaigns. Empty segments complete immediately.
Seeder/DDB errors set status=error. Existing Connect V2 path unchanged."
```

---

### Task 6: `tick()` branded poll + `_count_branded_queue()`

**Files:**
- Modify: `services/api-plans/src/executor.py:339` (tick) and `executor.py:422` (poll loop)
- Test: `services/api-plans/tests/unit/test_executor_v2.py`

**Interfaces:**
- Consumes: `_is_branded()` (Task 1), `_stop_branded_campaign()` (Task 7 — stub it in tests)
- Produces: `_count_branded_queue(campaign_id: str) -> int`; branded campaigns transition `running → completed` when count==0, `running → expired` when bucket time exceeded

- [ ] **Step 1: Write failing tests**

Add to `test_executor_v2.py`:

```python
class TestTickBrandedPoll:
    def _run_with_running_branded(self, campaign_id="bc-1"):
        cs = _campaign_state(campaign_id=campaign_id, status="running")
        cs["brandedCampaignId"] = campaign_id
        cs["queueArn"] = "arn::queue/q1"
        cs["connectCampaignId"] = None
        bucket = _bucket_def(campaigns=[{
            "id": campaign_id, "name": "B", "deliveryType": "branded",
            "campaignConfig": {"queueArn": "arn::queue/q1"},
        }])
        run = {"planId": "p-1", "runId": "r-1", "status": "running",
               "bucketStates": [{"status": "running", "campaignStates": [cs],
                                  "startedAt": datetime.utcnow().isoformat()}]}
        plan = {"planId": "p-1", "buckets": [bucket]}
        return run, plan, cs

    def test_completes_branded_when_queue_empty(self, mocker):
        run, plan, cs = self._run_with_running_branded()
        mocker.patch("executor._count_branded_queue", return_value=0)
        stop = mocker.patch("executor._stop_branded_campaign")
        mocker.patch("executor.save_run")
        mocker.patch("executor.get_run", return_value=run)
        mocker.patch("executor.get_plan", return_value=plan)

        from executor import tick
        tick("p-1", "r-1", 0)

        assert cs["status"] == "completed"
        stop.assert_called_once_with(cs)

    def test_does_not_poll_connect_v2_for_branded(self, mocker):
        run, plan, cs = self._run_with_running_branded()
        mocker.patch("executor._count_branded_queue", return_value=5)
        poll = mocker.patch("executor._poll_campaign_state")
        mocker.patch("executor.save_run")
        mocker.patch("executor.get_run", return_value=run)
        mocker.patch("executor.get_plan", return_value=plan)

        from executor import tick
        tick("p-1", "r-1", 0)

        poll.assert_not_called()
        assert cs["status"] == "running"  # still running, queue has 5 items

    def test_count_error_does_not_transition_status(self, mocker):
        run, plan, cs = self._run_with_running_branded()
        mocker.patch("executor._count_branded_queue", side_effect=Exception("DDB error"))
        mocker.patch("executor.save_run")
        mocker.patch("executor.get_run", return_value=run)
        mocker.patch("executor.get_plan", return_value=plan)

        from executor import tick
        tick("p-1", "r-1", 0)  # must not raise

        assert cs["status"] == "running"  # unchanged on poll error
```

- [ ] **Step 2: Run to verify failures**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-plans
python3 -m pytest tests/unit/test_executor_v2.py::TestTickBrandedPoll -v 2>&1
```

Expected: failures — `_count_branded_queue` not defined.

- [ ] **Step 3: Add `_count_branded_queue` to executor.py**

```python
def _count_branded_queue(campaign_id: str) -> int:
    """Count PENDING+DISPATCHING items in VipProgressiveCampaignQueue for this campaign.

    Uses eventual consistency — a count of 0 means the queue is drained.
    Callers must handle exceptions (transient DDB errors) without transitioning state.
    """
    ddb = _get_ddb_client()
    resp = ddb.query(
        TableName=_CAMPAIGN_QUEUE_TABLE_BRANDED,
        KeyConditionExpression="campaignId = :c",
        FilterExpression="#s IN (:p, :d)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":c": {"S": campaign_id},
            ":p": {"S": "PENDING"},
            ":d": {"S": "DISPATCHING"},
        },
        Select="COUNT",
    )
    return resp.get("Count", 0)
```

- [ ] **Step 4: Add branded poll block to `tick()` poll loop**

In `executor.py` at line 422, find the poll loop:

```python
    for cs in bucket_state["campaignStates"]:
        if cs["status"] == "running" and cs.get("connectCampaignId"):
            _poll_campaign_state(cs)
```

Add an `elif` branch immediately after the Connect V2 poll block for running campaigns:

```python
    for cs in bucket_state["campaignStates"]:
        if cs["status"] == "running" and cs.get("connectCampaignId"):
            _poll_campaign_state(cs)
            # ... (existing duration/prestart/force-stop logic unchanged) ...

        elif cs.get("brandedCampaignId") and cs["status"] == "running":
            # Branded campaign: poll VipProgressiveCampaignQueue instead of Connect
            try:
                count = _count_branded_queue(cs["brandedCampaignId"])
            except Exception as _poll_exc:
                logger.warning(
                    "tick: branded queue poll failed for %s: %s",
                    cs["brandedCampaignId"], type(_poll_exc).__name__,
                )
                continue  # don't transition on poll error

            if count == 0:
                logger.info(
                    "tick: branded campaign %s queue drained — completing",
                    cs["brandedCampaignId"],
                )
                cs["status"] = "completed"
                cs["exitReason"] = "queue_drained"
                cs["completedAt"] = _now_iso()
                _stop_branded_campaign(cs)
```

The bucket-time expiry for branded campaigns is handled by the existing `_expire_bucket` path (Task 8), which already fires when `endTime` is exceeded. No change needed here for the time-based expiry.

- [ ] **Step 5: Run all plans tests**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-plans
python3 -m pytest tests/unit/ -v 2>&1
```

Expected: all passing.

- [ ] **Step 6: Commit**

```bash
git add services/api-plans/src/executor.py \
        services/api-plans/tests/unit/test_executor_v2.py
git commit -m "feat(plans): tick() polls VipProgressiveCampaignQueue for branded campaigns

Add _count_branded_queue() that queries PENDING+DISPATCHING items.
Add elif branch in tick() poll loop: when count==0 transition to completed
and call _stop_branded_campaign(). Poll errors silently skip (next tick
retries). Connect V2 poll path unchanged — only triggered by connectCampaignId."
```

---

### Task 7: `_stop_branded_campaign()` + prewarm exclusion

**Files:**
- Modify: `services/api-plans/src/executor.py`
- Test: `services/api-plans/tests/unit/test_executor_v2.py`

**Interfaces:**
- Produces: `_stop_branded_campaign(cs: dict) -> None` — deletes from `VipActiveBrandedCampaigns`, expires queue items; never raises
- Produces: `_prestart_next_bucket` and `_prestart_plan` skip branded campaigns
- Consumed by: Tasks 6 and 8

- [ ] **Step 1: Write failing tests**

```python
class TestStopBrandedCampaign:
    def _cs(self, campaign_id="bc-1", queue_arn="arn::queue/q1"):
        return {
            "campaignId": campaign_id,
            "brandedCampaignId": campaign_id,
            "queueArn": queue_arn,
            "status": "running",
        }

    def test_deletes_from_vip_active_branded_campaigns(self, mocker):
        ddb = mocker.patch("executor._get_ddb_client").return_value
        mocker.patch("executor._expire_branded_queue_items")

        from executor import _stop_branded_campaign
        _stop_branded_campaign(self._cs())

        ddb.delete_item.assert_called_once_with(
            TableName=mocker.ANY,
            Key={
                "pk": {"S": "QUEUE#arn::queue/q1"},
                "sk": {"S": "CAMPAIGN#bc-1"},
            },
        )

    def test_expires_queue_items(self, mocker):
        mocker.patch("executor._get_ddb_client").return_value.delete_item.return_value = {}
        expire = mocker.patch("executor._expire_branded_queue_items")

        from executor import _stop_branded_campaign
        _stop_branded_campaign(self._cs())

        expire.assert_called_once_with("bc-1")

    def test_noop_when_no_branded_campaign_id(self, mocker):
        ddb = mocker.patch("executor._get_ddb_client").return_value

        from executor import _stop_branded_campaign
        _stop_branded_campaign({"campaignId": "c-1", "status": "running"})

        ddb.delete_item.assert_not_called()

    def test_delete_failure_does_not_raise(self, mocker):
        mocker.patch("executor._get_ddb_client").return_value.delete_item.side_effect = (
            Exception("DDB error")
        )
        mocker.patch("executor._expire_branded_queue_items")

        from executor import _stop_branded_campaign
        _stop_branded_campaign(self._cs())  # must not raise

    def test_expire_failure_does_not_raise(self, mocker):
        mocker.patch("executor._get_ddb_client").return_value.delete_item.return_value = {}
        mocker.patch("executor._expire_branded_queue_items", side_effect=Exception("batch fail"))

        from executor import _stop_branded_campaign
        _stop_branded_campaign(self._cs())  # must not raise


class TestPrestartSkipsBranded:
    def test_prestart_next_bucket_skips_branded(self, mocker):
        branded_campaign = {
            "id": "bc-1", "deliveryType": "branded",
            "campaignConfig": {"queueArn": "arn::queue/q1"},
        }
        create = mocker.patch("executor._create_campaign_only")
        # build a run where next bucket has a branded campaign
        # ... (construct minimal run/plan with next bucket having branded campaign)
        # Verify _create_campaign_only is never called
        create.assert_not_called()
```

- [ ] **Step 2: Run to verify failures**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-plans
python3 -m pytest tests/unit/test_executor_v2.py::TestStopBrandedCampaign -v 2>&1
```

- [ ] **Step 3: Add `_stop_branded_campaign` and `_expire_branded_queue_items` to executor.py**

```python
def _expire_branded_queue_items(campaign_id: str) -> None:
    """Set TTL=now on all PENDING/DISPATCHING items in VipProgressiveCampaignQueue.

    DynamoDB TTL sweep will delete them within 48h. This stops the consumer from
    dequeuing contacts for a stopped/aborted campaign.
    Batch-writes up to 3000 items in pages of 25 — acceptable for segment max size.
    """
    import time as _time
    now_epoch = int(_time.time())
    ddb = _get_ddb_client()
    last_key = None
    while True:
        kwargs = dict(
            TableName=_CAMPAIGN_QUEUE_TABLE_BRANDED,
            KeyConditionExpression="campaignId = :c",
            FilterExpression="#s IN (:p, :d)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":c": {"S": campaign_id},
                ":p": {"S": "PENDING"},
                ":d": {"S": "DISPATCHING"},
            },
            ProjectionExpression="campaignId, sk",
        )
        if last_key:
            kwargs["ExclusiveStartKey"] = last_key
        resp = ddb.query(**kwargs)
        items = resp.get("Items", [])
        # Batch write in chunks of 25
        for i in range(0, len(items), 25):
            chunk = items[i : i + 25]
            ddb.batch_write_item(
                RequestItems={
                    _CAMPAIGN_QUEUE_TABLE_BRANDED: [
                        {
                            "PutRequest": {
                                "Item": {
                                    "campaignId": item["campaignId"],
                                    "sk":         item["sk"],
                                    "status":     {"S": "EXPIRED"},
                                    "ttl":        {"N": str(now_epoch)},
                                }
                            }
                        }
                        for item in chunk
                    ]
                }
            )
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break


def _stop_branded_campaign(cs: dict) -> None:
    """Remove branded campaign from VipActiveBrandedCampaigns and expire its queue.

    Must NEVER raise — log errors and continue so the calling abort/stop path
    completes even if cleanup partially fails.
    """
    campaign_id = cs.get("brandedCampaignId")
    queue_arn = cs.get("queueArn")
    if not campaign_id:
        return

    try:
        _get_ddb_client().delete_item(
            TableName=_ACTIVE_BRANDED_CAMPAIGNS_TABLE,
            Key={
                "pk": {"S": f"QUEUE#{queue_arn}"},
                "sk": {"S": f"CAMPAIGN#{campaign_id}"},
            },
        )
    except Exception as exc:
        logger.error(
            "_stop_branded_campaign: delete failed for %s: %s",
            campaign_id, type(exc).__name__,
        )

    try:
        _expire_branded_queue_items(campaign_id)
    except Exception as exc:
        logger.error(
            "_stop_branded_campaign: expire queue failed for %s: %s",
            campaign_id, type(exc).__name__,
        )
```

- [ ] **Step 4: Add prewarm exclusion in `_prestart_next_bucket` (line 1339)**

In `executor.py` at line 1339, inside the loop `for ci, campaign in enumerate(next_bucket.get("campaigns", [])):`, add skip at the top of the loop body:

```python
    for ci, campaign in enumerate(next_bucket.get("campaigns", [])):
        if _is_branded(campaign):
            continue  # branded campaigns have no warmup phase — start directly in _start_one_campaign
        if not campaign.get("dependsOn"):
```

Apply the same exclusion in `_prestart_plan` at line 1609 (similar loop structure).

- [ ] **Step 5: Run all tests**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-plans
python3 -m pytest tests/unit/ -v 2>&1
```

- [ ] **Step 6: Commit**

```bash
git add services/api-plans/src/executor.py \
        services/api-plans/tests/unit/test_executor_v2.py
git commit -m "feat(plans): _stop_branded_campaign() + prewarm exclusion

Add _stop_branded_campaign(cs): deletes VipActiveBrandedCampaigns entry
and TTL-expires VipProgressiveCampaignQueue items. Never raises — log and
continue so abort/stop callers are not blocked by cleanup failures.
Exclude branded campaigns from _prestart_next_bucket and _prestart_plan:
no Connect V2 warmup applies, they start directly in _start_one_campaign."
```

---

### Task 8: Wire abort/stop/expire paths to `_stop_branded_campaign`

**Files:**
- Modify: `services/api-plans/src/executor.py` — 6 locations
- Test: `services/api-plans/tests/unit/test_executor_v2.py`

**Interfaces:**
- Consumes: `_stop_branded_campaign()` (Task 7)
- All existing abort/stop/expire behaviors for Connect V2 campaigns unchanged

- [ ] **Step 1: Write failing tests**

```python
class TestAbortStopCallsStopBranded:
    def _cs_branded(self, cid="bc-1"):
        return {
            "campaignId": cid, "brandedCampaignId": cid,
            "queueArn": "arn::queue/q1", "status": "running",
        }

    def test_abort_run_stops_branded_campaign(self, mocker):
        cs = self._cs_branded()
        run = {"planId": "p-1", "runId": "r-1", "status": "running",
               "bucketStates": [{"status": "running", "campaignStates": [cs]}]}
        mocker.patch("executor.get_run", return_value=run)
        stop = mocker.patch("executor._stop_branded_campaign")
        mocker.patch("executor.save_run")
        mocker.patch("executor.update_plan_pending_warmup")
        mocker.patch("executor.unlock_plan_run")
        mocker.patch("executor._delete_bucket_schedule_safe")

        from executor import abort_run
        abort_run("p-1", "r-1")

        stop.assert_called_once_with(cs)

    def test_expire_bucket_stops_branded_campaign(self, mocker):
        cs = self._cs_branded()
        run = {"planId": "p-1", "runId": "r-1",
               "bucketStates": [{"status": "running", "campaignStates": [cs]}]}
        plan = {"planId": "p-1", "buckets": [_bucket_def()]}
        stop = mocker.patch("executor._stop_branded_campaign")
        mocker.patch("executor._advance_bucket")

        from executor import _expire_bucket
        _expire_bucket(run, plan, 0)

        stop.assert_called_once_with(cs)

    def test_force_stop_campaign_stops_branded(self, mocker):
        cs = self._cs_branded()
        run = {"planId": "p-1", "runId": "r-1", "status": "running",
               "bucketStates": [{"status": "running", "campaignStates": [cs]}]}
        mocker.patch("executor.get_run", return_value=run)
        stop = mocker.patch("executor._stop_branded_campaign")
        mocker.patch("executor.save_run")
        mocker.patch("executor.unlock_plan_run")

        from executor import force_stop_campaign
        force_stop_campaign("p-1", "r-1", 0, 0)

        stop.assert_called_once_with(cs)
```

- [ ] **Step 2: Run to verify failures**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-plans
python3 -m pytest tests/unit/test_executor_v2.py::TestAbortStopCallsStopBranded -v 2>&1
```

- [ ] **Step 3: Wire `_stop_branded_campaign` into abort_run (line 630)**

In `abort_run`, inside the inner `for cs in bs["campaignStates"]:` loop, add after the existing Connect stop logic and before the status update:

```python
            for cs in bs["campaignStates"]:
                if cs["status"] == "running" and cs.get("connectCampaignId"):
                    _safe_stop_campaign(cs["connectCampaignId"])
                if cs["status"] == "warming" and cs.get("connectCampaignId"):
                    _safe_stop_campaign(cs["connectCampaignId"])
                    _safe_delete_campaign(cs["connectCampaignId"])
                    if cs.get("segmentName"):
                        _safe_delete_segment(cs["segmentName"])
                # ── Branded cleanup ──
                if cs["status"] in ("running",) and cs.get("brandedCampaignId"):
                    _stop_branded_campaign(cs)
                # ─────────────────────
                if cs["status"] in ("running", "warming", "queued", "creating"):
                    cs["status"] = "cancelled"
```

- [ ] **Step 4: Wire `_stop_branded_campaign` into `_force_finish_internal` (line 676)**

```python
        for cs in bs["campaignStates"]:
            if cs["status"] == "running" and cs.get("connectCampaignId"):
                _safe_stop_campaign(cs["connectCampaignId"])
            # ... existing warming cleanup ...
            # ── Branded cleanup ──
            if cs["status"] == "running" and cs.get("brandedCampaignId"):
                _stop_branded_campaign(cs)
            # ─────────────────────
            if cs["status"] in ("running", "warming", "queued", "creating"):
                cs["status"] = "completed"
```

- [ ] **Step 5: Wire `_stop_branded_campaign` into `_expire_bucket` (line 1461)**

```python
    for cs in bucket_state["campaignStates"]:
        if cs["status"] == "running":
            if cs.get("connectCampaignId"):
                _safe_stop_campaign(cs["connectCampaignId"])
            if cs.get("brandedCampaignId"):    # ← add
                _stop_branded_campaign(cs)     # ← add
            cs["status"] = "expired"
```

- [ ] **Step 6: Wire into `force_stop_campaign` (line 1063), `skip_campaign` (line 996)**

In each function, find the inner `if cs["status"] == "running"` block that calls `_safe_stop_campaign`. Add:

```python
            if cs.get("brandedCampaignId"):
                _stop_branded_campaign(cs)
```

- [ ] **Step 7: Wire into `force_start_campaign` Phase-2 cleanup (line 794)**

In `force_start_campaign`, find where the old campaign is stopped before re-starting. Add `_stop_branded_campaign(cs)` call before re-invoking `_start_one_campaign`.

- [ ] **Step 8: Run all tests**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-plans
python3 -m pytest tests/unit/ -v 2>&1
```

Expected: all passing.

- [ ] **Step 9: Commit**

```bash
git add services/api-plans/src/executor.py \
        services/api-plans/tests/unit/test_executor_v2.py
git commit -m "feat(plans): wire _stop_branded_campaign into all abort/stop/expire paths

Add _stop_branded_campaign(cs) calls in: abort_run, _force_finish_internal,
_expire_bucket, force_stop_campaign, skip_campaign, force_start_campaign.
Without these, dialing would continue after a run is explicitly stopped —
agents would keep receiving calls from a campaign the operator shut down."
```

---

### Task 9: CDK permissions — executor Lambda

**Files:**
- Modify: `infra/lib/stacks/api-plans-stack.ts`
- Modify: `infra/bin/app.ts`

**Interfaces:**
- Consumes: `progressiveDialer.campaignQueueTable`, `progressiveDialer.activeBrandedCampaignsTable`, `progressiveDialer.seederFunction` (all from Task 3)
- Produces: executor Lambda can invoke seeder, read/write both branded DDB tables

- [ ] **Step 1: Add optional props to `ApiPlansStack`**

In `infra/lib/stacks/api-plans-stack.ts`, find the `ApiPlansStackProps` interface. Add:

```typescript
export interface ApiPlansStackProps extends cdk.StackProps {
  // ... existing props ...
  readonly progressiveCampaignQueueTable?: dynamodb.ITable;
  readonly activeBrandedCampaignsTable?: dynamodb.ITable;
  readonly progressiveDialerSeederArn?: string;
}
```

In the constructor body, after the existing IAM grants, add:

```typescript
// ── Progressive Branded Dialer — executor access ─────────────────
if (props.progressiveCampaignQueueTable) {
  props.progressiveCampaignQueueTable.grantReadWriteData(role);
  lambdaFn.addEnvironment(
    'CAMPAIGN_QUEUE_TABLE_BRANDED',
    props.progressiveCampaignQueueTable.tableName,
  );
}

if (props.activeBrandedCampaignsTable) {
  props.activeBrandedCampaignsTable.grantReadWriteData(role);
  lambdaFn.addEnvironment(
    'ACTIVE_BRANDED_CAMPAIGNS_TABLE',
    props.activeBrandedCampaignsTable.tableName,
  );
}

if (props.progressiveDialerSeederArn) {
  role.addToPolicy(
    new iam.PolicyStatement({
      sid: 'InvokeProgressiveDialerSeeder',
      actions: ['lambda:InvokeFunction'],
      resources: [props.progressiveDialerSeederArn],
    }),
  );
  lambdaFn.addEnvironment(
    'PROGRESSIVE_DIALER_SEEDER_ARN',
    props.progressiveDialerSeederArn,
  );
}
```

- [ ] **Step 2: Wire in `app.ts`**

In `infra/bin/app.ts`, find the `ApiPlansStack` constructor call (Stack #6). Add the three new props:

```typescript
const plans = new ApiPlansStack(app, 'VipAdminApiPlansStack', {
  // ... existing props ...
  progressiveCampaignQueueTable: progressiveDialer.campaignQueueTable,
  activeBrandedCampaignsTable:   progressiveDialer.activeBrandedCampaignsTable,
  progressiveDialerSeederArn:    progressiveDialer.seederFunction.functionArn,
});
```

Note: the `progressiveDialer` stack is declared AFTER `plans` in the current `app.ts` order. Move `progressiveDialer` declaration to BEFORE `plans` (currently Stack #8 → move to before Stack #6).

- [ ] **Step 3: Verify TypeScript compiles**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/infra
npx tsc --noEmit 2>&1
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add infra/lib/stacks/api-plans-stack.ts infra/bin/app.ts
git commit -m "feat(infra): executor Lambda permissions for progressive branded dialer

Add optional props to ApiPlansStack: progressiveCampaignQueueTable,
activeBrandedCampaignsTable, progressiveDialerSeederArn. Wire from
ApiProgressiveDialerStack in app.ts (moved to before ApiPlansStack).
Executor gets DDB read/write on both tables + lambda:InvokeFunction on seeder."
```

---

### Task 10: Plan validation for branded campaign fields

**Files:**
- Modify: `services/api-plans/src/handlers/plans.py`
- Test: `services/api-plans/tests/unit/` (add `test_plans_handler.py` or extend existing)

**Interfaces:**
- Validated fields when `deliveryType == "branded"`: `campaignConfig.queueArn`, `campaignConfig.contactFlowId`, `campaignConfig.sourcePhone`
- Non-branded campaigns: validation unchanged

- [ ] **Step 1: Write failing tests**

Create `services/api-plans/tests/unit/test_branded_validation.py`:

```python
import pytest
from unittest.mock import MagicMock

def _validate_plan(plan_body):
    """Call the plan creation handler validation logic."""
    from handlers.plans import _validate_plan_body
    return _validate_plan_body(plan_body)


class TestBrandedCampaignValidation:
    def _valid_branded_campaign(self):
        return {
            "id": "bc-1",
            "name": "Branded",
            "deliveryType": "branded",
            "campaignConfig": {
                "dialerType": "progressive",
                "queueArn": "arn:aws:connect:us-east-1:123:instance/i/queue/q",
                "contactFlowId": "flow-abc",
                "sourcePhone": "+19174105649",
            },
        }

    def test_valid_branded_campaign_passes_validation(self):
        campaign = self._valid_branded_campaign()
        bucket = {"name": "b1", "campaigns": [campaign], "startTime": "09:00", "endTime": "17:00"}
        plan = {"name": "Test Plan", "timezone": "America/New_York", "buckets": [bucket]}
        errors = _validate_plan(plan)
        assert errors == [] or errors is None

    def test_missing_queue_arn_fails_validation(self):
        campaign = self._valid_branded_campaign()
        del campaign["campaignConfig"]["queueArn"]
        bucket = {"name": "b1", "campaigns": [campaign], "startTime": "09:00", "endTime": "17:00"}
        plan = {"name": "Test Plan", "timezone": "America/New_York", "buckets": [bucket]}
        errors = _validate_plan(plan)
        assert any("queueArn" in str(e) for e in (errors or []))

    def test_missing_contact_flow_id_fails_validation(self):
        campaign = self._valid_branded_campaign()
        del campaign["campaignConfig"]["contactFlowId"]
        bucket = {"name": "b1", "campaigns": [campaign]}
        plan = {"name": "Test Plan", "timezone": "America/New_York", "buckets": [bucket]}
        errors = _validate_plan(plan)
        assert any("contactFlowId" in str(e) for e in (errors or []))

    def test_missing_source_phone_fails_validation(self):
        campaign = self._valid_branded_campaign()
        del campaign["campaignConfig"]["sourcePhone"]
        bucket = {"name": "b1", "campaigns": [campaign]}
        plan = {"name": "Test Plan", "timezone": "America/New_York", "buckets": [bucket]}
        errors = _validate_plan(plan)
        assert any("sourcePhone" in str(e) for e in (errors or []))

    def test_non_branded_campaign_not_affected(self):
        campaign = {
            "id": "c-1", "name": "Connect", "deliveryType": "connect",
            "campaignConfig": {"dialerType": "progressive", "bandwidthAllocation": 1.0},
        }
        bucket = {"name": "b1", "campaigns": [campaign]}
        plan = {"name": "Test Plan", "timezone": "America/New_York", "buckets": [bucket]}
        errors = _validate_plan(plan)
        assert errors == [] or errors is None
```

- [ ] **Step 2: Run to verify failures**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-plans
python3 -m pytest tests/unit/test_branded_validation.py -v 2>&1
```

- [ ] **Step 3: Add validation to handlers/plans.py**

Find the plan body validation function in `services/api-plans/src/handlers/plans.py`. Add branded campaign validation:

```python
def _validate_branded_campaign(campaign: dict, bucket_name: str, ci: int) -> list[str]:
    """Return validation errors for a branded campaign's required config fields."""
    errors = []
    cfg = campaign.get("campaignConfig") or {}
    prefix = f"bucket '{bucket_name}' campaign[{ci}]"
    for required_key in ("queueArn", "contactFlowId", "sourcePhone"):
        if not cfg.get(required_key):
            errors.append(f"{prefix}: deliveryType='branded' requires campaignConfig.{required_key}")
    return errors
```

In the existing plan body validation function, add a call to `_validate_branded_campaign` inside the campaign loop:

```python
    for bi, bucket in enumerate(plan_body.get("buckets", [])):
        for ci, campaign in enumerate(bucket.get("campaigns", [])):
            if campaign.get("deliveryType") == "branded":
                errors.extend(_validate_branded_campaign(campaign, bucket.get("name", str(bi)), ci))
```

- [ ] **Step 4: Run all plans tests**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-plans
python3 -m pytest tests/unit/ -v 2>&1
```

Expected: all passing.

- [ ] **Step 5: Commit**

```bash
git add services/api-plans/src/handlers/plans.py \
        services/api-plans/tests/unit/test_branded_validation.py
git commit -m "feat(plans): validate branded campaign required fields on plan save

When deliveryType='branded', require queueArn, contactFlowId, and sourcePhone
in campaignConfig. Returns 400 with descriptive error if any is missing.
Non-branded campaigns are unaffected."
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Task |
|-----------------|------|
| `_is_branded()` discriminator, `deliveryType: "branded"` | Task 1 |
| `_initial_campaign_state` with `brandedCampaignId`/`queueArn` | Task 1 |
| Seeder direct invocation support | Task 2 |
| `VipActiveBrandedCampaigns` table + GSI | Task 3 |
| Consumer GSI query, multi-campaign priority sort | Task 4 |
| `_start_one_campaign` branded path, empty segment, seeder error | Task 5 |
| `tick()` branded poll, count=0 → completed, poll error → no-op | Task 6 |
| `_stop_branded_campaign`, `_expire_branded_queue_items` | Task 7 |
| Prewarm exclusion (`_prestart_next_bucket`, `_prestart_plan`) | Task 7 |
| abort_run, `_force_finish_internal`, `_expire_bucket` wiring | Task 8 |
| `force_stop_campaign`, `skip_campaign`, `force_start_campaign` wiring | Task 8 |
| CDK: executor permissions (DDB + lambda invoke) | Task 9 |
| Plan validation for branded required fields | Task 10 |
| 152 existing tests must not regress | Every task (run full suite each time) |
| PHI never in logs | Enforced throughout — only `type(e).__name__`, no phone numbers |

**Type consistency check:** `_stop_branded_campaign(cs: dict)` is used consistently in Tasks 6, 7, 8. `_count_branded_queue(campaign_id: str) -> int` is defined in Task 6 and used only in Task 6. `_invoke_seeder(...)` defined and used in Task 5 only. `_is_branded(campaign: dict) -> bool` defined Task 1, used Tasks 5, 7, 8. All consistent.

---

Plan complete and saved to `docs/superpowers/plans/2026-06-18-progressive-branded-dialer-plans-integration.md`.

**Two execution options:**

**1. Subagent-Driven (recommended)** — Fresh subagent per task, review between tasks, continuous execution

**2. Inline Execution** — Execute tasks in this session using executing-plans

Which approach?
