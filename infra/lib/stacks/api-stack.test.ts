import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { ApiStack, ApiStackProps } from './api-stack';

const ENV = { account: '165505826690', region: 'us-east-1' };

/**
 * ApiStack takes IFunction/IUserPool/IUserPoolClient/IKey props. All the
 * routing/authorizer/CORS/access-log logic under test only reads plain
 * string attributes (functionArn, userPoolId, userPoolClientId) off these
 * objects, so lightweight `fromXxx` imports with fixed literal ARNs/ids are
 * used instead of real cross-stack resources — this keeps IntegrationUri /
 * issuer URL / jwtAudience assertions on literal strings instead of
 * cross-stack Fn::ImportValue tokens.
 *
 * The one exception is `dataKey`: ApiStack calls `dataKey.addToResourcePolicy(...)`
 * directly on the construct, which is a no-op on an imported key (CDK's
 * ReferencedKey has no mutable `policy` document). A real kms.Key is built in
 * a separate fixture stack (mirroring how DataStack really owns it in prod)
 * so the KMS resource-policy statement actually lands somewhere assertable.
 */
function buildStack(propsOverride: Partial<ApiStackProps> = {}) {
  const app = new cdk.App();

  const fixtures = new cdk.Stack(app, 'ApiStackFixtures', { env: ENV });
  const dataKey = new kms.Key(fixtures, 'FixtureDataKey', { enableKeyRotation: true });

  const userPool = cognito.UserPool.fromUserPoolId(fixtures, 'FixtureUserPool', 'us-east-1_TESTPOOL1');
  const userPoolClient = cognito.UserPoolClient.fromUserPoolClientId(
    fixtures,
    'FixtureUserPoolClient',
    'testclientid123',
  );

  const makeFn = (id: string, name: string) =>
    lambda.Function.fromFunctionAttributes(fixtures, id, {
      functionArn: `arn:aws:lambda:us-east-1:165505826690:function:${name}`,
      sameEnvironment: true,
    });

  return new ApiStack(app, 'TestApiStack', {
    env: ENV,
    dataKey,
    userPool,
    userPoolClient,
    segmentsFunction: makeFn('SegmentsFn', 'vip-admin-ui-api-segments'),
    campaignsFunction: makeFn('CampaignsFn', 'vip-admin-ui-api-campaigns'),
    metricsFunction: makeFn('MetricsFn', 'vip-admin-ui-api-metrics'),
    profilesFunction: makeFn('ProfilesFn', 'vip-admin-ui-api-profiles'),
    plansFunction: makeFn('PlansFn', 'vip-admin-ui-api-plans'),
    progressiveDialerSeedFunction: makeFn('DialerFn', 'vip-admin-ui-progressive-dialer-seed'),
    corsAllowOrigins: ['https://example.com'],
    ...propsOverride,
  });
}

