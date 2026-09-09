import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { ApiSmsStack, ApiSmsStackProps } from './api-sms-stack';

const ACCOUNT = '165505826690';
const REGION = 'us-east-1';
const DATA_KEY_ARN = `arn:aws:kms:${REGION}:${ACCOUNT}:key/df585888-2f49-4de0-9cba-14803fda63f0`;

function buildStack(overrides: Partial<ApiSmsStackProps> = {}) {
  const app = new cdk.App();
  const props: ApiSmsStackProps = {
    env: { account: ACCOUNT, region: REGION },
    dataKeyArn: DATA_KEY_ARN,
    profilesDomainName: 'amazon-connect-vipmedicalgroup',
    smsConfigSetName: 'vip-sms-config-set',
    smsOptOutListName: 'vip-sms-opt-out',
    ...overrides,
  };
  return new ApiSmsStack(app, 'TestApiSmsStack', props);
}

describe('ApiSmsStack', () => {
  it('does not create a PermissionsBoundary construct when permissionsBoundaryName is omitted', () => {
    const stack = buildStack();
    expect(stack.node.tryFindChild('PermissionsBoundary')).toBeUndefined();
    expect(() => Template.fromStack(stack)).not.toThrow();
  });

  it('resolves the boundary managed policy and synthesizes without throwing when permissionsBoundaryName is provided', () => {
    expect(() =>
      Template.fromStack(buildStack({ permissionsBoundaryName: 'TestBoundary' })),
    ).not.toThrow();
  });

  it('imports the SMS campaign queue/runs tables and send queue by literal ARN (no cross-stack refs)', () => {
    const stack = buildStack();
    expect(stack.smsCampaignQueueTable.tableArn).toBe(
      `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/VipSmsCampaignQueue`,
    );
    expect(stack.smsRunsTable.tableArn).toBe(
      `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/VipSmsCampaignRuns`,
    );
    expect(stack.smsSendQueue.queueUrl).toBe(
      `https://sqs.${REGION}.amazonaws.com/${ACCOUNT}/vip-sms-campaign-queue`,
    );
  });

  it('creates exactly one KMS-encrypted DLQ shared by both Lambdas with a 14-day retention', () => {
    const template = Template.fromStack(buildStack());
    template.resourceCountIs('AWS::SQS::Queue', 1);
    template.hasResourceProperties('AWS::SQS::Queue', {
      QueueName: 'vip-admin-sms-dlq',
      KmsMasterKeyId: DATA_KEY_ARN,
      MessageRetentionPeriod: 1209600,
    });
  });

  it('creates the SmsSenderFunction with the imported (immutable) role, KMS env encryption, and the shared layer', () => {
    const template = Template.fromStack(buildStack());
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'vip-admin-sms-sender',
      Handler: 'sms_sender_handler.lambda_handler',
      Runtime: 'python3.12',
      Role: `arn:aws:iam::${ACCOUNT}:role/vip-sms-sender-role`,
      Timeout: 300,
      MemorySize: 512,
      ReservedConcurrentExecutions: 5,
      KmsKeyArn: DATA_KEY_ARN,
      Environment: {
        Variables: {
          SMS_CAMPAIGN_QUEUE_TABLE: 'VipSmsCampaignQueue',
          SMS_CAMPAIGN_RUNS_TABLE: 'VipSmsCampaignRuns',
          SMS_SQS_QUEUE_URL: `https://sqs.${REGION}.amazonaws.com/${ACCOUNT}/vip-sms-campaign-queue`,
          PROFILES_DOMAIN_NAME: 'amazon-connect-vipmedicalgroup',
        },
      },
    });
    // Layers is a list of { Ref: <SharedLayer logical id> } — assert it's non-empty
    // and points at a real LayerVersion resource, without over-fitting to a token shape.
    const senderResources = template.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: 'vip-admin-sms-sender' },
    });
    const [senderProps] = Object.values(senderResources).map((r) => r.Properties);
    expect(senderProps.Layers).toHaveLength(1);
    template.resourceCountIs('AWS::Lambda::LayerVersion', 1);
  });

  it('creates the SmsProcessorFunction with the imported role, no shared layer, and a distinct memory/timeout/concurrency profile', () => {
    const template = Template.fromStack(buildStack());
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'vip-admin-sms-processor',
      Handler: 'sms_processor_handler.lambda_handler',
      Runtime: 'python3.12',
      Role: `arn:aws:iam::${ACCOUNT}:role/vip-sms-processor-role`,
      Timeout: 30,
      MemorySize: 256,
      ReservedConcurrentExecutions: 10,
      KmsKeyArn: DATA_KEY_ARN,
      Layers: Match.absent(),
      Environment: {
        Variables: {
          SMS_CAMPAIGN_QUEUE_TABLE: 'VipSmsCampaignQueue',
          SMS_CAMPAIGN_RUNS_TABLE: 'VipSmsCampaignRuns',
          SMS_CONFIG_SET_NAME: 'vip-sms-config-set',
          SMS_OPT_OUT_LIST_NAME: 'vip-sms-opt-out',
        },
      },
    });
  });

  it('reflects custom smsConfigSetName/smsOptOutListName/profilesDomainName overrides in the environment', () => {
    const template = Template.fromStack(
      buildStack({
        smsConfigSetName: 'custom-config-set',
        smsOptOutListName: 'custom-opt-out',
        profilesDomainName: 'custom-domain',
      }),
    );
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'vip-admin-sms-processor',
      Environment: {
        Variables: Match.objectLike({
          SMS_CONFIG_SET_NAME: 'custom-config-set',
          SMS_OPT_OUT_LIST_NAME: 'custom-opt-out',
        }),
      },
    });
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'vip-admin-sms-sender',
      Environment: {
        Variables: Match.objectLike({ PROFILES_DOMAIN_NAME: 'custom-domain' }),
      },
    });
  });

  it('wires DeadLetterConfig on both Lambdas to the single DLQ resource', () => {
    const template = Template.fromStack(buildStack());
    const dlqs = template.findResources('AWS::SQS::Queue', {
      Properties: { QueueName: 'vip-admin-sms-dlq' },
    });
    const dlqLogicalIds = Object.keys(dlqs);
    expect(dlqLogicalIds).toHaveLength(1);
    const [dlqLogicalId] = dlqLogicalIds;

    const expectedDeadLetterConfig = {
      DeadLetterConfig: {
        TargetArn: { 'Fn::GetAtt': [dlqLogicalId, 'Arn'] },
      },
    };
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'vip-admin-sms-sender',
      ...expectedDeadLetterConfig,
    });
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'vip-admin-sms-processor',
      ...expectedDeadLetterConfig,
    });
  });

  it('applies the CKV_AWS_117 checkov suppression to both Lambda functions', () => {
    const template = Template.fromStack(buildStack());
    const expectedSkip = Match.arrayWith([
      Match.objectLike({
        id: 'CKV_AWS_117',
        comment: Match.stringLikeRegexp('^Not internet-reachable'),
      }),
    ]);
    template.hasResource('AWS::Lambda::Function', {
      Properties: Match.objectLike({ FunctionName: 'vip-admin-sms-sender' }),
      Metadata: Match.objectLike({ checkov: { skip: expectedSkip } }),
    });
    template.hasResource('AWS::Lambda::Function', {
      Properties: Match.objectLike({ FunctionName: 'vip-admin-sms-processor' }),
      Metadata: Match.objectLike({ checkov: { skip: expectedSkip } }),
    });
  });

  it('creates no AWS::IAM::Role resources — both Lambda roles are imported with mutable:false', () => {
    const template = Template.fromStack(buildStack());
    template.resourceCountIs('AWS::IAM::Role', 0);
  });

  it('wires an SQS event source (batch size 1) from the send queue to SmsProcessorFunction only', () => {
    const template = Template.fromStack(buildStack());
    template.resourceCountIs('AWS::Lambda::EventSourceMapping', 1);
    template.hasResourceProperties('AWS::Lambda::EventSourceMapping', {
      EventSourceArn: `arn:aws:sqs:${REGION}:${ACCOUNT}:vip-sms-campaign-queue`,
      BatchSize: 1,
    });
  });

  it('emits the 5 documented CfnOutputs with the expected values', () => {
    const stack = buildStack();
    const template = Template.fromStack(stack);
    template.hasOutput('SmsSenderFunctionArn', {});
    template.hasOutput('SmsProcessorFunctionArn', {});
    template.hasOutput('SmsCampaignQueueTableName', { Value: 'VipSmsCampaignQueue' });
    template.hasOutput('SmsRunsTableName', { Value: 'VipSmsCampaignRuns' });
    template.hasOutput('SmsSendQueueUrl', {
      Value: `https://sqs.${REGION}.amazonaws.com/${ACCOUNT}/vip-sms-campaign-queue`,
    });
  });

  it('exposes exactly 2 Lambda functions', () => {
    const template = Template.fromStack(buildStack());
    template.resourceCountIs('AWS::Lambda::Function', 2);
  });
});
