# Progressive Branded Dialer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a new `PROGRESSIVE_BRANDED` campaign mode to `vip-connect-external-campaigns` that fires a First Orion push per contact exactly 22 seconds before each individual SIP INVITE, enabling call branding for batch Progressive campaigns.

**Architecture:** A Kinesis Agent Event Stream consumer (Lambda 1) detects when an agent transitions to Available, fires a First Orion push for the next contact in the campaign queue, then enqueues an SQS message with `DelaySeconds=22`. An SQS consumer (Lambda 2) fires `StartOutboundVoiceContact` after the delay, routing through the existing AMD contact flow. A DynamoDB per-agent lock prevents double-dispatch under concurrent agent-availability events.

**PROGRESSIVE_BRANDED does NOT use OC V2 or Journeys.** For this mode, NO Outbound Campaign is created in Amazon Connect. The webapp seeds `VipProgressiveCampaignQueue` with contacts, and our Lambda calls `StartOutboundVoiceContact` directly — this is a native Connect API that initiates a single outbound call independently of the OC V2 scheduler. We ARE the dialer. The existing `*Agent-staffed Campaign AMD` contact flow still executes as-is once the call connects, but WE control exactly when each SIP INVITE fires. Non-branded campaigns (Predictive, Agentless, standard Progressive) continue using OC V2 + Journeys exactly as today — nothing is removed or modified.

**Tech Stack:** Python 3.12, boto3, AWS CDK (TypeScript), DynamoDB (conditional writes), SQS (delay queue), Kinesis (Agent Event Stream), `StartOutboundVoiceContact` API, First Orion REST API.

---

## Critical Constraints (from production research — read before coding)

- `StartOutboundVoiceContact` throttle: **2 RPS / 5 burst**, shared per AWS account+region (not per instance). Lambda 2 reserved concurrency must be ≤ 2.
- No dedicated `AVAILABLE` event exists. Filter: `EventType=STATE_CHANGE` + `AgentSnapshot.AgentStatus.Type=ROUTABLE` + `AgentStatus.Name=Available`. Also guard: if `NextAgentStatus` is present and its `Name ≠ Available`, agent is queuing a break — skip dispatch.
- STATE_CHANGE fires for 6 non-availability reasons too (routing profile reassignment, hierarchy group change, etc.). Always check `Type=ROUTABLE`, never just `EventType=STATE_CHANGE`.
- `TrafficType=CAMPAIGN` is required only when `EnableAnswerMachineDetection=true` in the API call. Since AMD detection happens inside the existing `*Agent-staffed Campaign AMD` contact flow (not via the API parameter), use `TrafficType=GENERAL` (default). No service quota increase needed.
- `AvailableSlots` in the Kinesis event is NOT atomic — use DynamoDB conditional writes for the per-agent lock, not slot count checks.
- Contact phone numbers are PHI. Never log them. Use `correlationId` (agentId + timestamp) in CloudWatch logs.

---

## File Map

```
services/
  api-progressive-dialer/
    src/
      handler_consumer.py          # Kinesis ESM entrypoint
      handler_caller.py            # SQS entrypoint
      handler_seeder.py            # HTTP entrypoint: POST /campaigns/{id}/seed-branded
      agent_event_filter.py        # Parse + filter AVAILABLE agent events
      campaign_queue.py            # DynamoDB FIFO contact queue operations
      agent_lock.py                # DynamoDB per-agent dispatch lock
      first_orion_client.py        # First Orion auth + push_precall
      connect_caller.py            # StartOutboundVoiceContact wrapper
    tests/
      unit/
        test_agent_event_filter.py
        test_campaign_queue.py
        test_agent_lock.py
        test_first_orion_client.py
        test_connect_caller.py
        test_handler_consumer.py
        test_handler_caller.py
    requirements.txt
    deploy.sh
infra/
  lib/
    stacks/
      api-progressive-dialer-stack.ts   # New CDK stack (consumer + caller + seeder Lambdas)
      api-stack.ts                      # Register /campaigns/{id}/seed-branded route (modify)
  bin/
    app.ts                              # Register new stack + pass seederFunction to ApiStack (modify)
```

> **Seeder approach:** The seeder Lambda lives in the `api-progressive-dialer` stack. It reads segment membership via Amazon Connect Customer Profiles `GetSegmentDefinition` + `BatchGetProfile` — no VPC needed. Phone numbers come from `profile["PhoneNumber"]` (standard CP field, mapped from `_source.phone` by the `leads-data-mapping` object type). Max segment size is 3,000 members (CP hard cap), requiring ≤30 batch calls (~6s total).

**DynamoDB tables (new):**

| Table | PK | SK | Notes |
|---|---|---|---|
| `VipProgressiveCampaignQueue` | `campaignId` (S) | `createdAt#contactUUID` (S) | FIFO contact queue. status: PENDING/DISPATCHING/DIALED. TTL=24h |
| `VipProgressiveAgentLocks` | `agentId` (S) | — | Per-agent lock. TTL=600s auto-release |

**SQS queue:** `vip-progressive-dialer-calls` (standard, NOT FIFO), `DelaySeconds=22`, visibility timeout=60s.

---

## Task 1: Enable Agent Event Stream on Connect Instance

> ✅ **ALREADY DONE** — Stream `vip-use1-datastream` is ACTIVE and already associated with the Connect instance for AGENT_EVENTS. Verified via `list-instance-storage-configs` on 2026-06-16. Skip this task.

**Stream ARN (confirmed production):**
`arn:aws:kinesis:us-east-1:165505826690:stream/vip-use1-datastream`

Use this ARN in Task 10 when configuring the CDK stack.

- [x] ~~Step 1: Open Amazon Connect console~~ — done
- [x] ~~Step 2: Enable Agent Event Stream~~ — already configured on `vip-use1-datastream`
- [x] ~~Step 3: Verify stream ARN~~ — confirmed ACTIVE
- [x] ~~Step 4: Verify events are flowing~~ — HEART_BEAT events confirmed
- [x] ~~Step 5: Commit note~~ — N/A

---

## Task 2: New Service Scaffold

**Files:**
- Create: `services/api-progressive-dialer/src/handler_consumer.py`
- Create: `services/api-progressive-dialer/src/handler_caller.py`
- Create: `services/api-progressive-dialer/requirements.txt`
- Create: `services/api-progressive-dialer/deploy.sh`

- [ ] **Step 1: Create directory structure**

  ```bash
  mkdir -p services/api-progressive-dialer/src
  mkdir -p services/api-progressive-dialer/tests/unit
  touch services/api-progressive-dialer/tests/__init__.py
  touch services/api-progressive-dialer/tests/unit/__init__.py
  ```

- [ ] **Step 2: Create requirements.txt and add `requests` to the shared layer**

  Create `services/api-progressive-dialer/requirements.txt` (used by `deploy.sh` only):

  ```text
  # services/api-progressive-dialer/requirements.txt
  # boto3 is provided by the Lambda runtime — do not bundle it (causes drift)
  requests>=2.31.0
  ```

  **CRITICAL — also add `requests` to the shared layer's requirements so `cdk deploy` works:**

  `lambda.Code.fromAsset('src/')` copies only `.py` files — it does NOT pip-install anything.
  The shared layer (`buildSharedLayer`) reads `services/shared/requirements.txt` and pip-installs
  everything there. `first_orion_client.py` does `import requests`, so if `requests` is missing
  from the shared layer, every consumer Lambda invocation crashes with `ModuleNotFoundError`
  when deployed via CDK.

  Open `services/shared/requirements.txt` and append:

  ```text
  requests>=2.31.0
  ```

  The file after edit:

  ```text
  redis>=5.0.0,<6
  requests>=2.31.0
  ```

- [ ] **Step 3: Create stub handler_consumer.py**

  ```python
  # services/api-progressive-dialer/src/handler_consumer.py
  """Kinesis Agent Event Stream consumer — dispatches contacts to branded progressive dialer."""
  from __future__ import annotations
  import base64, json, logging, os

  logger = logging.getLogger()
  logger.setLevel(logging.INFO)


  def lambda_handler(event: dict, _context) -> dict:
      records = event.get("Records", [])
      processed = 0
      for record in records:
          raw = base64.b64decode(record["kinesis"]["data"]).decode("utf-8")
          agent_event = json.loads(raw)
          logger.info("Received event type=%s", agent_event.get("EventType"))
          processed += 1
      return {"processed": processed}
  ```

- [ ] **Step 4: Create stub handler_caller.py**

  ```python
  # services/api-progressive-dialer/src/handler_caller.py
  """SQS consumer — fires StartOutboundVoiceContact after the 22s delay."""
  from __future__ import annotations
  import json, logging

  logger = logging.getLogger()
  logger.setLevel(logging.INFO)


  def lambda_handler(event: dict, _context) -> dict:
      for record in event.get("Records", []):
          body = json.loads(record["body"])
          logger.info("Caller received message agent_id=%s", body.get("agentId"))
      return {"status": "ok"}
  ```

- [ ] **Step 5: Create deploy.sh**

  ```bash
  #!/usr/bin/env bash
  # services/api-progressive-dialer/deploy.sh
  set -euo pipefail
  PROFILE="${AWS_PROFILE:-production}"
  REGION="${AWS_REGION:-us-east-1}"
  FUNCTION_CONSUMER="vip-admin-progressive-dialer-consumer"
  FUNCTION_CALLER="vip-admin-progressive-dialer-caller"
  FUNCTION_SEEDER="vip-admin-progressive-dialer-seeder"
  DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  TMP="$(mktemp -d)"

  echo "Building package..."
  pip install -r "$DIR/requirements.txt" -t "$TMP" -q \
    --platform manylinux2014_x86_64 --only-binary=:all: --python-version 3.12
  cp -r "$DIR/src/"* "$TMP/"
  # vip_shared is provided by the Lambda layer (attached by CDK) — do NOT bundle it here
  # to avoid version skew between the layer and the bundled copy.

  cd "$TMP"
  zip -r /tmp/progressive_dialer.zip . -q

  echo "Deploying consumer..."
  aws lambda update-function-code \
    --function-name "$FUNCTION_CONSUMER" \
    --zip-file fileb:///tmp/progressive_dialer.zip \
    --region "$REGION" --profile "$PROFILE" --output json | jq '.FunctionName, .CodeSize'

  echo "Deploying caller..."
  aws lambda update-function-code \
    --function-name "$FUNCTION_CALLER" \
    --zip-file fileb:///tmp/progressive_dialer.zip \
    --region "$REGION" --profile "$PROFILE" --output json | jq '.FunctionName, .CodeSize'

  echo "Deploying seeder..."
  aws lambda update-function-code \
    --function-name "$FUNCTION_SEEDER" \
    --zip-file fileb:///tmp/progressive_dialer.zip \
    --region "$REGION" --profile "$PROFILE" --output json | jq '.FunctionName, .CodeSize'

  rm -rf "$TMP" /tmp/progressive_dialer.zip
  echo "Done."
  ```

  ```bash
  chmod +x services/api-progressive-dialer/deploy.sh
  ```

- [ ] **Step 6: Create pytest.ini**

  ```bash
  cat > services/api-progressive-dialer/pytest.ini << 'EOF'
  [pytest]
  testpaths = tests
  pythonpath = src
  EOF
  ```

  This allows `pytest` to be run from `services/api-progressive-dialer/` without setting `PYTHONPATH` manually.

- [ ] **Step 7: Commit**

  ```bash
  git add services/api-progressive-dialer/
  git commit -m "feat(progressive-dialer): scaffold new service with stub handlers and pytest config"
  ```

---

## Task 3: agent_event_filter.py

**Files:**
- Create: `services/api-progressive-dialer/src/agent_event_filter.py`
- Create: `services/api-progressive-dialer/tests/unit/test_agent_event_filter.py`

