#!/usr/bin/env bash
# Deploy del feeder sin CDK — todo vía AWS CLI directo.
# Prereqs: AWS_PROFILE=production seteado, /tmp/vip-feeder.zip existente.
set -euo pipefail

# ═══════════════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════════════
ACCOUNT=165505826690
REGION=us-east-1
BOUNDARY_ARN="arn:aws:iam::${ACCOUNT}:policy/EngineeringPermissionBoundary"

VPC_ID=vpc-0d32b420acc84d370
SUBNET_A=subnet-06c7669b5e3e0e814
SUBNET_B=subnet-088367ac9fc0a2fec
SG_ID=sg-01d54d29c2a4785f1

REDIS_HOST="master.prod-medwork-api.jrdc0s.use1.cache.amazonaws.com"
REDIS_PORT=6379
TEAM="BASIC_TEAM"
CONNECT_INSTANCE_ID="6b3f17ba-68a4-472a-9b20-db1991507009"

KMS_ALIAS="alias/prod/external-campaigns/data"
FILTERS_TABLE="ExternalCampaignFilters"
TRACKING_TABLE="ExternalCampaignDialTracking"
AUDIT_TABLE="ExternalCampaignAudit"
LAMBDA_NAME="vip-external-campaigns-feeder"
ROLE_NAME="vip-external-campaigns-feeder-role"
RULE_NAME="vip-external-campaigns-feeder-schedule"
LOG_GROUP="/aws/lambda/${LAMBDA_NAME}"
METRICS_NAMESPACE="VipConnect/ExternalCampaigns"

LAMBDA_ZIP="${LAMBDA_ZIP:-/tmp/vip-feeder.zip}"

TAGS="Environment=prod Project=vip-connect-external-campaigns Owner=devaju CostCenter=vip-connect Compliance=hipaa ManagedBy=cli"

aws_="aws --region $REGION"

step() { echo -e "\n═══ $* ═══"; }

# ═══════════════════════════════════════════════════════════════════════
# 1. KMS CMK
# ═══════════════════════════════════════════════════════════════════════
step "1. Creating KMS CMK"
KMS_KEY_ID=$($aws_ kms describe-key --key-id "$KMS_ALIAS" --query 'KeyMetadata.KeyId' --output text 2>/dev/null || true)
if [ -z "$KMS_KEY_ID" ] || [ "$KMS_KEY_ID" = "None" ]; then
  KMS_KEY_ID=$($aws_ kms create-key \
    --description "VIP external campaigns data at rest" \
    --tags TagKey=Project,TagValue=vip-connect-external-campaigns TagKey=Compliance,TagValue=hipaa \
    --query 'KeyMetadata.KeyId' --output text)
  $aws_ kms enable-key-rotation --key-id "$KMS_KEY_ID"
  $aws_ kms create-alias --alias-name "$KMS_ALIAS" --target-key-id "$KMS_KEY_ID"
  echo "Created KMS key: $KMS_KEY_ID (alias $KMS_ALIAS)"
else
  echo "Using existing KMS key: $KMS_KEY_ID"
fi
KMS_KEY_ARN=$($aws_ kms describe-key --key-id "$KMS_KEY_ID" --query 'KeyMetadata.Arn' --output text)

# ═══════════════════════════════════════════════════════════════════════
# 2. DynamoDB tables
# ═══════════════════════════════════════════════════════════════════════
create_table() {
  local name=$1 schema=$2
  if $aws_ dynamodb describe-table --table-name "$name" >/dev/null 2>&1; then
    echo "Table $name already exists — skipping"
    return
  fi
  echo "Creating table $name"
  eval "$aws_ dynamodb create-table --table-name \"$name\" $schema \
    --billing-mode PAY_PER_REQUEST \
    --sse-specification Enabled=true,SSEType=KMS,KMSMasterKeyId=$KMS_KEY_ARN \
    --deletion-protection-enabled >/dev/null"
  $aws_ dynamodb wait table-exists --table-name "$name"
  $aws_ dynamodb update-continuous-backups --table-name "$name" \
    --point-in-time-recovery-specification PointInTimeRecoveryEnabled=true >/dev/null
}

step "2a. Table: $FILTERS_TABLE"
create_table "$FILTERS_TABLE" \
  "--attribute-definitions AttributeName=campaign_id,AttributeType=S \
   --key-schema AttributeName=campaign_id,KeyType=HASH"

step "2b. Table: $TRACKING_TABLE (with GSI + TTL)"
create_table "$TRACKING_TABLE" \
  "--attribute-definitions \
      AttributeName=lead_id,AttributeType=S \
      AttributeName=campaign_id_pushed_at,AttributeType=S \
      AttributeName=campaign_id,AttributeType=S \
      AttributeName=retry_scheduled_at,AttributeType=S \
   --key-schema AttributeName=lead_id,KeyType=HASH AttributeName=campaign_id_pushed_at,KeyType=RANGE \
   --global-secondary-indexes 'IndexName=GSI1_ReattemptSchedule,KeySchema=[{AttributeName=campaign_id,KeyType=HASH},{AttributeName=retry_scheduled_at,KeyType=RANGE}],Projection={ProjectionType=ALL}'"
