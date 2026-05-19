import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as path from 'path';

export interface FeederStackProps extends cdk.StackProps {
  readonly filtersTable: dynamodb.ITable;
  readonly trackingTable: dynamodb.ITable;
  readonly auditTable: dynamodb.ITable;
  readonly dataKey: kms.IKey;

  readonly vpcId: string;
  readonly privateSubnetIds: string[];
  readonly securityGroupId: string;

  readonly redisHost: string;
  readonly redisPort: string;
  readonly redisPasswordSecretArn: string;

  readonly team: string;
  readonly connectInstanceId: string;
  readonly scheduleMinutes: number;
  readonly permissionsBoundaryName?: string;
}

export class FeederStack extends cdk.Stack {
  public readonly feederFunction: lambda.Function;

  constructor(scope: Construct, id: string, props: FeederStackProps) {
    super(scope, id, props);

    if (props.permissionsBoundaryName) {
      const boundary = iam.ManagedPolicy.fromManagedPolicyName(
        this,
        'PermissionsBoundary',
        props.permissionsBoundaryName,
      );
      iam.PermissionsBoundary.of(this).apply(boundary);
    }

    const vpc = ec2.Vpc.fromVpcAttributes(this, 'Vpc', {
      vpcId: props.vpcId,
      availabilityZones: cdk.Fn.getAzs(this.region),
      privateSubnetIds: props.privateSubnetIds,
    });

    const securityGroup = ec2.SecurityGroup.fromSecurityGroupId(
      this,
      'LambdaSg',
      props.securityGroupId,
      { mutable: false },
    );

    const logGroup = new logs.LogGroup(this, 'FeederLogGroup', {
      logGroupName: '/aws/lambda/vip-external-campaigns-feeder',
      retention: logs.RetentionDays.ONE_YEAR,
      encryptionKey: props.dataKey,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const role = new iam.Role(this, 'FeederRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Execution role for external campaigns feeder Lambda',
    });

    role.addManagedPolicy(
      iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaVPCAccessExecutionRole'),
    );

    logGroup.grantWrite(role);

    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'DynamoDbRead',
        actions: ['dynamodb:GetItem', 'dynamodb:Query', 'dynamodb:Scan'],
        resources: [
          props.filtersTable.tableArn,
          props.trackingTable.tableArn,
          `${props.trackingTable.tableArn}/index/*`,
        ],
      }),
    );
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'DynamoDbWriteTracking',
        actions: ['dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:BatchWriteItem'],
        resources: [props.trackingTable.tableArn],
      }),
    );
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'DynamoDbWriteAudit',
        actions: ['dynamodb:PutItem'],
        resources: [props.auditTable.tableArn],
      }),
    );
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ConnectCampaigns',
        actions: [
          'connect-campaigns:PutDialRequestBatch',
          'connect-campaigns:GetCampaignState',
          'connect-campaigns:DescribeCampaign',
        ],
        resources: [`arn:aws:connect-campaigns:${this.region}:${this.account}:campaign/*`],
      }),
    );
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'CloudWatchMetrics',
        actions: ['cloudwatch:PutMetricData'],
        resources: ['*'],
        conditions: {
          StringEquals: { 'cloudwatch:namespace': 'VipConnect/ExternalCampaigns' },
        },
      }),
    );
    if (props.redisPasswordSecretArn) {
      role.addToPolicy(
        new iam.PolicyStatement({
          sid: 'RedisSecret',
          actions: ['secretsmanager:GetSecretValue'],
          resources: [props.redisPasswordSecretArn],
        }),
      );
    }
    props.dataKey.grantEncryptDecrypt(role);

    const baseEnv: Record<string, string> = {
      REDIS_HOST: props.redisHost,
      REDIS_PORT: props.redisPort,
      TEAM: props.team,
      CONNECT_INSTANCE_ID: props.connectInstanceId,
      FILTERS_TABLE: props.filtersTable.tableName,
      TRACKING_TABLE: props.trackingTable.tableName,
      AUDIT_TABLE: props.auditTable.tableName,
      METRICS_NAMESPACE: 'VipConnect/ExternalCampaigns',
      LOG_LEVEL: 'INFO',
      POWERTOOLS_SERVICE_NAME: 'external-campaigns-feeder',
    };
    if (props.redisPasswordSecretArn) {
      baseEnv.REDIS_PASSWORD_SECRET_ARN = props.redisPasswordSecretArn;
    }

    this.feederFunction = new lambda.Function(this, 'FeederFunction', {
      functionName: 'vip-external-campaigns-feeder',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../../services/feeder/src')),
      memorySize: 1024,
      timeout: cdk.Duration.minutes(5),
      role,
      vpc,
      vpcSubnets: { subnets: props.privateSubnetIds.map((id, i) => ec2.Subnet.fromSubnetId(this, `SubnetRef${i}`, id)) },
      securityGroups: [securityGroup],
      reservedConcurrentExecutions: 1,
      logGroup,
      environment: baseEnv,
    });

    new events.Rule(this, 'FeederSchedule', {
      ruleName: 'vip-external-campaigns-feeder-schedule',
      description: `Trigger feeder every ${props.scheduleMinutes} minutes`,
      schedule: events.Schedule.rate(cdk.Duration.minutes(props.scheduleMinutes)),
      targets: [new targets.LambdaFunction(this.feederFunction)],
    });

    new cdk.CfnOutput(this, 'FeederFunctionArn', { value: this.feederFunction.functionArn });
    new cdk.CfnOutput(this, 'FeederLogGroupName', { value: logGroup.logGroupName });
  }
}
