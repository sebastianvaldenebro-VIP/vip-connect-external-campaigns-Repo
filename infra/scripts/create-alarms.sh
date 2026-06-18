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

# ── feeder ────────────────────────────────────────────────────────────────────
alarm \
  --alarm-name "vip-feeder-errors" \
  --alarm-description "CRITICAL: feeder Lambda errors - lead push to Connect failed" \
  --namespace "AWS/Lambda" --metric-name "Errors" \
  --dimensions "Name=FunctionName,Value=vip-external-campaigns-feeder" \
  --statistic "Sum" --period 60 --threshold 0 \
  --comparison-operator "GreaterThanThreshold" \
  --evaluation-periods 1 --datapoints-to-alarm 1

alarm \
  --alarm-name "vip-feeder-throttles" \
  --alarm-description "CRITICAL: feeder throttled - 1 reserved concurrency, no data flowing" \
  --namespace "AWS/Lambda" --metric-name "Throttles" \
  --dimensions "Name=FunctionName,Value=vip-external-campaigns-feeder" \
  --statistic "Sum" --period 60 --threshold 0 \
  --comparison-operator "GreaterThanThreshold" \
  --evaluation-periods 1 --datapoints-to-alarm 1

alarm \
  --alarm-name "vip-feeder-duration-p99" \
  --alarm-description "WARNING: feeder p99 duration > 4min (approaching 5min timeout)" \
  --namespace "AWS/Lambda" --metric-name "Duration" \
  --dimensions "Name=FunctionName,Value=vip-external-campaigns-feeder" \
  --extended-statistic "p99" --period 300 --threshold 240000 \
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

# ── DynamoDB VipAdminPlans ────────────────────────────────────────────────────
alarm \
  --alarm-name "vip-plans-table-system-errors" \
  --alarm-description "CRITICAL: VipAdminPlans DynamoDB system errors - table availability issue" \
  --namespace "AWS/DynamoDB" --metric-name "SystemErrors" \
  --dimensions "Name=TableName,Value=VipAdminPlans" \
  --statistic "Sum" --period 60 --threshold 0 \
  --comparison-operator "GreaterThanThreshold" \
  --evaluation-periods 1 --datapoints-to-alarm 1

alarm \
  --alarm-name "vip-plans-table-throttles" \
  --alarm-description "WARNING: VipAdminPlans DynamoDB throttled requests" \
  --namespace "AWS/DynamoDB" --metric-name "ThrottledRequests" \
  --dimensions "Name=TableName,Value=VipAdminPlans" \
  --statistic "Sum" --period 60 --threshold 0 \
  --comparison-operator "GreaterThanThreshold" \
  --evaluation-periods 1 --datapoints-to-alarm 1

# ── EventBridge ───────────────────────────────────────────────────────────────
alarm \
  --alarm-name "vip-prestart-check-failed-invocations" \
  --alarm-description "WARNING: vip-plans-prestart-check failed to invoke api-plans Lambda" \
  --namespace "AWS/Events" --metric-name "FailedInvocations" \
  --dimensions "Name=RuleName,Value=vip-plans-prestart-check" \
  --statistic "Sum" --period 300 --threshold 0 \
  --comparison-operator "GreaterThanThreshold" \
  --evaluation-periods 1 --datapoints-to-alarm 1

echo ""
echo "=== 14 alarmas creadas ==="

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
