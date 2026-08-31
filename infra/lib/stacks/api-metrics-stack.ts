import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as path from 'path';
import { buildSharedLayer } from '../utils/shared-layer';

export interface ApiMetricsStackProps extends cdk.StackProps {
  readonly adminAuditTable: dynamodb.ITable;
  readonly dataKey: kms.IKey;
  readonly connectInstanceId: string;
  readonly permissionsBoundaryName?: string;
  /** Branded campaign monitoring tables — optional; collector Lambda skips if absent */
  readonly activeBrandedCampaignsTable?: dynamodb.ITable;
  readonly brandedRunSummaryTable?: dynamodb.ITable;
  readonly brandedCampaignMetricsTable?: dynamodb.ITable;
  readonly agentSnapshotTable?: dynamodb.ITable;
  /** Progressive campaign queue table — collector writes outcomes per DIALED contact */
  readonly progressiveCampaignQueueTable?: dynamodb.ITable;
}

export class ApiMetricsStack extends cdk.Stack {
  public readonly lambdaFunction: lambda.Function;

  constructor(scope: Construct, id: string, props: ApiMetricsStackProps) {
    super(scope, id, props);

    if (props.permissionsBoundaryName) {
      const boundary = iam.ManagedPolicy.fromManagedPolicyName(
        this,
        'PermissionsBoundary',
        props.permissionsBoundaryName,
      );
      iam.PermissionsBoundary.of(this).apply(boundary);
    }

    const logGroup = new logs.LogGroup(this, 'ApiMetricsLogs', {
      logGroupName: '/aws/lambda/vip-admin-ui-api-metrics',
      retention: logs.RetentionDays.ONE_YEAR,
      encryptionKey: props.dataKey,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const role = new iam.Role(this, 'FunctionRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Execution role for api-metrics Lambda',
    });
    logGroup.grantWrite(role);

    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'CloudWatchMetrics',
        actions: [
          'cloudwatch:GetMetricStatistics',
          'cloudwatch:GetMetricData',
        ],
        resources: ['*'],
      }),
    );

    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ConnectMetrics',
        actions: [
          'connect:GetMetricDataV2',
          'connect:GetCurrentMetricData',
          'connect:GetCurrentUserData',
          'connect:SearchContacts',
          'connect:ListRoutingProfiles',
          'connect:ListAgentStatuses',
          'connect:DescribeUser',
        ],
        resources: [
          `arn:aws:connect:${this.region}:${this.account}:instance/${props.connectInstanceId}`,
          `arn:aws:connect:${this.region}:${this.account}:instance/${props.connectInstanceId}/*`,
        ],
      }),
    );

    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ConnectCampaignsRead',
        actions: [
          'connect-campaigns:ListCampaigns',
          'connect-campaigns:DescribeCampaign',
          'connect-campaigns:GetCampaignState',
        ],
        resources: [
          `arn:aws:connect-campaigns:${this.region}:${this.account}:campaign/*`,
        ],
      }),
    );

    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'AuditRead',
        actions: [
          'dynamodb:Query',
          'dynamodb:Scan',
          'dynamodb:GetItem',
        ],
        resources: [
          props.adminAuditTable.tableArn,
          `${props.adminAuditTable.tableArn}/index/*`,
        ],
      }),
    );

    props.dataKey.grantDecrypt(role);

    // Grant api-metrics Lambda read on branded tables (needed by /metrics/branded/* endpoints)
    if (props.brandedRunSummaryTable) {
      props.brandedRunSummaryTable.grantReadData(role);
    }
    if (props.brandedCampaignMetricsTable) {
      props.brandedCampaignMetricsTable.grantReadData(role);
    }

    const sharedLayer = buildSharedLayer(this);

    this.lambdaFunction = new lambda.Function(this, 'FunctionMetrics', {
      functionName: 'vip-admin-ui-api-metrics',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: lambda.Code.fromAsset(
        path.join(__dirname, '../../../services/api-metrics/src'),
      ),
      layers: [sharedLayer],
      memorySize: 512,
      timeout: cdk.Duration.seconds(30),
      role,
      logGroup,
      reservedConcurrentExecutions: 10,
      environment: {
        CONNECT_INSTANCE_ID: props.connectInstanceId,
        AUDIT_TABLE: props.adminAuditTable.tableName,
        DATA_KEY_ARN: props.dataKey.keyArn,
        LOG_LEVEL: 'INFO',
        POWERTOOLS_SERVICE_NAME: 'api-metrics',
      },
    });

    if (props.brandedRunSummaryTable) {
      this.lambdaFunction.addEnvironment(
        'BRANDED_RUN_SUMMARY_TABLE', props.brandedRunSummaryTable.tableName,
      );
    }
    if (props.brandedCampaignMetricsTable) {
      this.lambdaFunction.addEnvironment(
        'BRANDED_CAMPAIGN_METRICS_TABLE', props.brandedCampaignMetricsTable.tableName,
      );
    }

    new cdk.CfnOutput(this, 'FunctionArn', { value: this.lambdaFunction.functionArn });

    // ── Branded Campaign Metrics Collector ────────────────────────────
    // Polls active branded campaigns every 1 minute, writes disposition + agent snapshots.
    // Only provisioned when the branded DDB tables are wired in (optional props).
    if (
      props.activeBrandedCampaignsTable &&
      props.brandedCampaignMetricsTable &&
      props.agentSnapshotTable
    ) {
      // LogGroup pre-exists (created by first CDK deploy attempt; RETAIN policy kept it).
      // Import instead of creating to avoid AlreadyExists error.
      const collectorLogGroup = logs.LogGroup.fromLogGroupName(
        this, 'CollectorLogs',
        '/aws/lambda/vip-admin-branded-metrics-collector',
      );

      const collectorRole = new iam.Role(this, 'CollectorRole', {
        assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
        description: 'Execution role for branded campaign metrics collector',
      });
      collectorLogGroup.grantWrite(collectorRole);
      props.dataKey.grantEncryptDecrypt(collectorRole);

      props.activeBrandedCampaignsTable.grantReadData(collectorRole);
      props.brandedCampaignMetricsTable.grantWriteData(collectorRole);
      props.agentSnapshotTable.grantWriteData(collectorRole);
      // dynamodb:Query on VipBrandedCampaignMetrics (BD-017 stall-detection check
      // reads its own prior snapshots) granted via CLI, NOT grantReadData here —
      // cfn-exec-role lacks iam:PutRolePolicy on this role (EngineeringPermissionBoundary),
      // confirmed by a failed deploy 2026-08-27 that also left the stack in
      // UPDATE_ROLLBACK_FAILED (recovered via continue-update-rollback --resources-to-skip).
      // Granted directly:
      //   aws iam put-role-policy --role-name <CollectorRole physical name> \
      //     --policy-name BrandedMetricsHistoryRead --policy-document '{"Version":"2012-10-17",
      //     "Statement":[{"Sid":"BrandedMetricsHistoryRead","Effect":"Allow","Action":"dynamodb:Query",
      //     "Resource":"arn:aws:dynamodb:us-east-1:165505826690:table/VipBrandedCampaignMetrics"}]}'
      // Do NOT add grantReadData/grantReadWriteData for this table here — CDK will retry
      // reconciling its own managed policy on every future deploy and hit the same denial.

      collectorRole.addToPolicy(
        new iam.PolicyStatement({
          sid: 'ConnectReadMetrics',
          actions: [
            'connect:SearchContacts',
            'connect:GetCurrentMetricData',
            'connect:DescribeContact',
          ],
          resources: [
            `arn:aws:connect:${this.region}:${this.account}:instance/${props.connectInstanceId}`,
            `arn:aws:connect:${this.region}:${this.account}:instance/${props.connectInstanceId}/*`,
          ],
        }),
      );

      if (props.progressiveCampaignQueueTable) {
        collectorRole.addToPolicy(
          new iam.PolicyStatement({
            sid: 'ProgressiveCampaignQueueOutcomes',
            actions: ['dynamodb:Query', 'dynamodb:UpdateItem'],
            resources: [props.progressiveCampaignQueueTable.tableArn],
          }),
        );
      }

      // PutMetricData for ActiveBrandedCampaigns + StuckBrandedCampaigns in VipBrandedMonitor namespace
      collectorRole.addToPolicy(
        new iam.PolicyStatement({
          sid: 'CloudWatchEmitMetrics',
          actions: ['cloudwatch:PutMetricData'],
          resources: ['*'],
          conditions: {
            StringEquals: { 'cloudwatch:namespace': 'VipBrandedMonitor' },
          },
        }),
      );

      const collectorFn = new lambda.Function(this, 'BrandedMetricsCollector', {
        functionName: 'vip-admin-branded-metrics-collector',
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: 'metrics_collector_handler.lambda_handler',
        code: lambda.Code.fromAsset(
          path.join(__dirname, '../../../services/api-metrics/src'),
        ),
        layers: [sharedLayer],
        memorySize: 256,
        timeout: cdk.Duration.seconds(120),
        tracing: lambda.Tracing.ACTIVE,
        role: collectorRole,
        logGroup: collectorLogGroup,
        environment: {
          ACTIVE_BRANDED_CAMPAIGNS_TABLE: props.activeBrandedCampaignsTable.tableName,
          BRANDED_CAMPAIGN_METRICS_TABLE: props.brandedCampaignMetricsTable.tableName,
          AGENT_SNAPSHOT_TABLE: props.agentSnapshotTable.tableName,
          CONNECT_INSTANCE_ID: props.connectInstanceId,
          LOG_LEVEL: 'INFO',
          ...(props.progressiveCampaignQueueTable && {
            PROGRESSIVE_CAMPAIGN_QUEUE_TABLE: props.progressiveCampaignQueueTable.tableName,
          }),
        },
      });

      // EventBridge rule created via CLI (cfn-exec-role lacks events:DescribeRule).
      // Rule name: vip-branded-metrics-collector-1min — rate(1 minute) → this Lambda.
      new cdk.CfnOutput(this, 'CollectorFunctionArn', { value: collectorFn.functionArn });
    }
  }
}
