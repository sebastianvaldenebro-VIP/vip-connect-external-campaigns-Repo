import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as authorizers from 'aws-cdk-lib/aws-apigatewayv2-authorizers';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as path from 'path';
import { buildBundledPythonCode } from '../utils/python-bundling';
import { skipCheckovChecks } from '../utils/checkov-skip';

const VPC_SKIP = {
  id: 'CKV_AWS_117',
  comment:
    'Not internet-reachable regardless of VPC config (invoked only by API Gateway as a ' +
    "Lambda authorizer, never a direct target). Talks only to Cognito's public JWKS " +
    'endpoint over TLS, not to any private-VPC-only resource.',
};

// EngineeringPermissionBoundary explicitly denies iam:CreateRole AND
// iam:PutRolePolicy/AttachRolePolicy for the CDK CFN exec role itself — not
// a scoping gap fixable from CDK, an org-level lockdown on the deploy
// pipeline's own ability to mint or modify IAM roles (confirmed 2026-09-09:
// deploying this stack via CDK failed on CreateRole with an explicit deny
// in that boundary). Sebastian created this role manually via the console
// with the exact trust policy + permissions boundary + inline policy this
// stack would otherwise have generated (see git history for the literal
// JSON). It is imported read-only — never grant/addToPolicy against it,
// that would just fail the same way on PutRolePolicy.
const FUNCTION_ROLE_ARN =
  'arn:aws:iam::165505826690:role/vip-admin-ui-api-authorizer-role';

export interface ApiAuthorizerStackProps extends cdk.StackProps {
  readonly dataKey: kms.IKey;
  readonly userPool: cognito.IUserPool;
  readonly userPoolClient: cognito.IUserPoolClient;
  readonly permissionsBoundaryName?: string;
}

/**
 * Custom Lambda authorizer for vip-admin-ui-api — replaces HttpJwtAuthorizer.
 *
 * HttpJwtAuthorizer only proves "this is a valid token for this user pool".
 * It cannot express "but only the Agent group may reach /deny-list". This
 * Lambda verifies the same ID token itself (signature/issuer/audience/expiry
 * via Cognito's JWKS) and additionally enforces Cognito User Pool Group
 * membership per route (see services/api-authorizer/src/handler.py for the
 * exact rule). Every existing admin-ui user must be in the Admin group —
 * see auth-stack.ts's CfnUserPoolGroup comment — or this switch locks them
 * out of the whole admin UI, not just the new page.
 */
export class ApiAuthorizerStack extends cdk.Stack {
  public readonly lambdaFunction: lambda.Function;
  public readonly authorizer: authorizers.HttpLambdaAuthorizer;

  constructor(scope: Construct, id: string, props: ApiAuthorizerStackProps) {
    super(scope, id, props);

    if (props.permissionsBoundaryName) {
      const boundary = iam.ManagedPolicy.fromManagedPolicyName(
        this,
        'PermissionsBoundary',
        props.permissionsBoundaryName,
      );
      iam.PermissionsBoundary.of(this).apply(boundary);
    }

    const logGroup = new logs.LogGroup(this, 'ApiAuthorizerLogs', {
      logGroupName: '/aws/lambda/vip-admin-ui-api-authorizer',
      retention: logs.RetentionDays.ONE_YEAR,
      encryptionKey: props.dataKey,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // Imported, not created — see FUNCTION_ROLE_ARN comment above.
    const role = iam.Role.fromRoleArn(this, 'FunctionRole', FUNCTION_ROLE_ARN, {
      mutable: false,
    });

    this.lambdaFunction = new lambda.Function(this, 'FunctionAuthorizer', {
      functionName: 'vip-admin-ui-api-authorizer',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      // Self-contained bundle (handler + python-jose + cryptography) — this
      // Lambda is intentionally NOT on the shared vip_shared layer: it must
      // keep working even if a bug ships in shared code that breaks every
      // other Lambda's import, since it gates access to all of them.
      code: buildBundledPythonCode({
        assetRoot: path.join(__dirname, '../../../services/api-authorizer'),
        srcSubdir: 'src',
        // Forces manylinux2014 wheels for cryptography's compiled
        // _cffi_backend extension — without this, the *local* (non-Docker)
        // bundling fallback on an arbitrary dev machine can pull a wheel
        // built against a newer glibc than Lambda's runtime ships, and it
        // fails at cold-start with an ImportError only in production.
        extraPipArgs: [
          '--platform',
          'manylinux2014_x86_64',
          '--implementation',
          'cp',
          '--python-version',
          '3.12',
          '--only-binary=:all:',
        ],
      }),
      memorySize: 256,
      timeout: cdk.Duration.seconds(10),
      role,
      logGroup,
      // Gates every route on the admin API (63 routes across 7 downstream
      // Lambdas) on cache misses, not just its own traffic — must not be a
      // new throughput bottleneck for the whole app.
      reservedConcurrentExecutions: 100,
      environmentEncryption: props.dataKey,
      environment: {
        USER_POOL_ID: props.userPool.userPoolId,
        USER_POOL_CLIENT_ID: props.userPoolClient.userPoolClientId,
        LOG_LEVEL: 'INFO',
        POWERTOOLS_SERVICE_NAME: 'api-authorizer',
      },
    });
    skipCheckovChecks(this.lambdaFunction, [VPC_SKIP]);

    this.authorizer = new authorizers.HttpLambdaAuthorizer(
      'ApiAuthorizer',
      this.lambdaFunction,
      {
        responseTypes: [authorizers.HttpLambdaResponseType.SIMPLE],
        // $context.routeKey is required alongside the bearer token:
        // is_route_allowed() decides per route (Admin/Agent/deny), but API
        // Gateway's authorizer cache keys purely on identitySource. Without
        // routeKey, a cached allow on one route (e.g. an Agent's /deny-list
        // call) replays as an allow on ANY other route hit with the same
        // token within the cache TTL — a real cross-route authorization
        // bypass, not a theoretical one (this is AWS's own documented
        // failure mode for multi-route Lambda authorizers with SIMPLE
        // responses and a token-only identity source).
        identitySource: ['$request.header.Authorization', '$context.routeKey'],
        // Short cache (default is 5 minutes) — a denied Agent user re-added to
        // Admin, or vice versa, should regain/lose access within seconds of a
        // fresh token, not up to 5 minutes later.
        resultsCacheTtl: cdk.Duration.seconds(60),
      },
    );

    new cdk.CfnOutput(this, 'FunctionArn', {
      value: this.lambdaFunction.functionArn,
    });
  }
}
