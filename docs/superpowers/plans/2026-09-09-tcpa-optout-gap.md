# TCPA Opt-Out (Do-Not-Contact) Gap — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the TCPA opt-out gap — a patient replying STOP/QUIT/UNSUBSCRIBE to any automated SMS must be automatically added to a shared, cross-channel Do-Not-Contact store, and both the voice dialer and the bulk SMS sender must consult that store before contacting anyone.

**Architecture:** A **new**, purpose-built DynamoDB table (`VipConnectOptOutList`) is the shared cross-channel store — deliberately **not** the existing `vip-connect-deny-list` table, which stays scoped to its original meaning (voice-only, agent-manual "Block Number" during a call) so the two concepts don't get mixed under one ambiguous name. The new table is owned and managed by this app's `DataStack` (CDK, encrypted, PITR, retained) — unlike the legacy table, which was created outside any IaC. A new `OptOutRepository` in `vip_shared` is the read/write contract for automated channels (voice dialer, SMS sender), which already load `vip_shared` via a Lambda layer. `inbound-sms-handler` (in `Connect-batch-redis-refactor`, which has no shared-code layer — every Lambda there is a self-contained flat file by repo convention) writes to the same new table with an inline, duplicated write, matching how that repo already duplicates small helpers like `_last4()` across files.

**Tech Stack:** Python 3.12 (vip-connect-external-campaigns Lambdas), Python 3.13 (`inbound-sms-handler`, deployed via raw `aws lambda update-function-code` — this repo has no CDK/SAM), boto3, pytest, AWS CDK (TypeScript) for `vip-connect-external-campaigns`.

**Spec:** No formal spec doc exists for this — it derives from the TCPA guardrail in the "Phase I: Omni-Channel Lead Engagement & Automation" business spec (Compliance & Technical Guardrails §B, Opt-Out clause) discussed in conversation on 2026-09-09.

## Global Constraints

