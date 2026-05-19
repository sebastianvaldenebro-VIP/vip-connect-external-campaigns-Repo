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
          'connect:SearchContacts',
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

    this.lambdaFunction = new lambda.Function(this, 'FunctionMetrics', {
      functionName: 'vip-admin-ui-api-metrics',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: lambda.Code.fromAsset(
        path.join(__dirname, '../../../services/api-metrics/src'),
      ),
      layers: [buildSharedLayer(this)],
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

    new cdk.CfnOutput(this, 'FunctionArn', { value: this.lambdaFunction.functionArn });
  }
}
