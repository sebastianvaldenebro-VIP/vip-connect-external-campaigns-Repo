import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as kinesis from 'aws-cdk-lib/aws-kinesis';
import { KinesisEventSource, SqsEventSource } from 'aws-cdk-lib/aws-lambda-event-sources';
import * as path from 'path';
import { buildSharedLayer } from '../utils/shared-layer';

export interface ApiProgressiveDialerStackProps extends cdk.StackProps {
  /** KMS CMK ARN — passed as string to avoid cross-stack Fn::ImportValue dependency */
  readonly dataKeyArn: string;
  readonly connectInstanceId: string;
  /** Kinesis stream ARN — vip-use1-datastream (Task 1 confirmed) */
  readonly agentEventStreamArn: string;
  /** Secrets Manager ARN from Task 6 Step 1 */
  readonly firstOrionSecretArn: string;
  /** CP domain — seeder reads segment + phones from here */
  readonly profilesDomainName: string;
  /** Comma-separated queue ARNs to filter agents. Empty = all queues. */
  readonly allowedQueueArns?: string;
  readonly permissionsBoundaryName?: string;
}

export class ApiProgressiveDialerStack extends cdk.Stack {
  public readonly seederFunction: lambda.Function;
  public readonly campaignQueueTable: dynamodb.ITable;
  public readonly activeBrandedCampaignsTable: dynamodb.ITable;

