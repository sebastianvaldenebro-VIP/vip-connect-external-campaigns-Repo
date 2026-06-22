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

echo ""
echo "=== 5 progressive dialer alarms created ==="
