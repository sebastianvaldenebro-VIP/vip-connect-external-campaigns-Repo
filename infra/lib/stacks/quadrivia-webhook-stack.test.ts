import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import * as kms from 'aws-cdk-lib/aws-kms';
import {
  QuadriviaWebhookStack,
  QuadriviaWebhookStackProps,
} from './quadrivia-webhook-stack';

const ENV = { account: '165505826690', region: 'us-east-1' };
const INSTANCE_ID = '6b3f17ba-68a4-472a-9b20-db1991507009';
const CONNECT_INSTANCE_ARN = `arn:aws:connect:us-east-1:165505826690:instance/${INSTANCE_ID}`;
const TASK_TEMPLATE_ID = '11111111-2222-3333-4444-555555555555';

// Synthetic placeholders — this stack has no real domain/cert/truststore yet
// (see the mtlsDomain prop comment in the stack).
const MTLS_DOMAIN = {
  domainName: 'quadrivia-webhook.example.invalid',
  certificateArn: 'arn:aws:acm:us-east-1:165505826690:certificate/00000000-0000-0000-0000-000000000000',
  truststoreBucketName: 'vip-quadrivia-truststore-example',
  truststoreKey: 'quadrivia/truststore.pem',
};

/**
 * `dataKey` is a real kms.Key in a separate fixture stack rather than an
 * imported one, because the stack calls `dataKey.addToResourcePolicy(...)` —
 * a silent no-op on an imported key, which would make the access-log grant
 * assertion untestable (same reasoning as api-stack.test.ts).
 */
function buildStack(propsOverride: Partial<QuadriviaWebhookStackProps> = {}) {
  const app = new cdk.App();
  const fixtures = new cdk.Stack(app, 'QuadriviaFixtures', { env: ENV });
  const dataKey = new kms.Key(fixtures, 'FixtureDataKey', { enableKeyRotation: true });

  const stack = new QuadriviaWebhookStack(app, 'TestQuadriviaWebhookStack', {
    env: ENV,
    dataKey,
    connectInstanceArn: CONNECT_INSTANCE_ARN,
    taskTemplateId: TASK_TEMPLATE_ID,
    ownerEmail: 'placeholder.owner@medwork.io',
    team: 'engineering',
    ...propsOverride,
  });
  return { stack, fixtures };
}

function templateOf(propsOverride: Partial<QuadriviaWebhookStackProps> = {}) {
  return Template.fromStack(buildStack(propsOverride).stack);
}

/** Every inline statement across every policy in the template. */
function allStatements(template: Template): any[] {
  return Object.values(template.findResources('AWS::IAM::Policy')).flatMap(
    (policy: any) => policy.Properties.PolicyDocument.Statement,
  );
}

function statementBySid(template: Template, sid: string): any {
  const match = allStatements(template).filter((s) => s.Sid === sid);
  expect(match).toHaveLength(1);
  return match[0];
}

