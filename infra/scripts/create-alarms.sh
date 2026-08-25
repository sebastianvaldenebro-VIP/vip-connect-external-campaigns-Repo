#!/bin/bash
set -e

export AWS_PROFILE=production
TOPIC="arn:aws:sns:us-east-1:165505826690:vip-admin-alerts"
R="--region us-east-1"

alarm() {
  aws cloudwatch put-metric-alarm "$@" $R \
    --alarm-actions "$TOPIC" --ok-actions "$TOPIC" \
    --treat-missing-data "notBreaching"
  echo "OK: $1 ($2)"
}

# ── One-time cleanup of stale/decommissioned alarms ────────────────────────────
# Safe to re-run: delete-alarms on a name that doesn't exist is a no-op.
# - vip-feeder-*: the feeder Lambda was decommissioned before these alarms were
#   created (audit 2026-08-21) — they've been watching a function that no longer
#   exists, docs/runbook.md's troubleshooting section for it is likewise stale.
# - vip-plans-stuck-run: removed from this script months ago (StuckRun's own
#   definition — time since run START, not since last progress — makes it unfit
#   to alarm on directly; a genuinely healthy 12h+ run trips it daily). The LIVE
#   alarm was never actually deleted when the script changed — pure code/reality
#   drift, caught by the 2026-08-21 audit. Deleting it now for real.
aws cloudwatch delete-alarms $R --alarm-names \
  "vip-feeder-errors" \
  "vip-feeder-throttles" \
  "vip-feeder-duration-p99" \
  "vip-plans-stuck-run" \
  2>/dev/null || true
echo "Cleanup: removed vip-feeder-* and vip-plans-stuck-run if present"

# ── api-plans ─────────────────────────────────────────────────────────────────
alarm \
  --alarm-name "vip-plans-errors" \
  --alarm-description "CRITICAL: api-plans Lambda errors - plan tick or prestart_check failed" \
  --namespace "AWS/Lambda" --metric-name "Errors" \
  --dimensions "Name=FunctionName,Value=vip-admin-ui-api-plans" \
  --statistic "Sum" --period 60 --threshold 0 \
  --comparison-operator "GreaterThanThreshold" \
  --evaluation-periods 1 --datapoints-to-alarm 1

alarm \
  --alarm-name "vip-plans-throttles" \
  --alarm-description "CRITICAL: api-plans throttled - 5 reserved concurrency exhausted" \
  --namespace "AWS/Lambda" --metric-name "Throttles" \
  --dimensions "Name=FunctionName,Value=vip-admin-ui-api-plans" \
  --statistic "Sum" --period 60 --threshold 0 \
  --comparison-operator "GreaterThanThreshold" \
  --evaluation-periods 1 --datapoints-to-alarm 1

alarm \
  --alarm-name "vip-plans-duration-p99" \
  --alarm-description "WARNING: api-plans p99 duration > 4min (approaching 5min timeout)" \
  --namespace "AWS/Lambda" --metric-name "Duration" \
  --dimensions "Name=FunctionName,Value=vip-admin-ui-api-plans" \
  --extended-statistic "p99" --period 300 --threshold 240000 \
  --comparison-operator "GreaterThanThreshold" \
  --evaluation-periods 1 --datapoints-to-alarm 1

# ── campaign-exporter ─────────────────────────────────────────────────────────
# Audit 2026-08-21: this Lambda swallowed every exception into {"ok": false}
# instead of raising, so its own AWS/Lambda Errors metric stayed at 0 for 14+
# days of 100% export failures (wrong KMS key granted — fixed separately).
# The code fix (raise instead of return) now lets this standard alarm catch it.
alarm \
  --alarm-name "vip-campaign-exporter-errors" \
  --alarm-description "CRITICAL: campaign-exporter Lambda errors - daily Snowflake export (campaigns/branded/SMS) failed" \
  --namespace "AWS/Lambda" --metric-name "Errors" \
  --dimensions "Name=FunctionName,Value=vip-admin-ui-campaign-exporter" \
  --statistic "Sum" --period 300 --threshold 0 \
  --comparison-operator "GreaterThanThreshold" \
  --evaluation-periods 1 --datapoints-to-alarm 1

