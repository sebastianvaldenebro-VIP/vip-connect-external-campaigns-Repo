# Scripts & Service Reference — VIP Connect Admin UI

---

## 1. Lambda services

| Lambda name | Source directory | Entry point | Event sources |
|---|---|---|---|
| `vip-admin-ui-api-plans` | `services/api-plans/src/` | `handler.lambda_handler` | API Gateway HTTP API · EventBridge Scheduler (tick, scheduled_run) · EventBridge rule (prestart_check) · direct invoke (chain_trigger) |
| `vip-admin-ui-api-segments` | `services/api-segments/src/` | `handler.lambda_handler` | API Gateway HTTP API |
| `vip-admin-ui-api-campaigns` | `services/api-campaigns/src/` | `handler.lambda_handler` | API Gateway HTTP API |
| `vip-admin-ui-api-profiles` | `services/api-profiles/src/` | `handler.lambda_handler` | API Gateway HTTP API |
| `vip-admin-ui-api-metrics` | `services/api-metrics/src/` | `handler.lambda_handler` | API Gateway HTTP API |
| `vip-external-campaigns-feeder` | `services/external-campaigns/src/` | `handler.lambda_handler` | EventBridge rule (campaign-events) · direct invoke |

---

## 2. `api-plans` module map

| Module | Role |
|---|---|
| `handler.py` | Lambda entrypoint — routes HTTP events via `router.py` and dispatches EventBridge actions |
| `router.py` | Route table mapping `"METHOD /path"` strings to handler functions |
| `handlers/plans.py` | CRUD for plan definitions: list, get, create, update, delete, list_templates, clone_from_template |
| `handlers/runs.py` | Run lifecycle: trigger_run, list_runs, get_run, abort_run |
| `executor.py` | Execution engine: DAG dispatch, tick, pre-start warming, bucket advance, run chaining, loop end-time enforcement |
| `store.py` | DynamoDB access layer: plan + run CRUD, serialization/deserialization, `pendingWarmup` and `loop` field support |
| `builders.py` | Connect campaign + Customer Profiles segment construction: name generation, filter mapping, campaign params |
| `scheduler_manager.py` | EventBridge Scheduler CRUD: create/delete `rate(1 min)` tick schedules per bucket |

### `handler.py` action dispatch

```python
event.get("action") == "tick"             → executor.tick(planId, runId, bucketIndex)
event.get("action") == "scheduled_run"    → executor.scheduled_run(planId)
event.get("action") == "chain_trigger"    → executor.start_run_chained(planId)
event.get("action") == "prestart_check"   → executor.prestart_check()
# otherwise → HTTP route via router.resolve(routeKey)
```

### `router.py` route table

```text
GET    /plans                          → plans_handler.list_plans
POST   /plans                          → plans_handler.create_plan
GET    /plans/{id}                     → plans_handler.get_plan
PUT    /plans/{id}                     → plans_handler.update_plan
DELETE /plans/{id}                     → plans_handler.delete_plan
GET    /templates                      → plans_handler.list_templates
POST   /plans/from-template/{tid}      → plans_handler.clone_from_template
POST   /plans/{id}/runs                → runs_handler.trigger_run
GET    /plans/{id}/runs                → runs_handler.list_runs
GET    /plans/{id}/runs/{runId}        → runs_handler.get_run
POST   /plans/{id}/runs/{runId}/abort  → runs_handler.abort_run
```

---

## 3. Shared layer (`services/shared/python/vip_shared/`)

The Lambda layer `vip-admin-shared` is shared by all five API Lambdas. It contains **both** the `vip_shared` Python source **and** pip-installed packages (redis, etc.). Always patch from an existing layer zip when deploying — never rebuild from source alone.

| Module | Purpose |
|---|---|
| `application.http` | `json_response`, `error_response`, `parse_body`, `extract_caller` |
| `infrastructure.persistence.audit` | `build_from_env()` → `AuditLogger` that writes to `AdminAuditLog` DynamoDB table |
| `infrastructure.persistence.redis_lead_source` | `RedisLeadSource` — iterates Redis list; `is_ready()` checks LLEN before iterating |
| `infrastructure.telemetry.structured_logger` | `StructuredLogger` — JSON CloudWatch logs with SHA-256 hashing for phone/email/name fields |

