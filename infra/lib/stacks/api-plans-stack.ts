import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { DynamoEventSource } from 'aws-cdk-lib/aws-lambda-event-sources';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as path from 'path';
import { buildSharedLayer } from '../utils/shared-layer';

export interface ApiPlansStackProps extends cdk.StackProps {
  readonly adminAuditTable: dynamodb.ITable;
  readonly dataKey: kms.IKey;
  readonly connectInstanceId: string;
  readonly profilesDomainName: string;
  readonly permissionsBoundaryName?: string;
  readonly redisVpc: {
    vpcId: string;
    subnetIds: string[];
    availabilityZones: string[];
    securityGroupId: string;
  };
  readonly redis: {
    host: string;
    port: number;
    team: string;
    passwordSecretArn?: string;
  };
  readonly progressiveCampaignQueueTable?: dynamodb.ITable;
  readonly activeBrandedCampaignsTable?: dynamodb.ITable;
  readonly brandedRunSummaryTable?: dynamodb.ITable;
  readonly brandedCampaignMetricsTable?: dynamodb.ITable;
  readonly agentSnapshotTable?: dynamodb.ITable;
  readonly progressiveDialerSeederArn?: string;
  readonly progressiveDialerDataKeyArn?: string;
  // SMS campaign props (optional — only needed when SMS stack is deployed)
  readonly smsCampaignQueueTable?: dynamodb.ITable;
  readonly smsRunsTable?: dynamodb.ITable;
  readonly smsSenderFunctionArn?: string;
  // Location Onboarding Guard — DynamoDB stream ARN for VipLocationMapping.
  // Enable with: aws dynamodb update-table --table-name VipLocationMapping
  //   --stream-specification StreamEnabled=true,StreamViewType=NEW_IMAGE
  // Omit to deploy without the guard Lambda's event source wired.
  readonly locationMappingStreamArn?: string;
}

export class ApiPlansStack extends cdk.Stack {
  public readonly lambdaFunction: lambda.Function;
  public readonly exporterFunction: lambda.Function;
  public readonly plansTable: dynamodb.Table;

  constructor(scope: Construct, id: string, props: ApiPlansStackProps) {
    super(scope, id, props);

    if (props.permissionsBoundaryName) {
      const boundary = iam.ManagedPolicy.fromManagedPolicyName(
        this,
        'PermissionsBoundary',
        props.permissionsBoundaryName,
      );
      iam.PermissionsBoundary.of(this).apply(boundary);
    }

    // ── DynamoDB table ───────────────────────────────────────────────
    this.plansTable = new dynamodb.Table(this, 'PlansTable', {
      tableName: 'VipAdminPlans',
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: props.dataKey,
      pointInTimeRecovery: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      deletionProtection: true,
    });

    // ── SNS alerts topic ─────────────────────────────────────────────
    // Topic created manually (CFN exec role lacks SNS:GetTopicAttributes within
    // the EngineeringPermissionBoundary). Import by ARN so CDK can wire IAM
    // and inject the ARN env var without managing lifecycle.
    // Subscribe team email addresses via the AWS Console or CLI.
    const alertsTopicArn = `arn:aws:sns:${this.region}:${this.account}:vip-plans-alerts`;
    const alertsTopic = sns.Topic.fromTopicArn(this, 'PlansAlertsTopic', alertsTopicArn);
    new cdk.CfnOutput(this, 'AlertsTopicArn', { value: alertsTopic.topicArn });

    // ── Lambda log group ─────────────────────────────────────────────
    const logGroup = new logs.LogGroup(this, 'ApiPlansLogs', {
      logGroupName: '/aws/lambda/vip-admin-ui-api-plans',
      retention: logs.RetentionDays.ONE_YEAR,
      encryptionKey: props.dataKey,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // ── VPC + security group (for Redis access) ──────────────────────
    const vpc = ec2.Vpc.fromVpcAttributes(this, 'RedisVpc', {
      vpcId: props.redisVpc.vpcId,
      availabilityZones: props.redisVpc.availabilityZones,
      privateSubnetIds: props.redisVpc.subnetIds,
    });
    const sg = ec2.SecurityGroup.fromSecurityGroupId(
      this,
      'RedisSG',
      props.redisVpc.securityGroupId,
      { mutable: false },
    );

    // ── Lambda execution role ────────────────────────────────────────
    const role = new iam.Role(this, 'FunctionRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Execution role for api-plans Lambda',
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          'service-role/AWSLambdaVPCAccessExecutionRole',
        ),
      ],
    });
    logGroup.grantWrite(role);