describe('QuadriviaWebhookStack', () => {
  describe('trust boundary / API surface', () => {
    it('creates its own HTTP API, not a route on the admin API', () => {
      const template = templateOf();
      template.resourceCountIs('AWS::ApiGatewayV2::Api', 1);
      template.hasResourceProperties('AWS::ApiGatewayV2::Api', {
        Name: 'vip-quadrivia-callback-webhook',
        ProtocolType: 'HTTP',
      });
    });

    it('disables the execute-api endpoint so mTLS on the custom domain cannot be bypassed', () => {
      templateOf().hasResourceProperties('AWS::ApiGatewayV2::Api', {
        DisableExecuteApiEndpoint: true,
      });
    });

    it('declares no CORS configuration at all (server-to-server traffic)', () => {
      const apis = templateOf().findResources('AWS::ApiGatewayV2::Api');
      for (const api of Object.values(apis) as any[]) {
        expect(api.Properties.CorsConfiguration).toBeUndefined();
      }
    });

    it('exposes exactly one route: POST /callbacks', () => {
      const template = templateOf();
      template.resourceCountIs('AWS::ApiGatewayV2::Route', 1);
      template.hasResourceProperties('AWS::ApiGatewayV2::Route', {
        RouteKey: 'POST /callbacks',
      });
    });

    it('attaches no API Gateway authorizer — HMAC is verified inline in the Lambda', () => {
      const template = templateOf();
      template.resourceCountIs('AWS::ApiGatewayV2::Authorizer', 0);
      const routes = template.findResources('AWS::ApiGatewayV2::Route');
      for (const route of Object.values(routes) as any[]) {
        expect(route.Properties.AuthorizerId).toBeUndefined();
        expect(route.Properties.AuthorizationType ?? 'NONE').toEqual('NONE');
      }
    });

    it('proxies the route to the webhook Lambda', () => {
      templateOf().hasResourceProperties('AWS::ApiGatewayV2::Integration', {
        IntegrationType: 'AWS_PROXY',
        PayloadFormatVersion: '2.0',
      });
    });
  });

  describe('layer 1 — mTLS custom domain', () => {
    it('creates no domain or mapping when mtlsDomain is omitted (fail-closed pending the domain decision)', () => {
      const template = templateOf();
      template.resourceCountIs('AWS::ApiGatewayV2::DomainName', 0);
      template.resourceCountIs('AWS::ApiGatewayV2::ApiMapping', 0);
      expect(buildStack().stack.domainName).toBeUndefined();
    });

    it('configures the truststore and TLS 1.2 on the domain when mtlsDomain is supplied', () => {
      const template = templateOf({ mtlsDomain: MTLS_DOMAIN });
      template.hasResourceProperties('AWS::ApiGatewayV2::DomainName', {
        DomainName: MTLS_DOMAIN.domainName,
        DomainNameConfigurations: Match.arrayWith([
          Match.objectLike({
            CertificateArn: MTLS_DOMAIN.certificateArn,
            SecurityPolicy: 'TLS_1_2',
            EndpointType: 'REGIONAL',
          }),
        ]),
        MutualTlsAuthentication: {
          TruststoreUri: `s3://${MTLS_DOMAIN.truststoreBucketName}/${MTLS_DOMAIN.truststoreKey}`,
        },
      });
      template.resourceCountIs('AWS::ApiGatewayV2::ApiMapping', 1);
    });

    it('pins the truststore object version when one is given', () => {
      templateOf({
        mtlsDomain: { ...MTLS_DOMAIN, truststoreVersion: 'v-abc123' },
      }).hasResourceProperties('AWS::ApiGatewayV2::DomainName', {
        MutualTlsAuthentication: Match.objectLike({ TruststoreVersion: 'v-abc123' }),
      });
    });

    it('wires an ownership certificate when the cert is imported/private-CA', () => {
      templateOf({
        mtlsDomain: {
          ...MTLS_DOMAIN,
          ownershipCertificateArn:
            'arn:aws:acm:us-east-1:165505826690:certificate/99999999-9999-9999-9999-999999999999',
        },
      }).hasResourceProperties('AWS::ApiGatewayV2::DomainName', {
        DomainNameConfigurations: Match.arrayWith([
          Match.objectLike({
            OwnershipVerificationCertificateArn:
              'arn:aws:acm:us-east-1:165505826690:certificate/99999999-9999-9999-9999-999999999999',
          }),
        ]),
      });
    });

    it('emits the Route53 ALIAS target outputs only when a domain exists', () => {
      const withDomain = templateOf({ mtlsDomain: MTLS_DOMAIN });
      withDomain.hasOutput('RegionalDomainName', {});
      withDomain.hasOutput('RegionalHostedZoneId', {});
      withDomain.hasOutput('WebhookUrl', {});

      const withoutDomain = templateOf();
      expect(Object.keys(withoutDomain.findOutputs('RegionalDomainName'))).toHaveLength(0);
      expect(Object.keys(withoutDomain.findOutputs('WebhookUrl'))).toHaveLength(0);
    });

    it('does not create a Route53 record itself (hosted zone is an open decision)', () => {
      templateOf({ mtlsDomain: MTLS_DOMAIN }).resourceCountIs('AWS::Route53::RecordSet', 0);
    });
  });

  describe('layer 2 — HMAC secret', () => {
    it('creates a generated secret with no value in source', () => {
      const template = templateOf();
      template.hasResourceProperties('AWS::SecretsManager::Secret', {
        Name: 'vip/quadrivia/webhook-hmac',
        GenerateSecretString: Match.objectLike({
          GenerateStringKey: 'signingKey',
          PasswordLength: 64,
          ExcludePunctuation: true,
        }),
      });
      const secrets = template.findResources('AWS::SecretsManager::Secret');
      for (const secret of Object.values(secrets) as any[]) {
        expect(secret.Properties.SecretString).toBeUndefined();
      }
    });

    it('encrypts the secret with the supplied CMK and retains it', () => {
      const template = templateOf();
      const [secret] = Object.values(
        template.findResources('AWS::SecretsManager::Secret'),
      ) as any[];
      expect(secret.Properties.KmsKeyId).toBeDefined();
      expect(secret.DeletionPolicy).toEqual('Retain');
    });
  });

  describe('layer 3 — idempotency table', () => {
    it('is on-demand, CMK-encrypted, TTL-enabled and keyed on requestId', () => {
      templateOf().hasResourceProperties('AWS::DynamoDB::Table', {
        TableName: 'VipQuadriviaCallbackIdempotency',
        BillingMode: 'PAY_PER_REQUEST',
        KeySchema: [{ AttributeName: 'requestId', KeyType: 'HASH' }],
        SSESpecification: { SSEEnabled: true },
        TimeToLiveSpecification: { AttributeName: 'ttl', Enabled: true },
      });
    });

    it('is RETAIN, never DESTROY — this is the production account', () => {
      templateOf().hasResource('AWS::DynamoDB::Table', {
        DeletionPolicy: 'Retain',
        UpdateReplacePolicy: 'Retain',
      });
    });
  });

  describe('IAM least privilege', () => {
    it('grants connect:StartTaskContact only, scoped to contacts of the given instance', () => {
      const statement = statementBySid(templateOf(), 'StartScheduledCallbackTask');
      expect(statement.Action).toEqual('connect:StartTaskContact');
      expect(statement.Resource).toEqual(`${CONNECT_INSTANCE_ARN}/contact/*`);
    });

    it('grants no other connect action anywhere in the stack', () => {
      const connectActions = allStatements(templateOf())
        .flatMap((s) => (Array.isArray(s.Action) ? s.Action : [s.Action]))
        .filter((action) => typeof action === 'string' && action.startsWith('connect:'));
      expect(connectActions).toEqual(['connect:StartTaskContact']);
    });

    it('grants exactly PutItem + GetItem + DeleteItem on the idempotency table and nothing else', () => {
      // DeleteItem is required by handler.py's _release_reservation (the
      // cleanup that runs when start_task_contact fails after the
      // reservation succeeds) — without it, that delete AccessDenied's and
      // the request_id stays claimed for the full TTL.
      const statement = statementBySid(templateOf(), 'IdempotencyClaim');
      expect(statement.Action).toEqual([
        'dynamodb:PutItem',
        'dynamodb:GetItem',
        'dynamodb:DeleteItem',
      ]);
      const ddbActions = allStatements(templateOf())
        .flatMap((s) => (Array.isArray(s.Action) ? s.Action : [s.Action]))
        .filter((action) => typeof action === 'string' && action.startsWith('dynamodb:'));
      expect(new Set(ddbActions)).toEqual(
        new Set(['dynamodb:PutItem', 'dynamodb:GetItem', 'dynamodb:DeleteItem']),
      );
    });

    it('grants secretsmanager:GetSecretValue scoped to a single secret Ref, not a wildcard', () => {
      const secretStatements = allStatements(templateOf()).filter((s) => {
        const actions = Array.isArray(s.Action) ? s.Action : [s.Action];
        return actions.includes('secretsmanager:GetSecretValue');
      });
      expect(secretStatements).toHaveLength(1);
      expect(secretStatements[0].Resource).not.toEqual('*');
      expect(JSON.stringify(secretStatements[0].Resource)).toContain('HmacSecret');
    });

    it('grants CMK use identity-side only, so the key-owning stack never depends back on this one', () => {
      const statement = statementBySid(templateOf(), 'UseDataKey');
      // GenerateDataKey*/Encrypt are required for PutItem into a CMK-encrypted
      // table — a Decrypt-only grant passes every mocked unit test and then
      // fails at runtime, so assert the write side explicitly.
      expect(statement.Action).toEqual([
        'kms:Decrypt',
        'kms:DescribeKey',
        'kms:Encrypt',
        'kms:ReEncrypt*',
        'kms:GenerateDataKey*',
      ]);
      expect(statement.Resource).not.toEqual('*');
      // grantDecrypt()/grantRead() would put this role's ARN in the CMK's
      // resource policy and create a CloudFormation dependency cycle with
      // DataStack. The fixture key's policy must mention no role.
      const { fixtures } = buildStack();
      const [key] = Object.values(
        Template.fromStack(fixtures).findResources('AWS::KMS::Key'),
      ) as any[];
      expect(JSON.stringify(key.Properties.KeyPolicy)).not.toContain('FunctionRole');
    });

    it('scopes log writes to this function log group instead of using the basic-execution managed policy', () => {
      const statement = statementBySid(templateOf(), 'WriteOwnLogs');
      expect(statement.Action).toEqual(['logs:CreateLogStream', 'logs:PutLogEvents']);
      const roles = templateOf().findResources('AWS::IAM::Role');
      for (const role of Object.values(roles) as any[]) {
        expect(JSON.stringify(role.Properties.ManagedPolicyArns ?? [])).not.toContain(
          'AWSLambdaBasicExecutionRole',
        );
      }
      // logs:CreateLogGroup would be account-wide — the log group is CDK-managed.
      const logActions = allStatements(templateOf())
        .flatMap((s) => (Array.isArray(s.Action) ? s.Action : [s.Action]))
        .filter((action) => typeof action === 'string' && action.startsWith('logs:'));
      expect(logActions).not.toContain('logs:CreateLogGroup');
    });

    it('creates a lambda.amazonaws.com execution role for the function', () => {
      templateOf().hasResourceProperties('AWS::IAM::Role', {
        RoleName: 'vip-quadrivia-callback-role',
        AssumeRolePolicyDocument: Match.objectLike({
          Statement: Match.arrayWith([
            Match.objectLike({ Principal: { Service: 'lambda.amazonaws.com' } }),
          ]),
        }),
      });
    });

    it('rejects a bare instance id instead of silently building a broken IAM scope', () => {
      expect(() => buildStack({ connectInstanceArn: INSTANCE_ID })).toThrow(
        /must be a full Connect instance ARN/,
      );
    });
  });

  describe('Lambda configuration', () => {
    it('uses a short timeout, no VPC, and a bounded concurrency reservation', () => {
      const template = templateOf();
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'vip-quadrivia-callback',
        Runtime: 'python3.12',
        Handler: 'handler.lambda_handler',
        Timeout: 3,
        MemorySize: 256,
        ReservedConcurrentExecutions: 100,
      });
      const fns = template.findResources('AWS::Lambda::Function');
      for (const fn of Object.values(fns) as any[]) {
        expect(fn.Properties.VpcConfig).toBeUndefined();
      }
    });

    it('passes the Connect instance id derived from the ARN, plus the task template', () => {
      const template = templateOf();
      template.hasResourceProperties('AWS::Lambda::Function', {
        Environment: {
          Variables: Match.objectLike({
            CONNECT_INSTANCE_ID: INSTANCE_ID,
            TASK_TEMPLATE_ID: TASK_TEMPLATE_ID,
            POWERTOOLS_SERVICE_NAME: 'quadrivia-afterhours-callback',
          }),
        },
      });
      // IDEMPOTENCY_TABLE / HMAC_SECRET_ARN are Refs to the resources this
      // stack owns, so assert they point at those logical ids rather than at a
      // hardcoded string that could drift from the real resource.
      const [fn] = Object.values(template.findResources('AWS::Lambda::Function')) as any[];
      const vars = fn.Properties.Environment.Variables;
      const tableLogicalId = Object.keys(template.findResources('AWS::DynamoDB::Table'))[0];
      const secretLogicalId = Object.keys(
        template.findResources('AWS::SecretsManager::Secret'),
      )[0];
      expect(vars.IDEMPOTENCY_TABLE).toEqual({ Ref: tableLogicalId });
      expect(JSON.stringify(vars.HMAC_SECRET_ARN)).toContain(secretLogicalId);
    });

    it('encrypts environment variables with the supplied CMK', () => {
      const [fn] = Object.values(
        templateOf().findResources('AWS::Lambda::Function'),
      ) as any[];
      expect(fn.Properties.KmsKeyArn).toBeDefined();
    });

    it('records Checkov suppressions for the deliberate no-VPC and no-DLQ choices', () => {
      const [fn] = Object.values(
        templateOf().findResources('AWS::Lambda::Function'),
      ) as any[];
      const skipIds = fn.Metadata.checkov.skip.map((s: any) => s.id);
      expect(skipIds).toEqual(['CKV_AWS_117', 'CKV_AWS_116']);
      for (const skip of fn.Metadata.checkov.skip) {
        expect(skip.comment.length).toBeGreaterThan(40);
      }
    });

    it('writes to a KMS-encrypted, 1-year-retention, retained log group', () => {
      const template = templateOf();
      template.hasResourceProperties('AWS::Logs::LogGroup', {
        LogGroupName: '/aws/lambda/vip-quadrivia-callback',
        RetentionInDays: 365,
      });
      const logGroups = template.findResources('AWS::Logs::LogGroup');
      for (const lg of Object.values(logGroups) as any[]) {
        expect(lg.DeletionPolicy).toEqual('Retain');
        expect(lg.Properties.KmsKeyId).toBeDefined();
      }
    });
  });

  describe('layer defense-in-depth — gateway throttle', () => {
    it('throttles the default stage as a backstop on top of mTLS + HMAC + idempotency', () => {
      templateOf().hasResourceProperties('AWS::ApiGatewayV2::Stage', {
        StageName: '$default',
        DefaultRouteSettings: Match.objectLike({
          ThrottlingRateLimit: 5,
          ThrottlingBurstLimit: 10,
        }),
      });
    });
  });

  describe('access logging', () => {
    it('logs request metadata plus client-cert identity, and no body or headers', () => {
      const template = templateOf();
      const format = JSON.stringify({
        requestId: '$context.requestId',
        ip: '$context.identity.sourceIp',
        requestTime: '$context.requestTime',
        httpMethod: '$context.httpMethod',
        routeKey: '$context.routeKey',
        status: '$context.status',
        integrationErrorMessage: '$context.integrationErrorMessage',
        responseLatency: '$context.responseLatency',
        clientCertSubjectDN: '$context.identity.clientCert.subjectDN',
        clientCertSerial: '$context.identity.clientCert.serialNumber',
      });
      template.hasResourceProperties('AWS::ApiGatewayV2::Stage', {
        StageName: '$default',
        AccessLogSettings: Match.objectLike({ Format: format }),
      });
      expect(format).not.toContain('$context.authorizer');
      expect(format).not.toContain('requestBody');
      expect(format).not.toContain('X-Signature');
    });

    it('grants apigateway.amazonaws.com CMK access scoped to this account and this log group', () => {
      const { fixtures } = buildStack();
      Template.fromStack(fixtures).hasResourceProperties('AWS::KMS::Key', {
        KeyPolicy: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Sid: 'AllowQuadriviaApiGatewayLogDelivery',
              Effect: 'Allow',
              Principal: { Service: 'apigateway.amazonaws.com' },
              Condition: {
                StringEquals: { 'aws:SourceAccount': '165505826690' },
                ArnLike: {
                  'aws:SourceArn':
                    'arn:aws:logs:us-east-1:165505826690:log-group:/aws/apigateway/vip-quadrivia-callback-access:*',
                },
              },
            }),
          ]),
        },
      });
    });
  });

  describe('SCP-mandated tagging', () => {
    const TAGGED_TYPES = [
      'AWS::Lambda::Function',
      'AWS::DynamoDB::Table',
      'AWS::SecretsManager::Secret',
    ];

    it('applies all 6 org-mandated tags, case-sensitive, to every taggable resource', () => {
      const template = templateOf();
      const expected: Record<string, string> = {
        'Backup-Tier': 'Standard',
        Environment: 'prod',
        Owner: 'placeholder.owner@medwork.io',
        Application: 'quadrivia-afterhours-callback',
        DataClassification: 'phi',
        Team: 'engineering',
      };

      for (const type of TAGGED_TYPES) {
        const resources = Object.values(template.findResources(type)) as any[];
        expect(resources.length).toBeGreaterThan(0);
        for (const resource of resources) {
          const raw = resource.Properties.Tags;
          // DynamoDB/Lambda render Tags as [{Key,Value}]; Secrets Manager too.
          const tags = Object.fromEntries(
            (raw as any[]).map((t) => [t.Key, t.Value]),
          );
          for (const [key, value] of Object.entries(expected)) {
            expect(tags[key]).toEqual(value);
          }
        }
      }
    });

    it('takes Owner and Team from props instead of hardcoding them', () => {
      const template = templateOf({
        ownerEmail: 'someone.else@medwork.io',
        team: 'specialOps',
      });
      const [table] = Object.values(
        template.findResources('AWS::DynamoDB::Table'),
      ) as any[];
      const tags = Object.fromEntries(table.Properties.Tags.map((t: any) => [t.Key, t.Value]));
      expect(tags.Owner).toEqual('someone.else@medwork.io');
      expect(tags.Team).toEqual('specialOps');
    });

    it('overrides a conflicting lower-priority app-level tag of the same key', () => {
      // infra/bin/app.ts applies Tags.of(app) with the default priority and a
      // non-email Owner; the SCP tags must win.
      const app = new cdk.App();
      const fixtures = new cdk.Stack(app, 'Fixtures', { env: ENV });
      const dataKey = new kms.Key(fixtures, 'FixtureDataKey', { enableKeyRotation: true });
      const stack = new QuadriviaWebhookStack(app, 'TaggedStack', {
        env: ENV,
        dataKey,
        connectInstanceArn: CONNECT_INSTANCE_ARN,
        taskTemplateId: TASK_TEMPLATE_ID,
        ownerEmail: 'placeholder.owner@medwork.io',
        team: 'engineering',
      });
      cdk.Tags.of(app).add('Owner', 'devaju');

      const [table] = Object.values(
        Template.fromStack(stack).findResources('AWS::DynamoDB::Table'),
      ) as any[];
      const tags = Object.fromEntries(table.Properties.Tags.map((t: any) => [t.Key, t.Value]));
      expect(tags.Owner).toEqual('placeholder.owner@medwork.io');
    });
  });

  describe('stack plumbing', () => {
    it('applies the permissions boundary when the name is provided', () => {
      const { stack } = buildStack({ permissionsBoundaryName: 'EngineeringPermissionBoundary' });
      expect(stack.node.tryFindChild('PermissionsBoundary')).toBeDefined();
      expect(() => Template.fromStack(stack)).not.toThrow();
    });

    it('does not create a PermissionsBoundary construct when the prop is omitted', () => {
      expect(buildStack().stack.node.tryFindChild('PermissionsBoundary')).toBeUndefined();
    });

    it('exposes the resources other stacks / operators need', () => {
      const { stack } = buildStack();
      const template = Template.fromStack(stack);
      template.hasOutput('HttpApiId', {});
      template.hasOutput('WebhookFunctionArn', {});
      template.hasOutput('IdempotencyTableName', {});
      template.hasOutput('HmacSecretArn', {});
      expect(stack.httpApi).toBeDefined();
      expect(stack.lambdaFunction).toBeDefined();
      expect(stack.idempotencyTable).toBeDefined();
      expect(stack.hmacSecret).toBeDefined();
    });
  });
});
