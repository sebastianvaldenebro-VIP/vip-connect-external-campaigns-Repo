#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { ApiCampaignsStack } from '../lib/stacks/api-campaigns-stack';
import { ApiMetricsStack } from '../lib/stacks/api-metrics-stack';
import { ApiProgressiveDialerStack } from '../lib/stacks/api-progressive-dialer-stack';
import { ApiPlansStack } from '../lib/stacks/api-plans-stack';
import { ApiProfilesStack } from '../lib/stacks/api-profiles-stack';
import { ApiSegmentsStack } from '../lib/stacks/api-segments-stack';
import { ApiStack } from '../lib/stacks/api-stack';
import { AuthStack } from '../lib/stacks/auth-stack';
import { DataStack } from '../lib/stacks/data-stack';
import { HostingStack } from '../lib/stacks/hosting-stack';

const app = new cdk.App();

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
const data = new DataStack(app, 'VipAdminDataStack', {
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

const auth = new AuthStack(app, 'VipAdminAuthStack', {
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

const segments = new ApiSegmentsStack(app, 'VipAdminApiSegmentsStack', {
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
const campaigns = new ApiCampaignsStack(app, 'VipAdminApiCampaignsStack', {
  env,
  description: 'api-campaigns Lambda — Outbound Campaigns V2 CRUD + lifecycle',
  adminAuditTable: data.adminAuditTable,
  dataKey: data.dataKey,
  connectInstanceId,
  profilesDomainName,
  permissionsBoundaryName,
});

// 5. api-metrics Lambda
const metrics = new ApiMetricsStack(app, 'VipAdminApiMetricsStack', {
  env,
  description: 'api-metrics Lambda — CloudWatch + audit log queries',
  adminAuditTable: data.adminAuditTable,
  dataKey: data.dataKey,
  connectInstanceId,
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

const progressiveDialer = new ApiProgressiveDialerStack(app, 'ApiProgressiveDialerStack', {
  env,
  description: 'Progressive Branded Dialer — Kinesis consumer + SQS caller + seeder Lambda',
  dataKeyArn: progressiveDialerDataKeyArn,
  connectInstanceId,
  agentEventStreamArn: 'arn:aws:kinesis:us-east-1:165505826690:stream/vip-use1-datastream',
  firstOrionSecretArn,
  profilesDomainName,
  permissionsBoundaryName,
});

// 6. api-plans Lambda + DynamoDB plans table
const plans = new ApiPlansStack(app, 'VipAdminApiPlansStack', {
  env,
  description: 'api-plans Lambda — Daily Plans sequential campaign orchestration',
  adminAuditTable: data.adminAuditTable,
  dataKey: data.dataKey,
  connectInstanceId,
  profilesDomainName,
  permissionsBoundaryName,
  redis: redisConfig,
  redisVpc: redisVpcConfig,
  progressiveCampaignQueueTable: progressiveDialer.campaignQueueTable,
  activeBrandedCampaignsTable:   progressiveDialer.activeBrandedCampaignsTable,
  progressiveDialerSeederArn:    progressiveDialer.seederFunction.functionArn,
  progressiveDialerDataKeyArn:   progressiveDialerDataKeyArn,
});

// 7. api-profiles Lambda
const profiles = new ApiProfilesStack(app, 'VipAdminApiProfilesStack', {
  env,
  description: 'api-profiles Lambda — Customer Profiles read-only operations',
  dataKey: data.dataKey,
  profilesDomainName,
  profileObjectType: app.node.tryGetContext('profileObjectType') ?? 'leads-data-mapping',
  permissionsBoundaryName,
});

// 10. API Gateway fronting all 5 Lambdas with Cognito JWT Authorizer
const corsAllowOrigins = (app.node.tryGetContext('corsAllowOrigins') as string[]) ?? [
  'http://localhost:5173',
];

new ApiStack(app, 'VipAdminApiStack', {
  env,
  description: 'API Gateway HTTP API + Cognito JWT Authorizer fronting admin Lambdas',
  userPool: auth.userPool,
  userPoolClient: auth.userPoolClient,
  segmentsFunction: segments.lambdaFunction,
  campaignsFunction: campaigns.lambdaFunction,
  metricsFunction: metrics.lambdaFunction,
  profilesFunction: profiles.lambdaFunction,
  plansFunction: plans.lambdaFunction,
  progressiveDialerSeedFunction: progressiveDialer.seederFunction,
  corsAllowOrigins,
  permissionsBoundaryName,
});

// 11. S3 + CloudFront hosting for the SPA
new HostingStack(app, 'VipAdminHostingStack', {
  env,
  description: 'CloudFront + S3 bucket that host the admin UI SPA',
  permissionsBoundaryName,
});

// NOTE: MonitoringStack (SNS + CloudWatch alarms + dashboard) is NOT managed by CDK.
// The CFN exec role lacks SNS and cloudwatch:PutDashboard permissions.
// All monitoring resources are created via CLI — see deploy-cli.sh.

Object.entries(mandatoryTags).forEach(([k, v]) => cdk.Tags.of(app).add(k, v));
