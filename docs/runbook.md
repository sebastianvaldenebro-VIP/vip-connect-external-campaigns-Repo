# Runbook — VIP Connect Admin UI

Day-to-day operation guide. Covers deploy, user management, troubleshooting, and incident response.

---

## 1. Deploy

### First-time setup

```bash
cd /home/devaju/projects/vip-connect-external-campaigns

# Ensure AWS credentials (SSO)
aws sso login --profile production
aws sts get-caller-identity --profile production
# Expected: Account=165505826690

# Install CDK deps
cd infra && npm install && cd ..

# CDK bootstrap (idempotent — skip if CDKToolkit stack already exists)
npx cdk bootstrap aws://165505826690/us-east-1 \
  --custom-permissions-boundary EngineeringPermissionBoundary \
  --profile production
```

### Deploy CDK stacks

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/infra

# Full deploy — CDK resolves dependency order automatically
npx cdk deploy --all --require-approval broadening --profile production
```

CDK deploy order (managed by dependency graph):

1. `VipAdminDataStack` — DynamoDB tables (`VipAdminPlans`, `AdminAuditLog`, `SegmentFilterConfig`) + KMS CMK
2. `VipAdminAuthStack` — Cognito User Pool + Hosted UI + MFA enforcement
3. `VipAdminApiSegmentsStack` — api-segments Lambda + S3 snapshot bucket + SharedLayer
4. `VipAdminApiCampaignsStack` — api-campaigns Lambda
5. `VipAdminApiMetricsStack` — api-metrics Lambda
6. `VipAdminApiPlansStack` — api-plans Lambda + `VipAdminPlans` table access
7. `VipAdminApiProfilesStack` — api-profiles Lambda
8. `VipAdminApiStack` — API Gateway HTTP API + Cognito JWT Authorizer
9. `VipAdminHostingStack` — S3 bucket + CloudFront distribution

**NOTE:** The MonitoringStack (SNS topic, CloudWatch alarms, dashboard) is NOT managed by CDK.
The CFN exec role `VipAdminCdkCfnExecPolicy` lacks SNS and cloudwatch:PutDashboard permissions.
All monitoring resources were created via CLI and persist independently.

### Deploy api-plans Lambda (code-only change)

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

### Deploy any other Lambda (code-only change)

Replace `api-plans` with the target function name (`api-segments`, `api-campaigns`, `api-profiles`, `api-metrics`):

```bash
# api-plans: services/api-plans/src/
# api-segments: services/api-segments/src/
# api-campaigns: services/api-campaigns/src/
# api-profiles: services/api-profiles/src/
# api-metrics: services/api-metrics/src/
# (feeder was decommissioned — the Lambda no longer exists. Verified live
# 2026-08-21: aws lambda get-function returns ResourceNotFoundException.)

cd /home/devaju/projects/vip-connect-external-campaigns/services/<service>/src
zip -r /tmp/<service>.zip . -x "__pycache__/*" "*.pyc"
AWS_PROFILE=production aws lambda update-function-code \
  --function-name vip-<function-name> \
  --zip-file fileb:///tmp/<service>.zip \
  --region us-east-1
```

### Deploy shared Lambda layer

> **CRITICAL:** The SharedLayer (`SharedLayer27DFABF0`) contains BOTH the `vip_shared` Python source AND pip-installed packages (redis, etc.). Never rebuild from source alone — always patch from an existing layer zip to preserve the pip dependencies.

```bash
# 1. Download the current layer zip to patch it
CURRENT_LAYER_VERSION=$(AWS_PROFILE=production aws lambda list-layer-versions \
  --layer-name vip-admin-shared \
  --region us-east-1 \
  --query 'LayerVersions[0].Version' --output text)

LAYER_URL=$(AWS_PROFILE=production aws lambda get-layer-version \
  --layer-name vip-admin-shared \
  --version-number $CURRENT_LAYER_VERSION \
  --region us-east-1 \
  --query 'Content.Location' --output text)

curl -o /tmp/current-layer.zip "$LAYER_URL"

# 2. Unzip, overwrite only the vip_shared source files, re-zip
mkdir -p /tmp/layer-patch
cd /tmp/layer-patch && unzip -q /tmp/current-layer.zip