# ── location-onboarding-guard ────────────────────────────────────────────────
alarm \
  --alarm-name "vip-location-onboarding-guard-errors" \
  --alarm-description "CRITICAL: location onboarding guard Lambda errors - new-state phone alert may be silently failing" \
  --namespace "AWS/Lambda" --metric-name "Errors" \
  --dimensions "Name=FunctionName,Value=vip-location-onboarding-guard" \
  --statistic "Sum" --period 60 --threshold 0 \
  --comparison-operator "GreaterThanThreshold" \
  --evaluation-periods 1 --datapoints-to-alarm 1

# ── api-segments ──────────────────────────────────────────────────────────────
alarm \
  --alarm-name "vip-segments-errors" \
  --alarm-description "WARNING: api-segments Lambda errors - segment rebuild failed" \
  --namespace "AWS/Lambda" --metric-name "Errors" \
  --dimensions "Name=FunctionName,Value=vip-admin-ui-api-segments" \
  --statistic "Sum" --period 300 --threshold 0 \
  --comparison-operator "GreaterThanThreshold" \
  --evaluation-periods 2 --datapoints-to-alarm 2

alarm \
  --alarm-name "vip-segments-duration-p99" \
  --alarm-description "WARNING: api-segments p99 duration > 4min (approaching 5min timeout)" \
  --namespace "AWS/Lambda" --metric-name "Duration" \
  --dimensions "Name=FunctionName,Value=vip-admin-ui-api-segments" \
  --extended-statistic "p99" --period 300 --threshold 240000 \
  --comparison-operator "GreaterThanThreshold" \
  --evaluation-periods 1 --datapoints-to-alarm 1

# ── api-campaigns ─────────────────────────────────────────────────────────────
alarm \
  --alarm-name "vip-campaigns-errors" \
  --alarm-description "WARNING: api-campaigns Lambda errors - Connect campaign CRUD failed" \
  --namespace "AWS/Lambda" --metric-name "Errors" \
  --dimensions "Name=FunctionName,Value=vip-admin-ui-api-campaigns" \
  --statistic "Sum" --period 300 --threshold 0 \
  --comparison-operator "GreaterThanThreshold" \
  --evaluation-periods 2 --datapoints-to-alarm 2

# ── api-metrics / api-profiles (baja severidad) ───────────────────────────────
alarm \
  --alarm-name "vip-metrics-errors" \
  --alarm-description "INFO: api-metrics elevated error rate" \
  --namespace "AWS/Lambda" --metric-name "Errors" \
  --dimensions "Name=FunctionName,Value=vip-admin-ui-api-metrics" \
  --statistic "Sum" --period 300 --threshold 3 \
  --comparison-operator "GreaterThanThreshold" \
  --evaluation-periods 3 --datapoints-to-alarm 3

alarm \
  --alarm-name "vip-profiles-errors" \
  --alarm-description "INFO: api-profiles elevated error rate" \
  --namespace "AWS/Lambda" --metric-name "Errors" \
  --dimensions "Name=FunctionName,Value=vip-admin-ui-api-profiles" \
  --statistic "Sum" --period 300 --threshold 3 \
  --comparison-operator "GreaterThanThreshold" \
  --evaluation-periods 3 --datapoints-to-alarm 3

# ── DynamoDB — all VIP tables ──────────────────────────────────────────────────
# AWS/DynamoDB SystemErrors/ThrottledRequests are published ONLY per {TableName,
# Operation} — never {TableName} alone. A plain --dimensions Name=TableName
# alarm subscribes to a series that structurally never receives a datapoint.
# Audit 2026-08-21 confirmed this dropped a real error on 2026-08-07 for
# VipAdminPlans, and that 11 of the 12 VIP tables had ZERO DynamoDB alarm
# coverage at all.
#
# Fix: sum SystemErrors/ThrottledRequests across all 8 possible DynamoDB
# data-plane operations via Metric Math (FILL(...,0) so an operation with zero
# errors ever — and therefore no metric series yet — contributes 0 instead of
# breaking the sum). A SEARCH()-based expression would be simpler but CloudWatch
# rejects it for alarms outright ("SEARCH is not supported on Metric Alarms",
# confirmed live 2026-08-21) — this explicit per-operation sum is the actual
# supported way to alarm across every operation for a table.
# (vip-connect-deny-list excluded: tagged Project=vip-connect but managed
# manually by the separate connectcampaign_denylist_* Lambdas' team, not this
# repo — out of scope here.)
VIP_TABLES=(
  "VipAdminPlans"
  "VipActiveBrandedCampaigns"
  "VipAdminSegmentFilterConfig"
  "VipAgentSnapshot"
  "VipBrandedCampaignMetrics"
  "VipBrandedRunSummary"
  "VipLocationMapping"
  "VipProgressiveAgentLocks"
  "VipProgressiveCampaignQueue"
  "VipSmsCampaignQueue"
  "VipSmsCampaignRuns"
)
DDB_OPERATIONS=(GetItem PutItem Query Scan UpdateItem DeleteItem BatchGetItem BatchWriteItem)

