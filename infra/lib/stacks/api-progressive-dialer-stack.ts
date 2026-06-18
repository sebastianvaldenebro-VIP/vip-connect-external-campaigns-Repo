import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as kinesis from 'aws-cdk-lib/aws-kinesis';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cloudwatch_actions from 'aws-cdk-lib/aws-cloudwatch-actions';
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
  /** Optional SNS topic ARN to receive alarm notifications. */
  readonly alertsTopicArn?: string;
}

export class ApiProgressiveDialerStack extends cdk.Stack {
  public readonly seederFunction: lambda.Function;
  public readonly campaignQueueTable: dynamodb.Table;
  public readonly activeBrandedCampaignsTable: dynamodb.Table;

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

    // Resolve KMS key from ARN — avoids cross-stack Fn::ImportValue dependency
    const dataKey = kms.Key.fromKeyArn(this, 'DataKey', props.dataKeyArn);

    // ── DynamoDB: Campaign Queue ──────────────────────────────────────
    const campaignQueueTable = new dynamodb.Table(this, 'CampaignQueueTable', {
      tableName: 'VipProgressiveCampaignQueue',
      partitionKey: { name: 'campaignId', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: dataKey,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    this.campaignQueueTable = campaignQueueTable;

    // ── DynamoDB: Active Branded Campaigns ───────────────────────────
    // One-to-many: PK=QUEUE#{queueArn}, SK=CAMPAIGN#{campaignId}
    // GSI queueArn-index used by consumer to find campaigns by queue ARN.
    const activeBrandedCampaignsTable = new dynamodb.Table(
      this,
      'ActiveBrandedCampaignsTable',
      {
        tableName: 'VipActiveBrandedCampaigns',
        partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
        sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
        billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
        encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
        encryptionKey: dataKey,
        pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
        timeToLiveAttribute: 'ttl',
        removalPolicy: cdk.RemovalPolicy.RETAIN,
      },
    );
    activeBrandedCampaignsTable.addGlobalSecondaryIndex({
      indexName: 'queueArn-index',
      partitionKey: { name: 'queueArn', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'createdAt', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    this.activeBrandedCampaignsTable = activeBrandedCampaignsTable;

    // ── DynamoDB: Agent Locks ─────────────────────────────────────────
    const agentLockTable = new dynamodb.Table(this, 'AgentLockTable', {
      tableName: 'VipProgressiveAgentLocks',
      partitionKey: { name: 'agentId', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: dataKey,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // ── SQS: Dead-letter queue ────────────────────────────────────────
    const dlq = new sqs.Queue(this, 'DialDLQ', {
      queueName: 'vip-progressive-dialer-calls-dlq',
      retentionPeriod: cdk.Duration.days(14),
      encryptionMasterKey: dataKey,
      // HIPAA: deny non-TLS access — even internal body has correlatable data
      enforceSSL: true,
    });

    // ── SQS: Dial delay queue ─────────────────────────────────────────
    // Delay is set per-message (DelaySeconds=22) in handler_consumer.py.
    // Do NOT set deliveryDelay here — it would stack with the per-message
    // delay and push the total to 44s, past First Orion's branding window.
    const dialQueue = new sqs.Queue(this, 'DialQueue', {
      queueName: 'vip-progressive-dialer-calls',
      // 6× caller Lambda timeout (6×30s=180s) per SQS/Lambda convention to prevent
      // re-delivery while an invocation is still running.
      visibilityTimeout: cdk.Duration.seconds(180),
      encryptionMasterKey: dataKey,
      enforceSSL: true,
      deadLetterQueue: { queue: dlq, maxReceiveCount: 3 },
    });

    // ── Shared Layer ──────────────────────────────────────────────────
    const sharedLayer = buildSharedLayer(this);

    // ── Lambda: Consumer (Kinesis) ────────────────────────────────────
    const consumerLogGroup = new logs.LogGroup(this, 'ConsumerLogs', {
      logGroupName: '/aws/lambda/vip-admin-progressive-dialer-consumer',
      retention: logs.RetentionDays.ONE_YEAR,
      encryptionKey: dataKey,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const consumerRole = new iam.Role(this, 'ConsumerRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Execution role for progressive-dialer consumer Lambda',
    });
    consumerLogGroup.grantWrite(consumerRole);

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

    agentStream.grantRead(consumerRole);
    campaignQueueTable.grantReadWriteData(consumerRole);
    agentLockTable.grantReadWriteData(consumerRole);
    activeBrandedCampaignsTable.grantReadData(consumerRole);
    dialQueue.grantSendMessages(consumerRole);
    consumerRole.addToPolicy(new iam.PolicyStatement({
      actions: ['secretsmanager:GetSecretValue'],
      resources: [props.firstOrionSecretArn],
    }));
    consumerRole.addToPolicy(new iam.PolicyStatement({
      actions: ['kms:Decrypt', 'kms:GenerateDataKey'],
      resources: [dataKey.keyArn],
    }));
    consumerRole.addToPolicy(new iam.PolicyStatement({
      sid: 'ConsumerPutMetrics',
      actions: ['cloudwatch:PutMetricData'],
      resources: ['*'],
      conditions: {
        StringEquals: { 'cloudwatch:namespace': 'VipConnect/ProgressiveDialer' },
      },
    }));

    // ── Lambda: Caller (SQS) ──────────────────────────────────────────
    const callerLogGroup = new logs.LogGroup(this, 'CallerLogs', {
      logGroupName: '/aws/lambda/vip-admin-progressive-dialer-caller',
      retention: logs.RetentionDays.ONE_YEAR,
      encryptionKey: dataKey,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const callerRole = new iam.Role(this, 'CallerRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Execution role for progressive-dialer caller Lambda',
    });
    callerLogGroup.grantWrite(callerRole);

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

    campaignQueueTable.grantReadWriteData(callerRole);
    agentLockTable.grantReadWriteData(callerRole);
    dialQueue.grantConsumeMessages(callerRole);
    callerRole.addToPolicy(new iam.PolicyStatement({
      // StartOutboundVoiceContact validates referenced resources under the caller's identity.
      // DescribeContactFlow + DescribeQueue prevent AccessDeniedException on the referenced
      // flow and queue IDs — same issue observed with CreateCampaign in api-campaigns-stack.
      actions: [
        'connect:StartOutboundVoiceContact',
        'connect:DescribeContactFlow',
        'connect:DescribeQueue',
      ],
      resources: [
        `arn:aws:connect:${this.region}:${this.account}:instance/${props.connectInstanceId}`,
        `arn:aws:connect:${this.region}:${this.account}:instance/${props.connectInstanceId}/*`,
      ],
    }));
    callerRole.addToPolicy(new iam.PolicyStatement({
      actions: ['kms:Decrypt', 'kms:GenerateDataKey'],
      resources: [dataKey.keyArn],
    }));
    // First Orion secret access — needed for re-push on throttle retry (Fix #6)
    callerRole.addToPolicy(new iam.PolicyStatement({
      actions: ['secretsmanager:GetSecretValue'],
      resources: [props.firstOrionSecretArn],
    }));
    callerRole.addToPolicy(new iam.PolicyStatement({
      sid: 'CallerPutMetrics',
      actions: ['cloudwatch:PutMetricData'],
      resources: ['*'],
      conditions: {
        StringEquals: { 'cloudwatch:namespace': 'VipConnect/ProgressiveDialer' },
      },
    }));

    // ── Lambda: Seeder (HTTP via API Gateway) ─────────────────────────
    // No VPC — uses Customer Profiles GetSegmentDefinition + BatchGetProfile.
    // Phone is at profile["PhoneNumber"] (standard CP field). 3,000-member max → ≤30 API calls.
    const seederLogGroup = new logs.LogGroup(this, 'SeederLogs', {
      logGroupName: '/aws/lambda/vip-admin-progressive-dialer-seeder',
      retention: logs.RetentionDays.ONE_YEAR,
      encryptionKey: dataKey,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const seederRole = new iam.Role(this, 'SeederRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Execution role for progressive-dialer seeder Lambda',
    });
    seederLogGroup.grantWrite(seederRole);
    campaignQueueTable.grantWriteData(seederRole);
    dataKey.grant(seederRole, 'kms:Decrypt', 'kms:GenerateDataKey');
    seederRole.addToPolicy(new iam.PolicyStatement({
      sid: 'CPSeedPerms',
      actions: ['profile:GetSegmentDefinition', 'profile:BatchGetProfile'],
      // Two explicit ARNs required — same pattern as api-profiles-stack and api-segments-stack:
      // • GetSegmentDefinition evaluates against domains/{name}/segment-definitions/*
      // • BatchGetProfile evaluates against domains/{name} (bare domain)
      // A single glob like domains/{name}* is fragile; list both explicitly.
      resources: [
        `arn:aws:profile:${this.region}:${this.account}:domains/${props.profilesDomainName}`,
        `arn:aws:profile:${this.region}:${this.account}:domains/${props.profilesDomainName}/*`,
      ],
    }));

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

    // ── Alarms ────────────────────────────────────────────────────────
    // 1. DLQ visible messages >= 1 — any message in DLQ means failures exceeded maxReceiveCount
    const dlqAlarm = new cloudwatch.Alarm(this, 'DlqMessagesAlarm', {
      alarmName: 'vip-progressive-dialer-dlq-messages',
      alarmDescription: 'Messages in progressive dialer DLQ — dial failures exceeded maxReceiveCount',
      metric: dlq.metricApproximateNumberOfMessagesVisible({
        period: cdk.Duration.minutes(1),
        statistic: 'Maximum',
      }),
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // 2. Consumer Lambda errors > 0 in 5min — Kinesis records failing to process
    const consumerErrorAlarm = new cloudwatch.Alarm(this, 'ConsumerErrorAlarm', {
      alarmName: 'vip-progressive-dialer-consumer-errors',
      alarmDescription: 'Consumer Lambda errors — Kinesis records failing to process',
      metric: consumerFn.metricErrors({
        period: cdk.Duration.minutes(5),
        statistic: 'Sum',
      }),
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // 3. Caller Lambda errors > 5 in 5min — some errors expected from throttle retries
    const callerErrorAlarm = new cloudwatch.Alarm(this, 'CallerErrorAlarm', {
      alarmName: 'vip-progressive-dialer-caller-errors',
      alarmDescription: 'Caller Lambda errors — StartOutboundVoiceContact failures beyond retries',
      metric: callerFn.metricErrors({
        period: cdk.Duration.minutes(5),
        statistic: 'Sum',
      }),
      threshold: 5,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // 4. Connect throttle metric (custom) — emitted by handler_caller.py on TooManyRequestsException
    const throttleAlarm = new cloudwatch.Alarm(this, 'ConnectThrottleAlarm', {
      alarmName: 'vip-progressive-dialer-connect-throttle',
      alarmDescription: 'Connect StartOutboundVoiceContact throttled — check dial concurrency',
      metric: new cloudwatch.Metric({
        namespace: 'VipConnect/ProgressiveDialer',
        metricName: 'ConnectThrottleCount',
        dimensionsMap: {},
        period: cdk.Duration.minutes(5),
        statistic: 'Sum',
      }),
      threshold: 10,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // 5. First Orion push failures (custom) — emitted by handler_consumer.py
    const firstOrionAlarm = new cloudwatch.Alarm(this, 'FirstOrionFailAlarm', {
      alarmName: 'vip-progressive-dialer-firstorion-failures',
      alarmDescription: 'First Orion INFORM push failures — calls going out without branding',
      metric: new cloudwatch.Metric({
        namespace: 'VipConnect/ProgressiveDialer',
        metricName: 'FirstOrionPushFailed',
        dimensionsMap: {},
        period: cdk.Duration.minutes(5),
        statistic: 'Sum',
      }),
      threshold: 5,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // Wire SNS alarm actions when an alertsTopicArn is provided
    if (props.alertsTopicArn) {
      const alertsTopic = sns.Topic.fromTopicArn(this, 'AlertsTopic', props.alertsTopicArn);
      [dlqAlarm, consumerErrorAlarm, callerErrorAlarm, throttleAlarm, firstOrionAlarm].forEach(alarm => {
        alarm.addAlarmAction(new cloudwatch_actions.SnsAction(alertsTopic));
      });
    }

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
    new cdk.CfnOutput(this, 'DlqAlarmName', { value: dlqAlarm.alarmName });
    new cdk.CfnOutput(this, 'ConsumerErrorAlarmName', { value: consumerErrorAlarm.alarmName });
  }
}