- **Do not modify any CloudHesive-owned resource.** CloudHesive's Lambdas are all prefixed `cloudhesive-integration-*` (confirmed list: `connectcampaign_sms_lookup`, `callback-tz-TimezoneCheck`, `callback-tz-RequeueCallbacks`, `agent-initiatied-sms-send-sms`, `-get-history`, `-receive-sms`, `-list-sessions`, `-ws-handler`) plus the `*PreCallSMSFlow` Connect flow and the `PreCallSMS` Wisdom/Q-Connect message template. None of those are touched by this plan. If any future task needs functionality CloudHesive already owns, duplicate it into our own resource rather than editing theirs.
- **Do not reuse or modify the legacy `vip-connect-deny-list` table or its Lambdas** (`connectcampaign_denylist_check.py`/`_write.py` in `Connect-batch-redis-refactor`, and `api-deny-list-stack.ts`'s `vip-admin-ui-api-deny-list` Lambda in this repo). That table keeps its original voice-only, agent-manual-block meaning. This plan creates a separate table for the new automated, cross-channel concept.
- **Medwork write-back is explicitly out of scope for this plan.** `connectcampaign_leadidupdate.py` proves a Medwork Lead API exists (`https://api-leads.medwork.io/v1/external`, credentials in `vip/lead-api/credentials`) but we have not confirmed it exposes a DNC/opt-out endpoint. Do not add a Medwork call in this plan. This is a deliberate, agreed gap — track it as follow-up work once the endpoint is confirmed.
- **Do not implement Phase II or Phase III work.** This plan is scoped strictly to the opt-out guardrail, which blocks all future automated-outreach work regardless of phase.
- All phone numbers are PHI (HIPAA identifier #4) — never log a full number. Every task in this plan follows the existing `_last4()`/`_mask()` convention already used in the sibling files being modified.
- New table `VipConnectOptOutList`: PK `ContactNumber` (String), no sort key, PAY_PER_REQUEST, SSE with the `DataStack` customer-managed key, PITR on, `RemovalPolicy.RETAIN` + `deletionProtection: true` (matches every other table in `data-stack.ts`).

---

## File Structure

New/modified files, by repo:

**`vip-connect-external-campaigns`** (has `vip_shared` Lambda layer + CDK):
- Modify: `infra/lib/stacks/data-stack.ts` (new `optOutTable`)
- Create: `services/shared/python/vip_shared/infrastructure/persistence/opt_out.py`
- Create: `services/shared/tests/unit/test_opt_out.py`
- Modify: `services/api-progressive-dialer/src/handler_caller.py`
- Modify: `services/api-progressive-dialer/tests/unit/test_handler_caller.py`
- Modify: `infra/lib/stacks/api-progressive-dialer-stack.ts:268-272` (add `OPT_OUT_TABLE` env var)
- Modify: `services/api-sms/src/sms_sender_handler.py`
- Modify: `services/api-sms/tests/unit/test_sms_sender.py`
- Modify: `infra/lib/stacks/api-sms-stack.ts:162-167` (add `OPT_OUT_TABLE` env var)

**`Connect-batch-redis-refactor`** (flat files, no shared layer, no CDK — manual deploy):
- Create: `inbound_sms_handler.py` (promoted from the live-deployed Lambda; `fase_c/inbound-sms-handler__lambda_function.py` is an unrelated reference dump per existing repo convention — leave it untouched, do not delete or move it)
- Create: `test_inbound_sms_handler.py`

---

### Task 1: Create the `VipConnectOptOutList` table

**Files:**
- Modify: `infra/lib/stacks/data-stack.ts`

**Interfaces:**
- Produces: DynamoDB table `VipConnectOptOutList` (PK `ContactNumber`), physical name is a literal string constant — Tasks 3-5 reference it by that literal name via a new env var, not via a CDK cross-stack prop (this repo already hit CloudFormation export-fragility problems doing that for the shared Lambda layer — see `infra/lib/utils/shared-layer.ts`'s comment — so table access for `handler_caller.py`/`sms_sender_handler.py`/`inbound_sms_handler.py` is wired the same lightweight way the legacy `vip-connect-deny-list` already is: a literal table name in each consumer's env, plus a manual IAM grant, no CDK-level cross-stack reference).
- Produces: KMS key ARN `arn:aws:kms:us-east-1:165505826690:key/df585888-2f49-4de0-9cba-14803fda63f0` (the `DataStack` CMK, alias `alias/prod/external-campaigns/data`) — every consumer in Tasks 3-5 needs `kms:Decrypt`/`kms:GenerateDataKey*`/`kms:Encrypt*` on this ARN in addition to the table-level DynamoDB grant.

- [ ] **Step 1: Add the table to `DataStack`**

In `infra/lib/stacks/data-stack.ts`, add the property declaration (with the other `public readonly ... dynamodb.Table` lines, around line 18):

```typescript
  public readonly optOutTable: dynamodb.Table;
```

Add the table definition after `segmentFilterConfigTable` (after line 149, before the `CfnOutput` block):

```typescript
    // Cross-channel automated opt-out (STOP/QUIT/UNSUBSCRIBE) store. Deliberately
    // separate from vip-connect-deny-list — that table is voice-only, agent-manual
    // "Block Number" and predates this app's CDK; mixing the two concepts under one
    // table/name is exactly the confusion this table avoids.
    this.optOutTable = new dynamodb.Table(this, 'OptOutTable', {
      tableName: 'VipConnectOptOutList',
      partitionKey: { name: 'ContactNumber', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: this.dataKey,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      deletionProtection: true,
    });
```

Add the output next to the others (after line 158):

```typescript
    new cdk.CfnOutput(this, 'OptOutTableArn', { value: this.optOutTable.tableArn });
```

- [ ] **Step 2: Synth to verify**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/infra
cdk synth VipAdminDataStack > /tmp/data-synth.yaml
grep -B2 -A15 "OptOutTable:" /tmp/data-synth.yaml
```

Expected: a `AWS::DynamoDB::Table` resource named `VipConnectOptOutList` with `SSESpecification` referencing the data key, `PointInTimeRecoverySpecification.PointInTimeRecoveryEnabled: true`, and `DeletionPolicy: Retain`.

- [ ] **Step 3: Commit**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns
git add infra/lib/stacks/data-stack.ts
git commit -m "feat: add VipConnectOptOutList table for cross-channel automated opt-out"
```

- [ ] **Step 4: Deploy (ask for explicit confirmation before running — this changes a production stack)**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/infra
cdk deploy VipAdminDataStack
```

- [ ] **Step 5: Verify**

```bash
aws dynamodb describe-table --profile production --region us-east-1 \
  --table-name VipConnectOptOutList \
  --query 'Table.[TableStatus,KeySchema,SSEDescription.SSEType]'
```

Expected: `ACTIVE`, `[{"AttributeName": "ContactNumber", "KeyType": "HASH"}]`, `KMS`.

---

### Task 2: `OptOutRepository` — shared cross-channel opt-out store

**Files:**
- Create: `services/shared/python/vip_shared/infrastructure/persistence/opt_out.py`
- Create: `services/shared/tests/unit/test_opt_out.py`

**Interfaces:**
- Consumes: nothing (table access is by literal name via env var, per Task 1).
- Produces: `OptOutRepository(table_name: str, dynamodb_resource=None)` with methods `is_blocked(phone: str) -> bool` and `block(phone: str, *, reason: str, source: str) -> None`; module-level `build_from_env() -> OptOutRepository` reading `OPT_OUT_TABLE` env var. Tasks 4 and 5 import both.

- [ ] **Step 1: Write the failing tests**

Create `services/shared/tests/unit/test_opt_out.py`:

```python
"""Tests for OptOutRepository — shared cross-channel Do-Not-Contact store."""

from __future__ import annotations

from unittest.mock import MagicMock

from vip_shared.infrastructure.persistence.opt_out import (
    OptOutRepository,
    build_from_env,
)


def test_is_blocked_returns_true_when_item_exists():
    mock_table = MagicMock()
    mock_table.get_item.return_value = {"Item": {"ContactNumber": "+15125551234"}}
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    repo = OptOutRepository(
        table_name="VipConnectOptOutList", dynamodb_resource=mock_resource
    )

    assert repo.is_blocked("+15125551234") is True
    mock_table.get_item.assert_called_once_with(Key={"ContactNumber": "+15125551234"})


def test_is_blocked_returns_false_when_item_missing():
    mock_table = MagicMock()
    mock_table.get_item.return_value = {}
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    repo = OptOutRepository(
        table_name="VipConnectOptOutList", dynamodb_resource=mock_resource
    )

    assert repo.is_blocked("+15125551234") is False


def test_block_writes_reason_and_source():
    mock_table = MagicMock()
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    repo = OptOutRepository(
        table_name="VipConnectOptOutList", dynamodb_resource=mock_resource
    )

    repo.block("+15125551234", reason="Patient replied STOP", source="sms_optout")

    mock_table.put_item.assert_called_once()
    item = mock_table.put_item.call_args.kwargs["Item"]
    assert item["ContactNumber"] == "+15125551234"
    assert item["reason"] == "Patient replied STOP"
    assert item["source"] == "sms_optout"
    assert "addedAt" in item


def test_build_from_env_reads_table_name(monkeypatch):
    monkeypatch.setenv("OPT_OUT_TABLE", "VipConnectOptOutList")

    repo = build_from_env()

    assert isinstance(repo, OptOutRepository)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/devaju/projects/vip-connect-external-campaigns/services/shared && python -m pytest tests/unit/test_opt_out.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'vip_shared.infrastructure.persistence.opt_out'`

- [ ] **Step 3: Write minimal implementation**

Create `services/shared/python/vip_shared/infrastructure/persistence/opt_out.py`:

```python
"""Shared cross-channel Do-Not-Contact store for automated outreach.

Backed by VipConnectOptOutList (PK: ContactNumber) — a table dedicated to
automated, cross-channel opt-out (STOP/QUIT/UNSUBSCRIBE, and any future
Medwork-driven DNC sync). Deliberately separate from vip-connect-deny-list,
which stays scoped to its original meaning: voice-only, agent-manual
"Block Number" during an active call.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import boto3


class OptOutRepository:
    """Read/write access to the shared cross-channel opt-out list."""

    def __init__(self, table_name: str, dynamodb_resource=None) -> None:
        self._table = (dynamodb_resource or boto3.resource("dynamodb")).Table(
            table_name
        )

    def is_blocked(self, phone: str) -> bool:
        """Return True if `phone` (E.164) has opted out."""
        response = self._table.get_item(Key={"ContactNumber": phone})
        return "Item" in response

    def block(self, phone: str, *, reason: str, source: str) -> None:
        """Record `phone` as opted out. Overwrites if already present (idempotent)."""
        self._table.put_item(
            Item={
                "ContactNumber": phone,
                "reason": reason,
                "source": source,
                "addedAt": datetime.now(timezone.utc).isoformat(),
            }
        )


def build_from_env() -> OptOutRepository:
    return OptOutRepository(table_name=os.environ["OPT_OUT_TABLE"])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/devaju/projects/vip-connect-external-campaigns/services/shared && python -m pytest tests/unit/test_opt_out.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns
git add services/shared/python/vip_shared/infrastructure/persistence/opt_out.py services/shared/tests/unit/test_opt_out.py
git commit -m "feat: add shared OptOutRepository for cross-channel opt-out"
```

No deploy step — this is a library file only picked up when Tasks 4/5 rebuild their stacks' Lambda layer.

---

### Task 3: STOP/QUIT/UNSUBSCRIBE detection in `inbound-sms-handler`

This is the Lambda that actually receives replies to the phone number used by Connect's native SMS channel (confirmed via its resource policy: invoke permission is granted to `connect.amazonaws.com`, scoped to instance `6b3f17ba-68a4-472a-9b20-db1991507009` — not to any SNS topic, despite the module docstring saying "Triggered by SNS"; that comment is stale). This is deliberately **not** the CloudHesive `agent-initiatied-sms-*` chat system, and not our own parallel `agent-initatied-sms-app-*` system — those are a separate ad-hoc agent-texting feature on a different SNS-driven path and are out of scope here.

**Files:**
- Create: `Connect-batch-redis-refactor/inbound_sms_handler.py` (baseline = current live code, pulled below)
- Create: `Connect-batch-redis-refactor/test_inbound_sms_handler.py`

**Interfaces:**
- Consumes: nothing from other tasks (this repo has no shared layer; the opt-out write is a self-contained ~10-line `put_item`, matching how `_last4()` is already duplicated verbatim across this repo's Lambdas rather than imported).
- Produces: nothing consumed elsewhere — this task is self-contained.

- [ ] **Step 1: Pull the current live source as the baseline**

```bash
cd /home/devaju/projects/Connect-batch-redis-refactor
mkdir -p /tmp/inbound_sms_baseline
URL=$(aws lambda get-function --profile production --region us-east-1 \
  --function-name inbound-sms-handler --query 'Code.Location' --output text)
curl -s "$URL" -o /tmp/inbound_sms_baseline/code.zip
unzip -o -q /tmp/inbound_sms_baseline/code.zip -d /tmp/inbound_sms_baseline
cp /tmp/inbound_sms_baseline/lambda_function.py inbound_sms_handler.py
```

Verify the copied file matches this baseline (if `aws lambda get-function` ever returns something different from this, stop and reconcile before continuing — do not blindly overwrite):

```python
import os
import json
import logging
import boto3
from datetime import datetime, timezone

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def _last4(value):
    """Mask a phone number for logging — HIPAA identifier #4."""
    if not value:
        return "????"
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return digits[-4:] if len(digits) >= 4 else "????"


def _event_shape(event):
    """Log-safe description of an event: keys only, never values."""
    if isinstance(event, dict):
        return {k: (f"{type(v).__name__}[{len(v)}]" if isinstance(v, (dict, list, str))
                    else type(v).__name__) for k, v in sorted(event.items())}
    return type(event).__name__


TABLE = os.environ["DYNAMODB_TABLE"]
CONNECT_INSTANCE = os.environ["CONNECT_INSTANCE_ID"]
TASK_FLOW_ID = os.environ["TASK_FLOW_ID"]  # Flow for reply Tasks
FALLBACK_QUEUE = os.environ.get("FALLBACK_QUEUE_ID")

dynamo = boto3.resource("dynamodb")
table = dynamo.Table(TABLE)
connect = boto3.client("connect")


def lambda_handler(event, context):
    """
    Triggered by SNS from End User Messaging.
    Routes customer reply to agent via Connect Task.
    """
    # HIPAA: el evento SNS lleva el numero y el cuerpo del SMS del paciente.
    logger.info("Inbound SMS event shape: %s", _event_shape(event))

    # Parse SNS message
    try:
        if "Records" in event:
            sns_msg = json.loads(event["Records"][0]["Sns"]["Message"])
        else:
            sns_msg = event
    except Exception as e:
        logger.error("Parse error: %s", e)
        return {"statusCode": 400, "body": "Invalid message"}

    customer_phone = sns_msg.get("originationNumber")
    message_body = sns_msg.get("messageBody")

    if not customer_phone or not message_body:
        logger.error("Missing fields: keys=%s", _event_shape(sns_msg))
        return {"statusCode": 400, "body": "Missing phone or message"}

    # Find active session
    session = get_active_session(customer_phone)

    if not session:
        logger.warning("No session for ****%s", _last4(customer_phone))
        create_fallback_task(customer_phone, message_body)
        return {"statusCode": 200, "body": "Fallback task created"}

    # Update history
    update_history(customer_phone, "inbound", message_body, "Customer")

    # Create reply Task for agent
    history_preview = get_history_preview(customer_phone)
    create_reply_task(
        customer_phone=customer_phone,
        message_body=message_body,
        agent_id=session["agentId"],
        agent_username=session.get("agentUsername", "Agent"),
        history_preview=history_preview
    )

    return {
        "statusCode": 200,
        "body": "Reply routed",
        "customerPhone": customer_phone,
        "agentId": session["agentId"]
    }


def get_active_session(customer_phone):
    """Get active session."""
    try:
        resp = table.get_item(Key={"customerPhone": customer_phone})
        item = resp.get("Item")
        if item and item.get("status") in ["active", "waiting"]:
            return item
        return None
    except Exception as e:
        logger.error("DynamoDB error: %s", e)
        return None


def update_history(customer_phone, direction, body, agent_name):
    """Append to history."""
    try:
        table.update_item(
            Key={"customerPhone": customer_phone},
            UpdateExpression="""
                SET lastMessages = list_append(
                    if_not_exists(lastMessages, :empty),
                    :new_msg
                ),
                totalMessageCount = if_not_exists(totalMessageCount, :zero) + :one,
                updatedAt = :now
            """,
            ExpressionAttributeValues={
                ":empty": [],
                ":new_msg": [{
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "direction": direction,
                    "body": body[:500],
                    "agentName": agent_name
                }],
                ":zero": 0,
                ":one": 1,
                ":now": datetime.now(timezone.utc).isoformat()
            }
        )
    except Exception as e:
        logger.error("History update failed: %s", e)


def get_history_preview(customer_phone):
    """Get last 2 messages for context."""
    try:
        resp = table.get_item(Key={"customerPhone": customer_phone})
        msgs = resp.get("Item", {}).get("lastMessages", [])
        return msgs[-3:-1] if len(msgs) > 1 else msgs
    except Exception as e:
        logger.error("History fetch failed: %s", e)
        return []


def create_reply_task(customer_phone, message_body, agent_id, agent_username, history_preview):
    """Create Task for agent with reply context."""
    lines = [
        "[REPLY] SMS from: %s" % customer_phone,
        ""
    ]

    if history_preview:
        lines.append("Recent context:")
        for msg in history_preview[-2:]:
            ts = msg["ts"].split("T")[1][:5] if "T" in msg["ts"] else "--:--"
            direction = "OUT>" if msg["direction"] == "outbound" else "IN<"
            sender = msg.get("agentName", "Customer")[:10]
            body = msg["body"][:35] + "..." if len(msg["body"]) > 35 else msg["body"]
            lines.append("  %s %s [%s]: %s" % (direction, ts, sender, body))
        lines.append("-" * 25)
        lines.append("")

    lines.append("Customer reply:")
    lines.append("  %s" % message_body[:200])
    lines.append("")
    lines.append("To reply: Create Task with Name=%s" % customer_phone)

    try:
        connect.start_task_contact(
            InstanceId=CONNECT_INSTANCE,
            ContactFlowId=TASK_FLOW_ID,
            Name="Reply: %s" % customer_phone,
            Description="\n".join(lines),
            Attributes={
                "customerPhone": customer_phone,
                "messageBody": message_body,
                "isReply": "true",
                "previousAgentId": agent_id,
                "previousAgentUsername": agent_username
            },
            References={
                "CustomerPhone": {
                    "Type": "STRING",
                    "Value": customer_phone
                }
            }
        )
        logger.info("Reply task created for ****%s", _last4(customer_phone))
    except Exception as e:
        logger.error("Task creation failed: %s", e)
        raise


def create_fallback_task(customer_phone, message_body):
    """Create fallback Task when no session exists."""
    lines = [
        "[FALLBACK] SMS from: %s" % customer_phone,
        "",
        "Message:",
        "  %s" % message_body[:200],
        "",
        "No active session - assign to available agent"
    ]

    try:
        connect.start_task_contact(
            InstanceId=CONNECT_INSTANCE,
            ContactFlowId=TASK_FLOW_ID,
            Name="Fallback: %s" % customer_phone,
            Description="\n".join(lines),
            Attributes={
                "customerPhone": customer_phone,
                "messageBody": message_body,
                "isReply": "true",
                "isFallback": "true"
            }
        )
        logger.info("Fallback task created")
    except Exception as e:
        logger.error("Fallback task failed: %s", e)
```

Note for whoever runs this: production's `TASK_FLOW_ID` and `FALLBACK_QUEUE_ID` env vars are currently **empty strings**, so `create_reply_task`/`create_fallback_task` are silently failing today (caught by the bare `except Exception`, logged, swallowed) — every inbound reply is likely being dropped. That is a pre-existing bug, not something this task fixes; it does not affect the STOP-detection added below, which returns before either function is ever called.

- [ ] **Step 2: Commit the baseline before changing it**

```bash
cd /home/devaju/projects/Connect-batch-redis-refactor
git add inbound_sms_handler.py
git commit -m "chore: track inbound-sms-handler source (previously untracked, live-only)"
```

- [ ] **Step 3: Write the failing test**

Create `Connect-batch-redis-refactor/test_inbound_sms_handler.py`:

```python
"""Tests for inbound_sms_handler.py — STOP/opt-out detection."""

import importlib
import os
import sys
from unittest.mock import MagicMock, patch

_ENV = {
    "DYNAMODB_TABLE": "sms-sessions",
    "CONNECT_INSTANCE_ID": "6b3f17ba-68a4-472a-9b20-db1991507009",
    "TASK_FLOW_ID": "flow-1",
}


def _load_handler():
    if "inbound_sms_handler" in sys.modules:
        del sys.modules["inbound_sms_handler"]
    with patch.dict(os.environ, _ENV):
        with patch("boto3.resource"), patch("boto3.client"):
            import inbound_sms_handler
            importlib.reload(inbound_sms_handler)
            return inbound_sms_handler


def test_stop_keyword_writes_opt_out_and_skips_session_lookup():
    handler = _load_handler()

    mock_opt_out_table = MagicMock()
    mock_ddb_resource = MagicMock()
    mock_ddb_resource.Table.return_value = mock_opt_out_table

    with patch.object(handler, "get_active_session") as mock_get_session, \
         patch("boto3.resource", return_value=mock_ddb_resource):
        result = handler.lambda_handler(
            {"originationNumber": "+15125551234", "messageBody": "STOP"}, None
        )

    mock_get_session.assert_not_called()
    mock_opt_out_table.put_item.assert_called_once()
    item = mock_opt_out_table.put_item.call_args.kwargs["Item"]
    assert item["ContactNumber"] == "+15125551234"
    assert result == {"statusCode": 200, "body": "Opt-out recorded"}


def test_quit_and_unsubscribe_are_also_treated_as_opt_out():
    handler = _load_handler()
    mock_ddb_resource = MagicMock()

    for keyword in ["quit", "Unsubscribe", "  STOP  "]:
        with patch.object(handler, "get_active_session") as mock_get_session, \
             patch("boto3.resource", return_value=mock_ddb_resource):
            result = handler.lambda_handler(
                {"originationNumber": "+15125551234", "messageBody": keyword}, None
            )
        mock_get_session.assert_not_called()
        assert result["statusCode"] == 200


def test_non_opt_out_message_still_routes_normally():
    handler = _load_handler()

    with patch.object(handler, "get_active_session", return_value=None) as mock_get_session, \
         patch.object(handler, "create_fallback_task") as mock_fallback:
        result = handler.lambda_handler(
            {"originationNumber": "+15125551234", "messageBody": "Yes please call me"}, None
        )

    mock_get_session.assert_called_once_with("+15125551234")
    mock_fallback.assert_called_once()
    assert result == {"statusCode": 200, "body": "Fallback task created"}
```

- [ ] **Step 4: Run test to verify it fails**

Run: `cd /home/devaju/projects/Connect-batch-redis-refactor && python -m pytest test_inbound_sms_handler.py -v`
Expected: FAIL — `test_stop_keyword_writes_opt_out_and_skips_session_lookup` and the quit/unsubscribe test fail because `get_active_session` IS called today (no early-exit exists yet).

- [ ] **Step 5: Implement the STOP-keyword check**

In `inbound_sms_handler.py`, add the opt-out table wiring near the top (after the existing `TABLE`/`CONNECT_INSTANCE`/`TASK_FLOW_ID`/`FALLBACK_QUEUE` env var reads, before `dynamo = boto3.resource("dynamodb")`):

```python
OPT_OUT_TABLE = os.environ.get("OPT_OUT_TABLE", "VipConnectOptOutList")
OPT_OUT_KEYWORDS = {"STOP", "QUIT", "UNSUBSCRIBE", "CANCEL", "END"}
```

Add a helper function (writes to the new dedicated opt-out table — not `vip-connect-deny-list`):

```python
def _record_opt_out(customer_phone, keyword):
    """Add customer_phone to the shared cross-channel opt-out list (VipConnectOptOutList).

    Deliberately not vip-connect-deny-list — that table is voice-only, agent-manual
    blocks. This is the automated, cross-channel (SMS + voice) equivalent.
    """
    try:
        boto3.resource("dynamodb").Table(OPT_OUT_TABLE).put_item(
            Item={
                "ContactNumber": customer_phone,
                "reason": f"Patient replied {keyword}",
                "source": "sms_optout",
                "addedAt": datetime.now(timezone.utc).isoformat(),
            }
        )
        logger.info("Opt-out recorded for ****%s (keyword=%s)", _last4(customer_phone), keyword)
    except Exception as e:
        logger.error("Failed to record opt-out for ****%s: %s", _last4(customer_phone), e)
```

In `lambda_handler`, insert the check immediately after the `if not customer_phone or not message_body:` guard and before `session = get_active_session(customer_phone)`:

```python
    keyword = message_body.strip().upper()
    if keyword in OPT_OUT_KEYWORDS:
        _record_opt_out(customer_phone, keyword)
        return {"statusCode": 200, "body": "Opt-out recorded"}

    # Find active session
    session = get_active_session(customer_phone)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /home/devaju/projects/Connect-batch-redis-refactor && python -m pytest test_inbound_sms_handler.py -v`
Expected: 3 passed

- [ ] **Step 7: Run ruff (repo convention — see `ruff.toml`)**

Run: `cd /home/devaju/projects/Connect-batch-redis-refactor && ruff check inbound_sms_handler.py test_inbound_sms_handler.py`
Expected: no new findings (baseline-compare if any pre-existing findings show up — do not fix unrelated lines)

- [ ] **Step 8: Commit**

```bash
cd /home/devaju/projects/Connect-batch-redis-refactor
git add inbound_sms_handler.py test_inbound_sms_handler.py
git commit -m "feat: detect STOP/QUIT/UNSUBSCRIBE in inbound SMS and record cross-channel opt-out"
```

- [ ] **Step 9: Grant the Lambda's execution role write access to the new table + KMS key**

This role is not IaC-tracked (raw CLI-created service role) — grant via CLI, additive policy, does not touch any existing policy on the role:

```bash
cat > /tmp/inbound-sms-handler-optout-write.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "dynamodb:PutItem",
      "Resource": "arn:aws:dynamodb:us-east-1:165505826690:table/VipConnectOptOutList"
    },
    {
      "Effect": "Allow",
      "Action": ["kms:GenerateDataKey*", "kms:Encrypt*", "kms:Decrypt*", "kms:DescribeKey"],
      "Resource": "arn:aws:kms:us-east-1:165505826690:key/df585888-2f49-4de0-9cba-14803fda63f0"
    }
  ]
}
EOF