alarm_ddb_all_ops() {
  local table="$1" metric="$2" alarm_name="$3" description="$4"
  local metrics_file
  metrics_file=$(mktemp)
  DDB_TABLE="$table" DDB_METRIC="$metric" python3 - "$metrics_file" "${DDB_OPERATIONS[@]}" <<'PYEOF'
import json, os, sys

metrics_file = sys.argv[1]
ops = sys.argv[2:]
table = os.environ["DDB_TABLE"]
metric = os.environ["DDB_METRIC"]

entries = []
ids = []
for i, op in enumerate(ops, start=1):
    mid = f"m{i}"
    ids.append(mid)
    entries.append({
        "Id": mid,
        "MetricStat": {
            "Metric": {
                "Namespace": "AWS/DynamoDB",
                "MetricName": metric,
                "Dimensions": [
                    {"Name": "TableName", "Value": table},
                    {"Name": "Operation", "Value": op},
                ],
            },
            "Period": 300,
            "Stat": "Sum",
        },
        "ReturnData": False,
    })
expr = "+".join(f"FILL({i},0)" for i in ids)
entries.append({
    "Id": "e1",
    "Expression": expr,
    "Label": f"{table} {metric} (all operations)",
    "ReturnData": True,
})
with open(metrics_file, "w") as f:
    json.dump(entries, f)
PYEOF
  aws cloudwatch put-metric-alarm $R \
    --alarm-name "$alarm_name" \
    --alarm-description "$description" \
    --metrics "file://$metrics_file" \
    --threshold 0 \
    --comparison-operator "GreaterThanThreshold" \
    --evaluation-periods 1 --datapoints-to-alarm 1 \
    --alarm-actions "$TOPIC" --ok-actions "$TOPIC" \
    --treat-missing-data "notBreaching"
  rm -f "$metrics_file"
  echo "OK: $alarm_name ($table $metric, all 8 operations)"
}

for table in "${VIP_TABLES[@]}"; do
  alarm_ddb_all_ops "$table" "SystemErrors" "vip-ddb-${table}-system-errors" \
    "CRITICAL: ${table} DynamoDB system errors (all operations) - table availability issue"
  alarm_ddb_all_ops "$table" "ThrottledRequests" "vip-ddb-${table}-throttles" \
    "WARNING: ${table} DynamoDB throttled requests (all operations)"
done

# ── EventBridge ───────────────────────────────────────────────────────────────
alarm \
  --alarm-name "vip-prestart-check-failed-invocations" \
  --alarm-description "WARNING: vip-plans-prestart-check failed to invoke api-plans Lambda" \
  --namespace "AWS/Events" --metric-name "FailedInvocations" \
  --dimensions "Name=RuleName,Value=vip-plans-prestart-check" \
  --statistic "Sum" --period 300 --threshold 0 \
  --comparison-operator "GreaterThanThreshold" \
  --evaluation-periods 1 --datapoints-to-alarm 1