- [ ] **Step 1: Write failing tests**

  ```python
  # services/api-progressive-dialer/tests/unit/test_agent_event_filter.py
  import pytest
  from agent_event_filter import is_agent_available, extract_agent_info

  # ── is_agent_available ────────────────────────────────────────────────

  def _state_change_event(status_type: str, status_name: str, next_status_name: str | None = None) -> dict:
      event = {
          "EventType": "STATE_CHANGE",
          "AgentSnapshot": {
              "AgentStatus": {
                  "Type": status_type,
                  "Name": status_name,
                  "StartTimestamp": "2026-06-16T14:00:00.000Z",
              },
              "Configuration": {
                  "RoutingProfile": {
                      "Concurrency": [
                          {"Channel": "VOICE", "AvailableSlots": 1, "MaximumSlots": 1}
                      ]
                  }
              }
          }
      }
      if next_status_name is not None:
          event["AgentSnapshot"]["NextAgentStatus"] = {
              "Name": next_status_name,
              "EnqueuedTimestamp": "2026-06-16T14:05:00.000Z"
          }
      return event

  def test_routable_available_is_available():
      assert is_agent_available(_state_change_event("ROUTABLE", "Available")) is True

  def test_custom_status_not_available():
      assert is_agent_available(_state_change_event("CUSTOM", "Break")) is False

  def test_offline_not_available():
      assert is_agent_available(_state_change_event("OFFLINE", "Offline")) is False

  def test_routable_but_pending_break_not_available():
      # Agent queued a break — will go offline after current contact
      assert is_agent_available(_state_change_event("ROUTABLE", "Available", next_status_name="Lunch")) is False

  def test_routable_next_status_also_available_is_available():
      # NextAgentStatus=Available is fine (e.g., returning from break)
      assert is_agent_available(_state_change_event("ROUTABLE", "Available", next_status_name="Available")) is True

  def test_heartbeat_not_available():
      assert is_agent_available({"EventType": "HEART_BEAT"}) is False

  def test_login_not_available():
      assert is_agent_available({"EventType": "LOGIN"}) is False

  def test_logout_not_available():
      assert is_agent_available({"EventType": "LOGOUT"}) is False

  def test_state_change_routing_profile_update_not_available():
      # STATE_CHANGE fired for config change, not status — AgentStatus.Type=CUSTOM
      assert is_agent_available(_state_change_event("CUSTOM", "Available")) is False

  # ── extract_agent_info ────────────────────────────────────────────────

  def test_extract_agent_info_returns_id_and_queue():
      event = _state_change_event("ROUTABLE", "Available")
      event["AgentARN"] = "arn:aws:connect:us-east-1:165505826690:instance/abc/agent/agent-001"
      event["AgentSnapshot"]["Configuration"] = {
          "RoutingProfile": {
              "DefaultOutboundQueue": {
                  "ARN": "arn:aws:connect:us-east-1:165505826690:instance/abc/queue/queue-001"
              },
              "Concurrency": []
          }
      }
      info = extract_agent_info(event)
      assert info["agent_arn"] == event["AgentARN"]
      assert info["queue_arn"] == "arn:aws:connect:us-east-1:165505826690:instance/abc/queue/queue-001"

  def test_extract_agent_info_missing_queue_returns_none():
      event = _state_change_event("ROUTABLE", "Available")
      event["AgentARN"] = "arn:aws:connect:us-east-1:165505826690:instance/abc/agent/agent-001"
      info = extract_agent_info(event)
      assert info["queue_arn"] is None

  # ── is_queue_allowed ──────────────────────────────────────────────────

  def test_is_queue_allowed_returns_true_when_allowed_set_is_empty():
      # Empty set means "allow all queues" (no filter configured)
      from agent_event_filter import is_queue_allowed
      assert is_queue_allowed("arn:aws:connect:us-east-1:123:instance/abc/queue/q1", set()) is True

  def test_is_queue_allowed_returns_true_when_queue_in_set():
      from agent_event_filter import is_queue_allowed
      allowed = {"arn:aws:connect:us-east-1:123:instance/abc/queue/q1"}
      assert is_queue_allowed("arn:aws:connect:us-east-1:123:instance/abc/queue/q1", allowed) is True

  def test_is_queue_allowed_returns_false_when_queue_not_in_set():
      from agent_event_filter import is_queue_allowed
      allowed = {"arn:aws:connect:us-east-1:123:instance/abc/queue/q2"}
      assert is_queue_allowed("arn:aws:connect:us-east-1:123:instance/abc/queue/q1", allowed) is False

  def test_is_queue_allowed_returns_false_when_queue_arn_is_none():
      from agent_event_filter import is_queue_allowed
      assert is_queue_allowed(None, {"arn:aws:connect:us-east-1:123:instance/abc/queue/q1"}) is False
  ```

- [ ] **Step 2: Run tests — expect FAIL**

  ```bash
  cd services/api-progressive-dialer
  python -m pytest tests/unit/test_agent_event_filter.py -v 2>&1 | head -20
  ```

  Expected: `ModuleNotFoundError: No module named 'agent_event_filter'`

- [ ] **Step 3: Implement agent_event_filter.py**

  ```python
  # services/api-progressive-dialer/src/agent_event_filter.py
  """Parses Amazon Connect Agent Event Stream records to detect agent availability."""
  from __future__ import annotations


  def is_agent_available(event: dict) -> bool:
      """Return True only when the agent just became fully available for a new call.

      Filters:
      - EventType must be STATE_CHANGE (not HEART_BEAT / LOGIN / LOGOUT)
      - AgentStatus.Type must be ROUTABLE (built-in Available only)
      - AgentStatus.Name must be 'Available'
      - NextAgentStatus must NOT be set to a non-Available status
        (agent queued a break and will go offline after current contact)
      """
      if event.get("EventType") != "STATE_CHANGE":
          return False

      snapshot = event.get("AgentSnapshot") or {}
      status = snapshot.get("AgentStatus") or {}

      if status.get("Type") != "ROUTABLE":
          return False
      if status.get("Name") != "Available":
          return False

      next_status = snapshot.get("NextAgentStatus")
      if next_status and next_status.get("Name") != "Available":
          return False

      return True


  def extract_agent_info(event: dict) -> dict:
      """Extract agent ARN and default outbound queue ARN from an agent event."""
      snapshot = event.get("AgentSnapshot") or {}
      config = snapshot.get("Configuration") or {}
      routing_profile = config.get("RoutingProfile") or {}
      default_queue = routing_profile.get("DefaultOutboundQueue") or {}

      return {
          "agent_arn": event.get("AgentARN"),
          "queue_arn": default_queue.get("ARN"),
      }


  def is_queue_allowed(queue_arn: str | None, allowed_arns: set[str]) -> bool:
      """Return True if this agent's queue should be served by the branded dialer.

      If allowed_arns is empty, all queues are allowed (no filter configured).
      If allowed_arns is non-empty, queue_arn must be in the set.
      Returns False when queue_arn is None regardless of allowed_arns.
      """
      if queue_arn is None:
          return False
      if not allowed_arns:
          return True  # no filter = accept all
      return queue_arn in allowed_arns
  ```

- [ ] **Step 4: Run tests — expect PASS**

  ```bash
  cd services/api-progressive-dialer
  PYTHONPATH=src python -m pytest tests/unit/test_agent_event_filter.py -v
  ```

  Expected: 15 PASSED.

- [ ] **Step 5: Commit**

  ```bash
  git add services/api-progressive-dialer/src/agent_event_filter.py \
          services/api-progressive-dialer/tests/unit/test_agent_event_filter.py
  git commit -m "feat(progressive-dialer): agent event filter with ROUTABLE+NextAgentStatus guards"
  ```

---

## Task 4: campaign_queue.py

**Files:**
- Create: `services/api-progressive-dialer/src/campaign_queue.py`
- Create: `services/api-progressive-dialer/tests/unit/test_campaign_queue.py`

The queue uses DynamoDB with `PK=campaignId`, `SK=createdAt#uuid`. Dequeue uses a conditional update (`PENDING → DISPATCHING`) to prevent double-dispatch under concurrent Lambda invocations.

- [ ] **Step 1: Write failing tests**

  ```python
  # services/api-progressive-dialer/tests/unit/test_campaign_queue.py
  from unittest.mock import MagicMock, patch
  import pytest
  from campaign_queue import CampaignQueue, Contact

  TABLE_NAME = "VipProgressiveCampaignQueue"


  def _make_queue(items: list[dict] | None = None):
      mock_table = MagicMock()
      mock_resource = MagicMock()
      mock_resource.Table.return_value = mock_table
      q = CampaignQueue(TABLE_NAME, dynamodb_resource=mock_resource)
      q._table = mock_table
      if items is not None:
          mock_table.query.return_value = {"Items": items}
      return q, mock_table


  def test_dequeue_returns_none_when_empty():
      q, table = _make_queue(items=[])
      assert q.dequeue("campaign-1") is None


  def test_dequeue_returns_contact_and_marks_dispatching():
      item = {
          "campaignId": "campaign-1",
          "contactUUID": "uuid-abc",
          "sk": "2026-06-16T14:00:00.000Z#uuid-abc",
          "phone": "+15551234567",
          "status": "PENDING",
          "ttl": 9999999999,
      }
      q, table = _make_queue(items=[item])
      table.update_item.return_value = {}

      contact = q.dequeue("campaign-1")

      assert contact is not None
      assert contact.phone == "+15551234567"
      assert contact.campaign_id == "campaign-1"
      assert contact.contact_uuid == "uuid-abc"

      # Verify conditional write was made
      call_kwargs = table.update_item.call_args[1]
      assert call_kwargs["ConditionExpression"] is not None
      assert "DISPATCHING" in str(call_kwargs["ExpressionAttributeValues"])


  def test_dequeue_skips_non_pending_items():
      items = [
          {"campaignId": "campaign-1", "sk": "ts1#uuid1", "contactUUID": "uuid1",
           "phone": "+15551111111", "status": "DISPATCHING"},
          {"campaignId": "campaign-1", "sk": "ts2#uuid2", "contactUUID": "uuid2",
           "phone": "+15552222222", "status": "PENDING"},
      ]
      q, table = _make_queue(items=items)
      table.update_item.return_value = {}

      contact = q.dequeue("campaign-1")
      assert contact.contact_uuid == "uuid2"


  def test_mark_dialed_updates_status():
      q, table = _make_queue()
      q.mark_dialed("campaign-1", "ts1#uuid1", "contact-id-xyz")
      call_kwargs = table.update_item.call_args[1]
      assert "DIALED" in str(call_kwargs["ExpressionAttributeValues"])
      assert "contact-id-xyz" in str(call_kwargs["ExpressionAttributeValues"])


  def test_enqueue_writes_pending_item():
      q, table = _make_queue()
      q.enqueue("campaign-1", "+15559876543")
      call_kwargs = table.put_item.call_args[1]
      item = call_kwargs["Item"]
      assert item["campaignId"] == "campaign-1"
      assert item["status"] == "PENDING"
      assert "phone" in item


  def test_reset_to_pending_updates_status():
      q, table = _make_queue()
      q.reset_to_pending("campaign-1", "ts1#uuid1")
      table.update_item.assert_called_once()
      call_kwargs = table.update_item.call_args[1]
      assert "PENDING" in str(call_kwargs["ExpressionAttributeValues"])
      assert call_kwargs["Key"] == {"campaignId": "campaign-1", "sk": "ts1#uuid1"}


  def test_get_phone_returns_phone():
      """Caller reads phone from DDB instead of SQS body — PHI stays out of the queue."""
      q, table = _make_queue()
      table.get_item.return_value = {
          "Item": {"campaignId": "campaign-1", "sk": "ts1#uuid1", "phone": "+15551234567"}
      }
      phone = q.get_phone("campaign-1", "ts1#uuid1")
      assert phone == "+15551234567"
      table.get_item.assert_called_once_with(Key={"campaignId": "campaign-1", "sk": "ts1#uuid1"})


  def test_get_phone_returns_none_when_item_missing():
      q, table = _make_queue()
      table.get_item.return_value = {}
      assert q.get_phone("campaign-1", "ts1#uuid1") is None
  ```

- [ ] **Step 2: Run tests — expect FAIL**

  ```bash
  cd services/api-progressive-dialer
  PYTHONPATH=src python -m pytest tests/unit/test_campaign_queue.py -v 2>&1 | head -10
  ```

  Expected: `ModuleNotFoundError: No module named 'campaign_queue'`

- [ ] **Step 3: Implement campaign_queue.py**

  ```python
  # services/api-progressive-dialer/src/campaign_queue.py
  """DynamoDB-backed FIFO contact queue for progressive branded campaigns.

  Table: VipProgressiveCampaignQueue
    PK: campaignId (S)
    SK: createdAt#contactUUID (S)  — ISO-8601 timestamp + UUID ensures strict FIFO
    Attributes:
      phone (S)          — customer phone number (PHI — encrypted at rest via KMS CMK)
      status (S)         — PENDING | DISPATCHING | DIALED | DONE
      contactUUID (S)    — for reference in the SK
      agentId (S)        — set when dispatched
      contactId (S)      — set after StartOutboundVoiceContact succeeds
      ttl (N)            — epoch seconds, 24h from enqueue
  """
  from __future__ import annotations

  import time
  import uuid
  from dataclasses import dataclass
  from datetime import datetime, timezone

  import boto3
  from boto3.dynamodb.conditions import Attr, Key


  @dataclass
  class Contact:
      campaign_id: str
      contact_uuid: str
      sk: str
      phone: str


  class CampaignQueue:
      def __init__(self, table_name: str, dynamodb_resource=None) -> None:
          self._table = (dynamodb_resource or boto3.resource("dynamodb")).Table(table_name)

      def enqueue(self, campaign_id: str, phone: str) -> str:
          """Add a contact to the queue. Returns the SK."""
          contact_uuid = str(uuid.uuid4())
          ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
          sk = f"{ts}#{contact_uuid}"
          ttl = int(time.time()) + 86400  # 24h

          self._table.put_item(Item={
              "campaignId": campaign_id,
              "sk": sk,
              "contactUUID": contact_uuid,
              "phone": phone,
              "status": "PENDING",
              "ttl": ttl,
          })
          return sk

      def dequeue(self, campaign_id: str) -> Contact | None:
          """Atomically dequeue the oldest PENDING contact. Returns None if empty.

          Paginates through the partition without Limit so that contacts in
          DISPATCHING/DIALED state at the front never block PENDING items further
          back. DynamoDB applies FilterExpression after Limit, so Limit=10 would
          silently return 0 results if the first 10 items are all non-PENDING.
          """
          query_kwargs: dict = {
              "KeyConditionExpression": Key("campaignId").eq(campaign_id),
              "FilterExpression": Attr("status").eq("PENDING"),
              "ScanIndexForward": True,  # oldest first
          }
          while True:
              response = self._table.query(**query_kwargs)
              for item in response.get("Items", []):
                  if item.get("status") != "PENDING":
                      continue  # client-side guard: mock returns all items regardless of filter
                  try:
                      self._table.update_item(
                          Key={"campaignId": item["campaignId"], "sk": item["sk"]},
                          UpdateExpression="SET #s = :dispatching",
                          ConditionExpression=Attr("status").eq("PENDING"),
                          ExpressionAttributeNames={"#s": "status"},
                          ExpressionAttributeValues={":dispatching": "DISPATCHING"},
                      )
                      return Contact(
                          campaign_id=item["campaignId"],
                          contact_uuid=item["contactUUID"],
                          sk=item["sk"],
                          phone=item["phone"],
                      )
                  except self._table.meta.client.exceptions.ConditionalCheckFailedException:
                      continue  # another Lambda won the race — try next item
              if "LastEvaluatedKey" not in response:
                  return None
              query_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

      def mark_dialed(self, campaign_id: str, sk: str, contact_id: str) -> None:
          """Record the Connect contactId after StartOutboundVoiceContact succeeds."""
          self._table.update_item(
              Key={"campaignId": campaign_id, "sk": sk},
              UpdateExpression="SET #s = :dialed, contactId = :cid",
              ExpressionAttributeNames={"#s": "status"},
              ExpressionAttributeValues={":dialed": "DIALED", ":cid": contact_id},
          )

      def reset_to_pending(self, campaign_id: str, sk: str) -> None:
          """Reset a DISPATCHING contact back to PENDING so the next available agent retries it.

          Called by handler_caller on dial failure (throttle, limit exceeded) to prevent
          contacts from being permanently stranded in DISPATCHING status.
          """
          try:
              self._table.update_item(
                  Key={"campaignId": campaign_id, "sk": sk},
                  UpdateExpression="SET #s = :pending",
                  ConditionExpression=Attr("status").eq("DISPATCHING"),
                  ExpressionAttributeNames={"#s": "status"},
                  ExpressionAttributeValues={":pending": "PENDING"},
              )
          except self._table.meta.client.exceptions.ConditionalCheckFailedException:
              pass  # already transitioned by another invocation — safe to ignore

      def get_phone(self, campaign_id: str, sk: str) -> str | None:
          """Return the phone number for a contact item. Returns None if item not found.

          Used by handler_caller to read the destination phone from DynamoDB instead of
          carrying it in the SQS message body — keeps PHI out of SQS and the DLQ.
          HIPAA: the returned value is PHI; the caller must not log it.
          """
          resp = self._table.get_item(Key={"campaignId": campaign_id, "sk": sk})
          item = resp.get("Item")
          if item is None:
              return None
          return item.get("phone")
  ```

