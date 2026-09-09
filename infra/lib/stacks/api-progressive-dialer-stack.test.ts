import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

// See api-plans-stack.test.ts for the rationale: buildSharedLayer's real
// implementation shells out to `pip install`/Docker during asset bundling,
// which happens synchronously when the Lambda Function construct is built.
// Mocked to an imported (zero-bundling) LayerVersion so this suite stays
// fast, deterministic, and independent of network/Docker availability.
jest.mock('../utils/shared-layer', () => ({
  buildSharedLayer: jest.fn((scope: import('constructs').Construct, id = 'SharedLayer') =>
    require('aws-cdk-lib/aws-lambda').LayerVersion.fromLayerVersionArn(
      scope,
      id,
      'arn:aws:lambda:us-east-1:165505826690:layer:mock-shared-layer:1',
    ),
  ),
}));

import { ApiProgressiveDialerStack, ApiProgressiveDialerStackProps } from './api-progressive-dialer-stack';

const ACCOUNT = '165505826690';
const REGION = 'us-east-1';
const CONNECT_INSTANCE_ID = '6b3f17ba-68a4-472a-9b20-db1991507009';
const PROFILES_DOMAIN_NAME = 'amazon-connect-vipmedicalgroup';
const DATA_KEY_ARN = `arn:aws:kms:${REGION}:${ACCOUNT}:key/df585888-2f49-4de0-9cba-14803fda63f0`;
const AGENT_EVENT_STREAM_ARN = `arn:aws:kinesis:${REGION}:${ACCOUNT}:stream/vip-use1-datastream`;
const FIRSTORION_SECRET_ARN = `arn:aws:secretsmanager:${REGION}:${ACCOUNT}:secret:vip/firstorion/credentials-abc123`;
const CAMPAIGN_QUEUE_STREAM_ARN =
  `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/VipProgressiveCampaignQueue/stream/2024-01-01T00:00:00.000`;
const ALLOWED_QUEUE_ARNS = `arn:aws:connect:${REGION}:${ACCOUNT}:instance/${CONNECT_INSTANCE_ID}/queue/some-queue-id`;

function minimalProps(): ApiProgressiveDialerStackProps {
  return {
    env: { account: ACCOUNT, region: REGION },
    dataKeyArn: DATA_KEY_ARN,
    connectInstanceId: CONNECT_INSTANCE_ID,
    agentEventStreamArn: AGENT_EVENT_STREAM_ARN,
    firstOrionSecretArn: FIRSTORION_SECRET_ARN,
    profilesDomainName: PROFILES_DOMAIN_NAME,
  };
}

function buildStack(overrides: Partial<ApiProgressiveDialerStackProps> = {}) {
  const app = new cdk.App();
  const props: ApiProgressiveDialerStackProps = { ...minimalProps(), ...overrides };
  const stack = new ApiProgressiveDialerStack(app, 'TestApiProgressiveDialerStack', props);
  return { app, stack };
}

function functionResource(template: Template, functionName: string) {
  const fns = template.findResources('AWS::Lambda::Function', {
    Properties: { FunctionName: functionName },
  });
  const ids = Object.keys(fns);
  expect(ids.length).toBe(1);
  return (fns[ids[0]] as any);
}

function functionEnv(template: Template, functionName: string): Record<string, unknown> {
  return functionResource(template, functionName).Properties.Environment.Variables;
}

function checkovSkipIds(resource: any): string[] {
  return (resource.Metadata?.checkov?.skip ?? []).map((s: { id: string }) => s.id);
}

function logicalIdOf(template: Template, type: string, props: Record<string, unknown>): string {
  const resources = template.findResources(type, { Properties: props });
  const ids = Object.keys(resources);
  expect(ids.length).toBe(1);
  return ids[0];
}

