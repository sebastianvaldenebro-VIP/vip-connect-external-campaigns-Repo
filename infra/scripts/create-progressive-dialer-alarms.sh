#!/bin/bash
# Progressive Branded Dialer — CloudWatch alarms + SNS topic
# cfn-exec-role lacks cloudwatch:PutMetricAlarm, so these are created via CLI.
# Run once after ApiProgressiveDialerStack deploys.
set -e

export AWS_PROFILE=production
TOPIC="arn:aws:sns:us-east-1:165505826690:vip-progressive-dialer-alerts"
R="--region us-east-1"

alarm() {
  aws cloudwatch put-metric-alarm "$@" $R \
    --alarm-actions "$TOPIC" --ok-actions "$TOPIC" \
    --treat-missing-data "notBreaching"
  echo "OK: $2"
}

alarm \
  --alarm-name "vip-progressive-dialer-dlq-messages" \
  --alarm-description "CRITICAL: Messages in progressive dialer DLQ — dial failures exceeded maxReceiveCount" \
  --namespace "AWS/SQS" --metric-name "ApproximateNumberOfMessagesVisible" \
  --dimensions "Name=QueueName,Value=vip-progressive-dialer-calls-dlq" \
  --statistic "Maximum" --period 60 --threshold 1 \
  --comparison-operator "GreaterThanOrEqualToThreshold" \
  --evaluation-periods 1 --datapoints-to-alarm 1

alarm \
  --alarm-name "vip-progressive-dialer-consumer-errors" \
  --alarm-description "CRITICAL: Consumer Lambda errors — Kinesis records failing to process" \
  --namespace "AWS/Lambda" --metric-name "Errors" \
  --dimensions "Name=FunctionName,Value=vip-admin-progressive-dialer-consumer" \
  --statistic "Sum" --period 300 --threshold 1 \
  --comparison-operator "GreaterThanOrEqualToThreshold" \
  --evaluation-periods 1 --datapoints-to-alarm 1

alarm \
  --alarm-name "vip-progressive-dialer-caller-errors" \
  --alarm-description "WARNING: Caller Lambda errors — StartOutboundVoiceContact failures beyond retries" \
  --namespace "AWS/Lambda" --metric-name "Errors" \
  --dimensions "Name=FunctionName,Value=vip-admin-progressive-dialer-caller" \
  --statistic "Sum" --period 300 --threshold 5 \
  --comparison-operator "GreaterThanOrEqualToThreshold" \
  --evaluation-periods 1 --datapoints-to-alarm 1

alarm \
  --alarm-name "vip-progressive-dialer-connect-throttle" \
  --alarm-description "WARNING: Connect StartOutboundVoiceContact throttled — check dial concurrency" \
  --namespace "VipConnect/ProgressiveDialer" --metric-name "ConnectThrottleCount" \
  --statistic "Sum" --period 300 --threshold 10 \
  --comparison-operator "GreaterThanOrEqualToThreshold" \
  --evaluation-periods 1 --datapoints-to-alarm 1

alarm \
  --alarm-name "vip-progressive-dialer-firstorion-failures" \
  --alarm-description "CRITICAL: First Orion INFORM push failures — calls going out without branding" \
  --namespace "VipConnect/ProgressiveDialer" --metric-name "FirstOrionPushFailed" \
  --statistic "Sum" --period 300 --threshold 5 \
  --comparison-operator "GreaterThanOrEqualToThreshold" \
  --evaluation-periods 1 --datapoints-to-alarm 1

# ── Kickstart (DynamoDB Streams on VipProgressiveCampaignQueue) ──────────────
# Audit 2026-08-21: kickstart had ZERO alarm coverage of any kind. It failed
# 100% of invocations for 30h+ (missing shared layer — import requests via
# first_orion_client crashed every cold start) until the DynamoDB Streams
# event source mapping's retries exhausted the 24h stream retention window and
# permanently dropped the backlog of fast-agent-dispatch events. Layer fix is
# separate; these two alarms are what should have caught it in the first 5
# minutes instead of 30 hours.
alarm \
  --alarm-name "vip-progressive-dialer-kickstart-errors" \
  --alarm-description "CRITICAL: kickstart Lambda errors — fast-dispatch-to-available-agent on new queue item is failing (BD-013-shaped: import/runtime crash means the DynamoDB Streams backlog silently ages toward the 24h retention limit)" \
  --namespace "AWS/Lambda" --metric-name "Errors" \
  --dimensions "Name=FunctionName,Value=vip-admin-progressive-dialer-kickstart" \
  --statistic "Sum" --period 60 --threshold 0 \
  --comparison-operator "GreaterThanThreshold" \
  --evaluation-periods 1 --datapoints-to-alarm 1