---

## 4. Frontend entry points

| File | Role |
|---|---|
| `frontend/src/main.tsx` | Vite app root — mounts React, sets up Amplify auth client |
| `frontend/src/App.tsx` | Router (React Router v6) — protected routes, idle timeout, layout |
| `frontend/src/lib/api.ts` | All API calls + TypeScript types for Plans, Runs, Campaigns, Segments, Profiles |
| `frontend/src/pages/Plans.tsx` | Plan list — trigger badges, New Plan button, Templates tab |
| `frontend/src/pages/PlanNew.tsx` | Plan create/edit — TriggerEditor, BucketEditor, DagCanvas, CampaignCard |
| `frontend/src/pages/PlanDetail.tsx` | Live run monitor — per-bucket/campaign status, polling via TanStack Query |

---

## 5. Deploy

### Prerequisites

```bash
# SSO login (run once per day or when token expires)
aws sso login --profile production
aws sts get-caller-identity --profile production
# Expected: Account=165505826690
```

### Deploy api-plans Lambda

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/api-plans/src
zip -r /tmp/api-plans.zip . -x "__pycache__/*" "*.pyc"
AWS_PROFILE=production aws lambda update-function-code \
  --function-name vip-admin-ui-api-plans \
  --zip-file fileb:///tmp/api-plans.zip \
  --region us-east-1

# Wait until update is active before invoking
AWS_PROFILE=production aws lambda wait function-updated \
  --function-name vip-admin-ui-api-plans \
  --region us-east-1
```

### Deploy feeder Lambda

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/services/external-campaigns/src
zip -r /tmp/feeder.zip . -x "__pycache__/*" "*.pyc"
AWS_PROFILE=production aws lambda update-function-code \
  --function-name vip-external-campaigns-feeder \
  --zip-file fileb:///tmp/feeder.zip \
  --region us-east-1
```

### Deploy shared layer (CRITICAL — patch from existing zip)

See full procedure in `docs/runbook.md` § "Deploy shared Lambda layer". Summary:

1. Download current layer version zip
2. Unzip → overwrite only `python/vip_shared/` directory
3. Re-zip → `publish-layer-version`
4. `update-function-configuration` on ALL five API Lambdas to point to new version

### Deploy frontend

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/frontend
npm run build

ASSET_BUCKET=$(AWS_PROFILE=production aws cloudformation describe-stacks \
  --stack-name VipAdminHostingStack \
  --region us-east-1 \
  --query "Stacks[0].Outputs[?OutputKey=='AssetBucketName'].OutputValue" \
  --output text)

DIST_ID=$(AWS_PROFILE=production aws cloudformation describe-stacks \
  --stack-name VipAdminHostingStack \
  --region us-east-1 \
  --query "Stacks[0].Outputs[?OutputKey=='DistributionId'].OutputValue" \
  --output text)

AWS_PROFILE=production aws s3 sync dist/ "s3://${ASSET_BUCKET}/" \
  --delete --region us-east-1

AWS_PROFILE=production aws cloudfront create-invalidation \
  --distribution-id "${DIST_ID}" \
  --paths '/*'
```

### Deploy CDK stacks

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/infra
npx cdk deploy --all --require-approval broadening --profile production
```

CDK-managed stacks (monitoring is NOT in CDK):

1. `VipAdminDataStack`
2. `VipAdminAuthStack`
3. `VipAdminApiSegmentsStack`
4. `VipAdminApiCampaignsStack`
5. `VipAdminApiMetricsStack`
6. `VipAdminApiPlansStack`
7. `VipAdminApiProfilesStack`
8. `VipAdminApiStack`
9. `VipAdminHostingStack`

---

## 6. Useful AWS CLI commands

### Tail Lambda logs

```bash
# api-plans (last 30 minutes)
AWS_PROFILE=production aws logs tail /aws/lambda/vip-admin-ui-api-plans \
  --region us-east-1 --since 30m --format short

# feeder
AWS_PROFILE=production aws logs tail /aws/lambda/vip-external-campaigns-feeder \
  --region us-east-1 --since 30m --format short

# Follow in real time
AWS_PROFILE=production aws logs tail /aws/lambda/vip-admin-ui-api-plans \
  --region us-east-1 --follow
```

