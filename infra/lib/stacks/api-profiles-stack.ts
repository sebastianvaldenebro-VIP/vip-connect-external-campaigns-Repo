import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as path from 'path';
import { buildSharedLayer } from '../utils/shared-layer';
import { skipCheckovChecks } from '../utils/checkov-skip';

const VPC_SKIP = {
  id: 'CKV_AWS_117',
  comment:
    'Not internet-reachable regardless of VPC config (invoked only via API Gateway ' +
    'HttpLambdaIntegration, never a direct target). Talks only to the Customer ' +
    'Profiles public API, already secured by TLS+IAM, not to any private-VPC-only ' +
    'resource (contrast FunctionPlans/FunctionSegments, which correctly use VPC for ' +
    'their ElastiCache Redis dependency).',
};

export interface ApiProfilesStackProps extends cdk.StackProps {
  readonly dataKey: kms.IKey;
  readonly profilesDomainName: string;
  readonly profileObjectType?: string;
  readonly permissionsBoundaryName?: string;
}

export class ApiProfilesStack extends cdk.Stack {
  public readonly lambdaFunction: lambda.Function;

  constructor(scope: Construct, id: string, props: ApiProfilesStackProps) {
    super(scope, id, props);

    if (props.permissionsBoundaryName) {
      const boundary = iam.ManagedPolicy.fromManagedPolicyName(
        this,
        'PermissionsBoundary',
        props.permissionsBoundaryName,
      );
      iam.PermissionsBoundary.of(this).apply(boundary);
    }

    const logGroup = new logs.LogGroup(this, 'ApiProfilesLogs', {
      logGroupName: '/aws/lambda/vip-admin-ui-api-profiles',
      retention: logs.RetentionDays.ONE_YEAR,
      encryptionKey: props.dataKey,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const role = new iam.Role(this, 'FunctionRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Execution role for api-profiles Lambda',
    });
    logGroup.grantWrite(role);

    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'CustomerProfilesRead',
        actions: [
          'profile:SearchProfiles',
          'profile:BatchGetProfile',
          'profile:ListProfileObjects',
          'profile:GetCalculatedAttributeForProfile',
          'profile:ListCalculatedAttributesForProfile',
          'profile:GetProfileObjectType',
        ],
        resources: [
          `arn:aws:profile:${this.region}:${this.account}:domains/${props.profilesDomainName}`,
          `arn:aws:profile:${this.region}:${this.account}:domains/${props.profilesDomainName}/*`,
        ],
      }),
    );

    props.dataKey.grantDecrypt(role);

    const dlq = new sqs.Queue(this, 'DeadLetterQueue', {
      queueName: 'vip-admin-ui-api-profiles-dlq',
      encryption: sqs.QueueEncryption.KMS,
      encryptionMasterKey: props.dataKey,
      retentionPeriod: cdk.Duration.days(14),
    });

    this.lambdaFunction = new lambda.Function(this, 'FunctionProfiles', {
      functionName: 'vip-admin-ui-api-profiles',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: lambda.Code.fromAsset(
        path.join(__dirname, '../../../services/api-profiles/src'),
      ),
      layers: [buildSharedLayer(this)],
      memorySize: 512,
      timeout: cdk.Duration.seconds(30),
      role,
      logGroup,
      reservedConcurrentExecutions: 10,
      environmentEncryption: props.dataKey,
      environment: {
        PROFILES_DOMAIN_NAME: props.profilesDomainName,
        PROFILE_OBJECT_TYPE: props.profileObjectType ?? 'leads-data-mapping',
        DATA_KEY_ARN: props.dataKey.keyArn,
        LOG_LEVEL: 'INFO',
        POWERTOOLS_SERVICE_NAME: 'api-profiles',
      },
    });
    skipCheckovChecks(this.lambdaFunction, [VPC_SKIP]);

    // deadLetterQueue prop intentionally omitted above — see api-segments-stack.ts
    // for the full explanation. Granted manually via a separate inline policy:
    //   aws iam put-role-policy --role-name <FunctionRole physical name> \
    //     --policy-name dlq-send-message --policy-document '{"Version":
    //     "2012-10-17","Statement":[{"Sid":"DlqSendMessage","Effect":"Allow",
    //     "Action":"sqs:SendMessage","Resource":"arn:aws:sqs:us-east-1:165505826690:
    //     vip-admin-ui-api-profiles-dlq"}]}'
    (this.lambdaFunction.node.defaultChild as lambda.CfnFunction).deadLetterConfig = {
      targetArn: dlq.queueArn,
    };

    new cdk.CfnOutput(this, 'FunctionArn', { value: this.lambdaFunction.functionArn });
  }
}
