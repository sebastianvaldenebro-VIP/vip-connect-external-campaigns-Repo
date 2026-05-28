# IAM Reference

Complete IAM documentation for the **VIP Connect External Campaigns** platform.

---

## 1. Permission Boundary

**Name:** `arn:aws:iam::165505826690:policy/EngineeringPermissionBoundary`

Every Lambda execution role and CDK deploy role in this project **must** carry this boundary. The CDK stack applies it automatically:

```ts
if (props.permissionsBoundaryName) {
  const boundary = iam.ManagedPolicy.fromManagedPolicyName(
    this, 'PermissionsBoundary', props.permissionsBoundaryName,
  );
  iam.PermissionsBoundary.of(this).apply(boundary);
}
```

### What the boundary blocks

| Action | Boundary effect |
|---|---|
| `iam:CreateRole` (without boundary) | Denied |
| `iam:PutRolePolicy` (escalation) | Denied |
| `iam:PassRole` to non-boundary roles | Denied |
| `sns:CreateTopic`, `sns:GetTopicAttributes` | **Denied** — hence ADR-005 (SNS topic imported by ARN) |
| `kms:CreateKey` | Denied — keys are pre-provisioned |
| `s3:PutBucketPolicy` without specific tag | Restricted |
| `cloudtrail:*` writes | Denied |

The boundary is the reason CDK can't manage SNS topics; this is a hard constraint inherited from Medwork's security baseline.

---

## 2. Lambda Execution Roles

Each api-* Lambda has its own role. The api-plans role is the most permissive because it orchestrates everything.

### api-plans execution role

Managed policies attached:
- `service-role/AWSLambdaVPCAccessExecutionRole` (for ENI creation/deletion in VPC)

Inline policy statements (from `infra/lib/stacks/api-plans-stack.ts`):

```json
{
  "Statement": [
    {
      "Sid": "CustomerProfilesSegments",
      "Effect": "Allow",
      "Action": [
        "profile:CreateSegmentDefinition",
        "profile:GetSegmentDefinition",
        "profile:DeleteSegmentDefinition",
        "profile:ListSegmentDefinitions",
        "profile:TagResource"
      ],
      "Resource": [
        "arn:aws:profile:us-east-1:165505826690:domains/${PROFILES_DOMAIN_NAME}",
        "arn:aws:profile:us-east-1:165505826690:domains/${PROFILES_DOMAIN_NAME}/*"
      ]
    },
    {
      "Sid": "ConnectCampaignsV2",
      "Effect": "Allow",
      "Action": [
        "connect-campaigns:CreateCampaign",
        "connect-campaigns:DeleteCampaign",
        "connect-campaigns:StartCampaign",
        "connect-campaigns:StopCampaign",
        "connect-campaigns:GetCampaignState",
        "connect-campaigns:DescribeCampaign",
        "connect-campaigns:TagResource"
      ],
      "Resource": "arn:aws:connect-campaigns:us-east-1:165505826690:campaign/*"
    },
    {
      "Sid": "ConnectReadInstanceResources",
      "Effect": "Allow",
      "Action": [
        "connect:ListQueues",
        "connect:ListContactFlows",
        "connect:DescribeContactFlow",
        "connect:DescribeQueue",
        "connect:DescribeInstance",
        "connect:CreateContactFlow",
        "connect:TagResource"
      ],
      "Resource": [
        "arn:aws:connect:us-east-1:165505826690:instance/${CONNECT_INSTANCE_ID}",
        "arn:aws:connect:us-east-1:165505826690:instance/${CONNECT_INSTANCE_ID}/*"
      ]
    },
    {
      "Sid": "ConnectPhoneNumberV2",
      "Effect": "Allow",
      "Action": ["connect:ListPhoneNumbersV2", "connect:DescribePhoneNumber"],
      "Resource": [
        "arn:aws:connect:us-east-1:165505826690:phone-number/*",
        "arn:aws:connect:us-east-1:165505826690:instance/${CONNECT_INSTANCE_ID}"
      ]
    },
    {
      "Sid": "DynamoDBPlans",
      "Effect": "Allow",
      "Action": [
        "dynamodb:GetItem", "dynamodb:PutItem",
        "dynamodb:Query", "dynamodb:Scan",
        "dynamodb:UpdateItem", "dynamodb:DeleteItem",
        "dynamodb:BatchGetItem", "dynamodb:BatchWriteItem",
        "dynamodb:ConditionCheckItem"
      ],
      "Resource": "arn:aws:dynamodb:us-east-1:165505826690:table/VipAdminPlans"
    },
    {
      "Sid": "AuditWrite",
      "Effect": "Allow",
      "Action": ["dynamodb:PutItem"],
      "Resource": "arn:aws:dynamodb:us-east-1:165505826690:table/VipAdminAudit"
    },
    {
      "Sid": "EventBridgeRules",
      "Effect": "Allow",
      "Action": [
        "events:PutRule",
        "events:PutTargets",
        "events:RemoveTargets",
        "events:DeleteRule"
      ],
      "Resource": [
        "arn:aws:events:us-east-1:165505826690:rule/vip-plan-*",
        "arn:aws:events:us-east-1:165505826690:rule/vip-sched-*"
      ]
    },
    {
      "Sid": "LambdaSelfPermission",
      "Effect": "Allow",
      "Action": ["lambda:AddPermission", "lambda:RemovePermission"],
      "Resource": "arn:aws:lambda:us-east-1:165505826690:function:vip-admin-ui-api-plans"
    },
    {
      "Sid": "StsGetCallerIdentity",
      "Effect": "Allow",
      "Action": ["sts:GetCallerIdentity"],
      "Resource": "*"
    },
    {
      "Sid": "CloudWatchPutMetric",
      "Effect": "Allow",
      "Action": ["cloudwatch:PutMetricData"],
      "Resource": "*"
    },
    {
      "Sid": "SNSPublish",
      "Effect": "Allow",
      "Action": ["sns:Publish"],
      "Resource": "arn:aws:sns:us-east-1:165505826690:vip-plans-alerts"
    },
    {
      "Sid": "KMSEncryptDecrypt",
      "Effect": "Allow",
      "Action": [
        "kms:Encrypt", "kms:Decrypt",
        "kms:GenerateDataKey", "kms:GenerateDataKeyWithoutPlaintext",
        "kms:ReEncrypt*"
      ],
      "Resource": "${DATA_KEY_ARN}"
    },
    {
      "Sid": "VPCNetworking",
      "Effect": "Allow",
      "Action": [
        "ec2:CreateNetworkInterface",
        "ec2:DescribeNetworkInterfaces",
        "ec2:DeleteNetworkInterface"
      ],
      "Resource": "*"
    },
    {
      "Sid": "CloudWatchLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:us-east-1:165505826690:log-group:/aws/lambda/vip-admin-ui-api-plans:*"
    }
  ]
}
```