# Copy updated source (adjust path to match actual layer layout)
cp -r /home/devaju/projects/vip-connect-external-campaigns/services/shared/python/vip_shared \
  /tmp/layer-patch/python/vip_shared/

cd /tmp/layer-patch && zip -r /tmp/vip-shared-patched.zip .

# 3. Publish new version
NEW_VERSION=$(AWS_PROFILE=production aws lambda publish-layer-version \
  --layer-name vip-admin-shared \
  --zip-file fileb:///tmp/vip-shared-patched.zip \
  --compatible-runtimes python3.12 \
  --region us-east-1 \
  --query 'Version' --output text)

echo "New layer version: $NEW_VERSION"

# 4. Update ALL five Lambdas to use the new layer version
ACCOUNT=165505826690
LAYER_ARN="arn:aws:lambda:us-east-1:${ACCOUNT}:layer:vip-admin-shared:${NEW_VERSION}"

for FN in vip-admin-ui-api-plans vip-admin-ui-api-segments vip-admin-ui-api-campaigns \
          vip-admin-ui-api-profiles vip-admin-ui-api-metrics; do
  AWS_PROFILE=production aws lambda update-function-configuration \
    --function-name $FN \
    --layers "$LAYER_ARN" \
    --region us-east-1
  echo "Updated $FN → layer:$NEW_VERSION"
done
```

### Deploy frontend (SPA)

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/frontend
npm run build

# Fetch bucket name and CloudFront distribution ID from CDK outputs
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
  --delete \
  --region us-east-1

AWS_PROFILE=production aws cloudfront create-invalidation \
  --distribution-id "${DIST_ID}" \
  --paths '/*'
```

### Rollback — Lambda

```bash
# Deploy the previous code version by checking out the prior commit
# and re-running the Lambda zip+update commands above.

# Or promote an older Lambda version alias directly:
AWS_PROFILE=production aws lambda update-alias \
  --function-name vip-admin-ui-api-plans \
  --name prod \
  --function-version <version-number> \
  --region us-east-1
```

### Rollback — CDK stacks

```bash
# CloudFormation rollback to previous successful deployment
AWS_PROFILE=production aws cloudformation rollback-stack \
  --stack-name VipAdminApiPlansStack \
  --region us-east-1
```

DynamoDB PITR — restore to any point in last 35 days:

```bash
AWS_PROFILE=production aws dynamodb restore-table-to-point-in-time \
  --source-table-name VipAdminPlans \
  --target-table-name VipAdminPlans_restore_$(date +%s) \
  --restore-date-time 2026-05-13T14:00:00Z
```

---

## 2. User management

### Add a new operator

```bash
# Get the User Pool ID from CDK outputs
POOL_ID=$(AWS_PROFILE=production aws cloudformation describe-stacks \
  --stack-name VipAdminAuthStack --region us-east-1 \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" --output text)

AWS_PROFILE=production aws cognito-idp admin-create-user \
  --user-pool-id $POOL_ID \
  --username operator.name@medwork.io \
  --user-attributes Name=email,Value=operator.name@medwork.io Name=email_verified,Value=true \
  --desired-delivery-mediums EMAIL \
  --temporary-password 'TempPass!23'

# User receives email, logs in, must set permanent password + enroll MFA (TOTP)
```

### Disable a user (compromised / offboarded)

```bash
AWS_PROFILE=production aws cognito-idp admin-disable-user \
  --user-pool-id $POOL_ID \
  --username operator.name@medwork.io

# Revoke all active tokens immediately
AWS_PROFILE=production aws cognito-idp admin-user-global-sign-out \
  --user-pool-id $POOL_ID \
  --username operator.name@medwork.io
```

### Reset MFA

```bash
AWS_PROFILE=production aws cognito-idp admin-set-user-mfa-preference \
  --user-pool-id $POOL_ID \
  --username operator.name@medwork.io \
  --software-token-mfa-settings Enabled=false,PreferredMfa=false

# User re-enrolls on next login
```

### List active users

```bash
AWS_PROFILE=production aws cognito-idp list-users \
  --user-pool-id $POOL_ID \
  --attributes-to-get email,sub,email_verified \
  --query 'Users[?Enabled==`true`].[Username,UserStatus,UserCreateDate]' \
  --output table
```