aws iam put-role-policy --profile production \
  --role-name inbound-sms-handler-role-9m707415 \
  --policy-name InboundSmsHandlerOptOutWrite \
  --policy-document file:///tmp/inbound-sms-handler-optout-write.json
```

- [ ] **Step 10: Deploy**

```bash
cd /home/devaju/projects/Connect-batch-redis-refactor
mkdir -p /tmp/inbound_sms_deploy && cp inbound_sms_handler.py /tmp/inbound_sms_deploy/lambda_function.py
(cd /tmp/inbound_sms_deploy && zip -q /tmp/inbound_sms_handler.zip lambda_function.py)
aws lambda update-function-code --profile production --region us-east-1 \
  --function-name inbound-sms-handler \
  --zip-file fileb:///tmp/inbound_sms_handler.zip \
  --output json
aws lambda update-function-configuration --profile production --region us-east-1 \
  --function-name inbound-sms-handler \
  --environment "Variables={DYNAMODB_TABLE=sms-sessions,CONNECT_INSTANCE_ID=6b3f17ba-68a4-472a-9b20-db1991507009,FALLBACK_QUEUE_ID=,TASK_FLOW_ID=,OPT_OUT_TABLE=VipConnectOptOutList}" \
  --output json
```

The `update-function-configuration` call above re-specifies the **existing** env vars verbatim (confirmed via `get-function-configuration` before writing this plan) plus the new `OPT_OUT_TABLE` key — `--environment` replaces the whole `Variables` map, it does not merge, so omitting an existing key would delete it.

- [ ] **Step 11: Verify with a real (non-PHI) test number**

Text "STOP" from a test phone number to the number this Lambda serves, then confirm the write landed:

```bash
aws dynamodb get-item --profile production --region us-east-1 \
  --table-name VipConnectOptOutList \
  --key '{"ContactNumber": {"S": "+1XXXXXXXXXX"}}'
