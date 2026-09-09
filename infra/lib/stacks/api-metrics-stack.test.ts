import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as kms from 'aws-cdk-lib/aws-kms';
import { ApiMetricsStack, ApiMetricsStackProps } from './api-metrics-stack';

const ACCOUNT = '165505826690';
const REGION = 'us-east-1';
const CONNECT_INSTANCE_ID = '6b3f17ba-68a4-472a-9b20-db1991507009';
const DATA_KEY_ARN = `arn:aws:kms:${REGION}:${ACCOUNT}:key/df585888-2f49-4de0-9cba-14803fda63f0`;

const ADMIN_AUDIT_TABLE_ARN = `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/AdminAuditLog`;
const ACTIVE_BRANDED_ARN = `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/VipActiveBrandedCampaigns`;
const BRANDED_METRICS_ARN = `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/VipBrandedCampaignMetrics`;
const AGENT_SNAPSHOT_ARN = `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/VipAgentSnapshot`;
const RUN_SUMMARY_ARN = `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/VipBrandedRunSummary`;
const PROGRESSIVE_QUEUE_ARN = `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/VipProgressiveCampaignQueue`;

interface BuildOptions {
  permissionsBoundaryName?: string;
  includeRunSummary?: boolean;
  includeActive?: boolean;
  includeMetrics?: boolean;
  includeAgentSnapshot?: boolean;
  includeProgressiveQueue?: boolean;
}

function buildStack(opts: BuildOptions = {}) {
  const app = new cdk.App();
  // kms.Key.fromKeyArn/dynamodb.Table.fromTableArn require a Stack in scope
  // (CDK validates "should be created in the scope of a Stack") — use a
  // dedicated imports stack, mirroring how app.ts wires real cross-stack
  // resources (DataStack/ApiProgressiveDialerStack) into ApiMetricsStack.
  const importsStack = new cdk.Stack(app, 'ImportsStack', {
    env: { account: ACCOUNT, region: REGION },
  });
  const dataKey = kms.Key.fromKeyArn(importsStack, 'ImportedDataKey', DATA_KEY_ARN);
  const adminAuditTable = dynamodb.Table.fromTableArn(
    importsStack,
    'ImportedAdminAuditTable',
    ADMIN_AUDIT_TABLE_ARN,
  );

  const props: {
    -readonly [K in keyof ApiMetricsStackProps]: ApiMetricsStackProps[K];
  } = {
    env: { account: ACCOUNT, region: REGION },
    adminAuditTable,
    dataKey,
    connectInstanceId: CONNECT_INSTANCE_ID,
    permissionsBoundaryName: opts.permissionsBoundaryName,
  };

  if (opts.includeRunSummary) {
    props.brandedRunSummaryTable = dynamodb.Table.fromTableArn(
      importsStack,
      'ImportedRunSummaryTable',
      RUN_SUMMARY_ARN,
    );
  }
  if (opts.includeActive) {
    props.activeBrandedCampaignsTable = dynamodb.Table.fromTableArn(
      importsStack,
      'ImportedActiveBrandedTable',
      ACTIVE_BRANDED_ARN,
    );
  }
  if (opts.includeMetrics) {
    props.brandedCampaignMetricsTable = dynamodb.Table.fromTableArn(
      importsStack,
      'ImportedBrandedMetricsTable',
      BRANDED_METRICS_ARN,
    );
  }
  if (opts.includeAgentSnapshot) {
    props.agentSnapshotTable = dynamodb.Table.fromTableArn(
      importsStack,
      'ImportedAgentSnapshotTable',
      AGENT_SNAPSHOT_ARN,
    );
  }
  if (opts.includeProgressiveQueue) {
    props.progressiveCampaignQueueTable = dynamodb.Table.fromTableArn(
      importsStack,
      'ImportedProgressiveQueueTable',
      PROGRESSIVE_QUEUE_ARN,
    );
  }

  return new ApiMetricsStack(app, 'TestApiMetricsStack', props);
}