---

## 3. Troubleshooting

### Plans — run stuck / campaigns not starting

**Symptom:** Plan is `running` but campaigns stay `queued` for > 2 minutes.

**Diagnostic:**

```bash
# 1. Get the current run record
AWS_PROFILE=production aws dynamodb query \
  --table-name VipAdminPlans \
  --region us-east-1 \
  --key-condition-expression "pk = :pk AND begins_with(sk, :prefix)" \
  --expression-attribute-values '{":pk":{"S":"PLAN#<planId>"},":prefix":{"S":"RUN#"}}' \
  --scan-index-forward false \
  --limit 1 \
  | jq '.Items[0] | {status: .status.S, currentBucketIndex: .currentBucketIndex.N,
      campaigns: [.bucketStates.L[0].M.campaignStates.L[].M |
        {id: .campaignId.S, status: .status.S, exitReason: .exitReason.S}]}'

# 2. Check for stuck EventBridge tick schedule
AWS_PROFILE=production aws scheduler list-schedules \
  --group-name vip-plans \
  --region us-east-1 \
  --query 'Schedules[?contains(Name, `<runId>`)].[Name,State]' \
  --output table

# 3. Tail Lambda logs
AWS_PROFILE=production aws logs tail /aws/lambda/vip-admin-ui-api-plans \
  --region us-east-1 --since 10m --format short
```

**Common causes:**

| Symptom | Cause | Fix |
|---|---|---|
| All campaigns `queued`, tick not firing | EventBridge Scheduler was not created | Check scheduler logs; abort and re-trigger run |
| Campaign `error`, `errorDetail` has "SEGMENT" | Segment build failed — likely Redis rebuilding | Retry: abort run, wait 5 min, re-trigger |
| Campaign `cancelled`, `exitReason: "parent_cancelled"` | A parent campaign errored or was cancelled | Check parent campaign's `errorDetail`; fix root cause and re-run |
| `exitReason: "bucket_expired"` on queued campaign | Bucket time limit expired before campaign could start | DAG has too many sequential stages for the bucket duration |

**Note on skipped buckets:** If a run was triggered with `startBucketIndex > 0`, all campaigns in earlier buckets will have `status: "cancelled"` with `exitReason: "skipped"`. This is expected and does NOT cascade-cancel subsequent campaigns.

---

### Plans — pre-start warmup not firing

**Symptom:** First campaign of a new bucket starts cold (no `connectCampaignId` pre-populated); there's a visible 5-minute lag at bucket transitions or plan starts.

**Diagnostic:**

```bash
# Check plan META for pendingWarmup field
AWS_PROFILE=production aws dynamodb get-item \
  --table-name VipAdminPlans --region us-east-1 \
  --key '{"pk":{"S":"PLAN#<planId>"},"sk":{"S":"META"}}' \
  | jq '.Item.pendingWarmup'

# Check prestart_check EventBridge rule exists
AWS_PROFILE=production aws events describe-rule \
  --name vip-plans-prestart-check \
  --region us-east-1

# Check prestart_check Lambda permission exists
AWS_PROFILE=production aws lambda get-policy \
  --function-name vip-admin-ui-api-plans \
  --region us-east-1 \
  | jq '.Policy | fromjson | .Statement[] | select(.Sid == "AllowEventBridgePrestartCheck")'
```

**Fix if rule or permission is missing:**

```bash
# Re-create the prestart_check rule (safe to run again — idempotent)
AWS_PROFILE=production aws events put-rule \
  --name "vip-plans-prestart-check" \
  --schedule-expression "rate(1 minute)" \
  --state ENABLED \
  --region us-east-1

AWS_PROFILE=production aws events put-targets \
  --rule "vip-plans-prestart-check" \
  --region us-east-1 \
  --targets '[{"Id":"1","Arn":"arn:aws:lambda:us-east-1:165505826690:function:vip-admin-ui-api-plans","Input":"{\"action\":\"prestart_check\"}"}]'

# Permission (only needed if it doesn't exist)
AWS_PROFILE=production aws lambda add-permission \
  --function-name vip-admin-ui-api-plans \
  --statement-id AllowEventBridgePrestartCheck \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn "arn:aws:events:us-east-1:165505826690:rule/vip-plans-prestart-check" \
  --region us-east-1
```

