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
const CONTACT_FLOW_ID = '11111111-2222-3333-4444-555555555555';
const PATIENT_LOOKUP_FUNCTION_ARN =
  'arn:aws:lambda:us-east-1:165505826690:function:SOPS-ConnectPatientLookup';
const CLIENT_CERT_SUBJECT_DN = 'CN=quadrivia-afterhours,OU=Integrations,O=Quadrivia,C=US';

// The domain itself (quadrivia-webhook.medwork.io) is real and already
// live — see EXISTING_DOMAIN_NAME in the stack. Only the truststore
// location is a placeholder here (that S3 object doesn't exist in this
// test's fixtures — it's an out-of-band artifact, see existingDomain prop
// comment in the stack).
const EXISTING_DOMAIN = {
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
    contactFlowId: CONTACT_FLOW_ID,
    patientLookupFunctionArn: PATIENT_LOOKUP_FUNCTION_ARN,
    clientCertSubjectDn: CLIENT_CERT_SUBJECT_DN,
    ownerEmail: 'placeholder.owner@medwork.io',
    team: 'engineering',
    ...propsOverride,
  });
  return { stack, fixtures };
}

function templateOf(propsOverride: Partial<QuadriviaWebhookStackProps> = {}) {
  return Template.fromStack(buildStack(propsOverride).stack);
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
    it('creates no domain or mapping when existingDomain is omitted (fail-closed pending mTLS activation)', () => {
      const template = templateOf();
      template.resourceCountIs('AWS::ApiGatewayV2::DomainName', 0);
      template.resourceCountIs('AWS::ApiGatewayV2::ApiMapping', 0);
      expect(buildStack().stack.domainName).toBeUndefined();
    });

    it('imports the existing quadrivia-webhook.medwork.io domain rather than creating one', () => {
      const template = templateOf({ existingDomain: EXISTING_DOMAIN });
      // No AWS::ApiGatewayV2::DomainName in the template at all — this
      // stack does not own that resource (IT created it directly) and
      // CloudFormation cannot manage a resource it didn't create.
      template.resourceCountIs('AWS::ApiGatewayV2::DomainName', 0);
      template.resourceCountIs('AWS::ApiGatewayV2::ApiMapping', 1);
      template.hasResourceProperties('AWS::ApiGatewayV2::ApiMapping', {
        DomainName: 'quadrivia-webhook.medwork.io',
      });
    });

    it('emits the imported domain facts as sanity-check outputs only when wired', () => {
      const withDomain = templateOf({ existingDomain: EXISTING_DOMAIN });
      withDomain.hasOutput('RegionalDomainName', {});
      withDomain.hasOutput('RegionalHostedZoneId', {});
      withDomain.hasOutput('WebhookUrl', {});

      const withoutDomain = templateOf();
      expect(Object.keys(withoutDomain.findOutputs('RegionalDomainName'))).toHaveLength(0);
      expect(Object.keys(withoutDomain.findOutputs('WebhookUrl'))).toHaveLength(0);
    });

    it('never creates a Route53 record — DNS is already live, owned by IT', () => {
      templateOf({ existingDomain: EXISTING_DOMAIN }).resourceCountIs('AWS::Route53::RecordSet', 0);
    });
  });

  describe('layer 2 — HMAC secret', () => {
    // IMPORTED, not created: the 2026-10-01 dry-run deploy got far enough to
    // fully create this secret before a later resource (the execution role)
    // hit the EngineeringPermissionBoundary deny and rolled the stack back.
    // RemovalPolicy.RETAIN left the real secret in place, so the stack now
    // imports it by ARN instead of re-declaring (and re-generating) it — see
    // the HmacSecret construct in the stack.
    it('creates no SecretsManager::Secret resource — the real key is imported by ARN', () => {
      templateOf().resourceCountIs('AWS::SecretsManager::Secret', 0);
    });

    it('exposes the imported secret ARN on the stack', () => {
      const { stack } = buildStack();
      expect(stack.hmacSecret.secretArn).toEqual(
        'arn:aws:secretsmanager:us-east-1:165505826690:secret:vip/quadrivia/webhook-hmac-C2Sw6c',
      );
    });
  });

  describe('layer 3 — idempotency table', () => {
    // IMPORTED, not created: a prior deploy attempt fully created this table
    // (on-demand, requestId hash key, CMK-encrypted, TTL on `ttl`, PITR
    // enabled — all confirmed live via `aws dynamodb describe-table` /
    // `describe-time-to-live` / `describe-continuous-backups` on 2026-10-01)
    // before a later resource failed and rolled the stack back.
    // RemovalPolicy.RETAIN left the real, correctly-configured table in
    // place, so there is nothing left for this stack to create or for a
    // template assertion to check configuration-wise.
    it('creates no AWS::DynamoDB::Table resource — the real table is imported by name', () => {
      templateOf().resourceCountIs('AWS::DynamoDB::Table', 0);
    });

    it('exposes the imported table name and ARN on the stack', () => {
      const { stack } = buildStack();
      expect(stack.idempotencyTable.tableName).toEqual('VipQuadriviaCallbackIdempotency');
      expect(stack.idempotencyTable.tableArn).toEqual(
        `arn:aws:dynamodb:us-east-1:165505826690:table/VipQuadriviaCallbackIdempotency`,
      );
    });
  });

  describe('IAM least privilege', () => {
    // IMPORTED, not created: EngineeringPermissionBoundary denies
    // iam:CreateRole/PutRolePolicy to this app's CFN exec role (reproduced
    // live on 2026-10-01), so the execution role was created manually via
    // the AWS console with the least-privilege statements that used to live
    // here (StartScheduledCallbackTask, LookupExistingPatient,
    // IdempotencyClaim, ReadWebhookSigningKey, UseDataKey, WriteOwnLogs —
    // same Sids, verified by hand against the role's inline policy, not
    // asserted here because they no longer exist in this stack's own
    // CloudFormation template). This stack only has to prove it imports the
    // right role immutably and never tries to synthesize IAM of its own.

    it('creates no AWS::IAM::Role and no AWS::IAM::Policy — the role is imported by ARN', () => {
      const template = templateOf();
      template.resourceCountIs('AWS::IAM::Role', 0);
      template.resourceCountIs('AWS::IAM::Policy', 0);
    });

    it("points the Lambda's execution role at the manually-created role ARN", () => {
      templateOf().hasResourceProperties('AWS::Lambda::Function', {
        Role: 'arn:aws:iam::165505826690:role/vip-quadrivia-callback-role',
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
        Timeout: 6,
        MemorySize: 256,
        ReservedConcurrentExecutions: 100,
      });
      const fns = template.findResources('AWS::Lambda::Function');
      for (const fn of Object.values(fns) as any[]) {
        expect(fn.Properties.VpcConfig).toBeUndefined();
      }
    });

    it('passes the Connect instance id derived from the ARN, plus the contact flow id', () => {
      const template = templateOf();
      template.hasResourceProperties('AWS::Lambda::Function', {
        Environment: {
          Variables: Match.objectLike({
            CONNECT_INSTANCE_ID: INSTANCE_ID,
            CONTACT_FLOW_ID: CONTACT_FLOW_ID,
            PATIENT_LOOKUP_FUNCTION_ARN: PATIENT_LOOKUP_FUNCTION_ARN,
            QUADRIVIA_CLIENT_CERT_SUBJECT_DN: CLIENT_CERT_SUBJECT_DN,
            POWERTOOLS_SERVICE_NAME: 'quadrivia-afterhours-callback',
          }),
        },
      });
      // IDEMPOTENCY_TABLE and HMAC_SECRET_ARN are both plain strings now,
      // not Refs — this stack imports both resources by name/ARN rather
      // than owning them (see "layer 2 — HMAC secret" / "layer 3 —
      // idempotency table").
      const [fn] = Object.values(template.findResources('AWS::Lambda::Function')) as any[];
      const vars = fn.Properties.Environment.Variables;
      expect(vars.IDEMPOTENCY_TABLE).toEqual('VipQuadriviaCallbackIdempotency');
      expect(vars.HMAC_SECRET_ARN).toEqual(
        'arn:aws:secretsmanager:us-east-1:165505826690:secret:vip/quadrivia/webhook-hmac-C2Sw6c',
      );
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

    // IMPORTED, not created: a prior rolled-back deploy left both log
    // groups physically created in CloudWatch Logs (a CFN rollback race —
    // the real CreateLogGroup call succeeded before the cancellation
    // reached it) with the real CMK already attached and retention set by
    // hand to match this stack's intent. No AWS::Logs::LogGroup resource is
    // synthesized by this stack at all, so there's nothing to assert about
    // retention/encryption/removal policy from the template — those now
    // live on the real, already-verified log groups instead.
    it('creates no AWS::Logs::LogGroup resource — both are imported by name', () => {
      templateOf().resourceCountIs('AWS::Logs::LogGroup', 0);
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
    // AWS::SecretsManager::Secret and AWS::DynamoDB::Table deliberately
    // excluded: this stack imports both by ARN/name rather than creating
    // them (see "layer 2 — HMAC secret" / "layer 3 — idempotency table"),
    // so they carry whatever tags they already have, not these.
    const TAGGED_TYPES = ['AWS::Lambda::Function'];

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
      const [fn] = Object.values(
        template.findResources('AWS::Lambda::Function'),
      ) as any[];
      const tags = Object.fromEntries(fn.Properties.Tags.map((t: any) => [t.Key, t.Value]));
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
        contactFlowId: CONTACT_FLOW_ID,
        patientLookupFunctionArn: PATIENT_LOOKUP_FUNCTION_ARN,
        clientCertSubjectDn: CLIENT_CERT_SUBJECT_DN,
        ownerEmail: 'placeholder.owner@medwork.io',
        team: 'engineering',
      });
      cdk.Tags.of(app).add('Owner', 'devaju');

      const [fn] = Object.values(
        Template.fromStack(stack).findResources('AWS::Lambda::Function'),
      ) as any[];
      const tags = Object.fromEntries(fn.Properties.Tags.map((t: any) => [t.Key, t.Value]));
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