### api-campaigns execution role

Minimum required:

- `connect-campaigns:CreateCampaign|DeleteCampaign|StartCampaign|StopCampaign|GetCampaignState|DescribeCampaign|ListCampaigns|TagResource`
- `connect:ListContactFlows|DescribeContactFlow|ListQueues`
- `dynamodb:PutItem` on `VipAdminAudit`
- KMS encrypt/decrypt on data key
- VPC + CloudWatch Logs basics

### api-metrics execution role

- `dynamodb:Query|Scan|GetItem` on `VipAdminPlans` and `VipAdminAudit`
- `cloudwatch:GetMetricStatistics|GetMetricData|ListMetrics`
- KMS decrypt on data key
- CloudWatch Logs basics

### api-profiles execution role

- `profile:*` (read + write) on the Customer Profiles domain
- `dynamodb:PutItem` on `VipAdminAudit`
- KMS encrypt/decrypt on data key
- VPC (only if reaching Redis) + CloudWatch Logs basics

### api-segments execution role

- `profile:CreateSegmentDefinition|GetSegmentDefinition|DeleteSegmentDefinition|ListSegmentDefinitions|TagResource`
- `dynamodb:GetItem|Query|Scan` on `VipAdminPlans` (read snapshot for context)
- `dynamodb:PutItem` on `VipAdminAudit`
- KMS encrypt/decrypt on data key
- VPC (Redis access for lead counts) + CloudWatch Logs basics

---

## 3. Boundary Restrictions Applied Above

Because of the boundary, the following are **NOT** included in any role:

- `iam:*` — no role-management calls allowed.
- `sns:CreateTopic|GetTopicAttributes` — topic is hand-managed.
- `kms:CreateKey|ScheduleKeyDeletion|DisableKey` — keys pre-provisioned by the security team.
- `s3:*` outside specific buckets carrying required tags.
- `cloudtrail:*` writes — auditing is centralised.

If you find yourself wanting any of the above during a feature, the answer is almost always **pre-create out-of-band and import**.

---

## 4. Least-Privilege Assessment

| Area | Status | Recommendation |
|---|---|---|
| Lambda → DynamoDB | Scoped to specific table ARNs | Keep. |
| Lambda → Connect Campaigns V2 | Scoped to `campaign/*` (account-wide); cannot scope tighter at present | Acceptable; Connect API limitation. |
| Lambda → Customer Profiles | Scoped to specific domain ARN | Keep. |
| Lambda → EventBridge | Scoped to `vip-plan-*` and `vip-sched-*` rule name patterns | Keep. |
| Lambda → self (`lambda:AddPermission`) | Scoped to its own function ARN | Keep. |
| `cloudwatch:PutMetricData` | `Resource: "*"` (no resource-level support) | Acceptable; AWS limitation. |
| `ec2:CreateNetworkInterface` | `Resource: "*"` (no resource-level support) | Acceptable; VPC ENI churn requirement. |
| `sts:GetCallerIdentity` | `Resource: "*"` (no resource-level support) | Acceptable; identity service quirk. |
| KMS | Scoped to single CMK ARN | Keep. Consider key alias granularity. |

### Recommendations

1. **Add `aws:SourceArn` condition** on the api-plans Lambda's `events:PutTargets` action to ensure it can only target itself. Currently allowed anywhere matching the rule-name pattern.
2. **Tag-based scope-down** for `cloudwatch:PutMetricData` once AWS adds resource-level support (track the AWS roadmap).
3. **Separate the audit-write role** out of every Lambda. Today every Lambda carries `PutItem` on the audit table. A dedicated audit sidecar function (invoked via SNS / EventBridge) would tighten this.
4. **Periodic IAM Access Analyzer review** — schedule quarterly to confirm no broader use than required.
5. **Service control policies (SCPs)** at the org level should mirror the boundary's intent; verify alignment.

---

## 5. Cognito / API Gateway Authorisation

The Admin UI authenticates with Cognito (`us-east-1_MeEkWO4P4`). API Gateway is configured with a Cognito User Pool authorizer that validates the JWT before invoking any api-* Lambda. The Lambdas re-decode the JWT claims through `vip_shared.application.http.extract_caller(event)` for audit purposes, but they do not re-validate the signature (that is API Gateway's job).

Two basic roles exist in the user pool:

| Cognito group | UI access | Lambda authorization |
|---|---|---|
| `admin` | Full plan + run management | All api-* endpoints |
| `viewer` | Read-only (planned) | GET-only enforcement at API Gateway layer (planned) |

Today the only enforced gate is "authenticated user". Role-based filtering at the Lambda layer is a tracked enhancement.
