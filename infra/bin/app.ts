#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { ApiCampaignsStack } from '../lib/stacks/api-campaigns-stack';
import { ApiSmsStack } from '../lib/stacks/api-sms-stack';
import { ApiMetricsStack } from '../lib/stacks/api-metrics-stack';
import { ApiProgressiveDialerStack } from '../lib/stacks/api-progressive-dialer-stack';
import { ApiPlansStack } from '../lib/stacks/api-plans-stack';
import { ApiProfilesStack } from '../lib/stacks/api-profiles-stack';
import { ApiDenyListStack } from '../lib/stacks/api-deny-list-stack';
import { ApiAuthorizerStack } from '../lib/stacks/api-authorizer-stack';
import { ApiSegmentsStack } from '../lib/stacks/api-segments-stack';
import { ApiStack } from '../lib/stacks/api-stack';
import { AuthStack } from '../lib/stacks/auth-stack';
import { DataStack } from '../lib/stacks/data-stack';
import { HostingStack } from '../lib/stacks/hosting-stack';
import { QuadriviaWebhookStack } from '../lib/stacks/quadrivia-webhook-stack';

export const app = new cdk.App();

const permissionsBoundaryName = app.node.tryGetContext('permissionsBoundaryName') as string | undefined;

const env = {
  account: app.node.tryGetContext('awsAccountId') ?? process.env.CDK_DEFAULT_ACCOUNT,
  region: app.node.tryGetContext('awsRegion') ?? process.env.CDK_DEFAULT_REGION,
};

const mandatoryTags = {
  Environment: 'prod',
  Project: 'vip-connect-admin-ui',
  Owner: 'devaju',
  CostCenter: 'vip-connect',
  Compliance: 'hipaa',
  ManagedBy: 'cdk',
};

const profilesDomainName =
  app.node.tryGetContext('profilesDomainName') ?? 'amazon-connect-vipmedicalgroup';
const connectInstanceId =
  app.node.tryGetContext('connectInstanceId') ?? '6b3f17ba-68a4-472a-9b20-db1991507009';

// 1. Data stack — KMS + DynamoDB tables (including AdminAuditLog)
export const data = new DataStack(app, 'VipAdminDataStack', {
  env,
  description: 'DynamoDB tables + KMS CMK for VIP Admin UI',
  auditRetentionYears: Number(app.node.tryGetContext('auditRetentionYears') ?? 6),
  permissionsBoundaryName,
});

// 2. Auth stack — Cognito User Pool
const cognitoDomainPrefix = app.node.tryGetContext('cognitoDomainPrefix') ?? 'vip-admin-ui';
const callbackUrls = (app.node.tryGetContext('cognitoCallbackUrls') as string[]) ?? [
  'http://localhost:5173/callback',
];
const logoutUrls = (app.node.tryGetContext('cognitoLogoutUrls') as string[]) ?? [
  'http://localhost:5173/',
];

export const auth = new AuthStack(app, 'VipAdminAuthStack', {
  env,
  description: 'Cognito User Pool for VIP Admin UI',
  permissionsBoundaryName,
  cognitoDomainPrefix,
  callbackUrls,
  logoutUrls,
});

// 3. api-segments Lambda + S3 snapshot bucket + shared Layer (defined here)
// Redis + VPC wiring reuses the existing feeder infrastructure so the Lambda
// can reach production-leads ElastiCache without a new ingress rule.
const redisConfig = {
  host:
    (app.node.tryGetContext('redisHost') as string) ??
    'master.prod-medwork-api.jrdc0s.use1.cache.amazonaws.com',
  port: Number(app.node.tryGetContext('redisPort') ?? 6379),
  team: (app.node.tryGetContext('redisTeam') as string) ?? 'BASIC_TEAM',
  profileObjectType:
    (app.node.tryGetContext('profileObjectType') as string) ?? 'leads-data-mapping',
  passwordSecretArn: (app.node.tryGetContext('redisPasswordSecretArn') as string | undefined) || undefined,
};
const redisVpcConfig = {
  vpcId:
    (app.node.tryGetContext('redisVpcId') as string) ?? 'vpc-0d32b420acc84d370',
  subnetIds: ((app.node.tryGetContext('redisSubnetIds') as string[]) ?? [
    'subnet-06c7669b5e3e0e814',
    'subnet-088367ac9fc0a2fec',
  ]),
  availabilityZones: ((app.node.tryGetContext('redisSubnetAZs') as string[]) ?? [
    'us-east-1a',
    'us-east-1b',
  ]),
  securityGroupId:
    (app.node.tryGetContext('redisSecurityGroupId') as string) ??
    'sg-01d54d29c2a4785f1',
};

