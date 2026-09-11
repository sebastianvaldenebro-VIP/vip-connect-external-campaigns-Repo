import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as path from 'path';
import { buildSharedLayer } from '../utils/shared-layer';
import { skipCheckovChecks } from '../utils/checkov-skip';

const VPC_SKIP = {
  id: 'CKV_AWS_117',
  comment:
    'Not internet-reachable regardless of VPC config (invoked only via API Gateway ' +
    'HttpLambdaIntegration, never a direct target). Talks only to DynamoDB, already ' +
    'secured by TLS+IAM, not to any private-VPC-only resource.',
};

const DLQ_SKIP = {
  id: 'CKV_AWS_116',
  comment:
    'No DLQ — this Lambda is invoked only synchronously via the API Gateway ' +
    'integration; Lambda DLQs apply exclusively to async invocations, so one ' +
    'here would provision dead, unreachable infra.',
};

// vip-connect-deny-list already exists — created by Connect-batch-redis-refactor's
// deny-list Lambdas (connectcampaign_denylist_check/write), not by this app's CDK.
// This stack only grants access to it; it does not own or manage its lifecycle
// (in particular, never add a DeletionPolicy/removalPolicy for it here).
const DENY_LIST_TABLE_NAME = 'vip-connect-deny-list';

// EngineeringPermissionBoundary explicitly denies iam:CreateRole AND
// iam:PutRolePolicy/AttachRolePolicy for the CDK CFN exec role itself — not
// a scoping gap fixable from CDK, an org-level lockdown on the deploy
// pipeline's own ability to mint or modify IAM roles (confirmed 2026-09-09:
// deploying this stack via CDK failed on CreateRole with an explicit deny
// in that boundary). Sebastian created this role manually via the console
// with the exact trust policy + permissions boundary + inline policy this
// stack would otherwise have generated. It is imported read-only — never
// grant/addToPolicy against it, that would just fail the same way on
// PutRolePolicy.
const FUNCTION_ROLE_ARN = 'arn:aws:iam::165505826690:role/vip-admin-ui-api-deny-list-role';

export interface ApiDenyListStackProps extends cdk.StackProps {
  readonly adminAuditTable: dynamodb.ITable;
  readonly dataKey: kms.IKey;
  readonly permissionsBoundaryName?: string;
}

export class ApiDenyListStack extends cdk.Stack {
  public readonly lambdaFunction: lambda.Function;

  constructor(scope: Construct, id: string, props: ApiDenyListStackProps) {
    super(scope, id, props);

    if (props.permissionsBoundaryName) {
      const boundary = iam.ManagedPolicy.fromManagedPolicyName(
        this,
        'PermissionsBoundary',
        props.permissionsBoundaryName,
      );
      iam.PermissionsBoundary.of(this).apply(boundary);
    }

    const logGroup = new logs.LogGroup(this, 'ApiDenyListLogs', {
      logGroupName: '/aws/lambda/vip-admin-ui-api-deny-list',
      retention: logs.RetentionDays.ONE_YEAR,
      encryptionKey: props.dataKey,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // Imported, not created — see FUNCTION_ROLE_ARN comment above. Its
    // inline policy already grants exactly GetItem/PutItem/Scan on the
    // deny-list table, PutItem on the audit table, and KMS Decrypt/Encrypt
    // on dataKey.
    const role = iam.Role.fromRoleArn(this, 'FunctionRole', FUNCTION_ROLE_ARN, {
      mutable: false,
    });

    this.lambdaFunction = new lambda.Function(this, 'FunctionDenyList', {
      functionName: 'vip-admin-ui-api-deny-list',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: lambda.Code.fromAsset(
        path.join(__dirname, '../../../services/api-deny-list/src'),
      ),
      layers: [buildSharedLayer(this)],
      memorySize: 512,
      timeout: cdk.Duration.seconds(30),
      role,
      logGroup,
      reservedConcurrentExecutions: 10,
      environmentEncryption: props.dataKey,
      // No DLQ — this Lambda is invoked only synchronously via the API
      // Gateway integration; Lambda DLQs apply exclusively to async
      // invocations, so one here would provision dead, unreachable infra.
      environment: {
        DENY_LIST_TABLE: DENY_LIST_TABLE_NAME,
        AUDIT_TABLE: props.adminAuditTable.tableName,
        DATA_KEY_ARN: props.dataKey.keyArn,
        LOG_LEVEL: 'INFO',
        POWERTOOLS_SERVICE_NAME: 'api-deny-list',
      },
    });
    skipCheckovChecks(this.lambdaFunction, [VPC_SKIP, DLQ_SKIP]);

    new cdk.CfnOutput(this, 'FunctionArn', { value: this.lambdaFunction.functionArn });
  }
}