- [ ] **Step 4: Run tests — expect PASS**

  ```bash
  cd services/api-progressive-dialer
  PYTHONPATH=src python -m pytest tests/unit/test_campaign_queue.py -v
  ```

  Expected: 8 PASSED.

- [ ] **Step 5: Commit**

  ```bash
  git add services/api-progressive-dialer/src/campaign_queue.py \
          services/api-progressive-dialer/tests/unit/test_campaign_queue.py
  git commit -m "feat(progressive-dialer): DynamoDB FIFO campaign queue with conditional dequeue"
  ```

---

## Task 5: agent_lock.py

**Files:**
- Create: `services/api-progressive-dialer/src/agent_lock.py`
- Create: `services/api-progressive-dialer/tests/unit/test_agent_lock.py`

Per-agent lock prevents double-dispatch when two events for the same agent arrive in quick succession. Uses conditional write: `attribute_not_exists(agentId)`.

- [ ] **Step 1: Write failing tests**

  ```python
  # services/api-progressive-dialer/tests/unit/test_agent_lock.py
  from unittest.mock import MagicMock
  import pytest
  from agent_lock import AgentLock

  TABLE_NAME = "VipProgressiveAgentLocks"


  def _make_lock():
      mock_table = MagicMock()
      mock_resource = MagicMock()
      mock_resource.Table.return_value = mock_table
      lock = AgentLock(TABLE_NAME, dynamodb_resource=mock_resource)
      lock._table = mock_table
      return lock, mock_table


  def test_acquire_succeeds_when_no_existing_lock():
      lock, table = _make_lock()
      table.put_item.return_value = {}
      assert lock.acquire("agent-001", campaign_id="campaign-1") is True


  def test_acquire_fails_when_lock_exists():
      from botocore.exceptions import ClientError
      lock, table = _make_lock()
      error = ClientError(
          {"Error": {"Code": "ConditionalCheckFailedException", "Message": ""}},
          "PutItem"
      )
      table.put_item.side_effect = error
      assert lock.acquire("agent-001", campaign_id="campaign-1") is False


  def test_release_deletes_lock():
      lock, table = _make_lock()
      lock.release("agent-001")
      call_kwargs = table.delete_item.call_args[1]
      assert call_kwargs["Key"]["agentId"] == "agent-001"


  def test_acquire_writes_correct_ttl():
      import time
      lock, table = _make_lock()
      table.put_item.return_value = {}
      lock.acquire("agent-001", campaign_id="campaign-1")
      item = table.put_item.call_args[1]["Item"]
      assert item["agentId"] == "agent-001"
      assert item["campaignId"] == "campaign-1"
      # TTL should be ~600s from now
      assert abs(item["ttl"] - (int(time.time()) + 600)) < 5


  def test_acquire_succeeds_when_lock_is_stale():
      """Stale lock (TTL expired but DynamoDB TTL sweep not yet run) must be atomically replaced."""
      from botocore.exceptions import ClientError
      lock, table = _make_lock()
      # First call: ConditionalCheckFailed (live lock) — returns False
      live_lock_error = ClientError(
          {"Error": {"Code": "ConditionalCheckFailedException", "Message": ""}}, "PutItem"
      )
      # Second call: success (stale lock condition matched) — returns True
      table.put_item.side_effect = [live_lock_error, {}]
      assert lock.acquire("agent-001", campaign_id="campaign-1") is False  # live lock blocks
      table.put_item.side_effect = [{}]  # stale lock — put_item succeeds
      assert lock.acquire("agent-001", campaign_id="campaign-1") is True
      # Verify the stale-lock condition expression is actually sent to DynamoDB
      call_kwargs = table.put_item.call_args[1]
      assert call_kwargs["ConditionExpression"] == "attribute_not_exists(agentId) OR #ttl < :now"
      assert call_kwargs["ExpressionAttributeNames"] == {"#ttl": "ttl"}
      assert ":now" in call_kwargs["ExpressionAttributeValues"]
  ```

- [ ] **Step 2: Run tests — expect FAIL**

  ```bash
  PYTHONPATH=src python -m pytest tests/unit/test_agent_lock.py -v 2>&1 | head -10
  ```

- [ ] **Step 3: Implement agent_lock.py**

  ```python
  # services/api-progressive-dialer/src/agent_lock.py
  """Per-agent dispatch lock backed by DynamoDB.

  Table: VipProgressiveAgentLocks
    PK: agentId (S)
    Attributes:
      campaignId (S)  — which campaign this dispatch is for
      lockedAt (N)    — epoch seconds
      ttl (N)         — epoch seconds, 600s from lock acquisition (auto-release)

  Acquire uses an atomic conditional PutItem:
    attribute_not_exists(agentId) OR #ttl < :now

  This prevents the release-before-acquire race: two concurrent consumer invocations
  for the same agent both try to PutItem atomically. Exactly one wins (the other sees
  ConditionalCheckFailed because the winner's item now exists and its TTL is in the future).
  Stale locks whose TTL has expired but whose item was not yet swept by DynamoDB TTL are
  also replaced atomically — no blind delete+insert required.

  release() is still called by: (a) the consumer when the campaign queue is empty and
  (b) the caller on dial failure, to unblock the agent for the next AVAILABLE event.
  """
  from __future__ import annotations

  import time

  import boto3
  from botocore.exceptions import ClientError


  _LOCK_TTL_SECONDS = 600  # 10 min — covers longest expected call


  class AgentLock:
      def __init__(self, table_name: str, dynamodb_resource=None) -> None:
          self._table = (dynamodb_resource or boto3.resource("dynamodb")).Table(table_name)

      def acquire(self, agent_id: str, *, campaign_id: str) -> bool:
          """Attempt to acquire the lock. Returns True on success, False if already locked.

          Succeeds when: no lock exists OR existing lock's TTL is in the past (stale).
          This single atomic write prevents the double-dispatch race that would occur if
          release() were called first and two concurrent invocations both saw the lock absent.
          """
          now = int(time.time())
          try:
              self._table.put_item(
                  Item={
                      "agentId": agent_id,
                      "campaignId": campaign_id,
                      "lockedAt": now,
                      "ttl": now + _LOCK_TTL_SECONDS,
                  },
                  ConditionExpression="attribute_not_exists(agentId) OR #ttl < :now",
                  ExpressionAttributeNames={"#ttl": "ttl"},
                  ExpressionAttributeValues={":now": now},
              )
              return True
          except ClientError as e:
              if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                  return False
              raise

      def release(self, agent_id: str) -> None:
          """Release the lock unconditionally."""
          self._table.delete_item(Key={"agentId": agent_id})
  ```

- [ ] **Step 4: Run tests — expect PASS**

  ```bash
  PYTHONPATH=src python -m pytest tests/unit/test_agent_lock.py -v
  ```

  Expected: 5 PASSED.

- [ ] **Step 5: Commit**

  ```bash
  git add services/api-progressive-dialer/src/agent_lock.py \
          services/api-progressive-dialer/tests/unit/test_agent_lock.py
  git commit -m "feat(progressive-dialer): DynamoDB per-agent dispatch lock"
  ```

---

## Task 6: first_orion_client.py

**Files:**
- Create: `services/api-progressive-dialer/src/first_orion_client.py`
- Create: `services/api-progressive-dialer/tests/unit/test_first_orion_client.py`

First Orion auth flow: `POST /v1/auth` with `X-API-KEY` + `X-SECRET-KEY` → token. Push: `POST /exchange/v1/calls/push` with `Authorization: <token>` + `{"aNumber": caller, "bNumber": callee}`.

The API_KEY and SECRET_KEY come from AWS Secrets Manager (secret name: `vip/firstorion/credentials`). The existing Keeper Lambda uses plain env vars — the new service uses Secrets Manager per HIPAA policy.

- [ ] **Step 1: Create secret in Secrets Manager** (one-time setup)

  First, retrieve the actual credentials from the existing production Keeper Lambda:

  ```bash
  aws lambda get-function-configuration \
    --function-name <KEEPER_LAMBDA_NAME> \
    --region us-east-1 --profile production \
    --query 'Environment.Variables.{KEY:X_API_KEY,SECRET:X_SECRET_KEY}' \
    --output table
  ```

  Then create the secret (replace `<API_KEY>` and `<SECRET_KEY>` with values from above):

  ```bash
  aws secretsmanager create-secret \
    --name "vip/firstorion/credentials" \
    --secret-string '{"api_key":"<API_KEY>","secret_key":"<SECRET_KEY>"}' \
    --region us-east-1 \
    --profile production \
    --kms-key-id alias/vip-data-key \
    --output json | jq '.ARN'
  ```

  Save the returned ARN — you'll need it in Task 10 (CDK stack).

- [ ] **Step 2: Write failing tests**

  ```python
  # services/api-progressive-dialer/tests/unit/test_first_orion_client.py
  from unittest.mock import MagicMock, patch
  import pytest
  from first_orion_client import FirstOrionClient


  def _make_client(api_key="test-key", secret_key="test-secret"):
      return FirstOrionClient(api_key=api_key, secret_key=secret_key)


  def test_get_token_returns_token_on_success():
      client = _make_client()
      mock_resp = MagicMock()
      mock_resp.status_code = 200
      mock_resp.json.return_value = {"token": "tok-abc"}
      with patch("first_orion_client.requests.post", return_value=mock_resp):
          token = client._get_token()
      assert token == "tok-abc"


  def test_get_token_raises_on_error():
      client = _make_client()
      mock_resp = MagicMock()
      mock_resp.status_code = 401
      mock_resp.text = "Unauthorized"
      mock_resp.raise_for_status.side_effect = Exception("401")
      with patch("first_orion_client.requests.post", return_value=mock_resp):
          with pytest.raises(Exception):
              client._get_token()


  def test_push_returns_true_on_200():
      client = _make_client()
      client._token = "tok-xyz"
      client._token_fetched_at = float("inf")  # prevents re-auth
      mock_resp = MagicMock()
      mock_resp.status_code = 200
      with patch("first_orion_client.requests.post", return_value=mock_resp):
          result = client.push(a_number="+19174105649", b_number="+15551234567")
      assert result is True


  def test_push_returns_false_on_4xx():
      client = _make_client()
      client._token = "tok-xyz"
      client._token_fetched_at = float("inf")
      mock_resp = MagicMock()
      mock_resp.status_code = 400
      with patch("first_orion_client.requests.post", return_value=mock_resp):
          result = client.push(a_number="+19174105649", b_number="+15551234567")
      assert result is False


  def test_push_refreshes_token_when_expired():
      client = _make_client()
      client._token = "old-token"
      client._token_fetched_at = 0  # expired

      auth_resp = MagicMock()
      auth_resp.status_code = 200
      auth_resp.json.return_value = {"token": "new-token"}

      push_resp = MagicMock()
      push_resp.status_code = 200

      with patch("first_orion_client.requests.post", side_effect=[auth_resp, push_resp]):
          result = client.push(a_number="+19174105649", b_number="+15551234567")
      assert result is True
      assert client._token == "new-token"


  def test_build_from_secret_calls_secretsmanager():
      mock_sm = MagicMock()
      mock_sm.get_secret_value.return_value = {
          "SecretString": '{"api_key":"k1","secret_key":"s1"}'
      }
      with patch("first_orion_client.boto3.client", return_value=mock_sm):
          client = FirstOrionClient.build_from_secret("vip/firstorion/credentials")
      assert client._api_key == "k1"
      assert client._secret_key == "s1"
  ```

- [ ] **Step 3: Run tests — expect FAIL**

  ```bash
  PYTHONPATH=src python -m pytest tests/unit/test_first_orion_client.py -v 2>&1 | head -10
  ```