export const segments = new ApiSegmentsStack(app, 'VipAdminApiSegmentsStack', {
  env,
  description: 'api-segments Lambda + snapshot bucket + shared layer',
  adminAuditTable: data.adminAuditTable,
  segmentFilterConfigTable: data.segmentFilterConfigTable,
  dataKey: data.dataKey,
  profilesDomainName,
  permissionsBoundaryName,
  redis: redisConfig,
  redisVpc: redisVpcConfig,
});

// 4. api-campaigns Lambda (builds its own copy of the shared layer)
export const campaigns = new ApiCampaignsStack(app, 'VipAdminApiCampaignsStack', {
  env,
  description: 'api-campaigns Lambda — Outbound Campaigns V2 CRUD + lifecycle',
  adminAuditTable: data.adminAuditTable,
  dataKey: data.dataKey,
  connectInstanceId,
  profilesDomainName,
  permissionsBoundaryName,
});

// 6a. Progressive Branded Dialer stack — moved before ApiPlansStack so its
// public properties (seederFunction, campaignQueueTable, activeBrandedCampaignsTable)
// can be passed as props to ApiPlansStack.
// ARNs passed as strings per the isolation rule: never import from already-deployed stacks.
// Before deploying, fill these context values in cdk.json or pass via --context:
//   dataKeyArn:          aws kms describe-key --key-id alias/vip-data-key --query KeyMetadata.Arn --output text --region us-east-1 --profile production
//   firstOrionSecretArn: ARN from Task 6 Step 1
function requireContext(key: string): string {
  const val = app.node.tryGetContext(key) as string | undefined;
  if (!val) throw new Error(`CDK context '${key}' is required — pass via --context or cdk.json`);
  return val;
}

const progressiveDialerDataKeyArn = requireContext('progressiveDialerDataKeyArn');
const firstOrionSecretArn         = requireContext('firstOrionSecretArn');

export const progressiveDialer = new ApiProgressiveDialerStack(app, 'ApiProgressiveDialerStack', {
  env,
  description: 'Progressive Branded Dialer — Kinesis consumer + SQS caller + seeder Lambda',
  dataKeyArn: progressiveDialerDataKeyArn,
  connectInstanceId,
  agentEventStreamArn: 'arn:aws:kinesis:us-east-1:165505826690:stream/vip-use1-datastream',
  firstOrionSecretArn,
  profilesDomainName,
  permissionsBoundaryName,
  campaignQueueStreamArn: 'arn:aws:dynamodb:us-east-1:165505826690:table/VipProgressiveCampaignQueue/stream/2026-07-23T00:43:38.549',
});

// 5. api-metrics Lambda (instantiated after progressiveDialer to reference branded tables)
export const metrics = new ApiMetricsStack(app, 'VipAdminApiMetricsStack', {
  env,
  description: 'api-metrics Lambda — CloudWatch + audit log queries + branded campaign monitor',
  adminAuditTable: data.adminAuditTable,
  dataKey: data.dataKey,
  connectInstanceId,
  permissionsBoundaryName,
  activeBrandedCampaignsTable:     progressiveDialer.activeBrandedCampaignsTable,
  brandedRunSummaryTable:          progressiveDialer.brandedRunSummaryTable,
  brandedCampaignMetricsTable:     progressiveDialer.brandedCampaignMetricsTable,
  agentSnapshotTable:              progressiveDialer.agentSnapshotTable,
  progressiveCampaignQueueTable:   progressiveDialer.campaignQueueTable,
});

// 6b. SMS Campaign stack — sender + processor Lambdas + SQS
// Before deploying, ensure these CLI resources exist (Phase 0):
//   - EUM SMS Config Set:  vip-sms-config-set
//   - EUM SMS Opt-Out List: vip-sms-opt-out
//   - DDB tables: VipSmsCampaignQueue, VipSmsCampaignRuns (pre-created via CLI)
//   - SQS queues: vip-sms-campaign-queue + vip-sms-campaign-queue-dlq (pre-created via CLI)
//   - IAM roles: vip-sms-sender-role, vip-sms-processor-role (pre-created via CLI;
//       vip-sms-sender-role is reused as-is by the retry Lambda below — no new role)
//   - Log groups: /aws/lambda/vip-admin-sms-sender, /aws/lambda/vip-admin-sms-processor,
//       /aws/lambda/vip-admin-sms-retry-quiet-hours (KMS-encrypted, see api-sms-stack.ts's
//       SmsRetryQuietHoursFunction comment for the exact pre-create CLI commands)
export const smsStack = new ApiSmsStack(app, 'VipAdminApiSmsStack', {
  env,
  description: 'SMS Campaign — bulk SMS via EUM SMS, SQS-driven processor',
  dataKeyArn: progressiveDialerDataKeyArn,
  profilesDomainName,
  snapshotBucketName: segments.snapshotBucket.bucketName,
  snapshotRoleArn: segments.snapshotRole.roleArn,
  snapshotKeyArn: data.dataKey.keyArn,
  smsConfigSetName: (app.node.tryGetContext('smsConfigSetName') as string) ?? 'vip-sms-config-set',
  smsOptOutListName: (app.node.tryGetContext('smsOptOutListName') as string) ?? 'vip-sms-opt-out',
  permissionsBoundaryName,
});

