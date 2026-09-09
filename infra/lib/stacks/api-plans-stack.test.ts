import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as kms from 'aws-cdk-lib/aws-kms';
import { Template } from 'aws-cdk-lib/assertions';

// buildSharedLayer's real implementation shells out to `pip install` (or Docker)
// against services/shared/requirements.txt during CDK asset bundling, which
// happens synchronously the moment the Lambda Function construct is created.
// That's appropriate for `cdk synth`/deploy but makes it unusable in a fast,
// hermetic unit test — it needs either network access or Docker, neither of
// which should be a precondition for `npm test`. Mocked out to an imported
// (zero-bundling) LayerVersion so tests only exercise this stack's own logic.
jest.mock('../utils/shared-layer', () => ({
  buildSharedLayer: jest.fn((scope: import('constructs').Construct, id = 'SharedLayer') =>
    require('aws-cdk-lib/aws-lambda').LayerVersion.fromLayerVersionArn(
      scope,
      id,
      'arn:aws:lambda:us-east-1:165505826690:layer:mock-shared-layer:1',
    ),
  ),
}));

import { ApiPlansStack, ApiPlansStackProps } from './api-plans-stack';

const ACCOUNT = '165505826690';
const REGION = 'us-east-1';
const CONNECT_INSTANCE_ID = '6b3f17ba-68a4-472a-9b20-db1991507009';
const PROFILES_DOMAIN_NAME = 'amazon-connect-vipmedicalgroup';

const ADMIN_AUDIT_TABLE_ARN = `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/AdminAuditLog`;
const DATA_KEY_ARN = `arn:aws:kms:${REGION}:${ACCOUNT}:key/df585888-2f49-4de0-9cba-14803fda63f0`;
const PROGRESSIVE_QUEUE_ARN = `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/VipProgressiveCampaignQueue`;
const ACTIVE_BRANDED_ARN = `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/VipActiveBrandedCampaigns`;
const BRANDED_RUN_SUMMARY_ARN = `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/VipBrandedRunSummary`;
const BRANDED_METRICS_ARN = `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/VipBrandedCampaignMetrics`;
const AGENT_SNAPSHOT_ARN = `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/VipAgentSnapshot`;
const SMS_QUEUE_ARN = `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/VipSmsCampaignQueue`;
const SMS_RUNS_ARN = `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/VipSmsCampaignRuns`;
const REDIS_SECRET_ARN = `arn:aws:secretsmanager:${REGION}:${ACCOUNT}:secret:vip/redis/auth-abc123`;
const SMS_SENDER_ARN = `arn:aws:lambda:${REGION}:${ACCOUNT}:function:vip-sms-sender`;
const SEEDER_ARN = `arn:aws:lambda:${REGION}:${ACCOUNT}:function:vip-admin-progressive-dialer-seeder`;
const PROGRESSIVE_DIALER_KEY_ARN = `arn:aws:kms:${REGION}:${ACCOUNT}:key/progressive-dialer-key`;
const LOCATION_MAPPING_STREAM_ARN =
  `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/VipLocationMapping/stream/2024-01-01T00:00:00.000`;

function fixtureTable(scope: cdk.Stack, id: string, arn: string): dynamodb.ITable {
  return dynamodb.Table.fromTableArn(scope, id, arn);
}

/** Every optional ITable/ARN prop as fixtures, keyed by name for easy spreading. */
function optionalFixtures(fixtures: cdk.Stack) {
  return {
    progressiveCampaignQueueTable: fixtureTable(fixtures, 'ProgressiveQueueTable', PROGRESSIVE_QUEUE_ARN),
    activeBrandedCampaignsTable: fixtureTable(fixtures, 'ActiveBrandedTable', ACTIVE_BRANDED_ARN),
    brandedRunSummaryTable: fixtureTable(fixtures, 'BrandedRunSummaryTable', BRANDED_RUN_SUMMARY_ARN),
    brandedCampaignMetricsTable: fixtureTable(fixtures, 'BrandedMetricsTable', BRANDED_METRICS_ARN),
    agentSnapshotTable: fixtureTable(fixtures, 'AgentSnapshotTable', AGENT_SNAPSHOT_ARN),
    smsCampaignQueueTable: fixtureTable(fixtures, 'SmsQueueTable', SMS_QUEUE_ARN),
    smsRunsTable: fixtureTable(fixtures, 'SmsRunsTable', SMS_RUNS_ARN),
  };
}

function minimalProps(fixtures: cdk.Stack): ApiPlansStackProps {
  return {
    env: { account: ACCOUNT, region: REGION },
    adminAuditTable: fixtureTable(fixtures, 'AdminAuditTable', ADMIN_AUDIT_TABLE_ARN),
    dataKey: kms.Key.fromKeyArn(fixtures, 'DataKey', DATA_KEY_ARN),
    connectInstanceId: CONNECT_INSTANCE_ID,
    profilesDomainName: PROFILES_DOMAIN_NAME,
    redisVpc: {
      vpcId: 'vpc-0d32b420acc84d370',
      subnetIds: ['subnet-06c7669b5e3e0e814', 'subnet-088367ac9fc0a2fec'],
      availabilityZones: ['us-east-1a', 'us-east-1b'],
      securityGroupId: 'sg-01d54d29c2a4785f1',
    },
    redis: {
      host: 'master.prod-medwork-api.jrdc0s.use1.cache.amazonaws.com',
      port: 6379,
      team: 'BASIC_TEAM',
    },
  };
}

/** Builds a fresh App + fixtures Stack + ApiPlansStack, merging overrides onto minimalProps. */
function buildStack(overrides: Partial<ApiPlansStackProps> = {}) {
  const app = new cdk.App();
  const fixtures = new cdk.Stack(app, 'Fixtures');
  const props: ApiPlansStackProps = { ...minimalProps(fixtures), ...overrides };
  const stack = new ApiPlansStack(app, 'TestApiPlansStack', props);
  return { app, fixtures, stack };
}

// ── Template inspection helpers ──────────────────────────────────────────

