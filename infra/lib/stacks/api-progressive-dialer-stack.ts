import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as kinesis from 'aws-cdk-lib/aws-kinesis';
import { KinesisEventSource, SqsEventSource } from 'aws-cdk-lib/aws-lambda-event-sources';
import * as events from 'aws-cdk-lib/aws-events';
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
  /**
   * DynamoDB stream ARN for VipProgressiveCampaignQueue.
   * Enable with: aws dynamodb update-table --table-name VipProgressiveCampaignQueue
   *   --stream-specification StreamEnabled=true,StreamViewType=NEW_IMAGE
   * Required to wire the kickstart Lambda ESM. Omit to deploy Lambda without ESM.
   */
  readonly campaignQueueStreamArn?: string;
}

export class ApiProgressiveDialerStack extends cdk.Stack {
  public readonly seederFunction: lambda.Function;
  public readonly campaignQueueTable: dynamodb.ITable;
  public readonly activeBrandedCampaignsTable: dynamodb.ITable;
  public readonly brandedRunSummaryTable: dynamodb.ITable;
  public readonly brandedCampaignMetricsTable: dynamodb.ITable;
  public readonly agentSnapshotTable: dynamodb.ITable;

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
    // PITR enabled, TTL=ttl. tableStreamArn required for kickstart DynamoEventSource.
    const campaignQueueTable = dynamodb.Table.fromTableAttributes(
      this, 'CampaignQueueTable',
      {
        tableArn: `arn:aws:dynamodb:${this.region}:${this.account}:table/VipProgressiveCampaignQueue`,
        ...(props.campaignQueueStreamArn ? { tableStreamArn: props.campaignQueueStreamArn } : {}),
      },
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

    // ── DynamoDB: Branded Run Summary ─────────────────────────────────
    // imported — pre-created via CLI (cfn-exec-role lacks kms:Decrypt on CMK).
    // Schema: PK=planId(S), SK=runId#campaignId(S), PAY_PER_REQUEST, KMS CMK, no TTL.
    const brandedRunSummaryTable = dynamodb.Table.fromTableArn(
      this, 'BrandedRunSummaryTable',
      `arn:aws:dynamodb:${this.region}:${this.account}:table/VipBrandedRunSummary`,
    );
    this.brandedRunSummaryTable = brandedRunSummaryTable;

    // ── DynamoDB: Branded Campaign Metrics ────────────────────────────
    // Time-series disposition + agent snapshots per active branded campaign.
    // imported — pre-created via CLI (cfn-exec-role lacks kms:Decrypt on CMK).
    // Schema: PK=brandedCampaignId(S), SK=snapshotAt(S), GSI1=planId/snapshotAt, TTL=ttl(90d).
    const brandedCampaignMetricsTable = dynamodb.Table.fromTableArn(
      this, 'BrandedCampaignMetricsTable',
      `arn:aws:dynamodb:${this.region}:${this.account}:table/VipBrandedCampaignMetrics`,
    );
    this.brandedCampaignMetricsTable = brandedCampaignMetricsTable;

    // ── DynamoDB: Agent Snapshot ──────────────────────────────────────
    // Per-queue agent availability history for trending and understaffed alerts.
    // imported — pre-created via CLI (cfn-exec-role lacks kms:Decrypt on CMK).
    // Schema: PK=queueId(S), SK=snapshotAt(S), TTL=ttl(30d).
    const agentSnapshotTable = dynamodb.Table.fromTableArn(
      this, 'AgentSnapshotTable',
      `arn:aws:dynamodb:${this.region}:${this.account}:table/VipAgentSnapshot`,
    );
    this.agentSnapshotTable = agentSnapshotTable;

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

    // ── Lambda: Kickstart (DynamoDB Streams) ─────────────────────────────
    // Fixes the event-driven gap: consumer fires only on AVAILABLE *transitions*; this
    // Lambda fires on every INSERT to VipProgressiveCampaignQueue and dispatches to any
    // already-AVAILABLE agent via connect:GetCurrentUserData.
    // imported — cfn-exec-role lacks iam:PassRole; Lambda pre-created via CLI.
    // Code updates: aws lambda update-function-code --function-name vip-admin-progressive-dialer-kickstart \
    //   --zip-file fileb:///tmp/kickstart.zip --region us-east-1 --profile production
    const kickstartFn = lambda.Function.fromFunctionArn(
      this, 'KickstartFunction',
      `arn:aws:lambda:${this.region}:${this.account}:function:vip-admin-progressive-dialer-kickstart`,
    );

    // DynamoDB stream ESM — INSERT events where NewImage.status = PENDING only.
    // Uses EventSourceMapping (works with IFunction; DynamoEventSource.addEventSource() requires concrete Function).
    if (props.campaignQueueStreamArn) {
      new lambda.EventSourceMapping(this, 'KickstartEsm', {
        target: kickstartFn,
        eventSourceArn: props.campaignQueueStreamArn,
        startingPosition: lambda.StartingPosition.LATEST,
        batchSize: 10,
        bisectBatchOnError: true,
        filters: [
          lambda.FilterCriteria.filter({
            eventName: lambda.FilterRule.isEqual('INSERT'),
            dynamodb: {
              NewImage: {
                status: { S: lambda.FilterRule.isEqual('PENDING') },
              },
            },
          }),
        ],
      });
    }

    // ── EventBridge: Kickstart sweep (timer backstop) ────────────────────
    // Fixes the "no re-arm" gap: dispatch is otherwise 100% event-driven (stream
    // INSERT + Kinesis STATE_CHANGE). A campaign whose calls mostly land on
    // voicemail never cycles an agent through a state change, so no event ever
    // re-triggers dispatch even with PENDING contacts and free agents on hand.
    // This rule invokes the SAME kickstart Lambda on a schedule; lambda_handler
    // branches on event.source === 'aws.events' to run the sweep path (scans all
    // active campaigns) instead of the stream-record path (one campaign).
    //
    // imported — confirmed by a failed deploy attempt (2026-08-27): cfn-exec-role
    // lacks events:DescribeRule, so CloudFormation cannot create/manage an
    // AWS::Events::Rule here (same permission-boundary class as the other
    // imported roles/functions/log groups in this stack). Rule pre-created via CLI:
    //   aws events put-rule --name vip-progressive-dialer-kickstart-sweep \
    //     --schedule-expression "rate(2 minutes)" --state ENABLED \
    //     --region us-east-1 --profile production
    //   aws lambda add-permission --function-name vip-admin-progressive-dialer-kickstart \
    //     --statement-id AllowEventBridgeSweep --action lambda:InvokeFunction \
    //     --principal events.amazonaws.com \
    //     --source-arn arn:aws:events:us-east-1:165505826690:rule/vip-progressive-dialer-kickstart-sweep \
    //     --region us-east-1 --profile production
    //   aws events put-targets --rule vip-progressive-dialer-kickstart-sweep \
    //     --targets "Id=1,Arn=arn:aws:lambda:us-east-1:165505826690:function:vip-admin-progressive-dialer-kickstart" \
    //     --region us-east-1 --profile production
    const sweepRule = events.Rule.fromEventRuleArn(
      this, 'KickstartSweepRule',
      `arn:aws:events:${this.region}:${this.account}:rule/vip-progressive-dialer-kickstart-sweep`,
    );

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
    new cdk.CfnOutput(this, 'KickstartFunctionArn', { value: kickstartFn.functionArn });
    new cdk.CfnOutput(this, 'KickstartSweepRuleArn', { value: sweepRule.ruleArn });
  }
}
