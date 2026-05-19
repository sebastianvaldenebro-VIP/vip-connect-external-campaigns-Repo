import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as path from 'path';
import { buildSharedLayer } from '../utils/shared-layer';

export interface ApiCampaignsStackProps extends cdk.StackProps {
  readonly adminAuditTable: dynamodb.ITable;
  readonly dataKey: kms.IKey;
  readonly connectInstanceId: string;
  readonly profilesDomainName: string;
  readonly permissionsBoundaryName?: string;
}

export class ApiCampaignsStack extends cdk.Stack {
  public readonly lambdaFunction: lambda.Function;

  constructor(scope: Construct, id: string, props: ApiCampaignsStackProps) {
    super(scope, id, props);

    if (props.permissionsBoundaryName) {
      const boundary = iam.ManagedPolicy.fromManagedPolicyName(
        this,
        'PermissionsBoundary',
        props.permissionsBoundaryName,
      );
      iam.PermissionsBoundary.of(this).apply(boundary);
    }

    const logGroup = new logs.LogGroup(this, 'ApiCampaignsLogs', {
      logGroupName: '/aws/lambda/vip-admin-ui-api-campaigns',
      retention: logs.RetentionDays.ONE_YEAR,
      encryptionKey: props.dataKey,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const role = new iam.Role(this, 'FunctionRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Execution role for api-campaigns Lambda',
    });
    logGroup.grantWrite(role);

    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ConnectCampaignsV2',
        actions: [
          'connect-campaigns:ListCampaigns',
          'connect-campaigns:DescribeCampaign',
          'connect-campaigns:CreateCampaign',
          'connect-campaigns:DeleteCampaign',
          'connect-campaigns:StartCampaign',
          'connect-campaigns:StopCampaign',
          'connect-campaigns:PauseCampaign',
          'connect-campaigns:ResumeCampaign',
          'connect-campaigns:GetCampaignState',
          'connect-campaigns:UpdateCampaignName',
          'connect-campaigns:UpdateCampaignSource',
          'connect-campaigns:UpdateCampaignSchedule',
          // Required when CreateCampaign includes tags. We auto-tag every
          // campaign with `owner=<instance-arn>` so the Connect console SLR
          // can DescribeCampaign — without TagResource the create call
          // fails with AccessDenied even though CreateCampaign itself is
          // permitted.
          'connect-campaigns:TagResource',
          'connect-campaigns:UntagResource',
          'connect-campaigns:ListTagsForResource',
        ],
        resources: [
          `arn:aws:connect-campaigns:${this.region}:${this.account}:campaign/*`,
        ],
      }),
    );

    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ConnectReadInstanceResources',
        actions: [
          'connect:ListQueues',
          'connect:ListContactFlows',
          // V2 CreateCampaign validates referenced resources by Describe
          // under the caller's identity. Without these, CreateCampaign
          // returns AccessDeniedException on the inner validation call.
          'connect:DescribeContactFlow',
          'connect:DescribeQueue',
          'connect:DescribeInstance',
        ],
        resources: [
          `arn:aws:connect:${this.region}:${this.account}:instance/${props.connectInstanceId}`,
          `arn:aws:connect:${this.region}:${this.account}:instance/${props.connectInstanceId}/*`,
        ],
      }),
    );

    // ListPhoneNumbersV2 + DescribePhoneNumber evaluate access against the
    // account-level phone-number resource, not the instance-scoped one
    // (see AWS IAM ref). Separate statement so the instance-scoped list
    // actions above stay tight.
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

    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'AuditWrite',
        actions: ['dynamodb:PutItem'],
        resources: [props.adminAuditTable.tableArn],
      }),
    );

    props.dataKey.grantEncryptDecrypt(role);

    this.lambdaFunction = new lambda.Function(this, 'FunctionCampaigns', {
      functionName: 'vip-admin-ui-api-campaigns',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: lambda.Code.fromAsset(
        path.join(__dirname, '../../../services/api-campaigns/src'),
      ),
      layers: [buildSharedLayer(this)],
      memorySize: 512,
      timeout: cdk.Duration.seconds(30),
      role,
      logGroup,
      reservedConcurrentExecutions: 10,
      environment: {
        CONNECT_INSTANCE_ID: props.connectInstanceId,
        AWS_ACCOUNT_ID: this.account,
        PROFILES_DOMAIN_NAME: props.profilesDomainName,
        AUDIT_TABLE: props.adminAuditTable.tableName,
        DATA_KEY_ARN: props.dataKey.keyArn,
        LOG_LEVEL: 'INFO',
        POWERTOOLS_SERVICE_NAME: 'api-campaigns',
      },
    });

    new cdk.CfnOutput(this, 'FunctionArn', { value: this.lambdaFunction.functionArn });
  }
}