    // Customer Profiles — segment CRUD
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'CustomerProfilesSegments',
        actions: [
          'profile:CreateSegmentDefinition',
          'profile:GetSegmentDefinition',
          'profile:DeleteSegmentDefinition',
          'profile:ListSegmentDefinitions',
          'profile:TagResource',
        ],
        resources: [
          `arn:aws:profile:${this.region}:${this.account}:domains/${props.profilesDomainName}`,
          `arn:aws:profile:${this.region}:${this.account}:domains/${props.profilesDomainName}/*`,
        ],
      }),
    );

    // Connect Campaigns V2 — campaign lifecycle
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ConnectCampaignsV2',
        actions: [
          'connect-campaigns:CreateCampaign',
          'connect-campaigns:DeleteCampaign',
          'connect-campaigns:StartCampaign',
          'connect-campaigns:StopCampaign',
          'connect-campaigns:GetCampaignState',
          'connect-campaigns:DescribeCampaign',
          'connect-campaigns:TagResource',
        ],
        resources: [
          `arn:aws:connect-campaigns:${this.region}:${this.account}:campaign/*`,
        ],
      }),
    );

    // Connect instance resources (for campaign creation + canonical flow auto-creation)
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ConnectReadInstanceResources',
        actions: [
          'connect:ListQueues',
          'connect:ListContactFlows',
          'connect:ListContactFlowVersions',
          'connect:DescribeContactFlow',
          'connect:DescribeQueue',
          'connect:DescribeInstance',
          'connect:CreateContactFlow',
          'connect:TagResource',
        ],
        resources: [
          `arn:aws:connect:${this.region}:${this.account}:instance/${props.connectInstanceId}`,
          `arn:aws:connect:${this.region}:${this.account}:instance/${props.connectInstanceId}/*`,
        ],
      }),
    );

    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ConnectPhoneNumberV2',
        actions: ['connect:ListPhoneNumbersV2', 'connect:DescribePhoneNumber'],
        resources: [
          `arn:aws:connect:${this.region}:${this.account}:phone-number/*`,
          `arn:aws:connect:${this.region}:${this.account}:instance/${props.connectInstanceId}`,
        ],
      }),
    );

    // DynamoDB — plans table
    this.plansTable.grantReadWriteData(role);

    // DynamoDB — audit table
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'AuditWrite',
        actions: ['dynamodb:PutItem'],
        resources: [props.adminAuditTable.tableArn],
      }),
    );

    // CloudWatch Events — create/delete rules and targets for per-bucket ticks
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'EventBridgeRules',
        actions: [
          'events:PutRule',
          'events:PutTargets',
          'events:RemoveTargets',
          'events:DeleteRule',
          'events:DescribeRule',
        ],
        resources: [
          `arn:aws:events:${this.region}:${this.account}:rule/vip-plan-*`,
          `arn:aws:events:${this.region}:${this.account}:rule/vip-sched-*`,
        ],
      }),
    );

    // Lambda self-permission — add/remove/read resource-based policy for EventBridge invocation.
    // GetPolicy is required by _ensure_scheduled_run_permission (reads current policy to check
    // whether the vip-sched-* statement is present before calling add_permission) and by
    // _cleanup_orphan_plan_permissions (reads policy to find stale vip-plan-* statements).
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'LambdaSelfPermission',
        actions: ['lambda:AddPermission', 'lambda:RemovePermission', 'lambda:GetPolicy'],
        resources: [
          `arn:aws:lambda:${this.region}:${this.account}:function:vip-admin-ui-api-plans`,
        ],
      }),
    );

    // STS GetCallerIdentity — used to resolve account ID for ARN construction
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'StsGetCallerIdentity',
        actions: ['sts:GetCallerIdentity'],
        resources: ['*'],
      }),
    );

    // CloudWatch PutMetricData — two callers, two namespaces:
    //   'VIPPlans'                   — prestart_check stuck-run metrics (existing, do not rename)
    //   'VipConnect/ProgressiveDialer' — branded campaign lifecycle metrics from _emit_branded_metric
    // PutMetricData does not support resource-level restrictions (resources must be '*'),
    // so we scope with a StringEquals multi-value condition covering both namespaces.
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'CloudWatchPutMetric',
        actions: ['cloudwatch:PutMetricData'],
        resources: ['*'],
        conditions: {
          StringEquals: { 'cloudwatch:namespace': ['VIPPlans', 'VipConnect/ProgressiveDialer'] },
        },
      }),
    );

    // SNS — publish operational alerts
    alertsTopic.grantPublish(role);

    props.dataKey.grantEncryptDecrypt(role);

    // #003 — Redis AUTH: grant Secrets Manager read when a password secret is configured
    if (props.redis.passwordSecretArn) {
      role.addToPolicy(
        new iam.PolicyStatement({
          sid: 'RedisPasswordSecret',
          actions: ['secretsmanager:GetSecretValue'],
          resources: [props.redis.passwordSecretArn],
        }),
      );
    }

    // ── Progressive Branded Dialer — executor access ─────────────────
    if (props.progressiveCampaignQueueTable) {
      props.progressiveCampaignQueueTable.grantReadWriteData(role);
    }

    if (props.activeBrandedCampaignsTable) {
      props.activeBrandedCampaignsTable.grantReadWriteData(role);
    }

    if (props.brandedRunSummaryTable) {
      props.brandedRunSummaryTable.grantReadWriteData(role);
    }

    if (props.brandedCampaignMetricsTable) {
      props.brandedCampaignMetricsTable.grantReadWriteData(role);
    }

    if (props.agentSnapshotTable) {
      props.agentSnapshotTable.grantReadWriteData(role);
    }

    if (props.progressiveDialerSeederArn) {
      role.addToPolicy(
        new iam.PolicyStatement({
          sid: 'InvokeProgressiveDialerSeeder',
          actions: ['lambda:InvokeFunction'],
          resources: [props.progressiveDialerSeederArn],
        }),
      );
    }

    if (props.progressiveDialerDataKeyArn) {
      role.addToPolicy(
        new iam.PolicyStatement({
          sid: 'ProgressiveDialerKmsAccess',
          actions: ['kms:Decrypt', 'kms:GenerateDataKey'],
          resources: [props.progressiveDialerDataKeyArn],
        }),
      );
    }

    // ── Location Mapping — builders.py scans this table to build segments ──
    // Table created via CLI (CFN exec role limitation); imported by name.
    const locationMappingTable = dynamodb.Table.fromTableName(
      this,
      'LocationMappingTable',
      'VipLocationMapping',
    );
    locationMappingTable.grantReadData(role);

    // ── Location Onboarding Guard — same physical table, second CDK
    // reference so we can expose its stream ARN (fromTableName can't).
    const locationMappingTableWithStream = props.locationMappingStreamArn
      ? dynamodb.Table.fromTableAttributes(this, 'LocationMappingTableStream', {
          tableArn: `arn:aws:dynamodb:${this.region}:${this.account}:table/VipLocationMapping`,
          tableStreamArn: props.locationMappingStreamArn,
        })
      : undefined;

    // ── SMS Campaign — executor polling + sender invocation ──────────
    if (props.smsCampaignQueueTable) {
      props.smsCampaignQueueTable.grantReadData(role);
    }

    if (props.smsRunsTable) {
      props.smsRunsTable.grantReadWriteData(role);
    }

    if (props.smsSenderFunctionArn) {
      role.addToPolicy(
        new iam.PolicyStatement({
          sid: 'InvokeSmsSender',
          actions: ['lambda:InvokeFunction'],
          resources: [props.smsSenderFunctionArn],
        }),
      );
    }

    // EUM SMS — list origination numbers (GET /sms/numbers endpoint)
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'EumSmsDescribePhoneNumbers',
        actions: ['sms-voice:DescribePhoneNumbers'],
        resources: ['*'],
      }),
    );

    // Contact artifacts — S3 list + presign for recordings bucket
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ContactArtifactsRecordings',
        actions: ['s3:GetObject', 's3:ListBucket'],
        resources: [
          'arn:aws:s3:::amazon-connect-c5a2158755eb',
          'arn:aws:s3:::amazon-connect-c5a2158755eb/*',
        ],
      }),
    );

    // Contact artifacts — S3 list + presign for voicemail bucket
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ContactArtifactsVoicemail',
        actions: ['s3:GetObject', 's3:ListBucket'],
        resources: [
          'arn:aws:s3:::vmx3-recordings-vipmedicalgroup',
          'arn:aws:s3:::vmx3-recordings-vipmedicalgroup/*',
        ],
      }),
    );

    // Contact artifacts — describe individual contact to resolve date prefix
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ContactArtifactsDescribeContact',
        actions: ['connect:DescribeContact'],
        resources: [
          `arn:aws:connect:${this.region}:${this.account}:instance/${props.connectInstanceId}`,
          `arn:aws:connect:${this.region}:${this.account}:instance/${props.connectInstanceId}/*`,
        ],
      }),
    );

    // ── Lambda function ──────────────────────────────────────────────
    const sharedLayer = buildSharedLayer(this);
    this.lambdaFunction = new lambda.Function(this, 'FunctionPlans', {
      functionName: 'vip-admin-ui-api-plans',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: lambda.Code.fromAsset(
        path.join(__dirname, '../../../services/api-plans/src'),
      ),
      layers: [sharedLayer],
      memorySize: 1024,
      // Redis scan of large lists can take tens of seconds; 5 min matches segments.
      timeout: cdk.Duration.minutes(5),
      role,
      logGroup,
      reservedConcurrentExecutions: 5,
      vpc,
      vpcSubnets: {
        subnets: props.redisVpc.subnetIds.map((sid, i) =>
          ec2.Subnet.fromSubnetAttributes(this, `RedisSubnet${i}`, {
            subnetId: sid,
            availabilityZone: props.redisVpc.availabilityZones[i],
          }),
        ),
      },
      securityGroups: [sg],
      environment: {
        CONNECT_INSTANCE_ID: props.connectInstanceId,
        RECORDINGS_BUCKET: 'amazon-connect-c5a2158755eb',
        VOICEMAIL_BUCKET: 'vmx3-recordings-vipmedicalgroup',
        PROFILES_DOMAIN_NAME: props.profilesDomainName,
        PLANS_TABLE_NAME: this.plansTable.tableName,
        AUDIT_TABLE: props.adminAuditTable.tableName,
        DATA_KEY_ARN: props.dataKey.keyArn,
        REDIS_HOST: props.redis.host,
        REDIS_PORT: String(props.redis.port),
        TEAM: props.redis.team,
        // LAMBDA_FUNCTION_ARN is set post-creation via addEnvironment
        SNS_ALERTS_TOPIC_ARN: alertsTopic.topicArn,
        LOG_LEVEL: 'INFO',
        POWERTOOLS_SERVICE_NAME: 'api-plans',
      },
    });

    // Hardcoded ARN to avoid CDK circular dependency — same pattern as the
    // schedulerRole policy below. functionArn token creates a cycle.
    this.lambdaFunction.addEnvironment(
      'LAMBDA_FUNCTION_ARN',
      `arn:aws:lambda:${this.region}:${this.account}:function:vip-admin-ui-api-plans`,
    );

    // #003 — inject Redis AUTH secret ARN when configured
    if (props.redis.passwordSecretArn) {
      this.lambdaFunction.addEnvironment('REDIS_PASSWORD_SECRET_ARN', props.redis.passwordSecretArn);
    }

    // Progressive Branded Dialer — inject table names and seeder ARN
    if (props.progressiveCampaignQueueTable) {
      this.lambdaFunction.addEnvironment(
        'CAMPAIGN_QUEUE_TABLE_BRANDED',
        props.progressiveCampaignQueueTable.tableName,
      );
    }

    if (props.activeBrandedCampaignsTable) {
      this.lambdaFunction.addEnvironment(
        'ACTIVE_BRANDED_CAMPAIGNS_TABLE',
        props.activeBrandedCampaignsTable.tableName,
      );
    }

    if (props.brandedRunSummaryTable) {
      this.lambdaFunction.addEnvironment(
        'BRANDED_RUN_SUMMARY_TABLE',
        props.brandedRunSummaryTable.tableName,
      );
    }

    if (props.brandedCampaignMetricsTable) {
      this.lambdaFunction.addEnvironment(
        'BRANDED_CAMPAIGN_METRICS_TABLE',
        props.brandedCampaignMetricsTable.tableName,
      );
    }

    if (props.agentSnapshotTable) {
      this.lambdaFunction.addEnvironment(
        'AGENT_SNAPSHOT_TABLE',
        props.agentSnapshotTable.tableName,
      );
    }

    if (props.progressiveDialerSeederArn) {
      this.lambdaFunction.addEnvironment(
        'PROGRESSIVE_DIALER_SEEDER_ARN',
        props.progressiveDialerSeederArn,
      );
    }

    if (props.smsCampaignQueueTable) {
      this.lambdaFunction.addEnvironment(
        'SMS_CAMPAIGN_QUEUE_TABLE',
        props.smsCampaignQueueTable.tableName,
      );
    }

    if (props.smsRunsTable) {
      this.lambdaFunction.addEnvironment(
        'SMS_CAMPAIGN_RUNS_TABLE',
        props.smsRunsTable.tableName,
      );
    }

    if (props.smsSenderFunctionArn) {
      this.lambdaFunction.addEnvironment(
        'SMS_SENDER_FUNCTION_ARN',
        props.smsSenderFunctionArn,
      );
    }

    // ── Location Onboarding Guard — detects a brand-new state appearing in
    // VipLocationMapping with no canonicalPhone set, alarms via SNS.
    // No Connect permissions — flow auto-creation is handled elsewhere
    // (builders.resolve_campaign_flow_arn, called from executor.py).
    if (locationMappingTableWithStream) {
      // Role created via CLI (CFN exec role lacks iam:CreateRole under the
      // EngineeringPermissionBoundary — same limitation documented elsewhere
      // in this stack, e.g. CampaignExporterFunction below). Imported
      // read-only; { mutable: false } makes any accidental .grantXxx() call
      // on it fail loudly at synth time instead of silently at deploy time,
      // since every permission it needs is already in its inline policy
      // (location-onboarding-guard-inline): DynamoDB Streams + table read on
      // VipLocationMapping, sns:Publish on vip-plans-alerts, and
      // kms:Decrypt/GenerateDataKey* on the data CMK (required because
      // vip-plans-alerts is SSE-KMS-encrypted and grantPublish alone does
      // not authorize KMS access to an encrypted topic).
      const guardRole = iam.Role.fromRoleArn(
        this,
        'LocationOnboardingGuardRole',
        'arn:aws:iam::165505826690:role/vip-location-onboarding-guard-role',
        { mutable: false },
      );

      // Created by an earlier deploy attempt (RemovalPolicy.RETAIN survived
      // that attempt's rollback) — import rather than re-create, same
      // pattern as every other externally-created resource in this stack.
      const guardLogGroup = logs.LogGroup.fromLogGroupName(
        this,
        'LocationOnboardingGuardLogs',
        '/aws/lambda/vip-location-onboarding-guard',
      );

      const guardFunction = new lambda.Function(this, 'LocationOnboardingGuardFunction', {
        functionName: 'vip-location-onboarding-guard',
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: 'location_onboarding_guard.lambda_handler',
        code: lambda.Code.fromAsset(
          path.join(__dirname, '../../../services/api-plans/src'),
        ),
        memorySize: 256,
        timeout: cdk.Duration.seconds(30),
        role: guardRole,
        logGroup: guardLogGroup,
        reservedConcurrentExecutions: 1,
        environment: {
          SNS_ALERTS_TOPIC_ARN: alertsTopic.topicArn,
          LOCATION_MAPPING_TABLE: 'VipLocationMapping',
          LOG_LEVEL: 'INFO',
        },
      });

      guardFunction.addEventSource(
        new DynamoEventSource(locationMappingTableWithStream, {
          startingPosition: lambda.StartingPosition.LATEST,
          batchSize: 10,
          retryAttempts: 2,
          filters: [
            lambda.FilterCriteria.filter({
              eventName: lambda.FilterRule.isEqual('INSERT'),
            }),
          ],
        }),
      );
    }

    new cdk.CfnOutput(this, 'FunctionArn', { value: this.lambdaFunction.functionArn });
    new cdk.CfnOutput(this, 'PlansTableArn', { value: this.plansTable.tableArn });

    // ── Campaign exporter Lambda ─────────────────────────────────────
    // Deployed and managed outside CloudFormation (CFN exec role lacks the
    // permissions needed to create the log group and the function already
    // exists). Import by name so the property is still usable by callers.
    this.exporterFunction = lambda.Function.fromFunctionName(
      this,
      'CampaignExporterFunction',
      'vip-admin-ui-campaign-exporter',
    ) as lambda.Function;

    new cdk.CfnOutput(this, 'ExporterFunctionArn', { value: this.exporterFunction.functionArn });
  }
}