```

Expected: item present with `source = sms_optout`, `reason` containing `STOP`.

---

### Task 4: Gate the voice dialer against the opt-out list

**Files:**
- Modify: `services/api-progressive-dialer/src/handler_caller.py`
- Modify: `services/api-progressive-dialer/tests/unit/test_handler_caller.py`
- Modify: `infra/lib/stacks/api-progressive-dialer-stack.ts:268-272` (add `OPT_OUT_TABLE` env var)

**Interfaces:**
- Consumes: `OptOutRepository` / `build_from_env()` from Task 2 (`vip_shared.infrastructure.persistence.opt_out`).
- Consumes: `CampaignQueue.mark_outcome(campaign_id, sk, outcome)` (existing method, `services/api-progressive-dialer/src/campaign_queue.py:127`).

- [ ] **Step 1: Write the failing test**

In `services/api-progressive-dialer/tests/unit/test_handler_caller.py`, add:

```python
def test_blocked_number_skips_dial_and_releases_lock():
    if "handler_caller" in sys.modules:
        del sys.modules["handler_caller"]

    with patch.dict("os.environ", {
        "CAMPAIGN_QUEUE_TABLE": "VipProgressiveCampaignQueue",
        "AGENT_LOCK_TABLE": "VipProgressiveAgentLocks",
        "FIRSTORION_SECRET_NAME": "vip/firstorion/credentials",
        "OPT_OUT_TABLE": "VipConnectOptOutList",
    }):
        mock_caller = MagicMock()
        mock_queue = MagicMock()
        mock_queue.get_phone.return_value = "+15551234567"
        mock_lock = MagicMock()
        mock_opt_out = MagicMock()
        mock_opt_out.is_blocked.return_value = True

        with patch("handler_caller.ConnectCaller", return_value=mock_caller), \
             patch("handler_caller.CampaignQueue", return_value=mock_queue), \
             patch("handler_caller.AgentLock", return_value=mock_lock), \
             patch("handler_caller.build_opt_out_from_env", return_value=mock_opt_out):
            from handler_caller import lambda_handler
            lambda_handler(_make_sqs_event(), None)

        mock_opt_out.is_blocked.assert_called_once_with("+15551234567")
        mock_caller.dial.assert_not_called()
        mock_queue.mark_outcome.assert_called_once_with(
            "campaign-1", "2026-06-16T14:00:00.000Z#uuid-1", "blocked_dnc"
        )
        mock_lock.release.assert_called_once_with(
            "arn:aws:connect:us-east-1:165505826690:instance/abc/agent/agent-001"
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/devaju/projects/vip-connect-external-campaigns/services/api-progressive-dialer && python -m pytest tests/unit/test_handler_caller.py::test_blocked_number_skips_dial_and_releases_lock -v`
Expected: FAIL — `AttributeError: <module 'handler_caller'> does not have the attribute 'build_opt_out_from_env'`

- [ ] **Step 3: Implement the gate**

In `services/api-progressive-dialer/src/handler_caller.py`, add the import (alongside the existing local imports):

```python
from vip_shared.infrastructure.persistence.opt_out import build_from_env as build_opt_out_from_env
```

Add a lazy singleton getter, matching the existing `_get_queue`/`_get_lock`/`_get_fo` pattern:

```python
_opt_out_store = None


def _get_opt_out():
    global _opt_out_store
    if _opt_out_store is None:
        _opt_out_store = build_opt_out_from_env()
    return _opt_out_store
```

In `_process_message`, insert the check right after `destination_phone` is confirmed non-empty and before `caller = ConnectCaller(...)`:

```python
    if _get_opt_out().is_blocked(destination_phone):
        logger.info(
            "Skipping dial — number on opt-out list campaign_id=%s correlation_id=%s",
            campaign_id,
            correlation_id,
        )
        # Terminal state, not a retry candidate — reuses the outcome field rather
        # than adding a new queue-state method for a number we will never dial.
        try:
            _get_queue().mark_outcome(campaign_id, contact_sk, "blocked_dnc")
        except Exception as e:
            logger.error(
                "mark_outcome_failed_on_opt_out correlation_id=%s error=%s",
                correlation_id,
                type(e).__name__,
            )
        try:
            _get_lock().release(agent_arn)
        except Exception as e:
            logger.error(
                "lock_release_failed_on_opt_out correlation_id=%s error=%s",
                correlation_id,
                type(e).__name__,
            )
        return

    caller = ConnectCaller(
```

- [ ] **Step 4: Fix the pre-existing tests, then run all tests to verify they pass**

`_get_opt_out()` now runs unconditionally on every `_process_message` call. Every pre-existing test in this file imports `handler_caller` fresh inside its own `with patch.dict("os.environ", {...})` block and calls `lambda_handler(...)` while that block is still active — so without a fix, `build_opt_out_from_env()` would (a) raise `KeyError` for the missing `OPT_OUT_TABLE` key, and even once that's added, (b) construct a **real, unmocked** `OptOutRepository` that calls the real `boto3.resource("dynamodb")` — none of the pre-existing tests patch `boto3` itself, only the higher-level `ConnectCaller`/`CampaignQueue`/`AgentLock` classes. Left unfixed, running the test suite would attempt a real AWS DynamoDB call. For every pre-existing test in `test_handler_caller.py` (e.g. `test_calls_start_outbound_voice_contact`):
1. Add `"OPT_OUT_TABLE": "VipConnectOptOutList"` to its `patch.dict("os.environ", {...})` dict.
2. Add `patch("handler_caller.build_opt_out_from_env", return_value=MagicMock(is_blocked=lambda *_: False))` to its inner `with patch(...)` chain (alongside the existing `ConnectCaller`/`CampaignQueue`/`AgentLock` patches), so `_get_opt_out()` never touches real AWS and the not-blocked path is exercised.

Run: `cd /home/devaju/projects/vip-connect-external-campaigns/services/api-progressive-dialer && python -m pytest tests/unit/test_handler_caller.py -v`
Expected: all pass.

- [ ] **Step 5: Add the env var in CDK**

In `infra/lib/stacks/api-progressive-dialer-stack.ts`, modify the `callerFn` environment block (around line 268):

```typescript
      environment: {
        CAMPAIGN_QUEUE_TABLE: campaignQueueTable.tableName,
        AGENT_LOCK_TABLE: agentLockTable.tableName,
        FIRSTORION_SECRET_NAME: 'vip/firstorion/credentials',
        OPT_OUT_TABLE: 'VipConnectOptOutList',
      },
```

- [ ] **Step 6: Grant read access on the caller role**

`callerRole` is imported with `mutable: false` (comment at line 243-246 explains why — the CDK exec role lacks `iam:CreateRole`/`iam:GetRolePolicy`). Grant via CLI, matching the existing pattern documented in that same comment. This table uses the `DataStack` CMK (`df585888-...`), not the legacy deny-list's key — grant both:

```bash
cat > /tmp/progressive-dialer-caller-optout-read.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "dynamodb:GetItem",
      "Resource": "arn:aws:dynamodb:us-east-1:165505826690:table/VipConnectOptOutList"
    },
    {
      "Effect": "Allow",
      "Action": ["kms:Decrypt", "kms:DescribeKey"],
      "Resource": "arn:aws:kms:us-east-1:165505826690:key/df585888-2f49-4de0-9cba-14803fda63f0"
    }
  ]
}
EOF