function toArray<T>(v: T | T[]): T[] {
  return Array.isArray(v) ? v : [v];
}

/** The stack has exactly one iam.Role (FunctionRole), so exactly one merged DefaultPolicy. */
function policyStatements(template: Template): Array<Record<string, unknown>> {
  const policies = template.findResources('AWS::IAM::Policy');
  const ids = Object.keys(policies);
  expect(ids.length).toBe(1);
  return (policies[ids[0]] as any).Properties.PolicyDocument.Statement;
}

function findStatement(
  statements: Array<Record<string, unknown>>,
  sid: string,
): Record<string, unknown> | undefined {
  return statements.find((s) => s.Sid === sid);
}

/** Every action granted across all statements whose Resource references arnFragment. */
function actionsForResource(statements: Array<Record<string, unknown>>, arnFragment: string): string[] {
  return statements
    .filter((s) => JSON.stringify(s.Resource).includes(arnFragment))
    .flatMap((s) => toArray(s.Action as string | string[]));
}

function logicalIdOf(template: Template, type: string, props: Record<string, unknown>): string {
  const resources = template.findResources(type, { Properties: props });
  const ids = Object.keys(resources);
  expect(ids.length).toBe(1);
  return ids[0];
}

function functionEnv(template: Template, functionName: string): Record<string, unknown> {
  const fns = template.findResources('AWS::Lambda::Function', {
    Properties: { FunctionName: functionName },
  });
  const ids = Object.keys(fns);
  expect(ids.length).toBe(1);
  return (fns[ids[0]] as any).Properties.Environment.Variables;
}