# 1h threshold: well below the stream's 24h retention (leaves ~23h to intervene
# before events are permanently dropped), well above normal transient backlog
# during a traffic spike (avoids paging on noise).
alarm \
  --alarm-name "vip-progressive-dialer-kickstart-iterator-age" \
  --alarm-description "CRITICAL: kickstart is falling behind its DynamoDB Streams shard by 1h+ — the 24h stream retention window will start dropping events if this isn't fixed. Check for a crash-looping Lambda (import errors, missing layer) first." \
  --namespace "AWS/Lambda" --metric-name "IteratorAge" \
  --dimensions "Name=FunctionName,Value=vip-admin-progressive-dialer-kickstart" \
  --statistic "Maximum" --period 300 --threshold 3600000 \
  --comparison-operator "GreaterThanThreshold" \
  --evaluation-periods 2 --datapoints-to-alarm 2

# ── Branded campaign monitoring ───────────────────────────────────────────────
# Metrics emitted by vip-admin-branded-metrics-collector (EventBridge rate(1 minute)).
# Namespace: VipBrandedMonitor

alarm \
  --alarm-name "vip-branded-collector-errors" \
  --alarm-description "WARNING: branded-metrics-collector Lambda errors — disposition snapshots may be stale" \
  --namespace "AWS/Lambda" --metric-name "Errors" \
  --dimensions "Name=FunctionName,Value=vip-admin-branded-metrics-collector" \
  --statistic "Sum" --period 300 --threshold 0 \
  --comparison-operator "GreaterThanThreshold" \
  --evaluation-periods 2 --datapoints-to-alarm 2

# ActiveBrandedCampaigns=0 for 15 min during business hours (7am-7pm COT = 12:00-23:59 UTC).
# Metric only emitted during business hours; outside hours treat-missing-data=notBreaching keeps alarm silent.
aws cloudwatch put-metric-alarm \
  --alarm-name "vip-branded-no-active-campaigns-biz-hours" \
  --alarm-description "INFO: No active branded campaigns during business hours (7am-7pm COT). Expected on non-campaign days; investigate if campaigns were scheduled." \
  --namespace "VipBrandedMonitor" --metric-name "ActiveBrandedCampaigns" \
  --statistic "Maximum" --period 300 --threshold 0 \
  --comparison-operator "LessThanOrEqualToThreshold" \
  --evaluation-periods 3 --datapoints-to-alarm 3 \
  --treat-missing-data "notBreaching" \
  --alarm-actions "$TOPIC" --ok-actions "$TOPIC" \
  $R
echo "OK: vip-branded-no-active-campaigns-biz-hours"

# StuckBrandedCampaigns > 0 = items in VipActiveBrandedCampaigns older than 26h.
# Emitted every minute by the collector (not restricted to business hours).
# Root cause: _stop_branded_campaign was not called after campaign completion.
alarm \
  --alarm-name "vip-branded-stuck-campaigns" \
  --alarm-description "CRITICAL: Campaign(s) stuck >26h in VipActiveBrandedCampaigns — _stop_branded_campaign was not called. TTL may eventually clean up but run summary record is missing. Verify _force_finish_internal and other exit paths." \
  --namespace "VipBrandedMonitor" --metric-name "StuckBrandedCampaigns" \
  --statistic "Maximum" --period 300 --threshold 0 \
  --comparison-operator "GreaterThanThreshold" \
  --evaluation-periods 2 --datapoints-to-alarm 2

echo ""
echo "=== 10 progressive dialer + branded alarms created ==="