aws iam put-role-policy --profile production \
  --role-name vip-progressive-dialer-caller-role \
  --policy-name ProgressiveDialerCallerOptOutRead \
  --policy-document file:///tmp/progressive-dialer-caller-optout-read.json
```

- [ ] **Step 7: Synth and review the diff (this repo's `npm run build`/lint are broken — see the `build-and-synth` skill; `cdk synth` is the real validation)**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/infra
cdk synth ApiProgressiveDialerStack > /tmp/dialer-synth.yaml
grep -A3 "OPT_OUT_TABLE" /tmp/dialer-synth.yaml
```

Expected: the `CallerFunction` resource's `Environment.Variables` includes `OPT_OUT_TABLE: VipConnectOptOutList`.

- [ ] **Step 8: Commit**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns
git add services/api-progressive-dialer/src/handler_caller.py services/api-progressive-dialer/tests/unit/test_handler_caller.py infra/lib/stacks/api-progressive-dialer-stack.ts
git commit -m "feat: skip dialing numbers on the shared opt-out list"
```

- [ ] **Step 9: Deploy (ask for explicit confirmation before running — this changes a production dialer stack)**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/infra
cdk deploy ApiProgressiveDialerStack
```

Then redeploy the Lambda code itself (CDK's `fromAsset` should pick up the change automatically on `cdk deploy`, but this repo's own `deploy.sh` is the documented fallback):

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-progressive-dialer
./deploy.sh
```

---

### Task 5: Gate the bulk SMS sender against the opt-out list

**Files:**
- Modify: `services/api-sms/src/sms_sender_handler.py`
- Modify: `services/api-sms/tests/unit/test_sms_sender.py`
- Modify: `infra/lib/stacks/api-sms-stack.ts:162-167` (add `OPT_OUT_TABLE` env var)

**Interfaces:**
- Consumes: `OptOutRepository` / `build_from_env()` from Task 2.

Context: `messageTemplate` here is enforced PHI-free bulk SMS (Plans V2 `sms` delivery type) — AWS End User Messaging already blocks re-sends to a number that replied STOP via its own managed suppression list (confirmed: `sms_processor_handler.py:98-114` already reacts to the resulting `ValidationException`). This task adds a **pre-send** check against our own shared list so a number blocked via voice, or via the Task 3 SMS keyword detection, is also skipped here — not just numbers AWS's own list already knows about.

- [ ] **Step 1: Write the failing test**

In `services/api-sms/tests/unit/test_sms_sender.py`, add (this repo's existing tests mock module state via `patch.object(handler, "_attr", mock)` post-reload — see `test_sender_enqueues_valid_e164_phones`, lines 69-73 of the current file — follow that exact pattern for `_opt_out` too):

```python
def test_sender_skips_phone_on_opt_out_list_and_counts_opted_out():
    handler = _load_handler()

    mock_ddb = MagicMock()
    mock_runs_table = MagicMock()
    mock_queue_table = MagicMock()
    mock_ddb.Table.side_effect = lambda name: (
        mock_runs_table if "Runs" in name else mock_queue_table
    )

    mock_sqs = MagicMock()
    mock_cp = _make_mock_cp(phones=["+15125559999", "+15125558888"])

    mock_opt_out = MagicMock()
    mock_opt_out.is_blocked.side_effect = lambda p: p == "+15125559999"

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_ddb", mock_ddb),
        patch.object(handler, "_sqs", mock_sqs),
        patch.object(handler, "_cp", mock_cp),
        patch.object(handler, "_opt_out", mock_opt_out),
    ):
        result = handler.lambda_handler(_base_event(), None)

    assert result["enqueued"] == 1
    mock_opt_out.is_blocked.assert_any_call("+15125559999")
    mock_opt_out.is_blocked.assert_any_call("+15125558888")
    runs_update = mock_runs_table.update_item.call_args.kwargs
    assert runs_update["ExpressionAttributeValues"][":o"] == 1
