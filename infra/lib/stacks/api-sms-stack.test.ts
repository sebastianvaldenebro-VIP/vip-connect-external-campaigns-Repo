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

  it('creates the SmsRetryQuietHoursFunction reusing the sender role/layer/tables, with its own function name/handler/log group', () => {
    const template = Template.fromStack(buildStack());
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'vip-admin-sms-retry-quiet-hours',
      Handler: 'sms_sender_handler.retry_quiet_hours_skipped',
      Runtime: 'python3.12',
      // Same imported, mutable:false role as SmsSenderFunction — its exact
      // permission set already covers everything this function needs
      // (VipSmsCampaignQueue/Runs read-write, SQS SendMessage, KMS decrypt,
      // Customer Profiles read), so no new IAM policy is required.
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
          QUIET_HOURS_START: '08:00',
          QUIET_HOURS_END: '21:00',
          QUIET_HOURS_DAYS: '0,1,2,3,4,5',
          QUIET_HOURS_DEFAULT_TZ: 'America/New_York',
        },
      },
    });
    const retryResources = template.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: 'vip-admin-sms-retry-quiet-hours' },
    });
    const [retryProps] = Object.values(retryResources).map((r) => r.Properties);
    expect(retryProps.Layers).toHaveLength(1);
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

  it('wires DeadLetterConfig on all 3 Lambdas to the single DLQ resource', () => {
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
      FunctionName: 'vip-admin-sms-retry-quiet-hours',
      ...expectedDeadLetterConfig,
    });
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'vip-admin-sms-processor',
      ...expectedDeadLetterConfig,
    });
  });

  it('applies the CKV_AWS_117 checkov suppression to all 3 Lambda functions', () => {
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
      Properties: Match.objectLike({ FunctionName: 'vip-admin-sms-retry-quiet-hours' }),
      Metadata: Match.objectLike({ checkov: { skip: expectedSkip } }),
    });
    template.hasResource('AWS::Lambda::Function', {
      Properties: Match.objectLike({ FunctionName: 'vip-admin-sms-processor' }),
      Metadata: Match.objectLike({ checkov: { skip: expectedSkip } }),
    });
  });

  it('creates no AWS::IAM::Role resources — all 3 Lambdas use imported, mutable:false roles (SmsRetryQuietHoursFunction reuses vip-sms-sender-role)', () => {
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

  it('emits the 6 documented CfnOutputs with the expected values', () => {
    const stack = buildStack();
    const template = Template.fromStack(stack);
    template.hasOutput('SmsSenderFunctionArn', {});
    template.hasOutput('SmsRetryQuietHoursFunctionArn', {});
    template.hasOutput('SmsProcessorFunctionArn', {});
    template.hasOutput('SmsCampaignQueueTableName', { Value: 'VipSmsCampaignQueue' });
    template.hasOutput('SmsRunsTableName', { Value: 'VipSmsCampaignRuns' });
    template.hasOutput('SmsSendQueueUrl', {
      Value: `https://sqs.${REGION}.amazonaws.com/${ACCOUNT}/vip-sms-campaign-queue`,
    });
  });

  it('exposes exactly 3 Lambda functions', () => {
    const template = Template.fromStack(buildStack());
    template.resourceCountIs('AWS::Lambda::Function', 3);
  });
});
