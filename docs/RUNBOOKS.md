# Runbooks

Operational playbooks for the **VIP Connect External Campaigns** platform. Every runbook follows the same structure.

> All AWS CLI commands assume `--profile production` and `us-east-1`. Replace `{planId}` / `{runId}` / `{state}` placeholders inline.

---

## RB-001 — Run is stuck in `running` for > 30 minutes

**Severity:** P2 (operator-visible delay).
**Trigger:** Operator reports a campaign that "isn't dialling" or a bucket that won't advance. `StuckRunDetected` metric alarm at 4h.
**Impact:** No outbound dials placed for the affected bucket. Other plans unaffected.
**Prerequisites:** AWS SSO login active, console + CLI access.

### Diagnosis

```bash
# 1. Find the run
aws dynamodb query \
  --table-name VipAdminPlans \
  --key-condition-expression "pk = :pk AND begins_with(sk, :run)" \
  --expression-attribute-values '{":pk":{"S":"PLAN#{planId}"},":run":{"S":"RUN#"}}' \
  --max-items 5 \
  --no-cli-pager \
  --profile production

# 2. Inspect tick logs for the last hour
aws logs filter-log-events \
  --log-group-name /aws/lambda/vip-admin-ui-api-plans \
  --filter-pattern '"event"' \
  --start-time $(($(date +%s) - 3600))000 \
  --profile production | jq '.events[].message' | tail -100

# 3. Confirm the EventBridge rule still exists
aws events list-rules --name-prefix vip-plan-{runId} --profile production
```

### Resolution

1. If the EventBridge rule was deleted but the run is still `running`, re-create the rule manually or abort + restart:
   ```bash
   # Abort the stuck run
   curl -X POST https://<api>/plans/{planId}/runs/{runId}/abort \
        -H "Authorization: Bearer <jwt>"
   ```
2. If ticks are firing but campaigns are stuck in `INITIALIZED`, check Connect campaign state directly:
   ```bash
   aws connectcampaignsv2 get-campaign-state --id <connectCampaignId> --profile production
   ```
3. If Connect reports `FAILED`, mark run failed via the `force-finish` endpoint:
   ```bash
   curl -X POST https://<api>/plans/{planId}/runs/{runId}/force-finish \
        -H "Authorization: Bearer <jwt>"
   ```

### Verification

DynamoDB Run record `status` is no longer `running`; the EventBridge rule is gone; no further `tick` log events for this run.

### Escalation

Page the platform engineer if the same plan goes stuck twice in 24 h.

### Prevention

- Daily janitor on the backlog to delete orphan rules.
- Monitor `StuckRunDetected` SNS topic.

---

## RB-002 — Campaign `exitReason=skipped_empty` for all states

**Severity:** P2.
**Trigger:** Every campaign in a bucket exits with `skipped_empty`, no calls placed.
**Impact:** Whole bucket effectively no-op.
**Prerequisites:** Same as RB-001.

### Diagnosis

```bash
# Inspect the run's campaignStates
aws dynamodb get-item \
  --table-name VipAdminPlans \
  --key '{"pk":{"S":"PLAN#{planId}"},"sk":{"S":"RUN#{runId}"}}' \
  --profile production | jq '.Item.bucketStates'
```

Check `reconcileRetryLimit` in `planSnapshot.buckets[0]`. If it is `1`, you've hit TD-005.

Check Redis lead count for one of the failed states:

```bash
# From a Lambda in the VPC, or via a bastion EC2:
redis-cli -h $REDIS_HOST -p 6379 HLEN wait_list:vip:list
```

### Resolution

If TD-005 (legacy `reconcileRetryLimit=1`):

1. Edit the plan; save (PUT will normalise the value to default 5).
2. Cancel the run.
3. Trigger a fresh run.

If Redis is truly empty: confirm the feeder pipeline is running. Out of scope for this repo.

### Verification

Subsequent run shows `leadCount > 0` and `exitReason=completed`.

### Escalation

Notify feeder team if Redis remains empty after 15 minutes.

### Prevention

Run a one-time migration to bump `reconcileRetryLimit` from `1` to `5` in all `planSnapshot` records.