```

(Reuse `_load_handler`/`_base_event`/`_make_mock_cp` exactly as already defined in this file — don't redefine them.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/devaju/projects/vip-connect-external-campaigns/services/api-sms && python -m pytest tests/unit/test_sms_sender.py::test_sender_skips_phone_on_opt_out_list_and_counts_opted_out -v`
Expected: FAIL — `AttributeError: <module 'sms_sender_handler'> does not have the attribute '_opt_out'`

- [ ] **Step 3: Implement the gate**

In `services/api-sms/src/sms_sender_handler.py`, add the import near the top (with the other imports):

```python
from vip_shared.infrastructure.persistence.opt_out import build_from_env as build_opt_out_from_env
```

Add the module-level instance, matching this file's existing eager-init style (`_ddb`, `_sqs`, `_cp` are all module-level, not lazy getters):

```python
_opt_out = build_opt_out_from_env()
```

In `lambda_handler`, initialize the counter before the loop (near `enqueued = 0` / `failed = 0`):

```python
    enqueued = 0
    failed = 0
    opted_out = 0
```

Inside the `for phone in phones:` loop, right after the E.164 check:

```python
    for phone in phones:
        if not _E164_RE.match(phone):
            continue
        if _opt_out.is_blocked(phone):
            opted_out += 1
            continue
        item_sk = f"{now_iso}#{uuid.uuid4().hex[:8]}"
```