- [ ] **Step 4: Implement first_orion_client.py**

  ```python
  # services/api-progressive-dialer/src/first_orion_client.py
  """First Orion INFORM push client.

  Auth: POST https://api.firstorion.com/v1/auth
    Headers: X-SERVICE=auth, X-API-KEY, X-SECRET-KEY
    Response: {"token": "<jwt>"}

  Push: POST https://api.firstorion.com/exchange/v1/calls/push
    Headers: Authorization=<token>, Content-Type=application/json
    Body: {"aNumber": "+1...", "bNumber": "+1..."}
  """
  from __future__ import annotations

  import json
  import logging
  import time

  import boto3
  import requests

  logger = logging.getLogger(__name__)

  _AUTH_URL = "https://api.firstorion.com/v1/auth"
  _PUSH_URL = "https://api.firstorion.com/exchange/v1/calls/push"
  _TOKEN_TTL_SECONDS = 270  # refresh before 5-min expiry


  class FirstOrionClient:
      def __init__(self, *, api_key: str, secret_key: str) -> None:
          self._api_key = api_key
          self._secret_key = secret_key
          self._token: str | None = None
          self._token_fetched_at: float = 0.0

      @classmethod
      def build_from_secret(cls, secret_name: str, region: str = "us-east-1") -> "FirstOrionClient":
          sm = boto3.client("secretsmanager", region_name=region)
          raw = sm.get_secret_value(SecretId=secret_name)["SecretString"]
          creds = json.loads(raw)
          return cls(api_key=creds["api_key"], secret_key=creds["secret_key"])

      def _get_token(self) -> str:
          resp = requests.post(
              _AUTH_URL,
              json={},
              headers={
                  "X-SERVICE": "auth",
                  "X-API-KEY": self._api_key,
                  "X-SECRET-KEY": self._secret_key,
                  "Content-Type": "application/json",
              },
              timeout=10,
          )
          resp.raise_for_status()
          return resp.json()["token"]

      def _ensure_token(self) -> None:
          if self._token is None or time.time() - self._token_fetched_at > _TOKEN_TTL_SECONDS:
              self._token = self._get_token()
              self._token_fetched_at = time.time()
              logger.info("First Orion token refreshed")

      def push(self, *, a_number: str, b_number: str) -> bool:
          """Fire a single pre-call push. Returns True on HTTP 200, False otherwise.

          HIPAA note: a_number and b_number are NOT logged here.
          """
          try:
              self._ensure_token()
              resp = requests.post(
                  _PUSH_URL,
                  json={"aNumber": a_number, "bNumber": b_number},
                  headers={
                      "Authorization": self._token,
                      "Content-Type": "application/json",
                  },
                  timeout=10,
              )
              if resp.status_code == 200:
                  logger.info("First Orion push success")
                  return True
              logger.warning("First Orion push failed status=%d", resp.status_code)
              return False
          except Exception as exc:
              logger.error("First Orion push exception: %s", type(exc).__name__)
              return False
  ```

- [ ] **Step 5: Run tests — expect PASS**

  ```bash
  PYTHONPATH=src python -m pytest tests/unit/test_first_orion_client.py -v
  ```

  Expected: 6 PASSED.

- [ ] **Step 6: Commit**

  ```bash
  git add services/api-progressive-dialer/src/first_orion_client.py \
          services/api-progressive-dialer/tests/unit/test_first_orion_client.py
  git commit -m "feat(progressive-dialer): First Orion push client with Secrets Manager auth"
  ```

---

## Task 7: connect_caller.py

**Files:**
- Create: `services/api-progressive-dialer/src/connect_caller.py`
- Create: `services/api-progressive-dialer/tests/unit/test_connect_caller.py`

Wraps `StartOutboundVoiceContact`. Uses `TrafficType=GENERAL` (AMD in contact flow, no quota increase required). Contact flow ID: `3d24320b-c1e3-40f3-90a2-b6867ef70c85`.

- [ ] **Step 1: Write failing tests**

  ```python
  # services/api-progressive-dialer/tests/unit/test_connect_caller.py
  from unittest.mock import MagicMock
  import pytest
  from connect_caller import ConnectCaller, DialResult

  INSTANCE_ID = "6b3f17ba-68a4-472a-9b20-db1991507009"
  FLOW_ID = "3d24320b-c1e3-40f3-90a2-b6867ef70c85"


  def _make_caller():
      mock_boto = MagicMock()
      return ConnectCaller(instance_id=INSTANCE_ID, contact_flow_id=FLOW_ID, boto_client=mock_boto), mock_boto


  def test_dial_returns_contact_id_on_success():
      caller, mock_boto = _make_caller()
      mock_boto.start_outbound_voice_contact.return_value = {"ContactId": "contact-001"}
      result = caller.dial(destination_phone="+15551234567", queue_id="queue-001")
      assert result.contact_id == "contact-001"
      assert result.success is True


  def test_dial_passes_correct_params():
      caller, mock_boto = _make_caller()
      mock_boto.start_outbound_voice_contact.return_value = {"ContactId": "contact-001"}
      caller.dial(destination_phone="+15551234567", queue_id="queue-001", source_phone="+19174105649")

      call_kwargs = mock_boto.start_outbound_voice_contact.call_args[1]
      assert call_kwargs["InstanceId"] == INSTANCE_ID
      assert call_kwargs["ContactFlowId"] == FLOW_ID
      # Phone number must NOT appear in the logged call — verify it IS passed
      assert call_kwargs["DestinationPhoneNumber"] == "+15551234567"
      assert call_kwargs["QueueId"] == "queue-001"
      assert call_kwargs["TrafficType"] == "GENERAL"


  def test_dial_returns_failure_on_throttle():
      from botocore.exceptions import ClientError
      caller, mock_boto = _make_caller()
      mock_boto.start_outbound_voice_contact.side_effect = ClientError(
          {"Error": {"Code": "TooManyRequestsException", "Message": "Rate exceeded"}},
          "StartOutboundVoiceContact",
      )
      result = caller.dial(destination_phone="+15551234567", queue_id="queue-001")
      assert result.success is False
      assert result.error_code == "TooManyRequestsException"


  def test_dial_returns_failure_on_limit_exceeded():
      from botocore.exceptions import ClientError
      caller, mock_boto = _make_caller()
      mock_boto.start_outbound_voice_contact.side_effect = ClientError(
          {"Error": {"Code": "LimitExceededException", "Message": "Concurrent calls exceeded"}},
          "StartOutboundVoiceContact",
      )
      result = caller.dial(destination_phone="+15551234567", queue_id="queue-001")
      assert result.success is False
      assert result.error_code == "LimitExceededException"
  ```

- [ ] **Step 2: Run tests — expect FAIL**

  ```bash
  PYTHONPATH=src python -m pytest tests/unit/test_connect_caller.py -v 2>&1 | head -10
  ```

- [ ] **Step 3: Implement connect_caller.py**

  ```python
  # services/api-progressive-dialer/src/connect_caller.py
  """Wrapper for StartOutboundVoiceContact.

  Hardcodes the AMD contact flow (3d24320b-c1e3-40f3-90a2-b6867ef70c85) and
  TrafficType=GENERAL (AMD detection happens inside the contact flow, not via
  EnableAnswerMachineDetection=true, so no CAMPAIGN quota increase is needed).

  Throttle limit: 2 RPS / 5 burst shared per account+region. Lambda 2 reserved
  concurrency is set to 2 in the CDK stack to stay within this limit.
  """
  from __future__ import annotations

  import logging
  from dataclasses import dataclass

  import boto3
  from botocore.exceptions import ClientError

  logger = logging.getLogger(__name__)


  @dataclass
  class DialResult:
      success: bool
      contact_id: str | None = None
      error_code: str | None = None


  class ConnectCaller:
      def __init__(
          self,
          *,
          instance_id: str,
          contact_flow_id: str,
          boto_client=None,
      ) -> None:
          self._instance_id = instance_id
          self._contact_flow_id = contact_flow_id
          self._client = boto_client or boto3.client("connect")

      def dial(
          self,
          *,
          destination_phone: str,
          queue_id: str,
          source_phone: str | None = None,
          attributes: dict | None = None,
          client_token: str | None = None,
      ) -> DialResult:
          """Initiate an outbound call. Returns DialResult; never raises.

          client_token: idempotency key (use contactSk) — Connect deduplicates calls with
          the same token within ~7 minutes, preventing double-dials on SQS redelivery.
          HIPAA: destination_phone is PHI and is NOT logged here.
          """
          kwargs: dict = {
              "InstanceId": self._instance_id,
              "ContactFlowId": self._contact_flow_id,
              "DestinationPhoneNumber": destination_phone,
              "QueueId": queue_id,
              "TrafficType": "GENERAL",
          }
          if source_phone:
              kwargs["SourcePhoneNumber"] = source_phone
          if attributes:
              kwargs["Attributes"] = attributes
          if client_token:
              kwargs["ClientToken"] = client_token

          try:
              resp = self._client.start_outbound_voice_contact(**kwargs)
              logger.info("StartOutboundVoiceContact success contact_id=%s", resp["ContactId"])
              return DialResult(success=True, contact_id=resp["ContactId"])
          except ClientError as exc:
              code = exc.response["Error"]["Code"]
              logger.warning("StartOutboundVoiceContact failed error_code=%s", code)
              return DialResult(success=False, error_code=code)
  ```

- [ ] **Step 4: Run tests — expect PASS**

  ```bash
  PYTHONPATH=src python -m pytest tests/unit/test_connect_caller.py -v
  ```

  Expected: 4 PASSED.

- [ ] **Step 5: Commit**

  ```bash
  git add services/api-progressive-dialer/src/connect_caller.py \
          services/api-progressive-dialer/tests/unit/test_connect_caller.py
  git commit -m "feat(progressive-dialer): StartOutboundVoiceContact wrapper with GENERAL traffic type"
  ```

---

## Task 8: handler_consumer.py (Kinesis Lambda)

**Files:**
- Modify: `services/api-progressive-dialer/src/handler_consumer.py`
- Create: `services/api-progressive-dialer/tests/unit/test_handler_consumer.py`

Wires: agent_event_filter → agent_lock.acquire (atomic, handles stale) → campaign_queue.dequeue → first_orion_client.push → SQS enqueue.

- [ ] **Step 1: Write failing tests**

  ```python
  # services/api-progressive-dialer/tests/unit/test_handler_consumer.py
  import base64, json
  from unittest.mock import MagicMock, patch
  import pytest

  # Patch all dependencies before importing handler
  import sys

  def _build_kinesis_event(agent_arn: str, status_type: str, status_name: str, next_status: str | None = None) -> dict:
      agent_event = {
          "EventType": "STATE_CHANGE",
          "AgentARN": agent_arn,
          "AgentSnapshot": {
              "AgentStatus": {"Type": status_type, "Name": status_name},
              "Configuration": {
                  "RoutingProfile": {
                      "DefaultOutboundQueue": {"ARN": "arn:aws:connect:us-east-1:123:instance/abc/queue/q1"},
                      "Concurrency": []
                  }
              }
          }
      }
      if next_status:
          agent_event["AgentSnapshot"]["NextAgentStatus"] = {"Name": next_status, "EnqueuedTimestamp": "2026"}
      encoded = base64.b64encode(json.dumps(agent_event).encode()).decode()
      return {"Records": [{"kinesis": {"data": encoded}}]}


  def test_skips_heartbeat_events():
      heartbeat = {"EventType": "HEART_BEAT"}
      encoded = base64.b64encode(json.dumps(heartbeat).encode()).decode()
      event = {"Records": [{"kinesis": {"data": encoded}}]}

      with patch.dict("os.environ", {
          "CAMPAIGN_QUEUE_TABLE": "VipProgressiveCampaignQueue",
          "AGENT_LOCK_TABLE": "VipProgressiveAgentLocks",
          "SQS_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123/queue",
          "CONNECT_INSTANCE_ID": "instance-1",
          "CONTACT_FLOW_ID": "flow-1",
          "SOURCE_PHONE": "+19174105649",
          "ACTIVE_CAMPAIGN_ID": "campaign-1",
          "FIRSTORION_SECRET_NAME": "vip/firstorion/credentials",
      }):
          with patch("handler_consumer.AgentLock") as mock_lock, \
               patch("handler_consumer.CampaignQueue") as mock_queue, \
               patch("handler_consumer.FirstOrionClient") as mock_fo:
              from handler_consumer import lambda_handler
              result = lambda_handler(event, None)
              mock_lock.return_value.acquire.assert_not_called()


  def test_dispatches_contact_when_agent_available():
      with patch.dict("os.environ", {
          "CAMPAIGN_QUEUE_TABLE": "VipProgressiveCampaignQueue",
          "AGENT_LOCK_TABLE": "VipProgressiveAgentLocks",
          "SQS_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123/queue",
          "CONNECT_INSTANCE_ID": "instance-1",
          "CONTACT_FLOW_ID": "flow-1",
          "SOURCE_PHONE": "+19174105649",
          "ACTIVE_CAMPAIGN_ID": "campaign-1",
          "FIRSTORION_SECRET_NAME": "vip/firstorion/credentials",
      }):
          from campaign_queue import Contact
          mock_lock = MagicMock()
          mock_lock.acquire.return_value = True
          mock_queue = MagicMock()
          mock_queue.dequeue.return_value = Contact(
              campaign_id="campaign-1", contact_uuid="uuid-1",
              sk="ts1#uuid-1", phone="+15551234567"
          )
          mock_fo = MagicMock()
          mock_fo.push.return_value = True
          mock_sqs = MagicMock()
          mock_sqs.send_message.return_value = {}

          if "handler_consumer" in sys.modules:
              del sys.modules["handler_consumer"]

          with patch("handler_consumer.AgentLock", return_value=mock_lock), \
               patch("handler_consumer.CampaignQueue", return_value=mock_queue), \
               patch("handler_consumer.FirstOrionClient") as fo_cls, \
               patch("handler_consumer.boto3.client", return_value=mock_sqs):
              fo_cls.build_from_secret.return_value = mock_fo
              from handler_consumer import lambda_handler
              event = _build_kinesis_event("arn:agent/001", "ROUTABLE", "Available")
              result = lambda_handler(event, None)

          mock_fo.push.assert_called_once()
          mock_sqs.send_message.assert_called_once()
          # Verify SQS message has DelaySeconds=22 and no PHI (no destinationPhone)
          sqs_kwargs = mock_sqs.send_message.call_args[1]
          assert sqs_kwargs["DelaySeconds"] == 22
          body = json.loads(sqs_kwargs["MessageBody"])
          assert "destinationPhone" not in body  # PHI must never appear in SQS body
  ```