---

## RB-003 — Connect rejects with `Operation is not valid`

**Severity:** P1 — full platform outage.
**Trigger:** Every `StartCampaign` fails; all campaigns show `exitReason=creation_failed`.
**Impact:** No campaigns can start.

### Diagnosis

```bash
aws logs filter-log-events \
  --log-group-name /aws/lambda/vip-admin-ui-api-plans \
  --filter-pattern '"Operation is not valid"' \
  --start-time $(($(date +%s) - 1800))000 \
  --profile production
```

If matched: the Connect instance has been reconfigured away from segment-driven dialing.

### Resolution

1. In the Connect console, verify the instance is enabled for outbound campaigns with **Customer Profiles** integration.
2. Re-enable segment-driven dialing on the instance.
3. Abort any in-flight runs.

### Verification

A manual `StartCampaign` test from a Lambda environment succeeds. New runs reach `running`.

### Escalation

Page AWS support (Enterprise tier). The fix may require re-provisioning the instance.

### Prevention

Lock down Connect console access. Add a CloudWatch alarm on `creation_failed` rate > 50% / 5 min.

---

## RB-004 — Pre-warm not creating campaigns

**Severity:** P2.
**Trigger:** Every time-triggered run starts 6 minutes late.
**Impact:** Plans lose 6 minutes of dial time per bucket.

### Diagnosis

```bash
# Confirm prestart_check rule exists
aws events list-rules --name-prefix vip-plans-prestart-check --profile production

# Look for prestart logs
aws logs filter-log-events \
  --log-group-name /aws/lambda/vip-admin-ui-api-plans \
  --filter-pattern '"event":"prestart_check"' \
  --start-time $(($(date +%s) - 600))000 \
  --profile production

# Check pendingWarmup on the next plan to trigger
aws dynamodb get-item \
  --table-name VipAdminPlans \
  --key '{"pk":{"S":"PLAN#{planId}"},"sk":{"S":"META"}}' \
  --profile production | jq '.Item.pendingWarmup'
```

### Resolution

1. If rule is missing, recreate (verified live rule name and target Input, 2026-08-21 —
   the rule is `vip-plans-prestart-check`, NOT `vip-sched-prestart-check`; that
   `vip-sched-*` prefix is used exclusively by per-plan daily time triggers,
   see scheduler_manager.py):
   ```bash
   aws events put-rule --name vip-plans-prestart-check \
     --schedule-expression "rate(1 minute)" \
     --profile production
   aws events put-targets --rule vip-plans-prestart-check \
     --targets 'Id=1,Arn=arn:aws:lambda:us-east-1:165505826690:function:vip-admin-ui-api-plans,Input="{\"action\":\"prestart_check\"}"' \
     --profile production
   aws lambda add-permission --function-name vip-admin-ui-api-plans \
     --statement-id vip-plans-prestart-check \
     --action lambda:InvokeFunction --principal events.amazonaws.com \
     --source-arn arn:aws:events:us-east-1:165505826690:rule/vip-plans-prestart-check \
     --profile production
   ```
2. If `pendingWarmup` exists but is stale, clear it manually:
   ```bash
   aws dynamodb update-item \
     --table-name VipAdminPlans \
     --key '{"pk":{"S":"PLAN#{planId}"},"sk":{"S":"META"}}' \
     --update-expression "REMOVE pendingWarmup" \
     --profile production
   ```

### Verification

Next time-trigger pre-warmed; logs show `_prestart_plan` event 5 minutes before run start.

### Escalation

Platform engineer.

### Prevention

Move `vip-plans-prestart-check` into CDK (when the boundary permits).

---

## RB-005 — Concurrent run rejected (`Plan already has an active run`)

**Severity:** P3.
**Trigger:** Operator tries to manually trigger a plan that already has a run; HTTP 400.
**Impact:** Operator cannot start a new run until the existing one terminates.

### Diagnosis

```bash
aws dynamodb get-item \
  --table-name VipAdminPlans \
  --key '{"pk":{"S":"PLAN#{planId}"},"sk":{"S":"META"}}' \
  --profile production | jq '.Item.runLock'
```