describe('ApiMetricsStack — base resources (present regardless of optional branded props)', () => {
  it('does not create a PermissionsBoundary construct when the prop is omitted', () => {
    const stack = buildStack();
    expect(stack.node.tryFindChild('PermissionsBoundary')).toBeUndefined();
  });

  it('resolves the boundary managed policy and synthesizes without throwing when permissionsBoundaryName is provided', () => {
    expect(() =>
      Template.fromStack(buildStack({ permissionsBoundaryName: 'TestBoundary' })),
    ).not.toThrow();
  });

  it('creates FunctionRole with CloudWatch, Connect, ConnectCampaigns, and AuditRead policy statements', () => {
    const template = Template.fromStack(buildStack());
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: 'CloudWatchMetrics',
            Effect: 'Allow',
            Action: ['cloudwatch:GetMetricStatistics', 'cloudwatch:GetMetricData'],
            Resource: '*',
          }),
          Match.objectLike({
            Sid: 'ConnectMetrics',
            Action: [
              'connect:GetMetricDataV2',
              'connect:GetCurrentMetricData',
              'connect:GetCurrentUserData',
              'connect:SearchContacts',
              'connect:ListRoutingProfiles',
              'connect:ListAgentStatuses',
              'connect:DescribeUser',
            ],
            Resource: [
              `arn:aws:connect:${REGION}:${ACCOUNT}:instance/${CONNECT_INSTANCE_ID}`,
              `arn:aws:connect:${REGION}:${ACCOUNT}:instance/${CONNECT_INSTANCE_ID}/*`,
            ],
          }),
          Match.objectLike({
            Sid: 'ConnectCampaignsRead',
            Action: [
              'connect-campaigns:ListCampaigns',
              'connect-campaigns:DescribeCampaign',
              'connect-campaigns:GetCampaignState',
            ],
            // Single-element resource arrays collapse to a plain string in synth.
            Resource: `arn:aws:connect-campaigns:${REGION}:${ACCOUNT}:campaign/*`,
          }),
          Match.objectLike({
            Sid: 'AuditRead',
            Action: ['dynamodb:Query', 'dynamodb:Scan', 'dynamodb:GetItem'],
            Resource: [ADMIN_AUDIT_TABLE_ARN, `${ADMIN_AUDIT_TABLE_ARN}/index/*`],
          }),
        ]),
      },
    });
  });

  it('grants FunctionRole kms:Decrypt on the (imported) data key and creates no new KMS::Key resource', () => {
    const template = Template.fromStack(buildStack());
    template.resourceCountIs('AWS::KMS::Key', 0); // dataKey is imported, not created by this stack
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: 'kms:Decrypt',
            Effect: 'Allow',
            Resource: DATA_KEY_ARN,
          }),
        ]),
      },
    });
  });

  it('creates a KMS-encrypted DLQ with 14-day retention shared by both Lambdas', () => {
    const template = Template.fromStack(buildStack());
    template.hasResourceProperties('AWS::SQS::Queue', {
      QueueName: 'vip-admin-ui-api-metrics-dlq',
      KmsMasterKeyId: DATA_KEY_ARN,
      MessageRetentionPeriod: 1209600,
    });
  });

  it('creates the primary FunctionMetrics Lambda with expected runtime/memory/timeout/concurrency/env', () => {
    const template = Template.fromStack(buildStack());
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'vip-admin-ui-api-metrics',
      Handler: 'handler.lambda_handler',
      Runtime: 'python3.12',
      MemorySize: 512,
      Timeout: 30,
      ReservedConcurrentExecutions: 10,
      KmsKeyArn: DATA_KEY_ARN,
      Environment: {
        Variables: Match.objectLike({
          CONNECT_INSTANCE_ID,
          DATA_KEY_ARN,
          LOG_LEVEL: 'INFO',
          POWERTOOLS_SERVICE_NAME: 'api-metrics',
        }),
      },
    });
  });

  it('applies the CKV_AWS_117 checkov suppression to the primary Lambda', () => {
    const template = Template.fromStack(buildStack());
    template.hasResource('AWS::Lambda::Function', {
      Properties: Match.objectLike({ FunctionName: 'vip-admin-ui-api-metrics' }),
      Metadata: Match.objectLike({
        checkov: {
          skip: Match.arrayWith([
            Match.objectLike({
              id: 'CKV_AWS_117',
              comment: Match.stringLikeRegexp('^Not internet-reachable'),
            }),
          ]),
        },
      }),
    });
  });

  it('wires DeadLetterConfig on the primary Lambda to the DLQ resource', () => {
    const template = Template.fromStack(buildStack());
    const dlqs = template.findResources('AWS::SQS::Queue', {
      Properties: { QueueName: 'vip-admin-ui-api-metrics-dlq' },
    });
    const [dlqLogicalId] = Object.keys(dlqs);
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'vip-admin-ui-api-metrics',
      DeadLetterConfig: { TargetArn: { 'Fn::GetAtt': [dlqLogicalId, 'Arn'] } },
    });
  });

  it('emits the FunctionArn CfnOutput', () => {
    const template = Template.fromStack(buildStack());
    template.hasOutput('FunctionArn', {});
  });
});