- [ ] **Step 2: Run tests — expect FAIL**

  ```bash
  PYTHONPATH=src python -m pytest tests/unit/test_handler_consumer.py -v 2>&1 | head -15
  ```

- [ ] **Step 3: Implement handler_consumer.py**

  ```python
  # services/api-progressive-dialer/src/handler_consumer.py
  """Kinesis Agent Event Stream consumer.

  Flow per record:
  1. Decode + filter: only STATE_CHANGE with ROUTABLE Available + no pending break
  2. Acquire agent lock (atomic conditional write: attribute_not_exists OR stale TTL) — skip if another invocation won
  3. Dequeue next PENDING contact from campaign queue (conditional update)
  4. Fire First Orion push (single call, not polling)
  5. Enqueue SQS message with DelaySeconds=22
  """
  from __future__ import annotations

  import base64
  import json
  import logging
  import os

  import boto3

  from agent_event_filter import extract_agent_info, is_agent_available, is_queue_allowed
  from agent_lock import AgentLock
  from campaign_queue import CampaignQueue
  from first_orion_client import FirstOrionClient

  logger = logging.getLogger()
  logger.setLevel(logging.INFO)

  _CAMPAIGN_QUEUE_TABLE = os.environ["CAMPAIGN_QUEUE_TABLE"]
  _AGENT_LOCK_TABLE = os.environ["AGENT_LOCK_TABLE"]
  _SQS_QUEUE_URL = os.environ["SQS_QUEUE_URL"]
  _CONNECT_INSTANCE_ID = os.environ["CONNECT_INSTANCE_ID"]
  _CONTACT_FLOW_ID = os.environ["CONTACT_FLOW_ID"]
  _SOURCE_PHONE = os.environ["SOURCE_PHONE"]
  _ACTIVE_CAMPAIGN_ID = os.environ["ACTIVE_CAMPAIGN_ID"]
  _FIRSTORION_SECRET_NAME = os.environ["FIRSTORION_SECRET_NAME"]
  _SQS_DELAY_SECONDS = 22
  # Optional comma-separated queue ARNs to restrict which agents trigger dispatch.
  # Empty = all queues served. Set to limit branded dialing to specific outbound queues.
  _ALLOWED_QUEUE_ARNS: set[str] = {
      arn.strip()
      for arn in os.environ.get("ALLOWED_QUEUE_ARNS", "").split(",")
      if arn.strip()
  }

  # Module-level singletons — re-used across warm invocations
  _lock_store: AgentLock | None = None
  _queue_store: CampaignQueue | None = None
  _fo_client: FirstOrionClient | None = None
  _sqs_client = None


  def _get_lock() -> AgentLock:
      global _lock_store
      if _lock_store is None:
          _lock_store = AgentLock(_AGENT_LOCK_TABLE)
      return _lock_store


  def _get_queue() -> CampaignQueue:
      global _queue_store
      if _queue_store is None:
          _queue_store = CampaignQueue(_CAMPAIGN_QUEUE_TABLE)
      return _queue_store


  def _get_fo() -> FirstOrionClient:
      global _fo_client
      if _fo_client is None:
          _fo_client = FirstOrionClient.build_from_secret(_FIRSTORION_SECRET_NAME)
      return _fo_client


  def _get_sqs():
      global _sqs_client
      if _sqs_client is None:
          _sqs_client = boto3.client("sqs")
      return _sqs_client


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
          return  # agent serves a different queue, not our branded campaign

      # Atomic acquire: succeeds if no lock exists OR the existing lock's TTL is expired.
      # No blind release before acquire — that would create a race where two concurrent
      # invocations both delete the lock then both acquire it, causing double-dispatch.
      lock = _get_lock()
      if not lock.acquire(agent_arn, campaign_id=_ACTIVE_CAMPAIGN_ID):
          logger.info("Lock already held for agent — skipping dispatch correlation_id=%s", agent_arn[-8:])
          return

      contact = _get_queue().dequeue(_ACTIVE_CAMPAIGN_ID)
      if contact is None:
          lock.release(agent_arn)
          logger.info("Campaign queue empty — releasing lock correlation_id=%s", agent_arn[-8:])
          return

      # Fire First Orion push — does NOT log phone numbers
      pushed = _get_fo().push(a_number=_SOURCE_PHONE, b_number=contact.phone)
      if not pushed:
          logger.warning("First Orion push failed — will retry via SQS caller")

      # Enqueue SQS with 22s delay regardless of push result
      # (caller Lambda fires StartOutboundVoiceContact, not dependent on push success)
      # destinationPhone (PHI) is intentionally NOT included in the SQS message.
      # The caller Lambda reads it from DynamoDB (encrypted at rest via KMS CMK).
      # This keeps PHI out of SQS and prevents it from sitting in the DLQ for 14 days.
      message = {
          "agentArn": agent_arn,
          "queueArn": queue_arn,
          "campaignId": contact.campaign_id,
          "contactSk": contact.sk,
          "sourcePhone": _SOURCE_PHONE,
          "contactFlowId": _CONTACT_FLOW_ID,
          "instanceId": _CONNECT_INSTANCE_ID,
      }
      _get_sqs().send_message(
          QueueUrl=_SQS_QUEUE_URL,
          MessageBody=json.dumps(message),
          DelaySeconds=_SQS_DELAY_SECONDS,
      )
      logger.info(
          "SQS message enqueued correlation_id=%s campaign_id=%s",
          agent_arn[-8:],
          contact.campaign_id,
      )


  def lambda_handler(event: dict, _context) -> dict:
      dispatched = 0
      for record in event.get("Records", []):
          try:
              _process_record(record)
              dispatched += 1
          except Exception as exc:
              logger.error("Failed to process record: %s", type(exc).__name__)
      return {"dispatched": dispatched}
  ```

- [ ] **Step 4: Run tests — expect PASS**

  ```bash
  # Clear module cache between tests
  PYTHONPATH=src python -m pytest tests/unit/test_handler_consumer.py -v
  ```

  Expected: 2 PASSED.

- [ ] **Step 5: Commit**

  ```bash
  git add services/api-progressive-dialer/src/handler_consumer.py \
          services/api-progressive-dialer/tests/unit/test_handler_consumer.py
  git commit -m "feat(progressive-dialer): Kinesis consumer — agent available → FO push → SQS enqueue"
  ```

---

## Task 9: handler_caller.py (SQS Lambda)

**Files:**
- Modify: `services/api-progressive-dialer/src/handler_caller.py`
- Create: `services/api-progressive-dialer/tests/unit/test_handler_caller.py`

Fires `StartOutboundVoiceContact` after the 22s SQS delay, then updates the campaign queue record.

- [ ] **Step 1: Write failing tests**

  ```python
  # services/api-progressive-dialer/tests/unit/test_handler_caller.py
  import json, sys
  from unittest.mock import MagicMock, patch
  import pytest


  def _make_sqs_event() -> dict:
      # destinationPhone is intentionally absent — caller reads it from DynamoDB
      body = {
          "agentArn": "arn:aws:connect:us-east-1:165505826690:instance/abc/agent/agent-001",
          "queueArn": "arn:aws:connect:us-east-1:165505826690:instance/abc/queue/queue-001",
          "campaignId": "campaign-1",
          "contactSk": "2026-06-16T14:00:00.000Z#uuid-1",
          "sourcePhone": "+19174105649",
          "contactFlowId": "3d24320b-c1e3-40f3-90a2-b6867ef70c85",
          "instanceId": "6b3f17ba-68a4-472a-9b20-db1991507009",
      }
      return {"Records": [{"body": json.dumps(body), "receiptHandle": "rh-001"}]}


  def test_calls_start_outbound_voice_contact():
      if "handler_caller" in sys.modules:
          del sys.modules["handler_caller"]

      with patch.dict("os.environ", {
          "CAMPAIGN_QUEUE_TABLE": "VipProgressiveCampaignQueue",
          "AGENT_LOCK_TABLE": "VipProgressiveAgentLocks",
      }):
          from connect_caller import DialResult
          mock_caller = MagicMock()
          mock_caller.dial.return_value = DialResult(success=True, contact_id="contact-001")
          mock_queue = MagicMock()
          mock_queue.get_phone.return_value = "+15551234567"  # PHI read from DDB, not SQS body

          with patch("handler_caller.ConnectCaller", return_value=mock_caller), \
               patch("handler_caller.CampaignQueue", return_value=mock_queue):
              from handler_caller import lambda_handler
              result = lambda_handler(_make_sqs_event(), None)

          # Verify phone is read from DDB (not from SQS body)
          mock_queue.get_phone.assert_called_once_with("campaign-1", "2026-06-16T14:00:00.000Z#uuid-1")
          mock_caller.dial.assert_called_once()
          call_kwargs = mock_caller.dial.call_args[1]
          assert call_kwargs["queue_id"] == "queue-001"
          assert call_kwargs["destination_phone"] == "+15551234567"
          # ClientToken must equal contactSk for SQS-redelivery idempotency
          assert call_kwargs["client_token"] == "2026-06-16T14:00:00.000Z#uuid-1"
          mock_queue.mark_dialed.assert_called_once_with("campaign-1", "2026-06-16T14:00:00.000Z#uuid-1", "contact-001")


  def test_does_not_raise_on_throttle():
      if "handler_caller" in sys.modules:
          del sys.modules["handler_caller"]

      with patch.dict("os.environ", {
          "CAMPAIGN_QUEUE_TABLE": "VipProgressiveCampaignQueue",
          "AGENT_LOCK_TABLE": "VipProgressiveAgentLocks",
      }):
          from connect_caller import DialResult
          mock_caller = MagicMock()
          mock_caller.dial.return_value = DialResult(success=False, error_code="TooManyRequestsException")
          mock_queue = MagicMock()
          mock_queue.get_phone.return_value = "+15551234567"
          mock_lock = MagicMock()

          with patch("handler_caller.ConnectCaller", return_value=mock_caller), \
               patch("handler_caller.CampaignQueue", return_value=mock_queue), \
               patch("handler_caller.AgentLock", return_value=mock_lock):
              from handler_caller import lambda_handler
              result = lambda_handler(_make_sqs_event(), None)

          # mark_dialed must NOT be called on failure
          mock_queue.mark_dialed.assert_not_called()
          # contact must be reset to PENDING so the next agent can retry it
          mock_queue.reset_to_pending.assert_called_once_with(
              "campaign-1", "2026-06-16T14:00:00.000Z#uuid-1"
          )
          # lock must be released so the next AVAILABLE event can dispatch
          mock_lock.release.assert_called_once_with(
              "arn:aws:connect:us-east-1:165505826690:instance/abc/agent/agent-001"
          )
  ```

- [ ] **Step 2: Run tests — expect FAIL**

  ```bash
  PYTHONPATH=src python -m pytest tests/unit/test_handler_caller.py -v 2>&1 | head -10
  ```

- [ ] **Step 3: Implement handler_caller.py**

  ```python
  # services/api-progressive-dialer/src/handler_caller.py
  """SQS consumer — fires StartOutboundVoiceContact after the 22-second delay.

  Each SQS message corresponds to one agent dispatch. The message was enqueued
  by handler_consumer.py with DelaySeconds=22, which ensures First Orion's
  branding window (10–30s) is active when the SIP INVITE is sent.

  Throttle: StartOutboundVoiceContact is capped at 2 RPS / 5 burst per account+region.
  Lambda reserved concurrency is set to 2 in the CDK stack.
  """
  from __future__ import annotations

  import json
  import logging
  import os

  from agent_lock import AgentLock
  from campaign_queue import CampaignQueue
  from connect_caller import ConnectCaller

  logger = logging.getLogger()
  logger.setLevel(logging.INFO)

  _CAMPAIGN_QUEUE_TABLE = os.environ["CAMPAIGN_QUEUE_TABLE"]
  _AGENT_LOCK_TABLE = os.environ["AGENT_LOCK_TABLE"]

  _queue_store: CampaignQueue | None = None
  _lock_store: AgentLock | None = None


  def _get_queue() -> CampaignQueue:
      global _queue_store
      if _queue_store is None:
          _queue_store = CampaignQueue(_CAMPAIGN_QUEUE_TABLE)
      return _queue_store


  def _get_lock() -> AgentLock:
      global _lock_store
      if _lock_store is None:
          _lock_store = AgentLock(_AGENT_LOCK_TABLE)
      return _lock_store


  def _process_message(body: dict) -> None:
      agent_arn = body["agentArn"]
      queue_arn = body["queueArn"]
      campaign_id = body["campaignId"]
      contact_sk = body["contactSk"]
      instance_id = body["instanceId"]
      contact_flow_id = body["contactFlowId"]
      source_phone = body["sourcePhone"]

      # Extract queue ID from ARN (last segment)
      queue_id = queue_arn.split("/")[-1]

      # Read destination phone from DynamoDB — PHI is not carried in the SQS message body.
      # This keeps the phone number out of SQS and the DLQ (14-day retention).
      destination_phone = _get_queue().get_phone(campaign_id, contact_sk)
      if not destination_phone:
          logger.error(
              "Contact not found or missing phone campaign_id=%s correlation_id=%s",
              campaign_id,
              agent_arn[-8:],
          )
          return

      caller = ConnectCaller(
          instance_id=instance_id,
          contact_flow_id=contact_flow_id,
      )
      result = caller.dial(
          destination_phone=destination_phone,
          queue_id=queue_id,
          source_phone=source_phone,
          # contact_sk is a deterministic idempotency key — Connect deduplicates within ~7min,
          # preventing double-dials if SQS redelivers the message on Lambda failure.
          client_token=contact_sk,
      )

      if result.success:
          _get_queue().mark_dialed(campaign_id, contact_sk, result.contact_id)
          logger.info(
              "Dial success campaign_id=%s contact_id=%s correlation_id=%s",
              campaign_id,
              result.contact_id,
              agent_arn[-8:],
          )
      else:
          logger.warning(
              "Dial failed error_code=%s campaign_id=%s correlation_id=%s",
              result.error_code,
              campaign_id,
              agent_arn[-8:],
          )
          # Reset contact to PENDING so the next available agent can retry it.
          # Without this, the contact stays DISPATCHING forever (24h TTL wasted).
          try:
              _get_queue().reset_to_pending(campaign_id, contact_sk)
          except Exception:
              logger.error("Failed to reset contact to PENDING correlation_id=%s", agent_arn[-8:])
          # Release agent lock so the next AVAILABLE event can dispatch
          _get_lock().release(agent_arn)


  def lambda_handler(event: dict, _context) -> dict:
      for record in event.get("Records", []):
          try:
              body = json.loads(record["body"])
              _process_message(body)
          except Exception as exc:
              logger.error("Failed to process SQS message: %s", type(exc).__name__)
      return {"status": "ok"}
  ```