describe('ApiPlansStack', () => {
  // ── Static / always-present resources ───────────────────────────────
  describe('resources present regardless of optional props', () => {
    const { stack } = buildStack();
    const template = Template.fromStack(stack);

    it('creates VipAdminPlans with customer-managed encryption, PITR, and deletion protection', () => {
      template.hasResourceProperties('AWS::DynamoDB::Table', {
        TableName: 'VipAdminPlans',
        KeySchema: [
          { AttributeName: 'pk', KeyType: 'HASH' },
          { AttributeName: 'sk', KeyType: 'RANGE' },
        ],
        BillingMode: 'PAY_PER_REQUEST',
        SSESpecification: { SSEEnabled: true, SSEType: 'KMS' },
        PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
        DeletionProtectionEnabled: true,
      });
      template.hasResource('AWS::DynamoDB::Table', { DeletionPolicy: 'Retain' });
    });

    it('imports the alerts topic by literal ARN and outputs it', () => {
      template.hasOutput('AlertsTopicArn', {
        Value: `arn:aws:sns:${REGION}:${ACCOUNT}:vip-plans-alerts`,
      });
    });

    it('creates the api-plans log group with 1-year retention, KMS encryption, and RETAIN policy', () => {
      template.hasResourceProperties('AWS::Logs::LogGroup', {
        LogGroupName: '/aws/lambda/vip-admin-ui-api-plans',
        RetentionInDays: 365,
        KmsKeyId: DATA_KEY_ARN,
      });
      template.hasResource('AWS::Logs::LogGroup', { DeletionPolicy: 'Retain' });
    });

    it('creates FunctionRole assumable by lambda.amazonaws.com with the VPC access managed policy', () => {
      template.hasResourceProperties('AWS::IAM::Role', {
        AssumeRolePolicyDocument: {
          Statement: [
            {
              Effect: 'Allow',
              Principal: { Service: 'lambda.amazonaws.com' },
              Action: 'sts:AssumeRole',
            },
          ],
        },
        Description: 'Execution role for api-plans Lambda',
        ManagedPolicyArns: [
          {
            'Fn::Join': [
              '',
              [
                'arn:',
                { Ref: 'AWS::Partition' },
                ':iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole',
              ],
            ],
          },
        ],
      });
    });

    it('grants the role write access to the api-plans log group', () => {
      const stmts = policyStatements(template);
      const logGroupId = logicalIdOf(template, 'AWS::Logs::LogGroup', {
        LogGroupName: '/aws/lambda/vip-admin-ui-api-plans',
      });
      const match = stmts.find(
        (s) =>
          toArray(s.Action as string | string[]).includes('logs:PutLogEvents') &&
          JSON.stringify(s.Resource).includes(logGroupId),
      );
      expect(match).toBeDefined();
    });

    it('grants the exact CustomerProfilesSegments statement', () => {
      const stmt = findStatement(policyStatements(template), 'CustomerProfilesSegments');
      expect(stmt).toBeDefined();
      expect(toArray(stmt!.Action as string | string[])).toEqual([
        'profile:CreateSegmentDefinition',
        'profile:GetSegmentDefinition',
        'profile:DeleteSegmentDefinition',
        'profile:ListSegmentDefinitions',
        'profile:TagResource',
      ]);
      expect(toArray(stmt!.Resource as string | string[])).toEqual([
        `arn:aws:profile:${REGION}:${ACCOUNT}:domains/${PROFILES_DOMAIN_NAME}`,
        `arn:aws:profile:${REGION}:${ACCOUNT}:domains/${PROFILES_DOMAIN_NAME}/*`,
      ]);
    });

    it('grants the exact ConnectCampaignsV2 statement', () => {
      const stmt = findStatement(policyStatements(template), 'ConnectCampaignsV2');
      expect(stmt).toBeDefined();
      expect(toArray(stmt!.Action as string | string[])).toEqual([
        'connect-campaigns:CreateCampaign',
        'connect-campaigns:DeleteCampaign',
        'connect-campaigns:StartCampaign',
        'connect-campaigns:StopCampaign',
        'connect-campaigns:GetCampaignState',
        'connect-campaigns:DescribeCampaign',
        'connect-campaigns:TagResource',
      ]);
      expect(toArray(stmt!.Resource as string | string[])).toEqual([
        `arn:aws:connect-campaigns:${REGION}:${ACCOUNT}:campaign/*`,
      ]);
    });

    it('grants the exact ConnectReadInstanceResources statement', () => {
      const stmt = findStatement(policyStatements(template), 'ConnectReadInstanceResources');
      expect(stmt).toBeDefined();
      expect(toArray(stmt!.Action as string | string[])).toEqual([
        'connect:ListQueues',
        'connect:ListContactFlows',
        'connect:ListContactFlowVersions',
        'connect:DescribeContactFlow',
        'connect:DescribeQueue',
        'connect:DescribeInstance',
        'connect:CreateContactFlow',
        'connect:TagResource',
      ]);
      expect(toArray(stmt!.Resource as string | string[])).toEqual([
        `arn:aws:connect:${REGION}:${ACCOUNT}:instance/${CONNECT_INSTANCE_ID}`,
        `arn:aws:connect:${REGION}:${ACCOUNT}:instance/${CONNECT_INSTANCE_ID}/*`,
      ]);
    });

    it('grants the exact ConnectPhoneNumberV2 statement', () => {
      const stmt = findStatement(policyStatements(template), 'ConnectPhoneNumberV2');
      expect(stmt).toBeDefined();
      expect(toArray(stmt!.Action as string | string[])).toEqual([
        'connect:ListPhoneNumbersV2',
        'connect:DescribePhoneNumber',
      ]);
      expect(toArray(stmt!.Resource as string | string[])).toEqual([
        `arn:aws:connect:${REGION}:${ACCOUNT}:phone-number/*`,
        `arn:aws:connect:${REGION}:${ACCOUNT}:instance/${CONNECT_INSTANCE_ID}`,
      ]);
    });

    it('grants the exact AuditWrite statement scoped to the imported admin audit table', () => {
      const stmt = findStatement(policyStatements(template), 'AuditWrite');
      expect(stmt).toBeDefined();
      expect(toArray(stmt!.Action as string | string[])).toEqual(['dynamodb:PutItem']);
      expect(toArray(stmt!.Resource as string | string[])).toEqual([ADMIN_AUDIT_TABLE_ARN]);
    });

    it('grants the exact EventBridgeRules statement scoped to vip-plan-*/vip-sched-*', () => {
      const stmt = findStatement(policyStatements(template), 'EventBridgeRules');
      expect(stmt).toBeDefined();
      expect(toArray(stmt!.Action as string | string[])).toEqual([
        'events:PutRule',
        'events:PutTargets',
        'events:RemoveTargets',
        'events:DeleteRule',
        'events:DescribeRule',
      ]);
      expect(toArray(stmt!.Resource as string | string[])).toEqual([
        `arn:aws:events:${REGION}:${ACCOUNT}:rule/vip-plan-*`,
        `arn:aws:events:${REGION}:${ACCOUNT}:rule/vip-sched-*`,
      ]);
    });

    it('grants the exact LambdaSelfPermission statement scoped to its own function', () => {
      const stmt = findStatement(policyStatements(template), 'LambdaSelfPermission');
      expect(stmt).toBeDefined();
      expect(toArray(stmt!.Action as string | string[])).toEqual([
        'lambda:AddPermission',
        'lambda:RemovePermission',
        'lambda:GetPolicy',
      ]);
      expect(toArray(stmt!.Resource as string | string[])).toEqual([
        `arn:aws:lambda:${REGION}:${ACCOUNT}:function:vip-admin-ui-api-plans`,
      ]);
    });

    it('grants StsGetCallerIdentity and CloudWatchPutMetric (namespace-scoped) statements', () => {
      const stmts = policyStatements(template);
      const sts = findStatement(stmts, 'StsGetCallerIdentity');
      expect(sts).toBeDefined();
      expect(toArray(sts!.Action as string | string[])).toEqual(['sts:GetCallerIdentity']);
      expect(toArray(sts!.Resource as string | string[])).toEqual(['*']);

      const metric = findStatement(stmts, 'CloudWatchPutMetric');
      expect(metric).toBeDefined();
      expect(toArray(metric!.Action as string | string[])).toEqual(['cloudwatch:PutMetricData']);
      expect(toArray(metric!.Resource as string | string[])).toEqual(['*']);
      expect(metric!.Condition).toEqual({
        StringEquals: { 'cloudwatch:namespace': ['VIPPlans', 'VipConnect/ProgressiveDialer'] },
      });
    });

    it('grants EumSmsDescribePhoneNumbers and the two contact-artifact S3 statements', () => {
      const stmts = policyStatements(template);
      const sms = findStatement(stmts, 'EumSmsDescribePhoneNumbers');
      expect(sms).toBeDefined();
      expect(toArray(sms!.Action as string | string[])).toEqual(['sms-voice:DescribePhoneNumbers']);
      expect(toArray(sms!.Resource as string | string[])).toEqual(['*']);

      const recordings = findStatement(stmts, 'ContactArtifactsRecordings');
      expect(recordings).toBeDefined();
      expect(toArray(recordings!.Action as string | string[])).toEqual(['s3:GetObject', 's3:ListBucket']);
      expect(toArray(recordings!.Resource as string | string[])).toEqual([
        'arn:aws:s3:::amazon-connect-c5a2158755eb',
        'arn:aws:s3:::amazon-connect-c5a2158755eb/*',
      ]);

      const voicemail = findStatement(stmts, 'ContactArtifactsVoicemail');
      expect(voicemail).toBeDefined();
      expect(toArray(voicemail!.Action as string | string[])).toEqual(['s3:GetObject', 's3:ListBucket']);
      expect(toArray(voicemail!.Resource as string | string[])).toEqual([
        'arn:aws:s3:::vmx3-recordings-vipmedicalgroup',
        'arn:aws:s3:::vmx3-recordings-vipmedicalgroup/*',
      ]);
    });

    it('grants the exact ContactArtifactsDescribeContact statement', () => {
      const stmt = findStatement(policyStatements(template), 'ContactArtifactsDescribeContact');
      expect(stmt).toBeDefined();
      expect(toArray(stmt!.Action as string | string[])).toEqual(['connect:DescribeContact']);
      expect(toArray(stmt!.Resource as string | string[])).toEqual([
        `arn:aws:connect:${REGION}:${ACCOUNT}:instance/${CONNECT_INSTANCE_ID}`,
        `arn:aws:connect:${REGION}:${ACCOUNT}:instance/${CONNECT_INSTANCE_ID}/*`,
      ]);
    });

    it('grants read-write access to the PlansTable itself', () => {
      const plansTableId = logicalIdOf(template, 'AWS::DynamoDB::Table', { TableName: 'VipAdminPlans' });
      const actions = actionsForResource(policyStatements(template), plansTableId);
      expect(actions).toEqual(expect.arrayContaining(['dynamodb:GetItem', 'dynamodb:PutItem']));
    });

    it('grants read access to the imported VipLocationMapping table unconditionally', () => {
      const actions = actionsForResource(policyStatements(template), 'VipLocationMapping');
      expect(actions).toEqual(expect.arrayContaining(['dynamodb:GetItem', 'dynamodb:Query']));
      expect(actions).not.toContain('dynamodb:PutItem');
    });

    it('grants encrypt/decrypt on the imported data CMK (no key policy available, so scoped to *)', () => {
      const stmts = policyStatements(template);
      const match = stmts.find(
        (s) =>
          !s.Sid &&
          toArray(s.Action as string | string[]).includes('kms:Decrypt') &&
          toArray(s.Action as string | string[]).includes('kms:GenerateDataKey*'),
      );
      expect(match).toBeDefined();
      expect(toArray(match!.Resource as string | string[])).toEqual(['*']);
    });

    it('grants SNS publish on the alerts topic', () => {
      const stmts = policyStatements(template);
      const match = stmts.find(
        (s) => s.Action === 'sns:Publish' && s.Resource === `arn:aws:sns:${REGION}:${ACCOUNT}:vip-plans-alerts`,
      );
      expect(match).toBeDefined();
    });

    it('creates exactly one Lambda::Function (guard not created without a stream ARN)', () => {
      template.resourceCountIs('AWS::Lambda::Function', 1);
    });

    it('creates no EventSourceMapping when locationMappingStreamArn is omitted', () => {
      template.resourceCountIs('AWS::Lambda::EventSourceMapping', 0);
    });

    it('configures FunctionPlans with the documented runtime/handler/memory/timeout/concurrency', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'vip-admin-ui-api-plans',
        Runtime: 'python3.12',
        Handler: 'handler.lambda_handler',
        MemorySize: 1024,
        Timeout: 300,
        ReservedConcurrentExecutions: 5,
      });
    });

    it('wires FunctionPlans DeadLetterConfig to the stack DLQ by reference', () => {
      const dlqId = logicalIdOf(template, 'AWS::SQS::Queue', { QueueName: 'vip-admin-ui-api-plans-dlq' });
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'vip-admin-ui-api-plans',
        DeadLetterConfig: { TargetArn: { 'Fn::GetAtt': [dlqId, 'Arn'] } },
      });
    });

    it('creates the DLQ as a 14-day-retention KMS-encrypted queue', () => {
      template.hasResourceProperties('AWS::SQS::Queue', {
        QueueName: 'vip-admin-ui-api-plans-dlq',
        KmsMasterKeyId: DATA_KEY_ARN,
        MessageRetentionPeriod: 1209600,
      });
    });

    it('places FunctionPlans in the given VPC subnets and security group', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'vip-admin-ui-api-plans',
        VpcConfig: {
          SubnetIds: ['subnet-06c7669b5e3e0e814', 'subnet-088367ac9fc0a2fec'],
          SecurityGroupIds: ['sg-01d54d29c2a4785f1'],
        },
      });
    });

    it('sets exactly the minimal (no optional props) environment variables, nothing extra', () => {
      const plansTableId = logicalIdOf(template, 'AWS::DynamoDB::Table', { TableName: 'VipAdminPlans' });
      expect(functionEnv(template, 'vip-admin-ui-api-plans')).toEqual({
        CONNECT_INSTANCE_ID,
        RECORDINGS_BUCKET: 'amazon-connect-c5a2158755eb',
        VOICEMAIL_BUCKET: 'vmx3-recordings-vipmedicalgroup',
        PROFILES_DOMAIN_NAME,
        PLANS_TABLE_NAME: { Ref: plansTableId },
        AUDIT_TABLE: 'AdminAuditLog',
        DATA_KEY_ARN,
        REDIS_HOST: 'master.prod-medwork-api.jrdc0s.use1.cache.amazonaws.com',
        REDIS_PORT: '6379',
        TEAM: 'BASIC_TEAM',
        SNS_ALERTS_TOPIC_ARN: `arn:aws:sns:${REGION}:${ACCOUNT}:vip-plans-alerts`,
        LOG_LEVEL: 'INFO',
        POWERTOOLS_SERVICE_NAME: 'api-plans',
        LAMBDA_FUNCTION_ARN: `arn:aws:lambda:${REGION}:${ACCOUNT}:function:vip-admin-ui-api-plans`,
      });
    });

    it('does NOT apply any Checkov skip metadata to FunctionPlans', () => {
      // NOTE — discrepancy vs. the task brief: only the guard Lambda gets
      // skipCheckovChecks() in the actual source (api-plans-stack.ts L624);
      // FunctionPlans (this.lambdaFunction) never calls it. Documenting the
      // real, current behavior rather than the assumption.
      const fns = template.findResources('AWS::Lambda::Function', {
        Properties: { FunctionName: 'vip-admin-ui-api-plans' },
      });
      const id = Object.keys(fns)[0];
      expect((fns[id] as any).Metadata).toBeUndefined();
    });

    it('emits FunctionArn and PlansTableArn outputs', () => {
      const fnId = logicalIdOf(template, 'AWS::Lambda::Function', { FunctionName: 'vip-admin-ui-api-plans' });
      const tableId = logicalIdOf(template, 'AWS::DynamoDB::Table', { TableName: 'VipAdminPlans' });
      template.hasOutput('FunctionArn', { Value: { 'Fn::GetAtt': [fnId, 'Arn'] } });
      template.hasOutput('PlansTableArn', { Value: { 'Fn::GetAtt': [tableId, 'Arn'] } });
    });

    it('imports the campaign exporter function by name and outputs its ARN', () => {
      // .functionName on an imported-by-name Function resolves to a CFN
      // intrinsic (Fn::Select/Fn::Split over the ARN) rather than the
      // literal string, even though the literal was the import key — that's
      // how lambda.Function.fromFunctionName's IFunction is implemented, not
      // a property of this stack. Assert the ARN output (a literal-derived
      // Fn::Join) instead of the token-wrapped name.
      template.hasOutput('ExporterFunctionArn', {
        Value: {
          'Fn::Join': [
            '',
            [
              'arn:',
              { Ref: 'AWS::Partition' },
              `:lambda:${REGION}:${ACCOUNT}:function:vip-admin-ui-campaign-exporter`,
            ],
          ],
        },
      });
    });

    it('exposes plansTable and lambdaFunction as public readonly members referencing the synthesized resources', () => {
      const plansTableId = logicalIdOf(template, 'AWS::DynamoDB::Table', { TableName: 'VipAdminPlans' });
      const fnId = logicalIdOf(template, 'AWS::Lambda::Function', { FunctionName: 'vip-admin-ui-api-plans' });
      expect(stack.resolve(stack.plansTable.tableName)).toEqual({ Ref: plansTableId });
      expect(stack.resolve(stack.lambdaFunction.functionName)).toEqual({ Ref: fnId });
    });
  });

  // ── permissionsBoundaryName ──────────────────────────────────────────
  describe('permissionsBoundaryName', () => {
    it('does not create a PermissionsBoundary construct when omitted', () => {
      const { stack } = buildStack();
      expect(stack.node.tryFindChild('PermissionsBoundary')).toBeUndefined();
    });

    it('resolves the boundary managed policy and synthesizes without throwing when provided', () => {
      const { stack } = buildStack({ permissionsBoundaryName: 'TestBoundary' });
      expect(() => Template.fromStack(stack)).not.toThrow();
    });
  });

  // ── redis.passwordSecretArn ──────────────────────────────────────────
  describe('redis.passwordSecretArn', () => {
    it('does not grant secretsmanager access or inject REDIS_PASSWORD_SECRET_ARN when absent', () => {
      const { stack } = buildStack();
      const template = Template.fromStack(stack);
      expect(findStatement(policyStatements(template), 'RedisPasswordSecret')).toBeUndefined();
      expect(functionEnv(template, 'vip-admin-ui-api-plans').REDIS_PASSWORD_SECRET_ARN).toBeUndefined();
    });

    it('grants secretsmanager:GetSecretValue and injects REDIS_PASSWORD_SECRET_ARN when present', () => {
      const { stack } = buildStack({
        redis: {
          host: 'master.prod-medwork-api.jrdc0s.use1.cache.amazonaws.com',
          port: 6379,
          team: 'BASIC_TEAM',
          passwordSecretArn: REDIS_SECRET_ARN,
        },
      });
      const template = Template.fromStack(stack);
      const stmt = findStatement(policyStatements(template), 'RedisPasswordSecret');
      expect(stmt).toBeDefined();
      expect(toArray(stmt!.Action as string | string[])).toEqual(['secretsmanager:GetSecretValue']);
      expect(toArray(stmt!.Resource as string | string[])).toEqual([REDIS_SECRET_ARN]);
      expect(functionEnv(template, 'vip-admin-ui-api-plans').REDIS_PASSWORD_SECRET_ARN).toBe(REDIS_SECRET_ARN);
    });
  });

  // ── progressiveCampaignQueueTable ────────────────────────────────────
  describe('progressiveCampaignQueueTable', () => {
    it('grants no access and injects no env var when absent', () => {
      const { stack } = buildStack();
      const template = Template.fromStack(stack);
      expect(actionsForResource(policyStatements(template), 'VipProgressiveCampaignQueue')).toEqual([]);
      expect(functionEnv(template, 'vip-admin-ui-api-plans').CAMPAIGN_QUEUE_TABLE_BRANDED).toBeUndefined();
    });

    it('grants read-write access and injects CAMPAIGN_QUEUE_TABLE_BRANDED when present', () => {
      const { fixtures, app } = (() => {
        const app = new cdk.App();
        const fixtures = new cdk.Stack(app, 'Fixtures');
        return { app, fixtures };
      })();
      const table = fixtureTable(fixtures, 'ProgressiveQueueTable', PROGRESSIVE_QUEUE_ARN);
      const stack = new ApiPlansStack(app, 'TestApiPlansStack', {
        ...minimalProps(fixtures),
        progressiveCampaignQueueTable: table,
      });
      const template = Template.fromStack(stack);
      const actions = actionsForResource(policyStatements(template), 'VipProgressiveCampaignQueue');
      expect(actions).toEqual(expect.arrayContaining(['dynamodb:GetItem', 'dynamodb:PutItem']));
      expect(functionEnv(template, 'vip-admin-ui-api-plans').CAMPAIGN_QUEUE_TABLE_BRANDED).toBe(
        'VipProgressiveCampaignQueue',
      );
    });
  });

  // ── activeBrandedCampaignsTable ──────────────────────────────────────
  describe('activeBrandedCampaignsTable', () => {
    it('grants no access and injects no env var when absent', () => {
      const { stack } = buildStack();
      const template = Template.fromStack(stack);
      expect(actionsForResource(policyStatements(template), 'VipActiveBrandedCampaigns')).toEqual([]);
      expect(functionEnv(template, 'vip-admin-ui-api-plans').ACTIVE_BRANDED_CAMPAIGNS_TABLE).toBeUndefined();
    });

    it('grants read-write access and injects ACTIVE_BRANDED_CAMPAIGNS_TABLE when present', () => {
      const app = new cdk.App();
      const fixtures = new cdk.Stack(app, 'Fixtures');
      const table = fixtureTable(fixtures, 'ActiveBrandedTable', ACTIVE_BRANDED_ARN);
      const stack = new ApiPlansStack(app, 'TestApiPlansStack', {
        ...minimalProps(fixtures),
        activeBrandedCampaignsTable: table,
      });
      const template = Template.fromStack(stack);
      const actions = actionsForResource(policyStatements(template), 'VipActiveBrandedCampaigns');
      expect(actions).toEqual(expect.arrayContaining(['dynamodb:GetItem', 'dynamodb:PutItem']));
      expect(functionEnv(template, 'vip-admin-ui-api-plans').ACTIVE_BRANDED_CAMPAIGNS_TABLE).toBe(
        'VipActiveBrandedCampaigns',
      );
    });
  });

  // ── brandedRunSummaryTable ───────────────────────────────────────────
  describe('brandedRunSummaryTable', () => {
    it('grants no access and injects no env var when absent', () => {
      const { stack } = buildStack();
      const template = Template.fromStack(stack);
      expect(actionsForResource(policyStatements(template), 'VipBrandedRunSummary')).toEqual([]);
      expect(functionEnv(template, 'vip-admin-ui-api-plans').BRANDED_RUN_SUMMARY_TABLE).toBeUndefined();
    });

    it('grants read-write access and injects BRANDED_RUN_SUMMARY_TABLE when present', () => {
      const app = new cdk.App();
      const fixtures = new cdk.Stack(app, 'Fixtures');
      const table = fixtureTable(fixtures, 'BrandedRunSummaryTable', BRANDED_RUN_SUMMARY_ARN);
      const stack = new ApiPlansStack(app, 'TestApiPlansStack', {
        ...minimalProps(fixtures),
        brandedRunSummaryTable: table,
      });
      const template = Template.fromStack(stack);
      const actions = actionsForResource(policyStatements(template), 'VipBrandedRunSummary');
      expect(actions).toEqual(expect.arrayContaining(['dynamodb:GetItem', 'dynamodb:PutItem']));
      expect(functionEnv(template, 'vip-admin-ui-api-plans').BRANDED_RUN_SUMMARY_TABLE).toBe(
        'VipBrandedRunSummary',
      );
    });
  });

  // ── brandedCampaignMetricsTable ──────────────────────────────────────
  describe('brandedCampaignMetricsTable', () => {
    it('grants no access and injects no env var when absent', () => {
      const { stack } = buildStack();
      const template = Template.fromStack(stack);
      expect(actionsForResource(policyStatements(template), 'VipBrandedCampaignMetrics')).toEqual([]);
      expect(functionEnv(template, 'vip-admin-ui-api-plans').BRANDED_CAMPAIGN_METRICS_TABLE).toBeUndefined();
    });

    it('grants read-write access and injects BRANDED_CAMPAIGN_METRICS_TABLE when present', () => {
      const app = new cdk.App();
      const fixtures = new cdk.Stack(app, 'Fixtures');
      const table = fixtureTable(fixtures, 'BrandedMetricsTable', BRANDED_METRICS_ARN);
      const stack = new ApiPlansStack(app, 'TestApiPlansStack', {
        ...minimalProps(fixtures),
        brandedCampaignMetricsTable: table,
      });
      const template = Template.fromStack(stack);
      const actions = actionsForResource(policyStatements(template), 'VipBrandedCampaignMetrics');
      expect(actions).toEqual(expect.arrayContaining(['dynamodb:GetItem', 'dynamodb:PutItem']));
      expect(functionEnv(template, 'vip-admin-ui-api-plans').BRANDED_CAMPAIGN_METRICS_TABLE).toBe(
        'VipBrandedCampaignMetrics',
      );
    });
  });

  // ── agentSnapshotTable ────────────────────────────────────────────────
  describe('agentSnapshotTable', () => {
    it('grants no access and injects no env var when absent', () => {
      const { stack } = buildStack();
      const template = Template.fromStack(stack);
      expect(actionsForResource(policyStatements(template), 'VipAgentSnapshot')).toEqual([]);
      expect(functionEnv(template, 'vip-admin-ui-api-plans').AGENT_SNAPSHOT_TABLE).toBeUndefined();
    });

    it('grants read-write access and injects AGENT_SNAPSHOT_TABLE when present', () => {
      const app = new cdk.App();
      const fixtures = new cdk.Stack(app, 'Fixtures');
      const table = fixtureTable(fixtures, 'AgentSnapshotTable', AGENT_SNAPSHOT_ARN);
      const stack = new ApiPlansStack(app, 'TestApiPlansStack', {
        ...minimalProps(fixtures),
        agentSnapshotTable: table,
      });
      const template = Template.fromStack(stack);
      const actions = actionsForResource(policyStatements(template), 'VipAgentSnapshot');
      expect(actions).toEqual(expect.arrayContaining(['dynamodb:GetItem', 'dynamodb:PutItem']));
      expect(functionEnv(template, 'vip-admin-ui-api-plans').AGENT_SNAPSHOT_TABLE).toBe('VipAgentSnapshot');
    });
  });

  // ── progressiveDialerSeederArn ───────────────────────────────────────
  describe('progressiveDialerSeederArn', () => {
    it('does not grant invoke access or inject the env var when absent', () => {
      const { stack } = buildStack();
      const template = Template.fromStack(stack);
      expect(findStatement(policyStatements(template), 'InvokeProgressiveDialerSeeder')).toBeUndefined();
      expect(functionEnv(template, 'vip-admin-ui-api-plans').PROGRESSIVE_DIALER_SEEDER_ARN).toBeUndefined();
    });

    it('grants lambda:InvokeFunction and injects PROGRESSIVE_DIALER_SEEDER_ARN when present', () => {
      const { stack } = buildStack({ progressiveDialerSeederArn: SEEDER_ARN });
      const template = Template.fromStack(stack);
      const stmt = findStatement(policyStatements(template), 'InvokeProgressiveDialerSeeder');
      expect(stmt).toBeDefined();
      expect(toArray(stmt!.Action as string | string[])).toEqual(['lambda:InvokeFunction']);
      expect(toArray(stmt!.Resource as string | string[])).toEqual([SEEDER_ARN]);
      expect(functionEnv(template, 'vip-admin-ui-api-plans').PROGRESSIVE_DIALER_SEEDER_ARN).toBe(SEEDER_ARN);
    });
  });

  // ── progressiveDialerDataKeyArn ──────────────────────────────────────
  describe('progressiveDialerDataKeyArn', () => {
    it('does not grant KMS access when absent', () => {
      const { stack } = buildStack();
      const template = Template.fromStack(stack);
      expect(findStatement(policyStatements(template), 'ProgressiveDialerKmsAccess')).toBeUndefined();
    });

    it('grants kms:Decrypt/GenerateDataKey when present (no env var — this prop has no addEnvironment call)', () => {
      const { stack } = buildStack({ progressiveDialerDataKeyArn: PROGRESSIVE_DIALER_KEY_ARN });
      const template = Template.fromStack(stack);
      const stmt = findStatement(policyStatements(template), 'ProgressiveDialerKmsAccess');
      expect(stmt).toBeDefined();
      expect(toArray(stmt!.Action as string | string[])).toEqual(['kms:Decrypt', 'kms:GenerateDataKey']);
      expect(toArray(stmt!.Resource as string | string[])).toEqual([PROGRESSIVE_DIALER_KEY_ARN]);
    });
  });

  // ── smsCampaignQueueTable (read-only grant, unlike the others) ───────
  describe('smsCampaignQueueTable', () => {
    it('grants no access and injects no env var when absent', () => {
      const { stack } = buildStack();
      const template = Template.fromStack(stack);
      expect(actionsForResource(policyStatements(template), 'VipSmsCampaignQueue')).toEqual([]);
      expect(functionEnv(template, 'vip-admin-ui-api-plans').SMS_CAMPAIGN_QUEUE_TABLE).toBeUndefined();
    });

    it('grants READ-ONLY access (no PutItem) and injects SMS_CAMPAIGN_QUEUE_TABLE when present', () => {
      const app = new cdk.App();
      const fixtures = new cdk.Stack(app, 'Fixtures');
      const table = fixtureTable(fixtures, 'SmsQueueTable', SMS_QUEUE_ARN);
      const stack = new ApiPlansStack(app, 'TestApiPlansStack', {
        ...minimalProps(fixtures),
        smsCampaignQueueTable: table,
      });
      const template = Template.fromStack(stack);
      const actions = actionsForResource(policyStatements(template), 'VipSmsCampaignQueue');
      expect(actions).toContain('dynamodb:GetItem');
      expect(actions).not.toContain('dynamodb:PutItem');
      expect(functionEnv(template, 'vip-admin-ui-api-plans').SMS_CAMPAIGN_QUEUE_TABLE).toBe(
        'VipSmsCampaignQueue',
      );
    });
  });

  // ── smsRunsTable ──────────────────────────────────────────────────────
  describe('smsRunsTable', () => {
    it('grants no access and injects no env var when absent', () => {
      const { stack } = buildStack();
      const template = Template.fromStack(stack);
      expect(actionsForResource(policyStatements(template), 'VipSmsCampaignRuns')).toEqual([]);
      expect(functionEnv(template, 'vip-admin-ui-api-plans').SMS_CAMPAIGN_RUNS_TABLE).toBeUndefined();
    });

    it('grants read-write access and injects SMS_CAMPAIGN_RUNS_TABLE when present', () => {
      const app = new cdk.App();
      const fixtures = new cdk.Stack(app, 'Fixtures');
      const table = fixtureTable(fixtures, 'SmsRunsTable', SMS_RUNS_ARN);
      const stack = new ApiPlansStack(app, 'TestApiPlansStack', {
        ...minimalProps(fixtures),
        smsRunsTable: table,
      });
      const template = Template.fromStack(stack);
      const actions = actionsForResource(policyStatements(template), 'VipSmsCampaignRuns');
      expect(actions).toEqual(expect.arrayContaining(['dynamodb:GetItem', 'dynamodb:PutItem']));
      expect(functionEnv(template, 'vip-admin-ui-api-plans').SMS_CAMPAIGN_RUNS_TABLE).toBe(
        'VipSmsCampaignRuns',
      );
    });
  });

  // ── smsSenderFunctionArn ──────────────────────────────────────────────
  describe('smsSenderFunctionArn', () => {
    it('does not grant invoke access or inject the env var when absent', () => {
      const { stack } = buildStack();
      const template = Template.fromStack(stack);
      expect(findStatement(policyStatements(template), 'InvokeSmsSender')).toBeUndefined();
      expect(functionEnv(template, 'vip-admin-ui-api-plans').SMS_SENDER_FUNCTION_ARN).toBeUndefined();
    });

    it('grants lambda:InvokeFunction and injects SMS_SENDER_FUNCTION_ARN when present', () => {
      const { stack } = buildStack({ smsSenderFunctionArn: SMS_SENDER_ARN });
      const template = Template.fromStack(stack);
      const stmt = findStatement(policyStatements(template), 'InvokeSmsSender');
      expect(stmt).toBeDefined();
      expect(toArray(stmt!.Action as string | string[])).toEqual(['lambda:InvokeFunction']);
      expect(toArray(stmt!.Resource as string | string[])).toEqual([SMS_SENDER_ARN]);
      expect(functionEnv(template, 'vip-admin-ui-api-plans').SMS_SENDER_FUNCTION_ARN).toBe(SMS_SENDER_ARN);
    });
  });

  // ── locationMappingStreamArn — the big conditional block ─────────────
  describe('locationMappingStreamArn', () => {
    it('creates no guard Lambda and no EventSourceMapping when absent', () => {
      const { stack } = buildStack();
      const template = Template.fromStack(stack);
      template.resourceCountIs('AWS::Lambda::Function', 1);
      template.resourceCountIs('AWS::Lambda::EventSourceMapping', 0);
    });

    describe('when present', () => {
      function buildWithGuard() {
        return buildStack({ locationMappingStreamArn: LOCATION_MAPPING_STREAM_ARN });
      }

      it('creates exactly 2 Lambda::Functions (FunctionPlans + the guard)', () => {
        const template = Template.fromStack(buildWithGuard().stack);
        template.resourceCountIs('AWS::Lambda::Function', 2);
      });

      it('configures the guard function with the documented name/runtime/handler/memory/timeout/concurrency', () => {
        const template = Template.fromStack(buildWithGuard().stack);
        template.hasResourceProperties('AWS::Lambda::Function', {
          FunctionName: 'vip-location-onboarding-guard',
          Runtime: 'python3.12',
          Handler: 'location_onboarding_guard.lambda_handler',
          MemorySize: 256,
          Timeout: 30,
          ReservedConcurrentExecutions: 1,
          Role: 'arn:aws:iam::165505826690:role/vip-location-onboarding-guard-role',
        });
      });

      it('sets exactly the guard function environment variables, no more no less', () => {
        const template = Template.fromStack(buildWithGuard().stack);
        const fns = template.findResources('AWS::Lambda::Function', {
          Properties: { FunctionName: 'vip-location-onboarding-guard' },
        });
        const id = Object.keys(fns)[0];
        expect((fns[id] as any).Properties.Environment.Variables).toEqual({
          SNS_ALERTS_TOPIC_ARN: `arn:aws:sns:${REGION}:${ACCOUNT}:vip-plans-alerts`,
          LOCATION_MAPPING_TABLE: 'VipLocationMapping',
          LOG_LEVEL: 'INFO',
        });
      });

      it('omits function-level DeadLetterConfig on the guard (failure path is the ESM onFailure destination)', () => {
        const template = Template.fromStack(buildWithGuard().stack);
        const fns = template.findResources('AWS::Lambda::Function', {
          Properties: { FunctionName: 'vip-location-onboarding-guard' },
        });
        const id = Object.keys(fns)[0];
        expect((fns[id] as any).Properties.DeadLetterConfig).toBeUndefined();
      });

      it('applies exactly VPC_SKIP (CKV_AWS_117) and a guard-specific CKV_AWS_116 Checkov skip', () => {
        const template = Template.fromStack(buildWithGuard().stack);
        const fns = template.findResources('AWS::Lambda::Function', {
          Properties: { FunctionName: 'vip-location-onboarding-guard' },
        });
        const id = Object.keys(fns)[0];
        const skip = (fns[id] as any).Metadata.checkov.skip;
        expect(skip).toHaveLength(2);
        expect(skip[0].id).toBe('CKV_AWS_117');
        expect(skip[0].comment).toContain('Not internet-reachable regardless of VPC config');
        expect(skip[1].id).toBe('CKV_AWS_116');
        expect(skip[1].comment).toContain('stream-triggered (DynamoDB Streams)');
      });

      it('wires a DynamoEventSource on VipLocationMapping filtered to INSERT, with onFailure -> the shared DLQ', () => {
        const template = Template.fromStack(buildWithGuard().stack);
        const dlqId = logicalIdOf(template, 'AWS::SQS::Queue', { QueueName: 'vip-admin-ui-api-plans-dlq' });
        template.hasResourceProperties('AWS::Lambda::EventSourceMapping', {
          EventSourceArn: LOCATION_MAPPING_STREAM_ARN,
          StartingPosition: 'LATEST',
          BatchSize: 10,
          MaximumRetryAttempts: 2,
          DestinationConfig: { OnFailure: { Destination: { 'Fn::GetAtt': [dlqId, 'Arn'] } } },
          FilterCriteria: { Filters: [{ Pattern: '{"eventName":["INSERT"]}' }] },
        });
      });
    });
  });

  // ── Integration: all optional props set simultaneously ───────────────
  it('coexists correctly when every optional prop is provided at once', () => {
    const app = new cdk.App();
    const fixtures = new cdk.Stack(app, 'Fixtures');
    const stack = new ApiPlansStack(app, 'TestApiPlansStack', {
      ...minimalProps(fixtures),
      permissionsBoundaryName: 'TestBoundary',
      redis: {
        host: 'master.prod-medwork-api.jrdc0s.use1.cache.amazonaws.com',
        port: 6379,
        team: 'BASIC_TEAM',
        passwordSecretArn: REDIS_SECRET_ARN,
      },
      ...optionalFixtures(fixtures),
      progressiveDialerSeederArn: SEEDER_ARN,
      progressiveDialerDataKeyArn: PROGRESSIVE_DIALER_KEY_ARN,
      smsSenderFunctionArn: SMS_SENDER_ARN,
      locationMappingStreamArn: LOCATION_MAPPING_STREAM_ARN,
    });
    const template = Template.fromStack(stack);
    template.resourceCountIs('AWS::Lambda::Function', 2);
    template.resourceCountIs('AWS::Lambda::EventSourceMapping', 1);
    const env = functionEnv(template, 'vip-admin-ui-api-plans');
    expect(env).toMatchObject({
      REDIS_PASSWORD_SECRET_ARN: REDIS_SECRET_ARN,
      CAMPAIGN_QUEUE_TABLE_BRANDED: 'VipProgressiveCampaignQueue',
      ACTIVE_BRANDED_CAMPAIGNS_TABLE: 'VipActiveBrandedCampaigns',
      BRANDED_RUN_SUMMARY_TABLE: 'VipBrandedRunSummary',
      BRANDED_CAMPAIGN_METRICS_TABLE: 'VipBrandedCampaignMetrics',
      AGENT_SNAPSHOT_TABLE: 'VipAgentSnapshot',
      PROGRESSIVE_DIALER_SEEDER_ARN: SEEDER_ARN,
      SMS_CAMPAIGN_QUEUE_TABLE: 'VipSmsCampaignQueue',
      SMS_CAMPAIGN_RUNS_TABLE: 'VipSmsCampaignRuns',
      SMS_SENDER_FUNCTION_ARN: SMS_SENDER_ARN,
    });
  });
});