  constructor(scope: Construct, id: string, props: ApiProgressiveDialerStackProps) {
    super(scope, id, props);

    if (props.permissionsBoundaryName) {
      const boundary = iam.ManagedPolicy.fromManagedPolicyName(
        this,
        'PermissionsBoundary',
        props.permissionsBoundaryName,
      );
      iam.PermissionsBoundary.of(this).apply(boundary);
    }

    // ── DynamoDB: Campaign Queue ──────────────────────────────────────
    // imported — cfn-exec-role lacks kms:Decrypt on this CMK and logs:DescribeIndexPolicies;
    // table pre-created via CLI. Schema: PK=campaignId(S), SK=sk(S), PAY_PER_REQUEST, KMS CMK,
    // PITR enabled, TTL=ttl.
    const campaignQueueTable = dynamodb.Table.fromTableArn(
      this, 'CampaignQueueTable',
      `arn:aws:dynamodb:${this.region}:${this.account}:table/VipProgressiveCampaignQueue`,
    );
    this.campaignQueueTable = campaignQueueTable;

    // ── DynamoDB: Active Branded Campaigns ───────────────────────────
    // One-to-many: PK=QUEUE#{queueArn}, SK=CAMPAIGN#{campaignId}
    // GSI queueArn-index used by consumer to find campaigns by queue ARN.
    // imported — cfn-exec-role lacks kms:Decrypt on this CMK; table pre-created via CLI.
    // Schema: PK=pk(S), SK=sk(S), GSI=queueArn-index(queueArn/createdAt), PAY_PER_REQUEST, KMS CMK,
    // PITR enabled, TTL=ttl.
    const activeBrandedCampaignsTable = dynamodb.Table.fromTableArn(
      this, 'ActiveBrandedCampaignsTable',
      `arn:aws:dynamodb:${this.region}:${this.account}:table/VipActiveBrandedCampaigns`,
    );
    this.activeBrandedCampaignsTable = activeBrandedCampaignsTable;

    // ── DynamoDB: Agent Locks ─────────────────────────────────────────
    // imported — cfn-exec-role lacks kms:Decrypt on this CMK; table pre-created via CLI.
    // Schema: PK=agentId(S), PAY_PER_REQUEST, KMS CMK, PITR enabled, TTL=ttl.
    const agentLockTable = dynamodb.Table.fromTableArn(
      this, 'AgentLockTable',
      `arn:aws:dynamodb:${this.region}:${this.account}:table/VipProgressiveAgentLocks`,
    );

    // ── SQS: Dial delay queue (imported — cfn-exec-role lacks sqs:CreateQueue) ──
    // Per-message delay=22s set in handler_consumer.py via DelaySeconds on SendMessage.
    // Create once via CLI before deploying this stack:
    //   aws sqs create-queue --queue-name vip-progressive-dialer-calls
    //     --attributes '{"VisibilityTimeout":"180","KmsMasterKeyId":"<kmsArn>",
    //                    "RedrivePolicy":"{...dlq arn...,maxReceiveCount:3}"}'
    // Then apply enforce-SSL resource policy via set-queue-attributes.
    const dialQueue = sqs.Queue.fromQueueAttributes(this, 'DialQueue', {
      queueArn: `arn:aws:sqs:${this.region}:${this.account}:vip-progressive-dialer-calls`,
      queueUrl: `https://sqs.${this.region}.amazonaws.com/${this.account}/vip-progressive-dialer-calls`,
      keyArn: props.dataKeyArn,
    });

    // ── Shared Layer ──────────────────────────────────────────────────
    const sharedLayer = buildSharedLayer(this);

    // ── Lambda: Consumer (Kinesis) ────────────────────────────────────
    // imported — cfn-exec-role lacks logs:DescribeIndexPolicies; log group pre-created via CLI
    // with KMS CMK and 365-day retention.
    const consumerLogGroup = logs.LogGroup.fromLogGroupName(
      this, 'ConsumerLogs', '/aws/lambda/vip-admin-progressive-dialer-consumer',
    );

    // imported — cfn-exec-role lacks iam:CreateRole + iam:GetRolePolicy; role pre-created via CLI
    // with EngineeringPermissionBoundary. mutable:false avoids AWS::IAM::Policy resource generation
    // (which requires iam:GetRolePolicy on cfn-exec-role). All permissions attached via CLI:
    //   aws iam put-role-policy --role-name vip-progressive-dialer-consumer-role \
    //     --policy-name ProgressiveDialerConsumerPerms --policy-document file:///tmp/consumer-policy.json
    const consumerRole = iam.Role.fromRoleArn(
      this, 'ConsumerRole',
      `arn:aws:iam::${this.account}:role/vip-progressive-dialer-consumer-role`,
      { mutable: false },
    );

    const consumerFn = new lambda.Function(this, 'ConsumerFunction', {
      functionName: 'vip-admin-progressive-dialer-consumer',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler_consumer.lambda_handler',
      code: lambda.Code.fromAsset(
        path.join(__dirname, '../../../services/api-progressive-dialer/src'),
      ),
      layers: [sharedLayer],
      role: consumerRole,
      logGroup: consumerLogGroup,
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
      environment: {
        CAMPAIGN_QUEUE_TABLE: campaignQueueTable.tableName,
        AGENT_LOCK_TABLE: agentLockTable.tableName,
        SQS_QUEUE_URL: dialQueue.queueUrl,
        CONNECT_INSTANCE_ID: props.connectInstanceId,
        ACTIVE_CAMPAIGNS_TABLE: activeBrandedCampaignsTable.tableName,
        ACTIVE_CAMPAIGNS_GSI: 'queueArn-index',
        FIRSTORION_SECRET_NAME: 'vip/firstorion/credentials',
        ...(props.allowedQueueArns ? { ALLOWED_QUEUE_ARNS: props.allowedQueueArns } : {}),
      },
    });

    // Kinesis ESM — filter to STATE_CHANGE only to reduce invocations
    const agentStream = kinesis.Stream.fromStreamArn(
      this, 'AgentEventStream', props.agentEventStreamArn,
    );
    consumerFn.addEventSource(new KinesisEventSource(agentStream, {
      startingPosition: lambda.StartingPosition.LATEST,
      batchSize: 100,
      bisectBatchOnError: true,
      filters: [
        lambda.FilterCriteria.filter({
          data: { EventType: lambda.FilterRule.isEqual('STATE_CHANGE') },
        }),
      ],
    }));

    // All grants pre-attached via CLI (mutable:false — CDK skips IAM policy generation).

    // ── Lambda: Caller (SQS) ──────────────────────────────────────────
    // imported — cfn-exec-role lacks logs:DescribeIndexPolicies; log group pre-created via CLI
    const callerLogGroup = logs.LogGroup.fromLogGroupName(
      this, 'CallerLogs', '/aws/lambda/vip-admin-progressive-dialer-caller',
    );

    // imported — cfn-exec-role lacks iam:CreateRole + iam:GetRolePolicy; role pre-created via CLI.
    // mutable:false — all permissions pre-attached via:
    //   aws iam put-role-policy --role-name vip-progressive-dialer-caller-role \
    //     --policy-name ProgressiveDialerCallerPerms --policy-document file:///tmp/caller-policy.json
    const callerRole = iam.Role.fromRoleArn(
      this, 'CallerRole',
      `arn:aws:iam::${this.account}:role/vip-progressive-dialer-caller-role`,
      { mutable: false },
    );

    const callerFn = new lambda.Function(this, 'CallerFunction', {
      functionName: 'vip-admin-progressive-dialer-caller',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler_caller.lambda_handler',
      code: lambda.Code.fromAsset(
        path.join(__dirname, '../../../services/api-progressive-dialer/src'),
      ),
      layers: [sharedLayer],
      role: callerRole,
      logGroup: callerLogGroup,
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      // Throttle to 2 concurrent max — matches StartOutboundVoiceContact 2 RPS limit
      reservedConcurrentExecutions: 2,
      environment: {
        CAMPAIGN_QUEUE_TABLE: campaignQueueTable.tableName,
        AGENT_LOCK_TABLE: agentLockTable.tableName,
        FIRSTORION_SECRET_NAME: 'vip/firstorion/credentials',
      },
    });

    callerFn.addEventSource(new SqsEventSource(dialQueue, {
      batchSize: 1, // one dial per invocation
    }));

    // All grants pre-attached via CLI (mutable:false — CDK skips IAM policy generation).

    // ── Lambda: Seeder (HTTP via API Gateway) ─────────────────────────
    // No VPC — uses Customer Profiles GetSegmentDefinition + BatchGetProfile.
    // Phone is at profile["PhoneNumber"] (standard CP field). 3,000-member max → ≤30 API calls.
    // imported — cfn-exec-role lacks logs:DescribeIndexPolicies; log group pre-created via CLI
    const seederLogGroup = logs.LogGroup.fromLogGroupName(
      this, 'SeederLogs', '/aws/lambda/vip-admin-progressive-dialer-seeder',
    );

    // imported — cfn-exec-role lacks iam:CreateRole + iam:GetRolePolicy; role pre-created via CLI.
    // mutable:false — all permissions pre-attached via:
    //   aws iam put-role-policy --role-name vip-progressive-dialer-seeder-role \
    //     --policy-name ProgressiveDialerSeederPerms --policy-document file:///tmp/seeder-policy.json
    const seederRole = iam.Role.fromRoleArn(
      this, 'SeederRole',
      `arn:aws:iam::${this.account}:role/vip-progressive-dialer-seeder-role`,
      { mutable: false },
    );
    // All grants pre-attached via CLI (mutable:false — CDK skips IAM policy generation).

    this.seederFunction = new lambda.Function(this, 'SeederFunction', {
      functionName: 'vip-admin-progressive-dialer-seeder',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler_seeder.lambda_handler',
      code: lambda.Code.fromAsset(
        path.join(__dirname, '../../../services/api-progressive-dialer/src'),
      ),
      layers: [sharedLayer],
      role: seederRole,
      logGroup: seederLogGroup,
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
      environment: {
        CAMPAIGN_QUEUE_TABLE: campaignQueueTable.tableName,
        PROFILES_DOMAIN_NAME: props.profilesDomainName,
      },
    });

    // ── Alarms (created via CLI — cfn-exec-role lacks cloudwatch:PutMetricAlarm) ──
    // Run infra/scripts/create-progressive-dialer-alarms.sh after stack deploys.

    // ── Outputs ───────────────────────────────────────────────────────
    new cdk.CfnOutput(this, 'CampaignQueueTableName', { value: campaignQueueTable.tableName });
    new cdk.CfnOutput(this, 'ActiveBrandedCampaignsTableName', {
      value: activeBrandedCampaignsTable.tableName,
    });
    new cdk.CfnOutput(this, 'ActiveBrandedCampaignsTableArn', {
      value: activeBrandedCampaignsTable.tableArn,
    });
    new cdk.CfnOutput(this, 'DialQueueUrl', { value: dialQueue.queueUrl });
    new cdk.CfnOutput(this, 'ConsumerFunctionArn', { value: consumerFn.functionArn });
    new cdk.CfnOutput(this, 'CallerFunctionArn', { value: callerFn.functionArn });
    new cdk.CfnOutput(this, 'SeederFunctionArn', { value: this.seederFunction.functionArn });
  }
}