If `runLock` is set but `get_latest_run` shows the run is `completed`, the lock leaked.

### Resolution

```bash
aws dynamodb update-item \
  --table-name VipAdminPlans \
  --key '{"pk":{"S":"PLAN#{planId}"},"sk":{"S":"META"}}' \
  --update-expression "REMOVE runLock" \
  --profile production
```

### Verification

Operator retries trigger; succeeds.

### Escalation

If leak repeats, file bug — `unlock_plan_run` not being called on a code path.

### Prevention

Audit terminal-state transitions in `executor.py` and confirm `unlock_plan_run` is called.

---

## RB-006 — 19:00 COT cutoff: campaigns still running past 19:00

**Severity:** P1 (HIPAA / contractual).
**Trigger:** Operator reports calls after 7 PM Colombia time.
**Impact:** Patients called outside permitted window.

### Diagnosis

```bash
# Find any campaign with status=running at 19:01 COT (00:01 UTC)
aws dynamodb scan \
  --table-name VipAdminPlans \
  --filter-expression "begins_with(sk, :run) AND #s = :running" \
  --expression-attribute-names '{"#s":"status"}' \
  --expression-attribute-values '{":run":{"S":"RUN#"},":running":{"S":"running"}}' \
  --profile production
```

### Resolution

For each still-running campaign:

```bash
aws connectcampaignsv2 stop-campaign --id <connectCampaignId> --profile production
```

Then force-finish the runs.

### Verification

`aws connectcampaignsv2 get-campaign-state --id …` returns `STOPPED`. Run records show `cancelled`.

### Escalation

Notify Compliance/HIPAA officer if calls were placed after the cutoff.

### Prevention

CloudWatch alarm on "campaigns running at 19:05 COT": custom metric emitted by tick.

---

## RB-007 — DynamoDB `ConcurrentWriteError` storm

**Severity:** P2.
**Trigger:** CloudWatch log shows repeated `ConcurrentWriteError` events for the same run.
**Impact:** Run state may diverge from Connect reality.

### Diagnosis

```bash
aws logs filter-log-events \
  --log-group-name /aws/lambda/vip-admin-ui-api-plans \
  --filter-pattern '"ConcurrentWriteError"' \
  --start-time $(($(date +%s) - 600))000 \
  --profile production
```

Likely cause: two EventBridge ticks firing within seconds (orphan rule + active rule).

### Resolution

```bash
aws events list-rules --name-prefix vip-plan-{runId} --profile production
# If more than one rule for the same {runId}-{idx}, delete duplicates:
aws events remove-targets --rule <dup-rule-name> --ids 1 --profile production
aws events delete-rule --name <dup-rule-name> --profile production
```

### Verification

ConcurrentWriteError rate drops to zero.

### Escalation

Platform engineer if duplicates keep appearing.

### Prevention

Daily janitor task (backlog).

---

## RB-008 — Frontend white-screen after deploy

**Severity:** P2.
**Trigger:** Operators see a blank page after a frontend deploy.
**Impact:** Cannot manage plans via UI.

### Diagnosis

1. Browser devtools → console. Look for missing assets (404) or auth errors.
2. Confirm CloudFront invalidation completed:
   ```bash
   aws cloudfront list-invalidations --distribution-id E3QCDJPG0LCO7E --profile production
   ```

### Resolution

1. If 404 on JS bundle: invalidate again:
   ```bash
   aws cloudfront create-invalidation --distribution-id E3QCDJPG0LCO7E --paths "/*" --profile production
   ```
2. If Cognito redirect URL mismatch: verify the app client's callback URL list includes the CloudFront domain.
3. If a bad build was pushed, roll back by syncing the previous `dist/` build:
   ```bash
   # Re-checkout the previous commit, build, deploy.
   git checkout <previous-sha>
   cd frontend && npm ci && npm run build
   aws s3 sync dist/ s3://vip-admin-ui-assets-165505826690/ --delete --profile production
   aws cloudfront create-invalidation --distribution-id E3QCDJPG0LCO7E --paths "/*" --profile production
   ```

### Verification

