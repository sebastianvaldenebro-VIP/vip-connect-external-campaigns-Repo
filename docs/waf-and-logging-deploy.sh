#!/usr/bin/env bash
# Findings #002 (WAF) + #005 (API Gateway access logging).
# cfn-exec-role lacks wafv2:CreateWebACL and logs:CreateLogDelivery,
# so both are deployed here via CLI with the production SSO role.
# Run with: AWS_PROFILE=production bash docs/waf-and-logging-deploy.sh
set -euo pipefail

REGION=us-east-1
ACCOUNT=165505826690
API_NAME="vip-admin-ui-api"
LOG_GROUP="/aws/apigateway/vip-admin-ui-api/access"
KMS_ALIAS="alias/prod/vip-admin-ui/api-access-logs"

step() { echo -e "\n═══ $* ═══"; }

# ── Resolve API ID ────────────────────────────────────────────────────
step "Resolving API Gateway ID"
API_ID=$(aws apigatewayv2 get-apis --region $REGION \
  --query "Items[?Name=='$API_NAME'].ApiId" --output text)
[ -z "$API_ID" ] && { echo "ERROR: API '$API_NAME' not found"; exit 1; }
echo "API ID: $API_ID"
CF_DIST_ID="E3QCDJPG0LCO7E"
CF_ARN="arn:aws:cloudfront::${ACCOUNT}:distribution/${CF_DIST_ID}"

# ── #005: KMS key for log group encryption ────────────────────────────
step "#005 — KMS key for access logs"
EXISTING_KEY=$(aws kms list-aliases --region $REGION \
  --query "Aliases[?AliasName=='$KMS_ALIAS'].TargetKeyId" --output text)

if [ -n "$EXISTING_KEY" ] && [ "$EXISTING_KEY" != "None" ]; then
  KMS_KEY_ID="$EXISTING_KEY"
  echo "Using existing KMS key: $KMS_KEY_ID"