describe('ApiProgressiveDialerStack', () => {
  // ── Static / always-present resources ───────────────────────────────
  describe('resources present regardless of optional props', () => {
    const { stack } = buildStack();
    const template = Template.fromStack(stack);

    it('creates no IAM::Role or IAM::Policy — all 3 execution roles are imported immutable', () => {
      template.resourceCountIs('AWS::IAM::Role', 0);
      template.resourceCountIs('AWS::IAM::Policy', 0);
    });

    it('creates exactly one shared KMS-encrypted DLQ with 14-day retention', () => {
      template.resourceCountIs('AWS::SQS::Queue', 1);
      template.hasResourceProperties('AWS::SQS::Queue', {
        QueueName: 'vip-admin-progressive-dialer-dlq',
        KmsMasterKeyId: DATA_KEY_ARN,
        MessageRetentionPeriod: 1209600,
      });
    });

    it('creates exactly 3 Lambda::Functions: consumer, caller, seeder', () => {
      template.resourceCountIs('AWS::Lambda::Function', 3);
    });

    it('configures ConsumerFunction with the documented runtime/handler/timeout/memory/concurrency and imported role', () => {
      const fn = functionResource(template, 'vip-admin-progressive-dialer-consumer');
      expect(fn.Properties).toMatchObject({
        Runtime: 'python3.12',
        Handler: 'handler_consumer.lambda_handler',
        Timeout: 60,
        MemorySize: 256,
        ReservedConcurrentExecutions: 10,
        Role: `arn:aws:iam::${ACCOUNT}:role/vip-progressive-dialer-consumer-role`,
      });
    });

    it('configures CallerFunction with the documented runtime/handler/timeout/memory/concurrency and imported role', () => {
      const fn = functionResource(template, 'vip-admin-progressive-dialer-caller');
      expect(fn.Properties).toMatchObject({
        Runtime: 'python3.12',
        Handler: 'handler_caller.lambda_handler',
        Timeout: 30,
        MemorySize: 256,
        ReservedConcurrentExecutions: 2,
        Role: `arn:aws:iam::${ACCOUNT}:role/vip-progressive-dialer-caller-role`,
      });
    });

    it('configures SeederFunction with the documented runtime/handler/timeout/memory/concurrency, imported role, and its own DLQ', () => {
      const fn = functionResource(template, 'vip-admin-progressive-dialer-seeder');
      const dlqId = logicalIdOf(template, 'AWS::SQS::Queue', {
        QueueName: 'vip-admin-progressive-dialer-dlq',
      });
      expect(fn.Properties).toMatchObject({
        Runtime: 'python3.12',
        Handler: 'handler_seeder.lambda_handler',
        Timeout: 60,
        MemorySize: 256,
        ReservedConcurrentExecutions: 2,
        Role: `arn:aws:iam::${ACCOUNT}:role/vip-progressive-dialer-seeder-role`,
        DeadLetterConfig: { TargetArn: { 'Fn::GetAtt': [dlqId, 'Arn'] } },
      });
    });

    it('omits function-level DeadLetterConfig on ConsumerFunction and CallerFunction (their real failure paths are the ESM/queue redrive)', () => {
      expect(functionResource(template, 'vip-admin-progressive-dialer-consumer').Properties.DeadLetterConfig).toBeUndefined();
      expect(functionResource(template, 'vip-admin-progressive-dialer-caller').Properties.DeadLetterConfig).toBeUndefined();
    });

    it('sets exactly the documented ConsumerFunction environment variables (minimal props — no ALLOWED_QUEUE_ARNS)', () => {
      const dialQueueUrl = `https://sqs.${REGION}.amazonaws.com/${ACCOUNT}/vip-progressive-dialer-calls`;
      expect(functionEnv(template, 'vip-admin-progressive-dialer-consumer')).toEqual({
        CAMPAIGN_QUEUE_TABLE: 'VipProgressiveCampaignQueue',
        AGENT_LOCK_TABLE: 'VipProgressiveAgentLocks',
        SQS_QUEUE_URL: dialQueueUrl,
        CONNECT_INSTANCE_ID,
        ACTIVE_CAMPAIGNS_TABLE: 'VipActiveBrandedCampaigns',
        ACTIVE_CAMPAIGNS_GSI: 'queueArn-index',
        FIRSTORION_SECRET_NAME: 'vip/firstorion/credentials',
      });
    });

    it('sets exactly the documented CallerFunction environment variables', () => {
      expect(functionEnv(template, 'vip-admin-progressive-dialer-caller')).toEqual({
        CAMPAIGN_QUEUE_TABLE: 'VipProgressiveCampaignQueue',
        AGENT_LOCK_TABLE: 'VipProgressiveAgentLocks',
        FIRSTORION_SECRET_NAME: 'vip/firstorion/credentials',
      });
    });

    it('sets exactly the documented SeederFunction environment variables', () => {
      expect(functionEnv(template, 'vip-admin-progressive-dialer-seeder')).toEqual({
        CAMPAIGN_QUEUE_TABLE: 'VipProgressiveCampaignQueue',
        PROFILES_DOMAIN_NAME,
      });
    });

    it('applies VPC_SKIP (CKV_AWS_117) AND CKV_AWS_116 to consumerFn and callerFn, but only VPC_SKIP to seederFunction', () => {
      const consumerSkips = checkovSkipIds(functionResource(template, 'vip-admin-progressive-dialer-consumer'));
      const callerSkips = checkovSkipIds(functionResource(template, 'vip-admin-progressive-dialer-caller'));
      const seederSkips = checkovSkipIds(functionResource(template, 'vip-admin-progressive-dialer-seeder'));
      expect(consumerSkips).toEqual(['CKV_AWS_117', 'CKV_AWS_116']);
      expect(callerSkips).toEqual(['CKV_AWS_117', 'CKV_AWS_116']);
      expect(seederSkips).toEqual(['CKV_AWS_117']);
    });

    it('gives consumerFn and callerFn distinct CKV_AWS_116 comments explaining their real failure path', () => {
      const consumerSkip = functionResource(template, 'vip-admin-progressive-dialer-consumer').Metadata.checkov.skip;
      const callerSkip = functionResource(template, 'vip-admin-progressive-dialer-caller').Metadata.checkov.skip;
      expect(consumerSkip[1].comment).toContain('stream-triggered (Kinesis)');
      expect(callerSkip[1].comment).toContain('SQS-triggered');
      expect(callerSkip[1].comment).toContain('RedrivePolicy');
    });

    it('wires a KinesisEventSource on ConsumerFunction filtered to STATE_CHANGE, bisecting on error, onFailure -> the DLQ', () => {
      const dlqId = logicalIdOf(template, 'AWS::SQS::Queue', {
        QueueName: 'vip-admin-progressive-dialer-dlq',
      });
      template.hasResourceProperties('AWS::Lambda::EventSourceMapping', {
        EventSourceArn: AGENT_EVENT_STREAM_ARN,
        StartingPosition: 'LATEST',
        BatchSize: 100,
        BisectBatchOnFunctionError: true,
        DestinationConfig: { OnFailure: { Destination: { 'Fn::GetAtt': [dlqId, 'Arn'] } } },
        FilterCriteria: { Filters: [{ Pattern: '{"data":{"EventType":["STATE_CHANGE"]}}' }] },
      });
    });

    it('wires an SqsEventSource on CallerFunction with batchSize 1 and no onFailure override (uses the source queue redrive)', () => {
      template.hasResourceProperties('AWS::Lambda::EventSourceMapping', {
        EventSourceArn: `arn:aws:sqs:${REGION}:${ACCOUNT}:vip-progressive-dialer-calls`,
        BatchSize: 1,
      });
    });

    it('creates exactly 2 EventSourceMappings when campaignQueueStreamArn is omitted (Kinesis + SQS, no Kickstart ESM)', () => {
      template.resourceCountIs('AWS::Lambda::EventSourceMapping', 2);
    });

    it('emits the documented CfnOutputs with correct values', () => {
      const consumerFnId = logicalIdOf(template, 'AWS::Lambda::Function', {
        FunctionName: 'vip-admin-progressive-dialer-consumer',
      });
      const callerFnId = logicalIdOf(template, 'AWS::Lambda::Function', {
        FunctionName: 'vip-admin-progressive-dialer-caller',
      });
      const seederFnId = logicalIdOf(template, 'AWS::Lambda::Function', {
        FunctionName: 'vip-admin-progressive-dialer-seeder',
      });
      template.hasOutput('CampaignQueueTableName', { Value: 'VipProgressiveCampaignQueue' });
      template.hasOutput('ActiveBrandedCampaignsTableName', { Value: 'VipActiveBrandedCampaigns' });
      template.hasOutput('ActiveBrandedCampaignsTableArn', {
        Value: `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/VipActiveBrandedCampaigns`,
      });
      template.hasOutput('DialQueueUrl', {
        Value: `https://sqs.${REGION}.amazonaws.com/${ACCOUNT}/vip-progressive-dialer-calls`,
      });
      template.hasOutput('ConsumerFunctionArn', { Value: { 'Fn::GetAtt': [consumerFnId, 'Arn'] } });
      template.hasOutput('CallerFunctionArn', { Value: { 'Fn::GetAtt': [callerFnId, 'Arn'] } });
      template.hasOutput('SeederFunctionArn', { Value: { 'Fn::GetAtt': [seederFnId, 'Arn'] } });
      template.hasOutput('KickstartFunctionArn', {
        Value: `arn:aws:lambda:${REGION}:${ACCOUNT}:function:vip-admin-progressive-dialer-kickstart`,
      });
      template.hasOutput('KickstartSweepRuleArn', {
        Value: `arn:aws:events:${REGION}:${ACCOUNT}:rule/vip-progressive-dialer-kickstart-sweep`,
      });
    });

    it('exposes the imported table references as public readonly members with literal ARNs (no cross-stack refs)', () => {
      expect(stack.campaignQueueTable.tableArn).toBe(
        `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/VipProgressiveCampaignQueue`,
      );
      expect(stack.activeBrandedCampaignsTable.tableArn).toBe(
        `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/VipActiveBrandedCampaigns`,
      );
      expect(stack.brandedRunSummaryTable.tableArn).toBe(
        `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/VipBrandedRunSummary`,
      );
      expect(stack.brandedCampaignMetricsTable.tableArn).toBe(
        `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/VipBrandedCampaignMetrics`,
      );
      expect(stack.agentSnapshotTable.tableArn).toBe(
        `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/VipAgentSnapshot`,
      );
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

  // ── allowedQueueArns ──────────────────────────────────────────────────
  describe('allowedQueueArns', () => {
    it('does not inject ALLOWED_QUEUE_ARNS on ConsumerFunction when absent', () => {
      const { stack } = buildStack();
      const template = Template.fromStack(stack);
      expect(functionEnv(template, 'vip-admin-progressive-dialer-consumer').ALLOWED_QUEUE_ARNS).toBeUndefined();
    });

    it('injects ALLOWED_QUEUE_ARNS on ConsumerFunction only (not caller/seeder) when present', () => {
      const { stack } = buildStack({ allowedQueueArns: ALLOWED_QUEUE_ARNS });
      const template = Template.fromStack(stack);
      expect(functionEnv(template, 'vip-admin-progressive-dialer-consumer').ALLOWED_QUEUE_ARNS).toBe(
        ALLOWED_QUEUE_ARNS,
      );
      expect(functionEnv(template, 'vip-admin-progressive-dialer-caller').ALLOWED_QUEUE_ARNS).toBeUndefined();
      expect(functionEnv(template, 'vip-admin-progressive-dialer-seeder').ALLOWED_QUEUE_ARNS).toBeUndefined();
    });
  });

  // ── campaignQueueStreamArn — gates both the tableStreamArn attribute and the Kickstart ESM ──
  describe('campaignQueueStreamArn', () => {
    it('does not create a KickstartEsm EventSourceMapping when absent (only Kinesis + SQS = 2 total)', () => {
      const { stack } = buildStack();
      const template = Template.fromStack(stack);
      template.resourceCountIs('AWS::Lambda::EventSourceMapping', 2);
    });

    it('creates the KickstartEsm targeting the kickstart Lambda, filtered to INSERT+status=PENDING, when present', () => {
      const { stack } = buildStack({ campaignQueueStreamArn: CAMPAIGN_QUEUE_STREAM_ARN });
      const template = Template.fromStack(stack);
      template.resourceCountIs('AWS::Lambda::EventSourceMapping', 3);
      template.hasResourceProperties('AWS::Lambda::EventSourceMapping', {
        EventSourceArn: CAMPAIGN_QUEUE_STREAM_ARN,
        FunctionName: 'vip-admin-progressive-dialer-kickstart',
        StartingPosition: 'LATEST',
        BatchSize: 10,
        BisectBatchOnFunctionError: true,
        FilterCriteria: {
          Filters: [
            {
              Pattern: '{"eventName":["INSERT"],"dynamodb":{"NewImage":{"status":{"S":["PENDING"]}}}}',
            },
          ],
        },
      });
    });

    it('does not add a DestinationConfig to the KickstartEsm (kickstart Lambda is imported by ARN, not managed here)', () => {
      const { stack } = buildStack({ campaignQueueStreamArn: CAMPAIGN_QUEUE_STREAM_ARN });
      const template = Template.fromStack(stack);
      const esms = template.findResources('AWS::Lambda::EventSourceMapping', {
        Properties: { EventSourceArn: CAMPAIGN_QUEUE_STREAM_ARN },
      });
      const id = Object.keys(esms)[0];
      expect((esms[id] as any).Properties.DestinationConfig).toBeUndefined();
    });
  });

  // ── Integration: all optional props set simultaneously ───────────────
  it('coexists correctly when every optional prop is provided at once', () => {
    const { stack } = buildStack({
      permissionsBoundaryName: 'TestBoundary',
      allowedQueueArns: ALLOWED_QUEUE_ARNS,
      campaignQueueStreamArn: CAMPAIGN_QUEUE_STREAM_ARN,
    });
    const template = Template.fromStack(stack);
    template.resourceCountIs('AWS::Lambda::Function', 3);
    template.resourceCountIs('AWS::Lambda::EventSourceMapping', 3);
    expect(functionEnv(template, 'vip-admin-progressive-dialer-consumer').ALLOWED_QUEUE_ARNS).toBe(
      ALLOWED_QUEUE_ARNS,
    );
  });
});