Update the final counts write to include the new counter:

```python
    _ddb.Table(_RUNS_TABLE).update_item(
        Key={"planId": event["planId"], "sk": f"{event['runId']}#{campaign_id}"},
        UpdateExpression="SET totalEnqueued = :n, totalFailed = :f, totalOptedOut = :o, updatedAt = :t",
        ExpressionAttributeValues={":n": enqueued, ":f": failed, ":o": opted_out, ":t": now_iso},
    )
```

(`totalOptedOut` already exists in the item schema written at campaign start — line 81 — but was never incremented anywhere; this closes that gap too.)

- [ ] **Step 4: Fix the shared `_ENV` fixture and every pre-existing test, then run tests to verify they pass**

`_opt_out = build_opt_out_from_env()` runs at **module import time** now, so `_load_handler()` will raise `KeyError: 'OPT_OUT_TABLE'` for every test in this file the moment `importlib.reload(sms_sender_handler)` runs — not just the new test. Add the key once to the shared `_ENV` dict at the top of `test_sms_sender.py`:

```python
_ENV = {
    "SMS_CAMPAIGN_QUEUE_TABLE": "VipSmsCampaignQueue",
    "SMS_CAMPAIGN_RUNS_TABLE": "VipSmsCampaignRuns",
    "SMS_SQS_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123/vip-sms-campaign-queue",
    "PROFILES_DOMAIN_NAME": "amazon-connect-test",
    "OPT_OUT_TABLE": "VipConnectOptOutList",
}
```