describe('ApiMetricsStack — brandedRunSummaryTable (independent optional prop)', () => {
  it('when absent: FunctionRole has no grant on the run-summary table and FunctionMetrics has no BRANDED_RUN_SUMMARY_TABLE env var', () => {
    const template = Template.fromStack(buildStack());
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'vip-admin-ui-api-metrics',
      Environment: { Variables: Match.not(Match.objectLike({ BRANDED_RUN_SUMMARY_TABLE: Match.anyValue() })) },
    });
  });

  it('when present: grants read on the run-summary table and adds BRANDED_RUN_SUMMARY_TABLE to FunctionMetrics env', () => {
    const template = Template.fromStack(buildStack({ includeRunSummary: true }));
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(['dynamodb:GetItem']),
            Resource: Match.arrayWith([RUN_SUMMARY_ARN]),
          }),
        ]),
      },
    });
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'vip-admin-ui-api-metrics',
      Environment: {
        Variables: Match.objectLike({ BRANDED_RUN_SUMMARY_TABLE: 'VipBrandedRunSummary' }),
      },
    });
  });
});

describe('ApiMetricsStack — brandedCampaignMetricsTable (independent optional prop; also required for the collector block)', () => {
  it('when absent (and collector block also absent): FunctionMetrics has no BRANDED_CAMPAIGN_METRICS_TABLE env var', () => {
    const template = Template.fromStack(buildStack());
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'vip-admin-ui-api-metrics',
      Environment: {
        Variables: Match.not(Match.objectLike({ BRANDED_CAMPAIGN_METRICS_TABLE: Match.anyValue() })),
      },
    });
  });

  it('when present alone (collector block still absent because activeBrandedCampaignsTable/agentSnapshotTable are missing): grants read + adds env var, no collector Lambda', () => {
    const template = Template.fromStack(buildStack({ includeMetrics: true }));
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(['dynamodb:GetItem']),
            Resource: Match.arrayWith([BRANDED_METRICS_ARN]),
          }),
        ]),
      },
    });
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'vip-admin-ui-api-metrics',
      Environment: {
        Variables: Match.objectLike({ BRANDED_CAMPAIGN_METRICS_TABLE: 'VipBrandedCampaignMetrics' }),
      },
    });
    template.resourceCountIs('AWS::Lambda::Function', 1); // no BrandedMetricsCollector
  });
});