$aws_ dynamodb update-time-to-live \
  --table-name "$TRACKING_TABLE" \
  --time-to-live-specification "Enabled=true, AttributeName=ttl" >/dev/null 2>&1 || true

step "2c. Table: $AUDIT_TABLE"
create_table "$AUDIT_TABLE" \
  "--attribute-definitions \
      AttributeName=entity_id,AttributeType=S \
      AttributeName=timestamp,AttributeType=S \
   --key-schema AttributeName=entity_id,KeyType=HASH AttributeName=timestamp,KeyType=RANGE"
$aws_ dynamodb update-time-to-live \
  --table-name "$AUDIT_TABLE" \
  --time-to-live-specification "Enabled=true, AttributeName=ttl" >/dev/null 2>&1 || true

# ═══════════════════════════════════════════════════════════════════════
# 3. IAM role for Lambda (con permission boundary)
# ═══════════════════════════════════════════════════════════════════════
step "3a. Creating Lambda execution role"
TRUST_DOC='{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "lambda.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}'

if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  aws iam create-role \
    --role-name "$ROLE_NAME" \
    --assume-role-policy-document "$TRUST_DOC" \
    --permissions-boundary "$BOUNDARY_ARN" \
    --tags Key=Project,Value=vip-connect-external-campaigns Key=Compliance,Value=hipaa \
    >/dev/null
  echo "Created role $ROLE_NAME"
else
  echo "Role $ROLE_NAME already exists"
fi

ROLE_ARN=$(aws iam get-role --role-name "$ROLE_NAME" --query 'Role.Arn' --output text)

step "3b. Attaching VPCAccess managed policy"
aws iam attach-role-policy --role-name "$ROLE_NAME" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole

step "3c. Inline policy for app permissions"
FILTERS_ARN=$($aws_ dynamodb describe-table --table-name "$FILTERS_TABLE" --query 'Table.TableArn' --output text)
TRACKING_ARN=$($aws_ dynamodb describe-table --table-name "$TRACKING_TABLE" --query 'Table.TableArn' --output text)
AUDIT_ARN=$($aws_ dynamodb describe-table --table-name "$AUDIT_TABLE" --query 'Table.TableArn' --output text)

INLINE_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid":"DDBRead","Effect":"Allow","Action":["dynamodb:GetItem","dynamodb:Query","dynamodb:Scan"],
      "Resource":["$FILTERS_ARN","$TRACKING_ARN","$TRACKING_ARN/index/*"]},
    {"Sid":"DDBWriteTracking","Effect":"Allow","Action":["dynamodb:PutItem","dynamodb:UpdateItem","dynamodb:BatchWriteItem"],
      "Resource":["$TRACKING_ARN"]},
    {"Sid":"DDBWriteAudit","Effect":"Allow","Action":["dynamodb:PutItem"],"Resource":["$AUDIT_ARN"]},
    {"Sid":"ConnectCampaigns","Effect":"Allow","Action":["connect-campaigns:PutDialRequestBatch","connect-campaigns:GetCampaignState","connect-campaigns:DescribeCampaign"],
      "Resource":"arn:aws:connect-campaigns:$REGION:$ACCOUNT:campaign/*"},
    {"Sid":"KMS","Effect":"Allow","Action":["kms:Encrypt","kms:Decrypt","kms:ReEncrypt*","kms:GenerateDataKey*","kms:DescribeKey"],
      "Resource":"$KMS_KEY_ARN"},
    {"Sid":"CWMetrics","Effect":"Allow","Action":["cloudwatch:PutMetricData"],"Resource":"*",
      "Condition":{"StringEquals":{"cloudwatch:namespace":"$METRICS_NAMESPACE"}}},
    {"Sid":"CWLogs","Effect":"Allow","Action":["logs:CreateLogStream","logs:PutLogEvents"],
      "Resource":"arn:aws:logs:$REGION:$ACCOUNT:log-group:$LOG_GROUP:*"}
  ]
}
EOF
)

aws iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name "vip-external-campaigns-feeder-inline" \
  --policy-document "$INLINE_POLICY"

echo "Role configured. Waiting 10s for IAM propagation..."
sleep 10

# ═══════════════════════════════════════════════════════════════════════
# 4. CloudWatch Log Group (KMS encrypted)
# ═══════════════════════════════════════════════════════════════════════
step "4. CloudWatch Log Group"
if ! $aws_ logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" --query "logGroups[?logGroupName=='$LOG_GROUP']" --output text | grep -q .; then
  $aws_ logs create-log-group --log-group-name "$LOG_GROUP" --kms-key-id "$KMS_KEY_ARN"
  $aws_ logs put-retention-policy --log-group-name "$LOG_GROUP" --retention-in-days 365
  echo "Created log group $LOG_GROUP"