# Fires when prestart_check had to manually trigger scheduled_run() because a vip-sched-* rule
# missed its Lambda invocation (resource policy statement wiped by a CDK deploy).
# Metric is emitted WITHOUT dimensions (aggregate) so this CLI alarm can catch it.
# The per-PlanId dimension is also emitted for drill-down in CloudWatch Metrics.
alarm \
  --alarm-name "vip-plans-scheduled-run-fallback" \
  --alarm-description "CRITICAL: vip-sched-* rule missed Lambda — prestart_check self-healed. Root cause: CDK deploy wiped resource policy. Fix: re-save plan settings to restore add_permission." \
  --namespace "VIPPlans" --metric-name "ScheduledRunFallback" \
  --statistic "Sum" --period 300 --threshold 0 \
  --comparison-operator "GreaterThanThreshold" \
  --evaluation-periods 1 --datapoints-to-alarm 1

# Fires when a campaign keeps reverting to "queued" instead of advancing (Redis
# mid-rebuild, empty segment) for 5 CONSECUTIVE 1-minute ticks — i.e. stalled ~5min+,
# not a normal transient blip (the fixed-point loop already tolerates single-tick
# reverts by design, see _dispatch_ready_campaigns's `stalled` set). A brief Redis
# rebuild self-heals in 1-2 ticks and should NOT page; this only fires when it doesn't.
# Metric emitted WITHOUT dimensions (aggregate) so this CLI alarm can catch it.
# The per-CampaignId dimension is also emitted for drill-down in CloudWatch Metrics.
alarm \
  --alarm-name "vip-plans-campaign-dispatch-stalled" \
  --alarm-description "WARNING: a campaign has been stalled (reverting to queued, not advancing) for 5+ consecutive minutes — check Redis lead-list rebuild status or _create_segment errors." \
  --namespace "VIPPlans" --metric-name "CampaignDispatchStalled" \
  --statistic "Sum" --period 60 --threshold 0 \
  --comparison-operator "GreaterThanThreshold" \
  --evaluation-periods 5 --datapoints-to-alarm 5

# NOT WIRED UP — StuckRun's own definition is unfit to alarm on directly.
# _STUCK_RUN_HOURS measures time since the RUN STARTED, not time since it
# last made progress. This plan's own repeat/multi-bucket design legitimately
# runs 12+ hours a day (verified live 2026-08-21: a genuinely healthy run,
# actively advancing bucket-by-bucket, tripped this within minutes of the
# alarm being created, at 27.5h total age). Alarming on it as-is would page
# on every normal long-running plan, every day — worse than no alarm at all.
# Fixing this needs the metric itself redefined (e.g. time since the CURRENT
# bucket's own startedAt, not the run's) — not done in this pass. The
# aggregate emission added for BD-013 is left in code (harmless, useful for
# manual CloudWatch Metrics inspection) but no alarm watches it. (The cleanup
# block at the top of this script deletes the live vip-plans-stuck-run alarm
# that drifted out of sync with this comment — audit 2026-08-21.)

# Fires when a "running" bucket has gone _NO_ACTIVE_CAMPAIGN_MINUTES (5) with
# zero campaigns in creating/warming/running status — the tick that should
# have created the bucket's campaigns crashed before any campaign existed
# (BD-013's exact shape; CampaignDispatchStalled above only catches a campaign
# that ALREADY existed reverting to queued, not this "never even started" case).
# The 5-minute debounce is already inside prestart_check's own emission
# condition, so 1 datapoint here is enough — no additional evaluation-period
# debounce needed on top of it.
alarm \
  --alarm-name "vip-plans-no-active-campaign" \
  --alarm-description "CRITICAL: a plan run has had no active campaign for 5+ minutes — the tick that should have created this bucket's campaigns likely crashed silently. Check CloudWatch Logs Insights for 'tick_unhandled_error' or 'no active campaign for' with this plan's id." \
  --namespace "VIPPlans" --metric-name "NoActiveCampaign" \
  --statistic "Sum" --period 60 --threshold 0 \
  --comparison-operator "GreaterThanThreshold" \
  --evaluation-periods 1 --datapoints-to-alarm 1