Hard refresh; UI loads; login works.

### Escalation

Frontend engineer if Cognito misconfiguration suspected.

### Prevention

Add a build-info marker (commit SHA) to UI footer for easier triage.

---

## RB-009 — `pendingWarmup` not cleared after run start

**Severity:** P3.
**Trigger:** Plan META carries `pendingWarmup` but the run has already started.
**Impact:** Next pre-warm cycle thinks warmup is in progress; new campaigns not created.

### Diagnosis

See RB-004 diagnostic block.

### Resolution

```bash
aws dynamodb update-item \
  --table-name VipAdminPlans \
  --key '{"pk":{"S":"PLAN#{planId}"},"sk":{"S":"META"}}' \
  --update-expression "REMOVE pendingWarmup" \
  --profile production
```

### Verification

Next `prestart_check` log event shows fresh warmup.

### Prevention

`start_run` always calls `update_plan_pending_warmup(plan_id, None)` — audit code paths where it might be skipped (e.g. exception before the clear).

---

## RB-010 — Lambda OOM / timeout on api-plans

**Severity:** P2.
**Trigger:** `vip-plans-errors` alarm — `Errors > 0` in a single 1-minute period
(verified live 2026-08-21; this doc previously said `Errors > 5` in 5 minutes,
which was never the real threshold — the live alarm pages on the FIRST error,
not after 5 accumulate). Also watch `vip-plans-duration-p99` — Duration p99 >
240000ms (4min) over a 5-min period, i.e. approaching the 5min timeout.
**Impact:** Some ticks fail silently; runs may stall.

### Diagnosis

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/Lambda \
  --metric-name Duration \
  --dimensions Name=FunctionName,Value=vip-admin-ui-api-plans \
  --start-time $(date -u -d '1 hour ago' +%FT%T) \
  --end-time $(date -u +%FT%T) \
  --period 60 \
  --statistics Maximum \
  --profile production
```

### Resolution

- If Redis scan dominates duration: confirm `wait_list:{team}:list` size; ask feeder team to prune.
- Temporary bump memory:
  ```bash
  aws lambda update-function-configuration \
    --function-name vip-admin-ui-api-plans \
    --memory-size 2048 \
    --profile production
  ```
- Increase reserved concurrency if throttled.

### Verification

`Duration` p95 < 60 s; `Errors` returns to 0.

### Escalation

Platform engineer for code-level optimisation.

### Prevention

Profile Redis paths; consider periodic compaction.

---

## RB-011 — `runLock` leaked

**Severity:** P3.
**Trigger:** Operator cannot start any run on a plan; previous run is `completed` but `runLock` still set.
**Impact:** That single plan is blocked.

See RB-005 — same resolution. The two RBs differ by entry point (concurrent vs leaked).

---

## RB-012 — SNS alerts not delivered

**Severity:** P3.
**Trigger:** Expected alert (stuck run, prewarm failure) never reaches subscribed inbox.
**Impact:** Operators miss operational signals.

### Diagnosis

```bash
# List subscriptions
aws sns list-subscriptions-by-topic \
  --topic-arn arn:aws:sns:us-east-1:165505826690:vip-plans-alerts \
  --profile production

# Look for Publish errors in api-plans logs
aws logs filter-log-events \
  --log-group-name /aws/lambda/vip-admin-ui-api-plans \
  --filter-pattern '"sns_publish_error"' \
  --start-time $(($(date +%s) - 86400))000 \
  --profile production
```

### Resolution

1. If subscription is `PendingConfirmation`, ask subscriber to confirm.
2. If subscription is missing, add:
   ```bash
   aws sns subscribe \
     --topic-arn arn:aws:sns:us-east-1:165505826690:vip-plans-alerts \
     --protocol email \
     --notification-endpoint ops@example.com \
     --profile production
   ```
3. If `sns:Publish` is denied, check Lambda execution role policy.

### Verification

Trigger a synthetic alert by aborting a healthy run; confirm subscriber receives it.

### Escalation

Security review if IAM policy was recently changed.

### Prevention

Document required subscribers in `INTEGRATION_CONTRACTS.md §7`.
