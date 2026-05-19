import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
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
  };
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
    });

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
        ],
        resources: [
          `arn:aws:events:${this.region}:${this.account}:rule/vip-plan-*`,
          `arn:aws:events:${this.region}:${this.account}:rule/vip-sched-*`,
        ],
      }),
    );

    // Lambda self-permission — add/remove resource-based policy for EventBridge invocation
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'LambdaSelfPermission',
        actions: ['lambda:AddPermission', 'lambda:RemovePermission'],
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

    // CloudWatch PutMetricData — used by prestart_check to emit stuck-run metrics
    // PutMetricData does not support resource-level restrictions
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'CloudWatchPutMetric',
        actions: ['cloudwatch:PutMetricData'],
        resources: ['*'],
      }),
    );

    props.dataKey.grantEncryptDecrypt(role);

    // ── Lambda function ──────────────────────────────────────────────
    this.lambdaFunction = new lambda.Function(this, 'FunctionPlans', {
      functionName: 'vip-admin-ui-api-plans',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: lambda.Code.fromAsset(
        path.join(__dirname, '../../../services/api-plans/src'),
      ),
      layers: [buildSharedLayer(this)],
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
        PROFILES_DOMAIN_NAME: props.profilesDomainName,
        PLANS_TABLE_NAME: this.plansTable.tableName,
        AUDIT_TABLE: props.adminAuditTable.tableName,
        DATA_KEY_ARN: props.dataKey.keyArn,
        REDIS_HOST: props.redis.host,
        REDIS_PORT: String(props.redis.port),
        TEAM: props.redis.team,
        // LAMBDA_FUNCTION_ARN is set post-creation via addEnvironment
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

    new cdk.CfnOutput(this, 'FunctionArn', { value: this.lambdaFunction.functionArn });
    new cdk.CfnOutput(this, 'PlansTableArn', { value: this.plansTable.tableArn });

    // ── Campaign exporter Lambda ─────────────────────────────────────
    // Separate function so the heavy awswrangler/Pandas layer does not
    // affect cold-start of the latency-sensitive HTTP API function.
    // EventBridge daily rule created manually (CFN role lacks events:DescribeRule).
    const dataExportBucket = s3.Bucket.fromBucketName(
      this,
      'CampaignDataExportBucket',
      'amazon-glue-etl-files',
    );

    const exporterLogGroup = new logs.LogGroup(this, 'CampaignExporterLogs', {
      logGroupName: '/aws/lambda/vip-admin-ui-campaign-exporter',
      retention: logs.RetentionDays.ONE_YEAR,
      encryptionKey: props.dataKey,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const exporterRole = new iam.Role(this, 'CampaignExporterRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Execution role for campaign-exporter Lambda',
    });
    exporterLogGroup.grantWrite(exporterRole);

    // DynamoDB — read plans table for historical campaign context
    this.plansTable.grantReadData(exporterRole);
    props.dataKey.grantEncryptDecrypt(exporterRole);

    // Connect Campaigns V2 — read-only for export
    exporterRole.addToPolicy(new iam.PolicyStatement({
      sid: 'ConnectCampaignsExportRead',
      actions: [
        'connect-campaigns:ListCampaigns',
        'connect-campaigns:GetCampaignState',
        'connect-campaigns:DescribeCampaign',
      ],
      resources: [
        `arn:aws:connect-campaigns:${this.region}:${this.account}:campaign/*`,
        `arn:aws:connect-campaigns:${this.region}:${this.account}:*`,
      ],
    }));

    // CloudWatch — lead count metrics per campaign
    exporterRole.addToPolicy(new iam.PolicyStatement({
      sid: 'CloudWatchGetMetrics',
      actions: ['cloudwatch:GetMetricData'],
      resources: ['*'],
    }));

    // S3 ETL bucket — write Parquet snapshots
    dataExportBucket.grantReadWrite(exporterRole);

    // Glue — trigger Snowflake loader after each export
    exporterRole.addToPolicy(new iam.PolicyStatement({
      sid: 'GlueStartSnowflakeLoader',
      actions: ['glue:StartJobRun'],
      resources: [
        `arn:aws:glue:${this.region}:${this.account}:job/specialOps-prod-snowflake-loader-glue`,
      ],
    }));

    const awsSdkPandasLayer = lambda.LayerVersion.fromLayerVersionArn(
      this,
      'AwsSdkPandasLayer',
      `arn:aws:lambda:${this.region}:336392948345:layer:AWSSDKPandas-Python312:24`,
    );

    this.exporterFunction = new lambda.Function(this, 'CampaignExporterFunction', {
      functionName: 'vip-admin-ui-campaign-exporter',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'campaign_exporter_handler.lambda_handler',
      code: lambda.Code.fromAsset(
        path.join(__dirname, '../../../services/api-plans/src'),
      ),
      layers: [buildSharedLayer(this), awsSdkPandasLayer],
      memorySize: 1024,
      timeout: cdk.Duration.minutes(10),
      role: exporterRole,
      logGroup: exporterLogGroup,
      reservedConcurrentExecutions: 1,
      environment: {
        PLANS_TABLE_NAME: this.plansTable.tableName,
        DATA_EXPORT_BUCKET: dataExportBucket.bucketName,
        GLUE_JOB_NAME: 'specialOps-prod-snowflake-loader-glue',
        SF_DATABASE: 'PRD_RAW_DB',
        SF_SCHEMA: 'AMAZON_CONNECT',
        SF_SRC_CFG_URI: 's3://config-json-etl-jobs/vip-connect/campaign_mapping_cfg.json',
        LOG_LEVEL: 'INFO',
        POWERTOOLS_SERVICE_NAME: 'campaign-exporter',
      },
    });

    new cdk.CfnOutput(this, 'ExporterFunctionArn', { value: this.exporterFunction.functionArn });
  }
}