### Inspect a plan

```bash
AWS_PROFILE=production aws dynamodb get-item \
  --table-name VipAdminPlans \
  --region us-east-1 \
  --key '{"pk":{"S":"PLAN#<planId>"},"sk":{"S":"META"}}' \
  | jq '.Item'
```

### List active runs (most recent first)

```bash
AWS_PROFILE=production aws dynamodb query \
  --table-name VipAdminPlans \
  --region us-east-1 \
  --key-condition-expression "pk = :pk AND begins_with(sk, :prefix)" \
  --expression-attribute-values '{":pk":{"S":"PLAN#<planId>"},":prefix":{"S":"RUN#"}}' \
  --scan-index-forward false \
  --limit 10 \
  | jq '.Items[] | {sk: .sk.S, status: .status.S, bucket: .currentBucketIndex.N}'
```

### List EventBridge Scheduler schedules for a run

```bash
AWS_PROFILE=production aws scheduler list-schedules \
  --group-name vip-plans \
  --region us-east-1 \
  --query 'Schedules[?contains(Name, `<runId>`)].[Name,State]' \
  --output table
```

### Force-delete a stuck EventBridge schedule

```bash
AWS_PROFILE=production aws scheduler delete-schedule \
  --name "vip-plan-<planId>-run-<runId>-b0" \
  --group-name vip-plans \
  --region us-east-1
```

### Check campaign state in Connect

```bash
AWS_PROFILE=production aws connectcampaignsv2 get-campaign-state \
  --id <connectCampaignId> \
  --region us-east-1 \
  | jq '.state'
```

### Manually invoke prestart_check (testing)

```bash
AWS_PROFILE=production aws lambda invoke \
  --function-name vip-admin-ui-api-plans \
  --region us-east-1 \
  --payload '{"action":"prestart_check"}' \
  --cli-binary-format raw-in-base64-out \
  /tmp/prestart-response.json && cat /tmp/prestart-response.json
```

### Manually invoke a tick (testing)

```bash
AWS_PROFILE=production aws lambda invoke \
  --function-name vip-admin-ui-api-plans \
  --region us-east-1 \
  --payload '{"action":"tick","planId":"<planId>","runId":"<runId>","bucketIndex":0}' \
  --cli-binary-format raw-in-base64-out \
  /tmp/tick-response.json && cat /tmp/tick-response.json
```

### Monitoring — list alarm states

```bash
AWS_PROFILE=production aws cloudwatch describe-alarms \
  --alarm-name-prefix "vip-" \
  --region us-east-1 \
  --query 'MetricAlarms[].{Name:AlarmName, State:StateValue, Reason:StateReason}' \
  --output table
```

### Monitoring — rebuild alarms or dashboard (if destroyed)

```bash
# Alarms
bash /home/devaju/projects/vip-connect-external-campaigns/infra/scripts/create-alarms.sh

# Dashboard
AWS_PROFILE=production aws cloudwatch put-dashboard \
  --dashboard-name VipConnect-Admin-UI \
  --dashboard-body file:///home/devaju/projects/vip-connect-external-campaigns/infra/scripts/dashboard.json \
  --region us-east-1
```

### Recreate prestart_check EventBridge rule (if deleted)

```bash
AWS_PROFILE=production aws events put-rule \
  --name "vip-plans-prestart-check" \
  --schedule-expression "rate(1 minute)" \
  --state ENABLED \
  --region us-east-1

AWS_PROFILE=production aws events put-targets \
  --rule "vip-plans-prestart-check" \
  --region us-east-1 \
  --targets '[{"Id":"1","Arn":"arn:aws:lambda:us-east-1:165505826690:function:vip-admin-ui-api-plans","Input":"{\"action\":\"prestart_check\"}"}]'

AWS_PROFILE=production aws lambda add-permission \
  --function-name vip-admin-ui-api-plans \
  --statement-id AllowEventBridgePrestartCheck \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn "arn:aws:events:us-east-1:165505826690:rule/vip-plans-prestart-check" \
  --region us-east-1
```