---

### Plans — loop runs all night / doesn't stop at endTime

**Symptom:** A looping plan (has `loop.endTime`) keeps creating new runs past the scheduled end time.

**Cause:** `_force_finish_internal` was previously calling `_maybe_loop`, creating a restart cycle. This was fixed. If the symptom reappears:

```bash
# 1. Find all active EventBridge schedules for this plan (may be dozens from repeated runs)
AWS_PROFILE=production aws scheduler list-schedules \
  --group-name vip-plans \
  --region us-east-1 \
  --query 'Schedules[?contains(Name, `<planId>`)].[Name,State]' \
  --output table

# 2. Abort the active run
AWS_PROFILE=production aws lambda invoke \
  --function-name vip-admin-ui-api-plans \
  --region us-east-1 \
  --payload '{"routeKey":"POST /plans/<planId>/runs/<runId>/abort"}' \
  --cli-binary-format raw-in-base64-out \
  /tmp/abort-response.json

# 3. Delete any orphaned schedules
aws scheduler delete-schedule \
  --name "vip-plan-<planId>-run-<runId>-b0" \
  --group-name vip-plans \
  --region us-east-1 \
  --profile production
```

---

### Segment estimate never completes

**Symptom:** UI shows "Computing..." indefinitely, polling `GetSegmentEstimate` returns `IN_PROGRESS` for > 5 min.

```bash
AWS_PROFILE=production aws customer-profiles get-segment-estimate \
  --region us-east-1 \
  --domain-name amazon-connect-vipmedicalgroup \
  --estimate-id <estimate-id>
```

**Common causes:**
- Segment too broad (millions of profiles) → typical < 3 min; escalate to AWS Support if > 15 min
- Customer Profiles domain busy (concurrent snapshot) → retry after 5 min
- Segment references a non-existent calculated attribute → delete + recreate segment

---

### Campaign won't start

**Symptom:** `StartCampaign` returns error or state stays `INITIALIZED`.

| Error | Cause | Fix |
|---|---|---|
| `Missing required campaign parameter Schedule` | Created without schedule | `UpdateCampaignSchedule` or recreate |
| `Missing required campaign parameter Campaign Flow` | Missing `connectCampaignFlowArn` | Recreate with correct flow ARN (type CAMPAIGN) |
| `Phone number is not in E.164 format` | Non-E.164 phone | Strip formatting, must start with `+` |
| `Schedule start time needs to be at least 5 minutes from now` | startTime too close | Add ≥ 10 min buffer |
| `Event Triggered Campaigns do not allow parameter: CommunicationTimeConfig` | Wrong config for event-trigger campaign | Remove `communicationTimeConfig` |

---

### PutOutboundRequestBatch returns `Operation is not valid for this campaign`

