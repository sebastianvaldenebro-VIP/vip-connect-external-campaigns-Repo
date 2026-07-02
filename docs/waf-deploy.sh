#!/usr/bin/env bash
# Finding #002 — WAFv2 WebACL for API Gateway HTTP API.
# The cfn-exec-role lacks wafv2:CreateWebACL so this runs outside CDK.
# Run once with AWS_PROFILE=production.
set -euo pipefail

REGION=us-east-1
API_ID="${API_ID:-}"  # set via: API_ID=$(aws apigatewayv2 get-apis --region us-east-1 --query "Items[?Name=='vip-admin-ui-api'].ApiId" --output text)

if [ -z "$API_ID" ]; then
  API_ID=$(aws apigatewayv2 get-apis --region $REGION \
    --query "Items[?Name=='vip-admin-ui-api'].ApiId" --output text)
fi
echo "API ID: $API_ID"
STAGE_ARN="arn:aws:apigateway:${REGION}::/apis/${API_ID}/stages/\$default"

# 1. Create WebACL (idempotent — skip if already exists)
EXISTING=$(aws wafv2 list-web-acls --scope REGIONAL --region $REGION \
  --query "WebACLs[?Name=='vip-admin-ui-api-waf'].ARN" --output text)

if [ -n "$EXISTING" ] && [ "$EXISTING" != "None" ]; then
  echo "WebACL already exists: $EXISTING"
  WAF_ARN="$EXISTING"
else
  WAF_ARN=$(aws wafv2 create-web-acl \
    --name vip-admin-ui-api-waf \
    --scope REGIONAL \
    --region $REGION \
    --default-action Allow={} \
    --visibility-config SampledRequestsEnabled=true,CloudWatchMetricsEnabled=true,MetricName=vip-admin-ui-api-waf \
    --rules '[
      {"Name":"AWSManagedRulesCommonRuleSet","Priority":1,"Statement":{"ManagedRuleGroupStatement":{"VendorName":"AWS","Name":"AWSManagedRulesCommonRuleSet"}},"OverrideAction":{"None":{}},"VisibilityConfig":{"SampledRequestsEnabled":true,"CloudWatchMetricsEnabled":true,"MetricName":"AWSManagedRulesCommonRuleSet"}},
      {"Name":"AWSManagedRulesKnownBadInputsRuleSet","Priority":2,"Statement":{"ManagedRuleGroupStatement":{"VendorName":"AWS","Name":"AWSManagedRulesKnownBadInputsRuleSet"}},"OverrideAction":{"None":{}},"VisibilityConfig":{"SampledRequestsEnabled":true,"CloudWatchMetricsEnabled":true,"MetricName":"AWSManagedRulesKnownBadInputsRuleSet"}},
      {"Name":"RateLimitPerIP","Priority":3,"Statement":{"RateBasedStatement":{"Limit":300,"AggregateKeyType":"IP"}},"Action":{"Block":{}},"VisibilityConfig":{"SampledRequestsEnabled":true,"CloudWatchMetricsEnabled":true,"MetricName":"RateLimitPerIP"}}
    ]' \
    --query 'Summary.ARN' --output text)
  echo "Created WebACL: $WAF_ARN"
fi

# 2. Associate with API Gateway $default stage
aws wafv2 associate-web-acl \
  --web-acl-arn "$WAF_ARN" \
  --resource-arn "$STAGE_ARN" \
  --region $REGION
echo "Associated WAF $WAF_ARN → $STAGE_ARN"

echo ""
echo "Done. Verify:"
echo "  aws wafv2 get-web-acl-for-resource --resource-arn '$STAGE_ARN' --region $REGION"