// 6. api-plans Lambda + DynamoDB plans table
export const plans = new ApiPlansStack(app, 'VipAdminApiPlansStack', {
  env,
  description: 'api-plans Lambda — Daily Plans sequential campaign orchestration',
  adminAuditTable: data.adminAuditTable,
  dataKey: data.dataKey,
  connectInstanceId,
  profilesDomainName,
  permissionsBoundaryName,
  redis: redisConfig,
  redisVpc: redisVpcConfig,
  progressiveCampaignQueueTable:  progressiveDialer.campaignQueueTable,
  activeBrandedCampaignsTable:    progressiveDialer.activeBrandedCampaignsTable,
  brandedRunSummaryTable:         progressiveDialer.brandedRunSummaryTable,
  brandedCampaignMetricsTable:    progressiveDialer.brandedCampaignMetricsTable,
  agentSnapshotTable:             progressiveDialer.agentSnapshotTable,
  progressiveDialerSeederArn:     progressiveDialer.seederFunction.functionArn,
  progressiveDialerDataKeyArn:   progressiveDialerDataKeyArn,
  smsCampaignQueueTable:          smsStack.smsCampaignQueueTable,
  smsRunsTable:                   smsStack.smsRunsTable,
  smsSenderFunctionArn:           smsStack.smsSenderFunction.functionArn,
  smsRetryFunctionArn:            smsStack.smsRetryQuietHoursFunction.functionArn,
  locationMappingStreamArn:       'arn:aws:dynamodb:us-east-1:165505826690:table/VipLocationMapping/stream/2026-08-18T21:05:11.209',
});

// 7. api-profiles Lambda
export const profiles = new ApiProfilesStack(app, 'VipAdminApiProfilesStack', {
  env,
  description: 'api-profiles Lambda — Customer Profiles read-only operations',
  dataKey: data.dataKey,
  profilesDomainName,
  profileObjectType: app.node.tryGetContext('profileObjectType') ?? 'leads-data-mapping',
  permissionsBoundaryName,
  // GET /phone-lookup invokes connectcampaignRedisAuxiliar (sibling repo,
  // already deployed, same account/region) — grant + env var applied
  // manually outside CDK, see api-profiles-stack.ts for why.
});

// 9. api-deny-list Lambda — manual "block this number" entry, backs the
// existing vip-connect-deny-list table (owned by Connect-batch-redis-refactor's
// Quick Connect Lambdas, not by this app).
export const denyList = new ApiDenyListStack(app, 'VipAdminApiDenyListStack', {
  env,
  description: 'api-deny-list Lambda — manual blocked-number entry portal',
  adminAuditTable: data.adminAuditTable,
  dataKey: data.dataKey,
  permissionsBoundaryName,
});

// 9b. Custom Lambda authorizer — Cognito-group-based per-route authorization.
// Deployed separately from ApiStack so an authorizer-only change (or a
// rollback) doesn't touch the HTTP API / route resources at all.
export const apiAuthorizer = new ApiAuthorizerStack(app, 'VipAdminApiAuthorizerStack', {
  env,
  description: 'Custom Lambda authorizer — Cognito Admin/Agent group enforcement per route',
  dataKey: data.dataKey,
  userPool: auth.userPool,
  userPoolClient: auth.userPoolClient,
  permissionsBoundaryName,
});

// 10. API Gateway fronting all 7 Lambdas with the custom Lambda authorizer
const corsAllowOrigins = (app.node.tryGetContext('corsAllowOrigins') as string[]) ?? [
  'http://localhost:5173',
];