describe('ApiMetricsStack — Branded Campaign Metrics Collector block (all 3 required props)', () => {
  it('activeBrandedCampaignsTable present but brandedCampaignMetricsTable absent: collector NOT created', () => {
    const template = Template.fromStack(buildStack({ includeActive: true }));
    template.resourceCountIs('AWS::Lambda::Function', 1);
  });

  it('activeBrandedCampaignsTable + brandedCampaignMetricsTable present but agentSnapshotTable absent: collector NOT created', () => {
    const template = Template.fromStack(buildStack({ includeActive: true, includeMetrics: true }));
    template.resourceCountIs('AWS::Lambda::Function', 1);
  });

  it('all 3 required props present (no progressiveCampaignQueueTable): collector IS created with expected shape', () => {
    const stack = buildStack({ includeActive: true, includeMetrics: true, includeAgentSnapshot: true });
    const template = Template.fromStack(stack);
    template.resourceCountIs('AWS::Lambda::Function', 2);

    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'vip-admin-branded-metrics-collector',
      Handler: 'metrics_collector_handler.lambda_handler',
      Runtime: 'python3.12',
      MemorySize: 256,
      Timeout: 120,
      ReservedConcurrentExecutions: 2,
      TracingConfig: { Mode: 'Active' },
      KmsKeyArn: DATA_KEY_ARN,
      Environment: {
        Variables: {
          ACTIVE_BRANDED_CAMPAIGNS_TABLE: 'VipActiveBrandedCampaigns',
          BRANDED_CAMPAIGN_METRICS_TABLE: 'VipBrandedCampaignMetrics',
          AGENT_SNAPSHOT_TABLE: 'VipAgentSnapshot',
          CONNECT_INSTANCE_ID,
          LOG_LEVEL: 'INFO',
        },
      },
    });

    // Imports the pre-existing log group by name rather than creating a new one.
    template.resourceCountIs('AWS::Logs::LogGroup', 1); // only ApiMetricsLogs — CollectorLogs is imported, not created

    // CollectorRole IAM policy: ConnectReadMetrics + CloudWatchEmitMetrics (scoped by namespace),
    // but NOT ProgressiveCampaignQueueOutcomes (progressiveCampaignQueueTable absent here).
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: 'ConnectReadMetrics',
            Action: ['connect:SearchContacts', 'connect:GetCurrentMetricData', 'connect:DescribeContact'],
            Resource: [
              `arn:aws:connect:${REGION}:${ACCOUNT}:instance/${CONNECT_INSTANCE_ID}`,
              `arn:aws:connect:${REGION}:${ACCOUNT}:instance/${CONNECT_INSTANCE_ID}/*`,
            ],
          }),
          Match.objectLike({
            Sid: 'CloudWatchEmitMetrics',
            Action: 'cloudwatch:PutMetricData',
            Resource: '*',
            Condition: { StringEquals: { 'cloudwatch:namespace': 'VipBrandedMonitor' } },
          }),
        ]),
      },
    });

    const collectorPolicies = template.findResources('AWS::IAM::Policy', {
      Properties: {
        PolicyDocument: {
          Statement: Match.arrayWith([Match.objectLike({ Sid: 'ProgressiveCampaignQueueOutcomes' })]),
        },
      },
    });
    expect(Object.keys(collectorPolicies)).toHaveLength(0);

    template.hasOutput('CollectorFunctionArn', {});
    template.hasResource('AWS::Lambda::Function', {
      Properties: Match.objectLike({ FunctionName: 'vip-admin-branded-metrics-collector' }),
      Metadata: Match.objectLike({
        checkov: {
          skip: Match.arrayWith([Match.objectLike({ id: 'CKV_AWS_117' })]),
        },
      }),
    });
  });

  it('all 3 required props + progressiveCampaignQueueTable present: adds ProgressiveCampaignQueueOutcomes policy and env var', () => {
    const stack = buildStack({
      includeActive: true,
      includeMetrics: true,
      includeAgentSnapshot: true,
      includeProgressiveQueue: true,
    });
    const template = Template.fromStack(stack);

    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: 'ProgressiveCampaignQueueOutcomes',
            Action: ['dynamodb:Query', 'dynamodb:UpdateItem'],
            Resource: PROGRESSIVE_QUEUE_ARN,
          }),
        ]),
      },
    });

    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'vip-admin-branded-metrics-collector',
      Environment: {
        Variables: Match.objectLike({
          PROGRESSIVE_CAMPAIGN_QUEUE_TABLE: 'VipProgressiveCampaignQueue',
        }),
      },
    });
  });

  it('collector Lambda uses the same DLQ as the primary Lambda', () => {
    const stack = buildStack({ includeActive: true, includeMetrics: true, includeAgentSnapshot: true });
    const template = Template.fromStack(stack);
    const dlqs = template.findResources('AWS::SQS::Queue', {
      Properties: { QueueName: 'vip-admin-ui-api-metrics-dlq' },
    });
    const [dlqLogicalId] = Object.keys(dlqs);
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'vip-admin-branded-metrics-collector',
      DeadLetterConfig: { TargetArn: { 'Fn::GetAtt': [dlqLogicalId, 'Arn'] } },
    });
  });
});