- [ ] **Step 4: Run tests — expect PASS**

  ```bash
  PYTHONPATH=src python -m pytest tests/unit/test_handler_caller.py -v
  ```

  Expected: 2 PASSED.

- [ ] **Step 5: Run full unit test suite**

  ```bash
  PYTHONPATH=src python -m pytest tests/unit/ -v
  ```

  Expected: 42 PASSED, 0 FAILED.
  (15 agent_event_filter + 8 campaign_queue + 5 agent_lock + 6 first_orion_client + 4 connect_caller + 2 handler_consumer + 2 handler_caller)

- [ ] **Step 6: Commit**

  ```bash
  git add services/api-progressive-dialer/src/handler_caller.py \
          services/api-progressive-dialer/tests/unit/test_handler_caller.py
  git commit -m "feat(progressive-dialer): SQS caller Lambda — StartOutboundVoiceContact after 22s delay"
  ```

---

## Task 10: CDK Stack

**Files:**
- Create: `infra/lib/stacks/api-progressive-dialer-stack.ts`
- Modify: `infra/bin/app.ts`

Follow the same pattern as `api-campaigns-stack.ts`. Lambda reserved concurrency for caller: 2 (matches 2 RPS throttle).

> **Isolation rule:** `ApiProgressiveDialerStack` must be fully autonomous — pass all ARNs and IDs as hardcoded strings or props, never as CDK object references from already-deployed stacks. Cross-stack references to existing stacks (e.g., `VipAdminDataStack`) would add `Fn::ImportValue` outputs to those stacks and force a redeploy of them. Use `kinesis.Stream.fromStreamArn(...)`, `kms.Key.fromKeyArn(...)`, etc. — never `otherStack.someResource`.

- [ ] **Step 0: Verify `buildSharedLayer` utility exists**

  ```bash
  grep -r "buildSharedLayer" infra/lib/ --include="*.ts" -l
  ```

  If found: note the import path and use it in the stack.  
  If NOT found: replace `buildSharedLayer(this)` in the stack below with the inline Lambda layer pattern used in `api-campaigns-stack.ts`. Check how that stack constructs its layer and replicate the same pattern.