export const apiStack = new ApiStack(app, 'VipAdminApiStack', {
  env,
  description: 'API Gateway HTTP API + custom Lambda authorizer (Cognito group-based) fronting admin Lambdas',
  dataKey: data.dataKey,
  authorizer: apiAuthorizer.authorizer,
  segmentsFunction: segments.lambdaFunction,
  campaignsFunction: campaigns.lambdaFunction,
  metricsFunction: metrics.lambdaFunction,
  profilesFunction: profiles.lambdaFunction,
  plansFunction: plans.lambdaFunction,
  progressiveDialerSeedFunction: progressiveDialer.seederFunction,
  denyListFunction: denyList.lambdaFunction,
  corsAllowOrigins,
  permissionsBoundaryName,
});

// 11. S3 + CloudFront hosting for the SPA
export const hostingStack = new HostingStack(app, 'VipAdminHostingStack', {
  env,
  description: 'CloudFront + S3 bucket that host the admin UI SPA',
  permissionsBoundaryName,
});

// NOTE: MonitoringStack (SNS + CloudWatch alarms + dashboard) is NOT managed by CDK.
// The CFN exec role lacks SNS and cloudwatch:PutDashboard permissions.
// All monitoring resources are created via CLI — see deploy-cli.sh.

// QuadriviaWebhookStack — the mTLS webhook that lets Quadrivia's after-hours
// AI agent schedule callback Tasks in Connect.
//
// Status as of 2026-10-01 — DELIBERATE DRY-RUN DEPLOY, not a real go-live:
//   - ownerEmail = sebastian.valdenebro@medwork.io, team = specialOps — DECIDED.
//   - patientLookupFunctionArn = SOPS-ConnectPatientLookup's ARN — DECIDED.
//   - existingDomain — intentionally OMITTED (left undefined). mTLS is not
//     yet active on quadrivia-webhook.medwork.io, so this stack creates no
//     ApiMapping and the webhook is reachable from nowhere. Safe by
//     construction, not by discipline — do not add existingDomain until
//     mTLS is confirmed active (see the DEPLOY-ORDER WARNING on that prop).
//   - clientCertSubjectDn below is still an EXPLICIT PLACEHOLDER, not a real
//     value — see QUADRIVIA_PLACEHOLDER_CERT_SUBJECT_DN for why this is
//     still safe to deploy. Must be replaced with a real value — via a
//     second, deliberate deploy — before mTLS is ever activated on the
//     domain: Quadrivia confirmed 2026-09-30 they'll state the exact
//     subjectDN when they send their CA cert PEM. Per the stack's own doc:
//     derive it from the actual PEM they send (`openssl x509 -noout
//     -subject`), not from a typed description.
//   - contactFlowId now points at the real TEST flow ("Quadrivia Test
//     Flow", created 2026-10-01, always transfers to the "Quadrivia Test"
//     queue — no live agent has that queue in their routing profile). This
//     is deliberately the test-window flow, not the real
//     billing/existing-patient/new-lead router — that one still needs the
//     real queue ARNs (PST pending Maria Jose) and must replace this value
//     before go-live.
const QUADRIVIA_PLACEHOLDER_CERT_SUBJECT_DN = 'PENDING-QUADRIVIA-CERT-DO-NOT-ACTIVATE-MTLS-WITH-THIS-VALUE';
const QUADRIVIA_TEST_CONTACT_FLOW_ID = '94aa3f9d-5ed3-4de5-aa7b-065012de3beb';

export const quadriviaWebhook = new QuadriviaWebhookStack(app, 'QuadriviaWebhookStack', {
  env,
  dataKey: data.dataKey,
  connectInstanceArn: `arn:aws:connect:us-east-1:165505826690:instance/${connectInstanceId}`,
  contactFlowId: QUADRIVIA_TEST_CONTACT_FLOW_ID,
  patientLookupFunctionArn:
    'arn:aws:lambda:us-east-1:165505826690:function:SOPS-ConnectPatientLookup',
  clientCertSubjectDn: QUADRIVIA_PLACEHOLDER_CERT_SUBJECT_DN,
  ownerEmail: 'sebastian.valdenebro@medwork.io',
  team: 'specialOps',
  // existingDomain intentionally omitted — see status note above.
  permissionsBoundaryName,
});

// AWS::IAM::Role is excluded: EngineeringPermissionBoundary explicitly denies
// iam:TagRole/iam:UntagRole account-wide, so any attempt to re-sync tags on a
// role fails deploy AND the subsequent rollback (same denied action), leaving
// the stack stuck in UPDATE_ROLLBACK_FAILED (hit 2026-09-14 on 4 stacks,
// see BUGLOG.md BD-024).
Object.entries(mandatoryTags).forEach(([k, v]) =>
  cdk.Tags.of(app).add(k, v, { excludeResourceTypes: ['AWS::IAM::Role'] }),
);