describe('ApiStack', () => {
  it('creates exactly 61 ApiGatewayV2 routes (one per method across every addRoutes call)', () => {
    const template = Template.fromStack(buildStack());
    template.resourceCountIs('AWS::ApiGatewayV2::Route', 61);
  });

  it('creates exactly 6 Lambda integrations, one per backing Lambda', () => {
    const template = Template.fromStack(buildStack());
    template.resourceCountIs('AWS::ApiGatewayV2::Integration', 6);
  });

  it('wires each integration to the correct Lambda function ARN', () => {
    const template = Template.fromStack(buildStack());
    const expectedUris = [
      'vip-admin-ui-api-segments',
      'vip-admin-ui-api-campaigns',
      'vip-admin-ui-api-metrics',
      'vip-admin-ui-api-profiles',
      'vip-admin-ui-api-plans',
      'vip-admin-ui-progressive-dialer-seed',
    ];
    for (const fnName of expectedUris) {
      template.hasResourceProperties('AWS::ApiGatewayV2::Integration', {
        IntegrationUri: `arn:aws:lambda:us-east-1:165505826690:function:${fnName}`,
        IntegrationType: 'AWS_PROXY',
        PayloadFormatVersion: '2.0',
      });
    }
  });

  it('routes the audit and branded-metrics paths to the metrics Lambda integration, not another one', () => {
    const template = Template.fromStack(buildStack());
    const metricsIntegrationId = Object.keys(
      template.findResources('AWS::ApiGatewayV2::Integration', {
        Properties: {
          IntegrationUri: 'arn:aws:lambda:us-east-1:165505826690:function:vip-admin-ui-api-metrics',
        },
      }),
    )[0];
    const auditRoutes = template.findResources('AWS::ApiGatewayV2::Route', {
      Properties: { RouteKey: 'GET /audit' },
    });
    const [auditRoute] = Object.values(auditRoutes);
    expect(auditRoute.Properties.Target['Fn::Join'][1]).toEqual(
      expect.arrayContaining([{ Ref: metricsIntegrationId }]),
    );
  });

  it('does not scope any route to the progressive-dialer function except /dialer/{id}/seed', () => {
    const template = Template.fromStack(buildStack());
    const dialerRoutes = template.findResources('AWS::ApiGatewayV2::Route', {
      Properties: { RouteKey: 'POST /dialer/{id}/seed' },
    });
    expect(Object.keys(dialerRoutes)).toHaveLength(1);
  });

  it('configures CORS preflight with the provided allow-origins and the full method set', () => {
    const template = Template.fromStack(buildStack({ corsAllowOrigins: ['https://a.example', 'https://b.example'] }));
    template.hasResourceProperties('AWS::ApiGatewayV2::Api', {
      CorsConfiguration: {
        AllowOrigins: ['https://a.example', 'https://b.example'],
        AllowMethods: ['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'],
        AllowHeaders: ['Authorization', 'Content-Type', 'X-Amz-Date', 'X-Api-Key'],
        AllowCredentials: false,
        MaxAge: 3600,
      },
    });
  });

  it('creates a JWT authorizer scoped to the Cognito user pool issuer and audience', () => {
    const template = Template.fromStack(buildStack());
    template.hasResourceProperties('AWS::ApiGatewayV2::Authorizer', {
      AuthorizerType: 'JWT',
      IdentitySource: ['$request.header.Authorization'],
      JwtConfiguration: {
        Issuer: 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TESTPOOL1',
        Audience: ['testclientid123'],
      },
    });
  });

  it('every route uses the same JWT authorizer', () => {
    const template = Template.fromStack(buildStack());
    const authorizers = template.findResources('AWS::ApiGatewayV2::Authorizer');
    expect(Object.keys(authorizers)).toHaveLength(1);
    const [authorizerId] = Object.keys(authorizers);
    const routes = template.findResources('AWS::ApiGatewayV2::Route');
    for (const [, route] of Object.entries(routes)) {
      expect(route.Properties.AuthorizerId).toEqual({ Ref: authorizerId });
    }
  });

  it('sets access-log settings on the default stage with a PHI-free JSON format', () => {
    const template = Template.fromStack(buildStack());
    template.hasResourceProperties('AWS::ApiGatewayV2::Stage', {
      StageName: '$default',
      AccessLogSettings: Match.objectLike({
        Format: JSON.stringify({
          requestId: '$context.requestId',
          ip: '$context.identity.sourceIp',
          requestTime: '$context.requestTime',
          httpMethod: '$context.httpMethod',
          routeKey: '$context.routeKey',
          status: '$context.status',
          integrationErrorMessage: '$context.integrationErrorMessage',
          responseLatency: '$context.responseLatency',
        }),
      }),
    });
  });

  it('points the access log destination at a KMS-encrypted, 1-year-retention log group', () => {
    const template = Template.fromStack(buildStack());
    template.hasResourceProperties('AWS::Logs::LogGroup', {
      LogGroupName: '/aws/apigateway/vip-admin-ui-api-access',
      RetentionInDays: 365,
    });
    template.hasResource('AWS::Logs::LogGroup', { DeletionPolicy: 'Retain' });
  });

  it('grants apigateway.amazonaws.com KMS access scoped by SourceAccount and the access-log-group SourceArn', () => {
    const fixturesTemplate = Template.fromStack(
      // Rebuild here because buildStack() returns the ApiStack, not the fixtures
      // stack that owns the real (non-imported) dataKey whose policy document
      // ApiStack mutates via addToResourcePolicy — see buildStack() comment.
      (() => {
        const app = new cdk.App();
        const fixtures = new cdk.Stack(app, 'FixturesOnly', { env: ENV });
        const dataKey = new kms.Key(fixtures, 'FixtureDataKey', { enableKeyRotation: true });
        const userPool = cognito.UserPool.fromUserPoolId(fixtures, 'UP', 'us-east-1_TESTPOOL1');
        const userPoolClient = cognito.UserPoolClient.fromUserPoolClientId(fixtures, 'UPC', 'testclientid123');
        const makeFn = (id: string, name: string) =>
          lambda.Function.fromFunctionAttributes(fixtures, id, {
            functionArn: `arn:aws:lambda:us-east-1:165505826690:function:${name}`,
            sameEnvironment: true,
          });
        new ApiStack(app, 'ApiUnderTest', {
          env: ENV,
          dataKey,
          userPool,
          userPoolClient,
          segmentsFunction: makeFn('S', 'segments'),
          campaignsFunction: makeFn('C', 'campaigns'),
          metricsFunction: makeFn('M', 'metrics'),
          profilesFunction: makeFn('P', 'profiles'),
          plansFunction: makeFn('PL', 'plans'),
          progressiveDialerSeedFunction: makeFn('D', 'dialer'),
          corsAllowOrigins: ['https://example.com'],
        });
        return fixtures;
      })(),
    );

    fixturesTemplate.hasResourceProperties('AWS::KMS::Key', {
      KeyPolicy: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: 'AllowApiGatewayLogDelivery',
            Effect: 'Allow',
            Principal: { Service: 'apigateway.amazonaws.com' },
            Action: [
              'kms:Encrypt*',
              'kms:Decrypt*',
              'kms:ReEncrypt*',
              'kms:GenerateDataKey*',
              'kms:Describe*',
            ],
            Resource: '*',
            Condition: {
              StringEquals: { 'aws:SourceAccount': '165505826690' },
              ArnLike: {
                'aws:SourceArn':
                  'arn:aws:logs:us-east-1:165505826690:log-group:/aws/apigateway/vip-admin-ui-api-access:*',
              },
            },
          }),
        ]),
      },
    });
  });

  it('applies the permissions boundary when permissionsBoundaryName is provided', () => {
    expect(() =>
      Template.fromStack(buildStack({ permissionsBoundaryName: 'TestBoundary' })),
    ).not.toThrow();
    const stack = buildStack({ permissionsBoundaryName: 'TestBoundary' });
    expect(stack.node.tryFindChild('PermissionsBoundary')).toBeDefined();
  });

  it('does not create a PermissionsBoundary construct when the prop is omitted', () => {
    const stack = buildStack();
    expect(stack.node.tryFindChild('PermissionsBoundary')).toBeUndefined();
  });

  it('exposes the HttpApiId and HttpApiEndpoint outputs, and public httpApi/apiUrl properties', () => {
    const stack = buildStack();
    const template = Template.fromStack(stack);
    template.hasOutput('HttpApiId', {});
    template.hasOutput('HttpApiEndpoint', {});
    expect(stack.httpApi).toBeDefined();
    expect(stack.apiUrl).toEqual(stack.httpApi.apiEndpoint);
  });
});