else
  echo "Log group exists — skipping"
fi

# ═══════════════════════════════════════════════════════════════════════
# 5. Lambda function
# ═══════════════════════════════════════════════════════════════════════
step "5. Lambda function: $LAMBDA_NAME"
ENV_VARS="{\"REDIS_HOST\":\"$REDIS_HOST\",\"REDIS_PORT\":\"$REDIS_PORT\",\"TEAM\":\"$TEAM\",\"CONNECT_INSTANCE_ID\":\"$CONNECT_INSTANCE_ID\",\"FILTERS_TABLE\":\"$FILTERS_TABLE\",\"TRACKING_TABLE\":\"$TRACKING_TABLE\",\"AUDIT_TABLE\":\"$AUDIT_TABLE\",\"METRICS_NAMESPACE\":\"$METRICS_NAMESPACE\",\"LOG_LEVEL\":\"INFO\",\"POWERTOOLS_SERVICE_NAME\":\"external-campaigns-feeder\"}"

if $aws_ lambda get-function --function-name "$LAMBDA_NAME" >/dev/null 2>&1; then
  echo "Lambda exists — updating code + config"
  $aws_ lambda update-function-code --function-name "$LAMBDA_NAME" \
    --zip-file "fileb://$LAMBDA_ZIP" >/dev/null
  $aws_ lambda wait function-updated --function-name "$LAMBDA_NAME"
  $aws_ lambda update-function-configuration --function-name "$LAMBDA_NAME" \
    --environment "Variables=$ENV_VARS" \
    --memory-size 1024 --timeout 300 >/dev/null
else
  echo "Creating Lambda..."
  $aws_ lambda create-function \
    --function-name "$LAMBDA_NAME" \
    --runtime python3.12 \
    --role "$ROLE_ARN" \
    --handler handler.lambda_handler \
    --zip-file "fileb://$LAMBDA_ZIP" \
    --memory-size 1024 \
    --timeout 300 \
    --vpc-config "SubnetIds=$SUBNET_A,$SUBNET_B,SecurityGroupIds=$SG_ID" \
    --environment "Variables=$ENV_VARS" \
    --reserved-concurrent-executions 1 \
    --tags "$(echo $TAGS | tr ' ' ',')" >/dev/null
  $aws_ lambda wait function-active --function-name "$LAMBDA_NAME"
fi

LAMBDA_ARN=$($aws_ lambda get-function --function-name "$LAMBDA_NAME" --query 'Configuration.FunctionArn' --output text)
echo "Lambda ARN: $LAMBDA_ARN"

# ═══════════════════════════════════════════════════════════════════════
# 6. EventBridge rule (cron 5 min)
# ═══════════════════════════════════════════════════════════════════════
step "6. EventBridge schedule rule"
$aws_ events put-rule \
  --name "$RULE_NAME" \
  --schedule-expression "rate(5 minutes)" \
  --state ENABLED \
  --description "Trigger external-campaigns feeder every 5 minutes" \
  --tags "Key=Project,Value=vip-connect-external-campaigns" >/dev/null

# Permission for EventBridge → Lambda invoke (idempotent)
$aws_ lambda add-permission \
  --function-name "$LAMBDA_NAME" \
  --statement-id "AllowEventBridgeInvoke" \
  --action "lambda:InvokeFunction" \
  --principal "events.amazonaws.com" \
  --source-arn "arn:aws:events:$REGION:$ACCOUNT:rule/$RULE_NAME" 2>/dev/null || echo "(permission may already exist)"

$aws_ events put-targets \
  --rule "$RULE_NAME" \
  --targets "Id=1,Arn=$LAMBDA_ARN" >/dev/null

echo "EventBridge rule $RULE_NAME → Lambda $LAMBDA_NAME (every 5 min)"

# ═══════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════
step "✅ DEPLOY COMPLETE"
cat <<SUMMARY

KMS key ARN:     $KMS_KEY_ARN
DDB Filters:     $FILTERS_ARN
DDB Tracking:    $TRACKING_ARN
DDB Audit:       $AUDIT_ARN
IAM Role:        $ROLE_ARN
Lambda:          $LAMBDA_ARN
EventBridge:     arn:aws:events:$REGION:$ACCOUNT:rule/$RULE_NAME
Log Group:       $LOG_GROUP

Next steps:
  1. Insertá un filter de test en DynamoDB:
     aws dynamodb put-item --table-name $FILTERS_TABLE --item '{"campaign_id":{"S":"test-001"},"enabled":{"BOOL":false},"connect_campaign_id":{"S":"xxx"},...}'

  2. Invocá el Lambda manualmente para verificar:
     aws lambda invoke --function-name $LAMBDA_NAME --region $REGION /tmp/feeder-out.json
     cat /tmp/feeder-out.json

  3. Ver logs:
     aws logs tail $LOG_GROUP --region $REGION --follow
SUMMARY