- [ ] **Step 1: Write the CDK stack**

  ```typescript
  // infra/lib/stacks/api-progressive-dialer-stack.ts
  import * as cdk from 'aws-cdk-lib';
  import { Construct } from 'constructs';
  import * as lambda from 'aws-cdk-lib/aws-lambda';
  import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
  import * as sqs from 'aws-cdk-lib/aws-sqs';
  import * as iam from 'aws-cdk-lib/aws-iam';
  import * as kms from 'aws-cdk-lib/aws-kms';
  import * as logs from 'aws-cdk-lib/aws-logs';
  import * as kinesis from 'aws-cdk-lib/aws-kinesis';
  import { KinesisEventSource, SqsEventSource } from 'aws-cdk-lib/aws-lambda-event-sources';
  import * as path from 'path';
  import { buildSharedLayer } from '../utils/shared-layer';

  export interface ApiProgressiveDialerStackProps extends cdk.StackProps {
    readonly dataKeyArn: string;           // KMS CMK ARN — passed as string to avoid cross-stack Fn::ImportValue
    readonly connectInstanceId: string;
    readonly agentEventStreamArn: string;  // Kinesis stream ARN — vip-use1-datastream (Task 1 confirmed)
    readonly firstOrionSecretArn: string;  // Secrets Manager ARN from Task 6 Step 1
    readonly sourcePhonenumber: string;    // e.g. "+19174105649"
    readonly activeCampaignId: string;     // Set at deploy time, update per campaign
    readonly profilesDomainName: string;   // CP domain — seeder reads segment + phones from here
    readonly allowedQueueArns?: string;    // Comma-separated queue ARNs to filter agents. Empty = all queues.
    readonly permissionsBoundaryName?: string;
  }

  const CONTACT_FLOW_ID = '3d24320b-c1e3-40f3-90a2-b6867ef70c85';

  export class ApiProgressiveDialerStack extends cdk.Stack {
    public readonly seederFunction: lambda.Function;

    constructor(scope: Construct, id: string, props: ApiProgressiveDialerStackProps) {
      super(scope, id, props);

      if (props.permissionsBoundaryName) {
        const boundary = iam.ManagedPolicy.fromManagedPolicyName(
          this, 'PermissionsBoundary', props.permissionsBoundaryName,
        );
        iam.PermissionsBoundary.of(this).apply(boundary);
      }

      // Resolve KMS key from ARN — avoids cross-stack Fn::ImportValue dependency
      const dataKey = kms.Key.fromKeyArn(this, 'DataKey', props.dataKeyArn);

      // ── DynamoDB: Campaign Queue ──────────────────────────────────────
      const campaignQueueTable = new dynamodb.Table(this, 'CampaignQueueTable', {
        tableName: 'VipProgressiveCampaignQueue',
        partitionKey: { name: 'campaignId', type: dynamodb.AttributeType.STRING },
        sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
        billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
        encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
        encryptionKey: dataKey,
        pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
        timeToLiveAttribute: 'ttl',
        removalPolicy: cdk.RemovalPolicy.RETAIN,
      });

      // ── DynamoDB: Agent Locks ─────────────────────────────────────────
      const agentLockTable = new dynamodb.Table(this, 'AgentLockTable', {
        tableName: 'VipProgressiveAgentLocks',
        partitionKey: { name: 'agentId', type: dynamodb.AttributeType.STRING },
        billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
        encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
        encryptionKey: dataKey,
        timeToLiveAttribute: 'ttl',
        removalPolicy: cdk.RemovalPolicy.RETAIN,
      });

      // ── SQS: Dial delay queue (22s) ───────────────────────────────────
      const dlq = new sqs.Queue(this, 'DialDLQ', {
        queueName: 'vip-progressive-dialer-calls-dlq',
        retentionPeriod: cdk.Duration.days(14),
        encryptionMasterKey: dataKey,
        enforceSSL: true,  // HIPAA: deny non-TLS access even without destinationPhone in body
      });

      const dialQueue = new sqs.Queue(this, 'DialQueue', {
        queueName: 'vip-progressive-dialer-calls',
        deliveryDelay: cdk.Duration.seconds(22),
        visibilityTimeout: cdk.Duration.seconds(60),
        encryptionMasterKey: dataKey,
        enforceSSL: true,
        deadLetterQueue: { queue: dlq, maxReceiveCount: 3 },
      });

      // ── Shared Layer ──────────────────────────────────────────────────
      const sharedLayer = buildSharedLayer(this);

      // ── Lambda: Consumer (Kinesis) ────────────────────────────────────
      const consumerLogGroup = new logs.LogGroup(this, 'ConsumerLogs', {
        logGroupName: '/aws/lambda/vip-admin-progressive-dialer-consumer',
        retention: logs.RetentionDays.ONE_YEAR,
        encryptionKey: dataKey,
        removalPolicy: cdk.RemovalPolicy.RETAIN,
      });

      const consumerRole = new iam.Role(this, 'ConsumerRole', {
        assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      });
      consumerLogGroup.grantWrite(consumerRole);

      const consumerFn = new lambda.Function(this, 'ConsumerFunction', {
        functionName: 'vip-admin-progressive-dialer-consumer',
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: 'handler_consumer.lambda_handler',
        code: lambda.Code.fromAsset(
          path.join(__dirname, '../../../services/api-progressive-dialer/src'),
        ),
        layers: [sharedLayer],
        role: consumerRole,
        logGroup: consumerLogGroup,
        timeout: cdk.Duration.seconds(60),
        memorySize: 256,
        environment: {
          CAMPAIGN_QUEUE_TABLE: campaignQueueTable.tableName,
          AGENT_LOCK_TABLE: agentLockTable.tableName,
          SQS_QUEUE_URL: dialQueue.queueUrl,
          CONNECT_INSTANCE_ID: props.connectInstanceId,
          CONTACT_FLOW_ID: CONTACT_FLOW_ID,
          SOURCE_PHONE: props.sourcePhonenumber,
          ACTIVE_CAMPAIGN_ID: props.activeCampaignId,
          FIRSTORION_SECRET_NAME: 'vip/firstorion/credentials',
          ...(props.allowedQueueArns ? { ALLOWED_QUEUE_ARNS: props.allowedQueueArns } : {}),
        },
      });

      // Kinesis ESM — filter to STATE_CHANGE only to reduce invocations
      const agentStream = kinesis.Stream.fromStreamArn(
        this, 'AgentEventStream', props.agentEventStreamArn,
      );
      consumerFn.addEventSource(new KinesisEventSource(agentStream, {
        startingPosition: lambda.StartingPosition.LATEST,
        batchSize: 100,
        bisectBatchOnError: true,
        filters: [
          lambda.FilterCriteria.filter({
            data: { EventType: lambda.FilterRule.isEqual('STATE_CHANGE') },
          }),
        ],
      }));

      // IAM
      agentStream.grantRead(consumerRole);
      campaignQueueTable.grantReadWriteData(consumerRole);
      agentLockTable.grantReadWriteData(consumerRole);
      dialQueue.grantSendMessages(consumerRole);
      consumerRole.addToPolicy(new iam.PolicyStatement({
        actions: ['secretsmanager:GetSecretValue'],
        resources: [props.firstOrionSecretArn],
      }));
      consumerRole.addToPolicy(new iam.PolicyStatement({
        actions: ['kms:Decrypt', 'kms:GenerateDataKey'],
        resources: [dataKey.keyArn],
      }));

      // ── Lambda: Caller (SQS) ──────────────────────────────────────────
      const callerLogGroup = new logs.LogGroup(this, 'CallerLogs', {
        logGroupName: '/aws/lambda/vip-admin-progressive-dialer-caller',
        retention: logs.RetentionDays.ONE_YEAR,
        encryptionKey: dataKey,
        removalPolicy: cdk.RemovalPolicy.RETAIN,
      });

      const callerRole = new iam.Role(this, 'CallerRole', {
        assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      });
      callerLogGroup.grantWrite(callerRole);

      const callerFn = new lambda.Function(this, 'CallerFunction', {
        functionName: 'vip-admin-progressive-dialer-caller',
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: 'handler_caller.lambda_handler',
        code: lambda.Code.fromAsset(
          path.join(__dirname, '../../../services/api-progressive-dialer/src'),
        ),
        layers: [sharedLayer],
        role: callerRole,
        logGroup: callerLogGroup,
        timeout: cdk.Duration.seconds(30),
        memorySize: 256,
        // Throttle to 2 concurrent max — matches StartOutboundVoiceContact 2 RPS limit
        reservedConcurrentExecutions: 2,
        environment: {
          CAMPAIGN_QUEUE_TABLE: campaignQueueTable.tableName,
          AGENT_LOCK_TABLE: agentLockTable.tableName,
        },
      });

      callerFn.addEventSource(new SqsEventSource(dialQueue, {
        batchSize: 1,  // one dial per invocation
      }));

      campaignQueueTable.grantReadWriteData(callerRole);
      agentLockTable.grantReadWriteData(callerRole);
      dialQueue.grantConsumeMessages(callerRole);
      callerRole.addToPolicy(new iam.PolicyStatement({
        // StartOutboundVoiceContact validates referenced resources under the caller's identity.
        // DescribeContactFlow + DescribeQueue prevent AccessDeniedException on the referenced
        // flow and queue IDs — same issue observed with CreateCampaign in api-campaigns-stack.
        actions: [
          'connect:StartOutboundVoiceContact',
          'connect:DescribeContactFlow',
          'connect:DescribeQueue',
        ],
        resources: [
          `arn:aws:connect:${this.region}:${this.account}:instance/${props.connectInstanceId}/*`,
        ],
      }));
      callerRole.addToPolicy(new iam.PolicyStatement({
        actions: ['kms:Decrypt', 'kms:GenerateDataKey'],
        resources: [dataKey.keyArn],
      }));

      // ── Lambda: Seeder (HTTP via API Gateway) ────────────────────────
      // No VPC — uses Customer Profiles GetSegmentDefinition + BatchGetProfile.
      // Phone is at profile["PhoneNumber"] (standard CP field). 3,000-member max → ≤30 API calls.
      const seederLogGroup = new logs.LogGroup(this, 'SeederLogs', {
        logGroupName: '/aws/lambda/vip-admin-progressive-dialer-seeder',
        retention: logs.RetentionDays.ONE_YEAR,
        encryptionKey: dataKey,
        removalPolicy: cdk.RemovalPolicy.RETAIN,
      });

      const seederRole = new iam.Role(this, 'SeederRole', {
        assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      });
      seederLogGroup.grantWrite(seederRole);
      campaignQueueTable.grantWriteData(seederRole);
      dataKey.grant(seederRole, 'kms:Decrypt', 'kms:GenerateDataKey');
      seederRole.addToPolicy(new iam.PolicyStatement({
        sid: 'CPSeedPerms',
        actions: ['profile:GetSegmentDefinition', 'profile:BatchGetProfile'],
        // Two explicit ARNs required — same pattern as api-profiles-stack and api-segments-stack:
        // • GetSegmentDefinition evaluates against domains/{name}/segment-definitions/*
        // • BatchGetProfile evaluates against domains/{name} (bare domain)
        // A single glob like domains/{name}* is fragile; list both explicitly.
        resources: [
          `arn:aws:profile:${this.region}:${this.account}:domains/${props.profilesDomainName}`,
          `arn:aws:profile:${this.region}:${this.account}:domains/${props.profilesDomainName}/*`,
        ],
      }));

      this.seederFunction = new lambda.Function(this, 'SeederFunction', {
        functionName: 'vip-admin-progressive-dialer-seeder',
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: 'handler_seeder.lambda_handler',
        code: lambda.Code.fromAsset(
          path.join(__dirname, '../../../services/api-progressive-dialer/src'),
        ),
        layers: [sharedLayer],
        role: seederRole,
        logGroup: seederLogGroup,
        timeout: cdk.Duration.seconds(60),
        memorySize: 256,
        environment: {
          CAMPAIGN_QUEUE_TABLE: campaignQueueTable.tableName,
          PROFILES_DOMAIN_NAME: props.profilesDomainName,
        },
      });

      // ── Outputs ───────────────────────────────────────────────────────
      new cdk.CfnOutput(this, 'CampaignQueueTableName', { value: campaignQueueTable.tableName });
      new cdk.CfnOutput(this, 'DialQueueUrl', { value: dialQueue.queueUrl });
      new cdk.CfnOutput(this, 'ConsumerFunctionArn', { value: consumerFn.functionArn });
      new cdk.CfnOutput(this, 'CallerFunctionArn', { value: callerFn.functionArn });
      new cdk.CfnOutput(this, 'SeederFunctionArn', { value: this.seederFunction.functionArn });
    }
  }
  ```

- [ ] **Step 2: Register stack in app.ts**

  Open `infra/bin/app.ts`. Find where the other stacks are registered (e.g., `new ApiCampaignsStack(...)`) and add after them:

  ```typescript
  // Add this import at the top of app.ts:
  import { ApiProgressiveDialerStack } from '../lib/stacks/api-progressive-dialer-stack';

  // Add this near the other stack instantiations (after ApiCampaignsStack, before ApiStack).
  // redisConfig and redisVpcConfig are already declared at the top of app.ts — same values
  // used by ApiSegmentsStack and ApiPlansStack. No new variables needed.
  // NOTE: no inline `tags` block — the global `cdk.Tags.of(app).add(...)` aspect at the
  // bottom of app.ts applies mandatoryTags (Environment, Project, Owner, etc.) to every
  // resource. Adding a per-stack tags prop would produce conflicting values and be silently
  // overridden by the aspect. Rely on the global tags only.
  const progressiveDialer = new ApiProgressiveDialerStack(app, 'ApiProgressiveDialerStack', {
    env,
    dataKeyArn: '<KMS CMK ARN — run: aws kms describe-key --key-id alias/vip-data-key --query KeyMetadata.Arn --output text --region us-east-1 --profile production>',
    connectInstanceId: '6b3f17ba-68a4-472a-9b20-db1991507009',
    agentEventStreamArn: 'arn:aws:kinesis:us-east-1:165505826690:stream/vip-use1-datastream',
    firstOrionSecretArn: '<ARN from Task 6 Step 1>',
    sourcePhonenumber: '+19174105649',
    activeCampaignId: '<current campaign ID>',
    profilesDomainName,  // already declared at top of app.ts: 'amazon-connect-vipmedicalgroup'
    permissionsBoundaryName,
  });

  // Also update the ApiStack instantiation (already in app.ts) to add progressiveDialerSeedFunction:
  // Find: new ApiStack(app, 'VipAdminApiStack', { ... })
  // Add inside props:
  //   progressiveDialerSeedFunction: progressiveDialer.seederFunction,
  ```

- [ ] **Step 3: Build and synth**

  ```bash
  cd infra
  npm run build 2>&1 | tail -5
  npx cdk synth ApiProgressiveDialerStack 2>&1 | tail -20
  ```

  Expected: `Successfully synthesized` with no errors.

- [ ] **Step 4: Deploy (requires confirmation)**

  ```bash
  npx cdk deploy ApiProgressiveDialerStack \
    --profile production \
    --require-approval broadening
  ```

  Review the changeset: should show 2 DynamoDB tables, 1 SQS + 1 DLQ, **3 Lambda functions** (consumer + caller + seeder), 1 Kinesis ESM (consumer), 1 SQS ESM (caller), **3 IAM roles**, **3 CloudWatch log groups**. No VPC resources — seeder calls CP API directly (no Redis, no ENIs).

- [ ] **Step 5: Commit**

  ```bash
  git add infra/lib/stacks/api-progressive-dialer-stack.ts infra/bin/app.ts
  git commit -m "feat(progressive-dialer): CDK stack — Kinesis+SQS+Lambda+DynamoDB with 2-RPS concurrency guard"
  ```

---

## Task 11: Campaign Queue Seeder (API endpoint)

**Files:**

- Create: `services/api-progressive-dialer/src/handler_seeder.py`
- Create: `services/api-progressive-dialer/tests/unit/test_handler_seeder.py`
- Modify: `infra/lib/stacks/api-stack.ts` (add route + prop)
- Modify: `infra/bin/app.ts` (pass seederFunction to ApiStack)

Adds `POST /campaigns/{id}/seed-branded` endpoint. The seeder reads the segment definition from Customer Profiles (`GetSegmentDefinition` → parse profile IDs from SegmentGroups), fetches phone numbers via `BatchGetProfile` (100 IDs per call, `profile["PhoneNumber"]`), and writes to `VipProgressiveCampaignQueue`. No VPC, no Redis. Call this endpoint once before starting a branded campaign run.

- [ ] **Step 1: Write failing tests for handler_seeder.py**

  Create `services/api-progressive-dialer/tests/unit/test_handler_seeder.py`:

  ```python
  # services/api-progressive-dialer/tests/unit/test_handler_seeder.py
  import json
  import sys
  import types
  from unittest.mock import MagicMock, patch

  import pytest

  # ---------------------------------------------------------------------------
  # Minimal env vars so the module-level os.environ reads don't KeyError on import
  # ---------------------------------------------------------------------------
  import os
  os.environ.setdefault("PROFILES_DOMAIN_NAME", "test-domain")
  os.environ.setdefault("CAMPAIGN_QUEUE_TABLE", "test-table")

  import handler_seeder  # noqa: E402  — imported after env setup


  # ---------------------------------------------------------------------------
  # _extract_profile_ids
  # ---------------------------------------------------------------------------

  def _make_segment_groups(*id_lists):
      """Build SegmentGroups in the shape produced by SegmentGroupsTranslator."""
      dimensions = []
      for values in id_lists:
          dimensions.append({
              "ProfileAttributes": {
                  "Attributes": {
                      "ID": {
                          "DimensionType": "INCLUSIVE",
                          "Values": values,
                      }
                  }
              }
          })
      return {"Groups": [{"Dimensions": dimensions}]}


  def test_extract_profile_ids_correct_nesting():
      """Verifies the Attributes key is traversed (the B2 bug was missing this level)."""
      sg = _make_segment_groups(["id-1", "id-2"], ["id-3"])
      result = handler_seeder._extract_profile_ids(sg)
      assert result == ["id-1", "id-2", "id-3"]


  def test_extract_profile_ids_empty_groups():
      assert handler_seeder._extract_profile_ids({}) == []


  def test_extract_profile_ids_missing_attributes_key():
      """Old (wrong) structure — ProfileAttributes.ID — must return empty, not crash."""
      sg = {"Groups": [{"Dimensions": [{"ProfileAttributes": {"ID": {"Values": ["x"]}}}]}]}
      result = handler_seeder._extract_profile_ids(sg)
      assert result == []


  # ---------------------------------------------------------------------------
  # _fetch_phones
  # ---------------------------------------------------------------------------

  def test_fetch_phones_skips_profiles_without_phone_number():
      profile_ids = ["p1", "p2", "p3"]
      mock_cp = MagicMock()
      mock_cp.batch_get_profile.return_value = {
          "Profiles": [
              {"ProfileId": "p1", "PhoneNumber": "+15550001111"},
              {"ProfileId": "p2"},                        # no PhoneNumber — must be skipped
              {"ProfileId": "p3", "PhoneNumber": "+15550002222"},
          ],
          "Errors": [],
      }
      with patch("handler_seeder._get_cp", return_value=mock_cp):
          phones = handler_seeder._fetch_phones(profile_ids)
      assert phones == ["+15550001111", "+15550002222"]


  def test_fetch_phones_empty_list():
      # _fetch_phones has an early-return guard — _get_cp() must never be called with empty input
      with patch("handler_seeder._get_cp") as mock_get_cp:
          phones = handler_seeder._fetch_phones([])
      assert phones == []
      mock_get_cp.assert_not_called()


  # ---------------------------------------------------------------------------
  # lambda_handler — validation
  # ---------------------------------------------------------------------------

  def _api_event(campaign_id=None, body=None):
      event = {"pathParameters": {}, "body": None}
      if campaign_id:
          event["pathParameters"]["id"] = campaign_id
      if body is not None:
          event["body"] = json.dumps(body)
      return event


  def test_lambda_handler_missing_campaign_id():
      resp = handler_seeder.lambda_handler(_api_event(), None)
      assert resp["statusCode"] == 400
      assert "missing campaign id" in json.loads(resp["body"])["error"]


  def test_lambda_handler_missing_segment_name():
      resp = handler_seeder.lambda_handler(_api_event("camp-1", {}), None)
      assert resp["statusCode"] == 400
      assert "missing segmentName" in json.loads(resp["body"])["error"]


  # ---------------------------------------------------------------------------
  # lambda_handler — segment not found (W4)
  # ---------------------------------------------------------------------------

  def test_lambda_handler_segment_not_found_returns_404():
      from botocore.exceptions import ClientError
      mock_cp = MagicMock()
      mock_cp.get_segment_definition.side_effect = ClientError(
          {"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}},
          "GetSegmentDefinition",
      )
      with patch("handler_seeder._get_cp", return_value=mock_cp):
          resp = handler_seeder.lambda_handler(
              _api_event("camp-1", {"segmentName": "missing-seg"}), None
          )
      assert resp["statusCode"] == 404
      assert "segment not found" in json.loads(resp["body"])["error"]


  def test_lambda_handler_access_denied_returns_403():
      from botocore.exceptions import ClientError
      mock_cp = MagicMock()
      mock_cp.get_segment_definition.side_effect = ClientError(
          {"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}},
          "GetSegmentDefinition",
      )
      with patch("handler_seeder._get_cp", return_value=mock_cp):
          resp = handler_seeder.lambda_handler(
              _api_event("camp-1", {"segmentName": "restricted-seg"}), None
          )
      assert resp["statusCode"] == 403
      assert "access denied" in json.loads(resp["body"])["error"]


  # ---------------------------------------------------------------------------
  # lambda_handler — success path
  # ---------------------------------------------------------------------------

  def test_lambda_handler_success_seeds_contacts():
      from unittest.mock import MagicMock, patch, call
      mock_cp = MagicMock()
      mock_cp.get_segment_definition.return_value = {
          "SegmentGroups": {
              "Groups": [{
                  "Dimensions": [{
                      "ProfileAttributes": {
                          "Attributes": {
                              "ID": {"DimensionType": "INCLUSIVE", "Values": ["p1", "p2"]}
                          }
                      }
                  }]
              }]
          }
      }
      mock_cp.batch_get_profile.return_value = {
          "Profiles": [
              {"ProfileId": "p1", "PhoneNumber": "+15550001111"},
              {"ProfileId": "p2", "PhoneNumber": "+15550002222"},
          ],
          "Errors": [],
      }
      mock_table = MagicMock()
      mock_batch_writer = MagicMock()
      mock_table.batch_writer.return_value.__enter__ = MagicMock(return_value=mock_batch_writer)
      mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

      with patch("handler_seeder._get_cp", return_value=mock_cp), \
           patch("handler_seeder._get_table", return_value=mock_table):
          resp = handler_seeder.lambda_handler(
              _api_event("camp-1", {"segmentName": "my-seg"}), None
          )

      assert resp["statusCode"] == 200
      body = json.loads(resp["body"])
      assert body["seeded"] == 2
      assert body["profilesFound"] == 2
      assert body["contactsWithPhone"] == 2
      assert mock_batch_writer.put_item.call_count == 2
  ```

- [ ] **Step 2: Run tests to confirm they fail**

  ```bash
  cd services/api-progressive-dialer
  python -m pytest tests/unit/test_handler_seeder.py -v 2>&1 | tail -20
  ```

  Expected: `ModuleNotFoundError: No module named 'handler_seeder'` or 10 failures (10 tests × 1 file) — the module doesn't exist yet.

- [ ] **Step 3: Create handler_seeder.py**

  Create `services/api-progressive-dialer/src/handler_seeder.py`:

  ```python
  # services/api-progressive-dialer/src/handler_seeder.py
  """Seed VipProgressiveCampaignQueue from a Customer Profiles segment.

  Flow:
    POST /campaigns/{id}/seed-branded {"segmentName": "my-segment"}
    1. GetSegmentDefinition(segmentName) → SegmentGroups
    2. Parse all profile IDs from SegmentGroups (ID IN [...] filter built by reconcile.py)
    3. BatchGetProfile in chunks of 100 → profile["PhoneNumber"]
    4. BatchWriteItem to VipProgressiveCampaignQueue

  HIPAA: phone numbers are not logged. Only counts appear in logs.
  """
  import json
  import os
  import time
  import uuid
  from datetime import datetime, timezone

  import boto3
  from botocore.exceptions import ClientError

  _ddb_table = None
  _cp_client = None

  _PROFILES_DOMAIN = os.environ["PROFILES_DOMAIN_NAME"]
  _QUEUE_TABLE = os.environ["CAMPAIGN_QUEUE_TABLE"]
  _BATCH_SIZE = 100  # CP BatchGetProfile max per call


  def _get_table():
      global _ddb_table
      if _ddb_table is None:
          _ddb_table = boto3.resource("dynamodb").Table(_QUEUE_TABLE)
      return _ddb_table


  def _get_cp():
      global _cp_client
      if _cp_client is None:
          _cp_client = boto3.client("customer-profiles")
      return _cp_client


  def _extract_profile_ids(segment_groups: dict) -> list:
      """Parse all customer/profile IDs from SegmentGroups.

      reconcile.py builds segments via SegmentGroupsTranslator.customer_ids_to_segment_groups,
      which produces this nesting:
        Groups[].Dimensions[].ProfileAttributes.Attributes.ID.Values[...]
      Note the Attributes key between ProfileAttributes and the field name — a direct
      ProfileAttributes.ID lookup always returns None and yields an empty list.
      """
      ids = []
      for group in (segment_groups.get("Groups") or []):
          for dimension in (group.get("Dimensions") or []):
              attrs = (dimension.get("ProfileAttributes") or {}).get("Attributes") or {}
              id_dim = attrs.get("ID") or {}
              ids.extend(id_dim.get("Values") or [])
      return ids


  def _fetch_phones(profile_ids: list) -> list:
      """Return phone numbers for the given profile IDs using BatchGetProfile.

      Profiles without PhoneNumber are silently skipped (no logging of PHI).
      """
      if not profile_ids:
          return []
      phones = []
      cp = _get_cp()
      for i in range(0, len(profile_ids), _BATCH_SIZE):
          chunk = profile_ids[i:i + _BATCH_SIZE]
          resp = cp.batch_get_profile(
              DomainName=_PROFILES_DOMAIN,
              ProfileIds=chunk,
          )
          for profile in (resp.get("Profiles") or []):
              phone = profile.get("PhoneNumber")
              if phone:
                  phones.append(phone)
      return phones


  def lambda_handler(event: dict, _context) -> dict:
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

      segment_name = body.get("segmentName")
      if not segment_name:
          return {"statusCode": 400, "body": json.dumps({"error": "missing segmentName"})}

      # 1. Get segment definition and extract profile IDs
      try:
          resp = _get_cp().get_segment_definition(
              DomainName=_PROFILES_DOMAIN,
              SegmentDefinitionName=segment_name,
          )
      except ClientError as exc:
          code = exc.response["Error"]["Code"]
          if code == "ResourceNotFoundException":
              return {"statusCode": 404, "body": json.dumps({"error": "segment not found"})}
          if code == "AccessDeniedException":
              return {"statusCode": 403, "body": json.dumps({"error": "access denied to segment"})}
          return {"statusCode": 500, "body": json.dumps({"error": "failed to read segment"})}

      profile_ids = _extract_profile_ids(resp.get("SegmentGroups") or {})
      if not profile_ids:
          return {"statusCode": 400, "body": json.dumps({"error": "segment has no members"})}

      # 2. Fetch phones via BatchGetProfile
      phones = _fetch_phones(profile_ids)

      # 3. Write to DynamoDB queue (phone is PHI — encrypted at rest via KMS CMK on the table)
      table = _get_table()
      ttl = int(time.time()) + 86400  # 24h TTL
      written = 0

      with table.batch_writer() as batch:
          for phone in phones:
              contact_uuid = str(uuid.uuid4())
              ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
              batch.put_item(Item={
                  "campaignId": campaign_id,
                  "sk": f"{ts}#{contact_uuid}",
                  "contactUUID": contact_uuid,
                  "phone": phone,
                  "status": "PENDING",
                  "ttl": ttl,
              })
              written += 1

      return {
          "statusCode": 200,
          "headers": {"Content-Type": "application/json"},
          "body": json.dumps({
              "seeded": written,
              "campaignId": campaign_id,
              "profilesFound": len(profile_ids),
              "contactsWithPhone": written,
          }),
      }
  ```

- [ ] **Step 4: Run tests to verify they pass**

  ```bash
  cd services/api-progressive-dialer
  python -m pytest tests/unit/test_handler_seeder.py -v 2>&1 | tail -20
  ```

  Expected: **10 PASSED** (3 extract + 2 fetch + 2 validation + 3 handler paths: 404/403/success). If any fail, fix handler_seeder.py before continuing.

- [ ] **Step 5: Add progressiveDialerSeedFunction prop to api-stack.ts**

  Open `infra/lib/stacks/api-stack.ts`. Add the new prop to `ApiStackProps`:

  ```typescript
  // Inside ApiStackProps interface, alongside the other function props:
  readonly progressiveDialerSeedFunction: lambda.IFunction;
  ```

  Then add the integration and route inside the constructor, after the existing campaigns routes block:

  ```typescript
  // After the existing campaigns routes, add:
  const progressiveDialerIntegration = new integrations.HttpLambdaIntegration(
    'ProgressiveDialerSeedIntegration',
    props.progressiveDialerSeedFunction,
  );
  this.httpApi.addRoutes({
    path: '/campaigns/{id}/seed-branded',
    methods: [apigatewayv2.HttpMethod.POST],
    integration: progressiveDialerIntegration,
    authorizer,
  });
  ```

- [ ] **Step 6: Wire seederFunction to ApiStack in app.ts**

  Open `infra/bin/app.ts`. Find the `new ApiStack(...)` call and add the new prop:

  ```typescript
  // Inside the ApiStack instantiation:
  progressiveDialerSeedFunction: progressiveDialer.seederFunction,
  ```

  The `progressiveDialer` variable was declared in Task 10 Step 2.

- [ ] **Step 7: Validate TypeScript and synth**

  ```bash
  cd infra
  npm run build 2>&1 | tail -5
  npx cdk synth VipAdminApiStack 2>&1 | tail -10
  ```

  Expected: no TypeScript errors, successful synth. The diff for `VipAdminApiStack` should show only `+ Add AWS::ApiGatewayV2::Route` for the new path — nothing else modified.

- [ ] **Step 8: Verify with test seed call (after Task 10 deploy)**

  ```bash
  curl -X POST https://<API_ENDPOINT>/campaigns/<CAMPAIGN_ID>/seed-branded \
    -H "Authorization: Bearer <token>" \
    -H "Content-Type: application/json" \
    -d '{"segmentName": "<your-segment-name>"}'
  ```

  Expected: `{"seeded": N, "campaignId": "<id>", "profilesFound": N, "contactsWithPhone": N}` where N matches the number of segment members that have a `PhoneNumber` in their CP profile.

  Verify in DynamoDB:

  ```bash
  aws dynamodb query \
    --table-name VipProgressiveCampaignQueue \
    --key-condition-expression "campaignId = :cid" \
    --expression-attribute-values '{":cid": {"S": "<CAMPAIGN_ID>"}}' \
    --select COUNT \
    --region us-east-1 --profile production
  ```

  Expected: Count = N.

- [ ] **Step 9: Commit**

  ```bash
  git add services/api-progressive-dialer/src/handler_seeder.py \
          services/api-progressive-dialer/tests/unit/test_handler_seeder.py \
          infra/lib/stacks/api-stack.ts \
          infra/bin/app.ts
  git commit -m "feat(progressive-dialer): seed-branded HTTP endpoint via CP BatchGetProfile (no VPC)"
  ```

---

## Task 12: End-to-End Integration Test

- [ ] **Step 1: Pre-flight checklist**

  Verify all of the following before running:
  - [ ] Kinesis stream ACTIVE (Task 1)
  - [ ] CDK stack deployed (Task 10)
  - [ ] Secrets Manager secret created (Task 6 Step 1)
  - [ ] Consumer Lambda env var `ACTIVE_CAMPAIGN_ID` matches the seeded campaign
  - [ ] At least 1 contact in queue with `status=PENDING`

  ```bash
  aws dynamodb query \
    --table-name VipProgressiveCampaignQueue \
    --key-condition-expression "campaignId = :cid" \
    --filter-expression "#s = :p" \
    --expression-attribute-names '{"#s": "status"}' \
    --expression-attribute-values '{":cid": {"S": "<CAMPAIGN_ID>"}, ":p": {"S": "PENDING"}}' \
    --select COUNT --region us-east-1 --profile production
  ```

- [ ] **Step 2: Trigger manually (simulate agent going Available)**

  > ⚠️ **WARNING: This fires a REAL First Orion push and a REAL outbound call** to the first PENDING contact in the queue. Make sure the queue contains only a test contact (or a consenting QA number) before running. Do NOT run against a full production contact queue.

  Open the Connect CCP (Agent Desktop), set one agent to Available. This will generate a STATE_CHANGE event on the Kinesis stream.

  Or inject a test event directly to the consumer Lambda (replace `<QUEUE_ID>` with the actual outbound queue ID):

  ```bash
  aws lambda invoke \
    --function-name vip-admin-progressive-dialer-consumer \
    --payload '{"Records":[{"kinesis":{"data":"'"$(echo '{"EventType":"STATE_CHANGE","AgentARN":"arn:aws:connect:us-east-1:165505826690:instance/6b3f17ba-68a4-472a-9b20-db1991507009/agent/TEST-AGENT","AgentSnapshot":{"AgentStatus":{"Type":"ROUTABLE","Name":"Available"},"Configuration":{"RoutingProfile":{"DefaultOutboundQueue":{"ARN":"arn:aws:connect:us-east-1:165505826690:instance/6b3f17ba-68a4-472a-9b20-db1991507009/queue/<QUEUE_ID>"},"Concurrency":[]}}}}' | base64 -w0)"'"}}]}' \
    --region us-east-1 --profile production \
    /tmp/consumer-response.json && cat /tmp/consumer-response.json
  ```

- [ ] **Step 3: Monitor CloudWatch Logs (consumer)**

  ```bash
  aws logs tail /aws/lambda/vip-admin-progressive-dialer-consumer \
    --follow --format short --region us-east-1 --profile production
  ```

  Expected log sequence:
  ```
  First Orion token refreshed
  First Orion push success
  SQS message enqueued correlation_id=... campaign_id=...
  ```

- [ ] **Step 4: Monitor CloudWatch Logs (caller, after 22s)**

  ```bash
  aws logs tail /aws/lambda/vip-admin-progressive-dialer-caller \
    --follow --format short --region us-east-1 --profile production
  ```

  Expected:
  ```
  StartOutboundVoiceContact success contact_id=...
  Dial success campaign_id=... contact_id=... correlation_id=...
  ```

- [ ] **Step 5: Verify contact status in DynamoDB**

  ```bash
  aws dynamodb query \
    --table-name VipProgressiveCampaignQueue \
    --key-condition-expression "campaignId = :cid" \
    --filter-expression "#s = :d" \
    --expression-attribute-names '{"#s": "status"}' \
    --expression-attribute-values '{":cid": {"S": "<CAMPAIGN_ID>"}, ":d": {"S": "DIALED"}}' \
    --region us-east-1 --profile production
  ```

  Expected: 1 item with `status=DIALED` and `contactId` populated.

- [ ] **Step 6: Verify call branding (visual)**

  Check the destination phone — the caller ID should show the company name and logo (First Orion INFORM branding active).

- [ ] **Step 7: Final commit**

  ```bash
  git add -A
  git commit -m "feat(progressive-dialer): end-to-end integration validated — branded batch Progressive dialing"
  ```

---

## Known Limitations and Follow-ups

| Issue | Impact | Recommended Fix |
|---|---|---|
| `ACTIVE_CAMPAIGN_ID` is a static env var | Only one campaign can run at a time | Move to DynamoDB config table or pass per-event |
| No UI to start/stop branded campaigns | Manual API calls required | Add webapp toggle (separate plan) |
| Contact queue drained but campaign continues | Agents dispatch to empty queue | Add "campaign complete" signal to release all locks |
| No retry on FO push failure | Rare push failures silently pass | Consumer: retry once before enqueuing SQS |
| Agent lock never explicitly released on success | Relies on next AVAILABLE event to release+reacquire | Add CTR Kinesis stream consumer for DISCONNECTED event (separate plan) |

---

*Plan generated 2026-06-16 · Research: 109 agents, 25 verified claims, 7 refuted*