**This is documented and expected.** Voice-channel V2 campaigns do NOT accept external push. See [ADR-001](./architecture-decisions.md#adr-001--reject-v2-external-push-for-voice-outbound-campaigns). Do not attempt to fix this by changing source type or flow config.

---

### Operator can't log in

1. Check user status: `aws cognito-idp admin-get-user --user-pool-id $POOL_ID --username <email> --profile production`
2. Verify `Enabled: true` and `UserStatus: CONFIRMED`
3. Check MFA enrollment under `UserMFASettingList`
4. Reset if locked: `aws cognito-idp admin-reset-user-password --user-pool-id $POOL_ID --username <email> --profile production`

---

### Audit log not recording actions

```bash
# Check Lambda logs for audit write failures
AWS_PROFILE=production aws logs tail /aws/lambda/vip-admin-ui-api-segments \
  --region us-east-1 --since 15m --filter-pattern "audit"

# Simulate IAM policy check
AWS_PROFILE=production aws iam simulate-principal-policy \
  --policy-source-arn $(AWS_PROFILE=production aws lambda get-function-configuration \
    --function-name vip-admin-ui-api-segments --query 'Role' --output text) \
  --action-names dynamodb:PutItem \
  --resource-arns arn:aws:dynamodb:us-east-1:165505826690:table/AdminAuditLog
```

**Common causes:** KMS CMK policy missing Lambda role as key user; redeploy CDK if IAM drift.

---

### Campaigns quota exceeded

**Symptom:** `ServiceQuotaExceededException: Campaign count service limit would be exceeded`

Default quota: 10 campaigns per account per region. Completed campaigns count until deleted.

```bash
# List all campaigns
AWS_PROFILE=production aws connectcampaignsv2 list-campaigns --region us-east-1 \
  --query 'campaignSummaryList[].{id:id, name:name}' --output table

# Stop + delete old campaigns
AWS_PROFILE=production aws connectcampaignsv2 stop-campaign --region us-east-1 --id <id>
AWS_PROFILE=production aws connectcampaignsv2 delete-campaign --region us-east-1 --id <id>
```

---

### UI gives CORS errors

**Symptom:** Browser console shows `Access-Control-Allow-Origin` blocked.

```bash
API_ID=$(AWS_PROFILE=production aws cloudformation describe-stacks \
  --stack-name VipAdminApiStack --region us-east-1 \
  --query "Stacks[0].Outputs[?OutputKey=='ApiId'].OutputValue" --output text)

AWS_PROFILE=production aws apigatewayv2 get-api \
  --api-id $API_ID \
  --query 'CorsConfiguration'
```

**Fix:** Update `api-stack.ts` `corsAllowOrigins` context value and redeploy `VipAdminApiStack`.

---

## 4. Monitoring

### Dashboard

URL: `https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#dashboards:name=VipConnect-Admin-UI`

Widgets:
- Lambda Errors (api-plans, api-segments, api-campaigns, api-metrics, api-profiles, campaign-exporter, location-onboarding-guard)
- Lambda Throttles (api-plans, api-segments, api-campaigns)
- Lambda Invocations (all functions above)
- Lambda Duration p99 (api-plans, api-segments)
- DynamoDB RCU/WCU + Throttles/SystemErrors, all 11 in-repo VIP tables
- EventBridge Invocations + Failed Invocations (prestart-check, campaign-events rules)
- API Gateway 5xx (vip-admin-ui-api)
- Alarm status panel

(`feeder` was decommissioned — removed from all widgets. Verified live
2026-08-21: the Lambda no longer exists.)

### CloudWatch Alarms (58 total as of 2026-08-21)

All alarms send to SNS topic `arn:aws:sns:us-east-1:165505826690:vip-admin-alerts` (email: sebastian.valdenebro@medwork.io).

This table used to hand-list every alarm and had drifted badly out of date (it
still listed 3 alarms for the decommissioned `feeder` Lambda, and named the
DynamoDB alarms `vip-plans-table-*` after they'd been renamed/expanded to
`vip-ddb-{TableName}-*` across all 11 VIP tables). Rather than re-create a
list that will drift again, the source of truth is
`infra/scripts/create-alarms.sh` (this topic) and
`infra/scripts/create-progressive-dialer-alarms.sh` (`vip-progressive-dialer-alerts`
topic, 10 alarms) — every alarm name, threshold, and description lives there
as a comment next to the `aws cloudwatch put-metric-alarm` call that creates
it. To see the live set: `aws cloudwatch describe-alarms --query
"MetricAlarms[?contains(join(',',AlarmActions),'vip-admin-alerts')].AlarmName"`.

Categories covered: Lambda Errors/Throttles/Duration for every function in
this repo (including `campaign-exporter` and `location-onboarding-guard`),
DynamoDB SystemErrors/ThrottledRequests for all 11 VIP tables (Metric Math sum
across all 8 operations — a plain `TableName`-only dimension never receives
data), EventBridge FailedInvocations, app-level `VIPPlans` custom metrics
(`ScheduledRunFallback`, `CampaignDispatchStalled`, `NoActiveCampaign`), and
API Gateway 5xx on `vip-admin-ui-api` (catches application-level failures
that handlers convert into well-formed HTTP responses, which AWS/Lambda's own
Errors metric structurally cannot see).

### Re-create monitoring infrastructure (if ever destroyed)

```bash
# SNS topic
AWS_PROFILE=production aws sns create-topic \
  --name vip-admin-alerts \
  --region us-east-1

AWS_PROFILE=production aws sns subscribe \
  --topic-arn arn:aws:sns:us-east-1:165505826690:vip-admin-alerts \
  --protocol email \
  --notification-endpoint sebastian.valdenebro@medwork.io \
  --region us-east-1

# Alarms — run the script
bash /tmp/create-alarms.sh   # script lives at /tmp/create-alarms.sh in this repo at infra/scripts/create-alarms.sh

# Dashboard
AWS_PROFILE=production aws cloudwatch put-dashboard \
  --dashboard-name VipConnect-Admin-UI \
  --dashboard-body file:///tmp/dashboard.json \
  --region us-east-1
```

### Ad-hoc queries — CloudWatch Logs Insights

**All actions by a specific user in last 24h:**
```
fields @timestamp, event, campaign_id, segment_id, action
| filter actor_sub = "<cognito-sub>"
| sort @timestamp desc
| limit 100
```

**Failed campaign starts in last 7 days:**
```
fields @timestamp, campaign_id, error
| filter event = "campaign_start_failed"
| sort @timestamp desc
```

**api-plans tick errors in last hour:**
```
fields @timestamp, @message
| filter ispresent(error) and function = "tick"
| sort @timestamp desc
| limit 50
```

---

## 5. Incident response

### Severity classification

| Sev | Description | Example |
|---|---|---|
| 1 | Data breach / PHI exposure | Log contains unhashed phone number |
| 2 | Service down | UI unreachable, API 5xx > 50% |
| 3 | Degraded | Segment estimates slow, alarm firing |
| 4 | Cosmetic | UI warning, no functional impact |

### Response checklist (Sev 1 — PHI breach)

1. **Contain**: Disable all operator accounts: `admin-disable-user` for each
2. **Assess**: Pull CloudTrail events from last 30 days for affected resource
3. **Notify**: Privacy officer + CISO within 24h
4. **Document**: Incident ticket with timeline, affected records count, PHI types
5. **Remediate**: Fix root cause; rotate any exposed credentials
6. **Notify individuals + HHS**: Within 60 days if ≥ 500 records affected
7. **Post-mortem**: Within 14 days; document in `docs/incidents/YYYY-MM-DD.md`

### Response checklist (Sev 2 — service down)

1. Check AWS Health Dashboard for Cognito, API Gateway, Lambda, DynamoDB status
2. Look at last deploy — if recent, consider CDK rollback or Lambda function version rollback
3. Check KMS key health (all Lambdas depend on CMK for DynamoDB writes)
4. Escalate to AWS Support (Business/Enterprise) if no root cause in 30 min

---

## 6. Common operator questions (FAQ)

**Q: I created a segment but the count shows "0" — why?**
A: Segment has no matching profiles. Verify filters aren't too strict. Click "Refresh" (triggers `CreateSegmentEstimate`) and wait up to 90s for recomputation.

**Q: My campaign says "Running" but no calls are going out.**
A: Check (1) agents available in the queue, (2) segment has members, (3) schedule window is active now, (4) outbound country allowlist includes destination countries.

**Q: Can I edit a segment's filters after creating it?**
A: Not directly — Customer Profiles segments are immutable. The UI handles this by deleting + recreating. Campaigns using the old segment need to be re-linked.

**Q: How do I see who dialed a specific lead?**
A: Profiles page → search by phone → "Related campaigns" tab → click campaign → contact records.

**Q: A plan ran past its endTime — what happened?**
A: The loop `endTime` field is evaluated in COT (UTC-5, no DST). Confirm the configured `endTime` matches COT, not Eastern time. If the plan had no `loop.endTime`, it runs until all campaigns complete naturally.

**Q: Where are call recordings stored?**
A: In the Connect instance's configured S3 bucket (pre-existing, not managed by this project). Access via Connect Admin → Contact search.

---

## 7. Planned enhancements

- [ ] Custom dashboards per-campaign in Analytics screen
- [ ] Multi-role RBAC (admin vs. read-only analyst)
- [ ] AWS Macie scanning on snapshot bucket if > 10GB/month
- [ ] Automated quarterly user access review export
- [ ] Route 53 custom domain (`campaigns.vipmedical.internal`)
- [ ] Integration with Salesforce/HubSpot for bi-directional sync