Every pre-existing test in the file uses this same `_ENV` dict via `_load_handler()`/`patch.dict(os.environ, _ENV)`, so this one change fixes the `KeyError`. But each pre-existing test's `with patch.object(handler, "_ddb", ...), patch.object(handler, "_sqs", ...), patch.object(handler, "_cp", ...):` block does not touch `_opt_out`, so it stays bound to whatever `OptOutRepository` was built at `_load_handler()`'s `importlib.reload()` time — a real repository wrapping a mocked `boto3.resource` (mocked only inside `_load_handler`'s own `with patch("boto3.resource")` block, which has already exited by the time the test body runs). Calling `.is_blocked()` on it later calls `.get_item()` on that detached mock, returning another `MagicMock`; `"Item" in response` then evaluates `response.__contains__(...)`, and `MagicMock.__bool__` defaults to `True` — so **every** phone number will read as blocked, silently zeroing out `enqueued` in every pre-existing test. Add `patch.object(handler, "_opt_out", MagicMock(is_blocked=lambda *_: False))` to every pre-existing test's `with (...)` block.

Run: `cd /home/devaju/projects/vip-connect-external-campaigns/services/api-sms && python -m pytest tests/unit/test_sms_sender.py -v`
Expected: all pass.

- [ ] **Step 5: Add the env var in CDK**

In `infra/lib/stacks/api-sms-stack.ts:162-167`, modify the `smsSenderFunction` environment block:

```typescript
      environment: {
        SMS_CAMPAIGN_QUEUE_TABLE: this.smsCampaignQueueTable.tableName,
        SMS_CAMPAIGN_RUNS_TABLE: this.smsRunsTable.tableName,
        SMS_SQS_QUEUE_URL: this.smsSendQueue.queueUrl,
        PROFILES_DOMAIN_NAME: props.profilesDomainName,
        OPT_OUT_TABLE: 'VipConnectOptOutList',
      },
```

- [ ] **Step 6: Grant read access on the sender role**

```bash
cat > /tmp/sms-sender-optout-read.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "dynamodb:GetItem",
      "Resource": "arn:aws:dynamodb:us-east-1:165505826690:table/VipConnectOptOutList"
    },
    {
      "Effect": "Allow",
      "Action": ["kms:Decrypt", "kms:DescribeKey"],
      "Resource": "arn:aws:kms:us-east-1:165505826690:key/df585888-2f49-4de0-9cba-14803fda63f0"
    }
  ]
}
EOF

aws iam put-role-policy --profile production \
  --role-name vip-sms-sender-role \
  --policy-name SmsSenderOptOutRead \
  --policy-document file:///tmp/sms-sender-optout-read.json
```

- [ ] **Step 7: Synth and review**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/infra
cdk synth VipAdminApiSmsStack > /tmp/sms-synth.yaml
grep -A5 "SmsSenderFunction" /tmp/sms-synth.yaml | grep "OPT_OUT_TABLE"
```

- [ ] **Step 8: Commit**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns
git add services/api-sms/src/sms_sender_handler.py services/api-sms/tests/unit/test_sms_sender.py infra/lib/stacks/api-sms-stack.ts
git commit -m "feat: skip enqueuing SMS to numbers on the shared opt-out list, wire up totalOptedOut"
```

- [ ] **Step 9: Deploy (ask for explicit confirmation before running — this changes a production stack)**

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/infra
cdk deploy VipAdminApiSmsStack
```

---

## Explicitly Out of Scope (do not implement as part of this plan)

- **Medwork write-back.** No task calls `api-leads.medwork.io`. Confirm the DNC/opt-out endpoint exists before adding this in a follow-up plan.
- **Voice-side STOP equivalent.** There is no spoken "STOP" to detect on a phone call; if a patient asks verbally to not be contacted, that still goes through the existing manual agent "Block Number" Quick Connect flow (`connectcampaign_denylist_write.py`) against the legacy `vip-connect-deny-list` table, unchanged.
- **Merging or syncing `vip-connect-deny-list` and `VipConnectOptOutList`.** They intentionally represent different things (manual voice block vs. automated cross-channel opt-out). If a future requirement needs "check both," that's new, separate scope.
- **CloudHesive's `agent-initiatied-sms-*` chat system and our parallel `agent-initatied-sms-app-*` system.** Neither is touched. If either turns out to also need opt-out detection, that is new, separate scope — ask before starting it.
- **Fixing `TASK_FLOW_ID`/`FALLBACK_QUEUE_ID` being empty in `inbound-sms-handler`.** Noted as a discovered pre-existing bug in Task 3; not fixed here.
- **Phase II and Phase III work of any kind.**