# Fires when pre-warm attempts keep failing for 5 CONSECUTIVE minutes — same
# debounce, same reasoning as CampaignDispatchStalled above. A single failed
# attempt is routine, not a page: _prestart_plan's own retry-merge logic
# already retries missing campaigns on every subsequent tick within the 4-6
# min pre-warm window, and the most common cause (_RedisRebuildingError) is
# a brief, expected blip that normally clears within 1 tick. Confirmed live
# 2026-08-25: a real _RedisRebuildingError on 2 campaigns paged immediately
# under the original evaluation-periods=1, then fully self-healed one minute
# later on the very next tick (failed: 2 -> failed: 0) — a false-positive page
# for a non-incident. _emit_prewarm_failure() in executor.py emits this on all
# 4 failure sites. Metric emitted WITHOUT dimensions (aggregate) so this CLI
# alarm can catch it; per-PlanId dimension also emitted for drill-down.
alarm \
  --alarm-name "vip-plans-prewarm-failure" \
  --alarm-description "WARNING: pre-warm has failed for 5+ consecutive minutes — a downstream plan may start cold instead of already-warmed. Check CloudWatch Logs Insights for 'prestart_plan_campaign_failed' or '_prestart_after_campaign' errors with this plan's id. If the error is _RedisRebuildingError, check ElastiCache Redis replication group status." \
  --namespace "VIPPlans" --metric-name "PrewarmFailure" \
  --statistic "Sum" --period 60 --threshold 0 \
  --comparison-operator "GreaterThanThreshold" \
  --evaluation-periods 5 --datapoints-to-alarm 5

# Per-plan vip-sched-* FailedInvocations alarms cannot be created here because rule names
# are dynamic (created at runtime by upsert_schedule). Options:
#   a) Console: Metric Math alarm with SEARCH('{AWS/Events,RuleName} vip-sched FailedInvocations')
#   b) CLI per plan (replace PLAN_ID with the actual plan's hashed rule name):
#      aws cloudwatch put-metric-alarm --alarm-name "vip-sched-PLAN_ID-failed-invocations" \
#        --namespace "AWS/Events" --metric-name "FailedInvocations" \
#        --dimensions "Name=RuleName,Value=vip-sched-PLAN_ID" \
#        --statistic Sum --period 300 --threshold 0 \
#        --comparison-operator GreaterThanThreshold --evaluation-periods 1 \
#        --alarm-actions $TOPIC $R

# ── API Gateway — vip-admin-ui-api (ApiId me1idvufo6) ─────────────────────────
# Audit 2026-08-21: api-campaigns/api-metrics (and every other HTTP handler in
# this repo) deliberately convert application-level failures (ServiceQuotaExceeded,
# validation errors, DynamoDB throttles) into well-formed 4xx/5xx HTTP responses —
# that's correct behavior for an HTTP API, but it means AWS/Lambda's own Errors
# metric can NEVER see them (Lambda's own invocation completed successfully from
# its point of view). No alarm anywhere in this account was watching the actual
# HTTP-level error rate. This is the correct backstop: it sees every status code
# regardless of how gracefully the Lambda handled the underlying failure.
alarm \
  --alarm-name "vip-admin-ui-api-5xx" \
  --alarm-description "WARNING: vip-admin-ui-api (api-campaigns/api-metrics/api-plans/api-segments/api-profiles) returning 5xx — catches application-level failures that AWS/Lambda Errors can't see because handlers convert them to well-formed error responses" \
  --namespace "AWS/ApiGateway" --metric-name "5xx" \
  --dimensions "Name=ApiId,Value=me1idvufo6" \
  --statistic "Sum" --period 300 --threshold 0 \
  --comparison-operator "GreaterThanThreshold" \
  --evaluation-periods 2 --datapoints-to-alarm 2

echo ""
echo "=== alarmas creadas/actualizadas (ver conteo real con describe-alarms) ==="

# ── Progressive Branded Dialer SNS topic (manual pre-req) ─────────────────────
# The CDK stack imports this topic by ARN (SNS:GetTopicAttributes is outside the
# CFN exec role boundary). Create once before deploying ApiProgressiveDialerStack.
# Uncomment and run when deploying the stack for the first time:
#
# aws sns create-topic \
#   --name vip-progressive-dialer-alerts \
#   --region us-east-1 \
#   --profile production
#
# Then subscribe team email:
# aws sns subscribe \
#   --topic-arn arn:aws:sns:us-east-1:165505826690:vip-progressive-dialer-alerts \
#   --protocol email \
#   --notification-endpoint ops@medwork.io \
#   --region us-east-1 \
#   --profile production