else
  KMS_KEY_ID=$(aws kms create-key \
    --region $REGION \
    --description "KMS CMK for API Gateway HTTP API access logs" \
    --tags TagKey=Project,TagValue=vip-connect-admin-ui TagKey=Compliance,TagValue=hipaa \
    --query 'KeyMetadata.KeyId' --output text)
  aws kms enable-key-rotation --key-id $KMS_KEY_ID --region $REGION
  aws kms create-alias --alias-name $KMS_ALIAS --target-key-id $KMS_KEY_ID --region $REGION
  echo "Created KMS key: $KMS_KEY_ID"

  # Allow CloudWatch Logs service to use this key
  CURRENT_POLICY=$(aws kms get-key-policy --key-id $KMS_KEY_ID --policy-name default --region $REGION --query Policy --output text)
  LOGS_STMT=$(cat <<EOF
{
  "Sid": "AllowCloudWatchLogs",
  "Effect": "Allow",
  "Principal": {"Service": "logs.${REGION}.amazonaws.com"},
  "Action": ["kms:Encrypt*","kms:Decrypt*","kms:ReEncrypt*","kms:GenerateDataKey*","kms:Describe*"],
  "Resource": "*",
  "Condition": {"ArnLike": {"kms:EncryptionContext:aws:logs:arn": "arn:aws:logs:${REGION}:${ACCOUNT}:*"}}
}
EOF
)
  UPDATED_POLICY=$(echo "$CURRENT_POLICY" | python3 -c "
import sys, json
pol = json.load(sys.stdin)
new_stmt = json.loads(sys.argv[1])
pol['Statement'].append(new_stmt)
print(json.dumps(pol))
" "$LOGS_STMT")
  aws kms put-key-policy --key-id $KMS_KEY_ID --policy-name default \
    --policy "$UPDATED_POLICY" --region $REGION
fi
KMS_KEY_ARN=$(aws kms describe-key --key-id $KMS_KEY_ID --region $REGION \
  --query 'KeyMetadata.Arn' --output text)

# ── #005: CloudWatch Log Group ────────────────────────────────────────
step "#005 — CloudWatch Log Group"
if aws logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" --region $REGION \
    --query "logGroups[?logGroupName=='$LOG_GROUP']" --output text | grep -q .; then
  echo "Log group already exists"
else
  aws logs create-log-group --log-group-name "$LOG_GROUP" \
    --kms-key-id "$KMS_KEY_ARN" --region $REGION
  echo "Created log group $LOG_GROUP"
fi
aws logs put-retention-policy --log-group-name "$LOG_GROUP" \
  --retention-in-days 365 --region $REGION

LOG_GROUP_ARN=$(aws logs describe-log-groups \
  --log-group-name-prefix "$LOG_GROUP" --region $REGION \
  --query "logGroups[?logGroupName=='$LOG_GROUP'].arn" --output text)
# Remove trailing :* if present
LOG_GROUP_ARN="${LOG_GROUP_ARN%:*}"
echo "Log group ARN: $LOG_GROUP_ARN"

# ── #005: Enable access logging on $default stage ─────────────────────
step "#005 — Enable access logging on API Gateway \$default stage"
# Build JSON payload via Python to avoid shell quoting issues with the format string
API_ID_VAL="$API_ID" LOG_ARN_VAL="$LOG_GROUP_ARN" python3 - << 'PYEOF'
import json, os, subprocess, sys

payload = {
    "ApiId": os.environ["API_ID_VAL"],
    "StageName": "$default",
    "AccessLogSettings": {
        "DestinationArn": os.environ["LOG_ARN_VAL"],
        "Format": json.dumps({
            "requestId": "$context.requestId",
            "ip": "$context.identity.sourceIp",
            "requestTime": "$context.requestTime",
            "httpMethod": "$context.httpMethod",
            "routeKey": "$context.routeKey",
            "status": "$context.status",
            "protocol": "$context.protocol",
            "responseLength": "$context.responseLength",
            "integrationLatency": "$context.integrationLatency",
            "integrationStatus": "$context.integrationStatus",
            "domainName": "$context.domainName",
        })
    }
}
with open("/tmp/apigw-update-stage.json", "w") as f:
    json.dump(payload, f)
PYEOF
aws apigatewayv2 update-stage \
  --cli-input-json file:///tmp/apigw-update-stage.json \
  --region $REGION >/dev/null
echo "Access logging enabled → $LOG_GROUP"

# ── #002: WAFv2 WebACL (CLOUDFRONT scope) ────────────────────────────
# NOTE: WAFv2 AssociateWebACL rejects HTTP API v2 $default stage ARNs (WAFInvalidParameterException)
# from both the CLI and CloudFormation — the WAFv2 ARN validator rejects the '$' character in
# stage names. WAF is therefore applied at the CloudFront layer (CLOUDFRONT scope). Since all
# production traffic routes through CloudFront (dprtjww5c9892.cloudfront.net), this provides
# equivalent protection. The orphaned REGIONAL WebACL from previous attempts can be deleted.
step "#002 — WAFv2 WebACL (CLOUDFRONT scope for distribution ${CF_DIST_ID})"
EXISTING_CF_WAF=$(aws wafv2 list-web-acls --scope CLOUDFRONT --region $REGION \
  --query "WebACLs[?Name=='vip-admin-ui-cf-waf'].ARN" --output text)

if [ -n "$EXISTING_CF_WAF" ] && [ "$EXISTING_CF_WAF" != "None" ]; then
  echo "CLOUDFRONT WAF WebACL already exists: $EXISTING_CF_WAF"
  CF_WAF_ARN="$EXISTING_CF_WAF"
else
  CF_WAF_ARN=$(aws wafv2 create-web-acl \
    --name vip-admin-ui-cf-waf \
    --scope CLOUDFRONT \
    --region $REGION \
    --default-action Allow={} \
    --visibility-config SampledRequestsEnabled=true,CloudWatchMetricsEnabled=true,MetricName=vip-admin-ui-cf-waf \
    --rules '[
      {"Name":"AWSManagedRulesCommonRuleSet","Priority":1,"Statement":{"ManagedRuleGroupStatement":{"VendorName":"AWS","Name":"AWSManagedRulesCommonRuleSet"}},"OverrideAction":{"None":{}},"VisibilityConfig":{"SampledRequestsEnabled":true,"CloudWatchMetricsEnabled":true,"MetricName":"AWSManagedRulesCommonRuleSet"}},
      {"Name":"AWSManagedRulesKnownBadInputsRuleSet","Priority":2,"Statement":{"ManagedRuleGroupStatement":{"VendorName":"AWS","Name":"AWSManagedRulesKnownBadInputsRuleSet"}},"OverrideAction":{"None":{}},"VisibilityConfig":{"SampledRequestsEnabled":true,"CloudWatchMetricsEnabled":true,"MetricName":"AWSManagedRulesKnownBadInputsRuleSet"}},
      {"Name":"RateLimitPerIP","Priority":3,"Statement":{"RateBasedStatement":{"Limit":300,"AggregateKeyType":"IP"}},"Action":{"Block":{}},"VisibilityConfig":{"SampledRequestsEnabled":true,"CloudWatchMetricsEnabled":true,"MetricName":"RateLimitPerIP"}}
    ]' \
    --query 'Summary.ARN' --output text)
  echo "Created CLOUDFRONT WAF WebACL: $CF_WAF_ARN"
fi

# ── #002: Associate WAF with CloudFront via update-distribution ───────
# CloudFront WAF (CLOUDFRONT scope) must be set via cloudfront update-distribution,
# not via wafv2 associate-web-acl (which only supports REGIONAL resources).
step "#002 — Attach WAF to CloudFront distribution ${CF_DIST_ID}"
DIST_JSON=$(aws cloudfront get-distribution-config --id "$CF_DIST_ID")
ETAG=$(echo "$DIST_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['ETag'])")

echo "$DIST_JSON" | CF_WAF_ARN_VAL="$CF_WAF_ARN" python3 -c "
import json, sys, os
data = json.load(sys.stdin)
config = data['DistributionConfig']
config['WebACLId'] = os.environ['CF_WAF_ARN_VAL']
print(json.dumps(config))
" > /tmp/cf-dist-config.json

aws cloudfront update-distribution \
  --id "$CF_DIST_ID" \
  --distribution-config file:///tmp/cf-dist-config.json \
  --if-match "$ETAG" \
  --query 'Distribution.Status' --output text
echo "WAF attached to CloudFront ${CF_DIST_ID} (deployment ~5 min)"

# ── Verify ────────────────────────────────────────────────────────────
step "Verification"
echo "Access logging (API Gateway):"
aws apigatewayv2 get-stage --api-id "$API_ID" --stage-name '$default' --region $REGION \
  --query 'AccessLogSettings'
echo ""
echo "WAF (CloudFront distribution):"
aws cloudfront get-distribution --id "$CF_DIST_ID" \
  --query 'Distribution.DistributionConfig.WebACLId' --output text

echo ""
echo "✅ Done."
echo "   #005 — API Gateway access logging: ACTIVE"
echo "   #002 — WAF on CloudFront ${CF_DIST_ID}: ACTIVE (deploying)"
echo "   Note: REGIONAL WebACL 'vip-admin-ui-api-waf' (unused) can be deleted manually."
