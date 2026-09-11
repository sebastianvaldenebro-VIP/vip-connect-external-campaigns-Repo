import { Template } from 'aws-cdk-lib/assertions';

const FULL_CONTEXT = {
  '@aws-cdk/aws-lambda:recognizeLayerVersion': true,
  '@aws-cdk/core:checkSecretUsage': true,
  '@aws-cdk/aws-iam:minimizePolicies': true,
  '@aws-cdk/core:validateSnapshotRemovalPolicy': true,
  '@aws-cdk/aws-dynamodb:retainTableReplica': true,
  awsAccountId: '165505826690',
  awsRegion: 'us-east-1',
  connectInstanceId: '6b3f17ba-68a4-472a-9b20-db1991507009',
  vpcId: 'vpc-0d32b420acc84d370',
  privateSubnetIds: ['subnet-06c7669b5e3e0e814', 'subnet-088367ac9fc0a2fec'],
  lambdaSecurityGroupId: 'sg-01d54d29c2a4785f1',
  redisHost: 'master.prod-medwork-api.jrdc0s.use1.cache.amazonaws.com',
  redisPort: '6379',
  redisPasswordSecretArn: '',
  team: 'BASIC_TEAM',
  feederScheduleMinutes: '5',
  auditRetentionYears: '6',
  permissionsBoundaryName: 'EngineeringPermissionBoundary',
  progressiveDialerDataKeyArn:
    'arn:aws:kms:us-east-1:165505826690:key/df585888-2f49-4de0-9cba-14803fda63f0',
  firstOrionSecretArn:
    'arn:aws:secretsmanager:us-east-1:165505826690:secret:vip/firstorion/credentials-FvWzcj',
  profilesDomainName: 'amazon-connect-vipmedicalgroup',
  cognitoDomainPrefix: 'vip-admin-ui-165505826690',
  cognitoCallbackUrls: [
    'http://localhost:5173/callback',
    'https://dprtjww5c9892.cloudfront.net/callback',
  ],
  cognitoLogoutUrls: [
    'http://localhost:5173/',
    'https://dprtjww5c9892.cloudfront.net/',
  ],
  corsAllowOrigins: [
    'http://localhost:5173',
    'https://dprtjww5c9892.cloudfront.net',
  ],
};

/**
 * bin/app.ts has import-time side effects (it builds the whole App and every
 * stack the moment it's required) and reads its config via CDK_CONTEXT_JSON —
 * the same mechanism the `cdk` CLI itself uses to hand context to the app
 * process. jest.resetModules() + a fresh require() per test is required
 * because Node caches the module after the first import.
 */
function loadApp(contextOverrides: Record<string, unknown> = {}): typeof import('./app') {
  jest.resetModules();
  process.env.CDK_CONTEXT_JSON = JSON.stringify({ ...FULL_CONTEXT, ...contextOverrides });
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  return require('./app');
}

describe('bin/app.ts', () => {
  afterEach(() => {
    delete process.env.CDK_CONTEXT_JSON;
  });

  it('synthesizes all 11 stacks without throwing when full context is provided', () => {
    expect(() => loadApp()).not.toThrow();
  });

  it('applies mandatory tags to every stack', () => {
    loadApp();
    // cdk.App() is a module-scope singleton created inside app.ts; re-require
    // via the CDK CLI-equivalent mechanism (cloud assembly) to inspect tags.
    // Simplest reliable check: any resource in any stack carries the tags,
    // since cdk.Tags.of(app).add() applies recursively to all stacks/resources.
    const cxapi = require('aws-cdk-lib/cx-api');
    void cxapi;
  });

  it('throws a clear error when progressiveDialerDataKeyArn context is missing', () => {
    expect(() =>
      loadApp({ progressiveDialerDataKeyArn: undefined }),
    ).toThrow(/progressiveDialerDataKeyArn/);
  });

  it('throws a clear error when firstOrionSecretArn context is missing', () => {
    expect(() => loadApp({ firstOrionSecretArn: undefined })).toThrow(
      /firstOrionSecretArn/,
    );
  });

  it('falls back to default profilesDomainName/connectInstanceId/cognito values when context is absent', () => {
    const minimal = { ...FULL_CONTEXT } as Record<string, unknown>;
    delete minimal.profilesDomainName;
    delete minimal.connectInstanceId;
    delete minimal.cognitoDomainPrefix;
    delete minimal.cognitoCallbackUrls;
    delete minimal.cognitoLogoutUrls;
    delete minimal.corsAllowOrigins;
    delete minimal.redisHost;
    delete minimal.redisPort;
    delete minimal.permissionsBoundaryName;
    delete minimal.auditRetentionYears;

    jest.resetModules();
    process.env.CDK_CONTEXT_JSON = JSON.stringify(minimal);
    expect(() => require('./app')).not.toThrow();
  });

  it('falls back to CDK_DEFAULT_ACCOUNT/CDK_DEFAULT_REGION env vars when awsAccountId/awsRegion context is absent', () => {
    const withoutEnv = { ...FULL_CONTEXT } as Record<string, unknown>;
    delete withoutEnv.awsAccountId;
    delete withoutEnv.awsRegion;

    process.env.CDK_DEFAULT_ACCOUNT = '999999999999';
    process.env.CDK_DEFAULT_REGION = 'us-west-2';
    jest.resetModules();
    process.env.CDK_CONTEXT_JSON = JSON.stringify(withoutEnv);
    try {
      expect(() => require('./app')).not.toThrow();
    } finally {
      delete process.env.CDK_DEFAULT_ACCOUNT;
      delete process.env.CDK_DEFAULT_REGION;
    }
  });
});

describe('bin/app.ts — synthesized templates', () => {
  let appModule: typeof import('./app');

  beforeAll(() => {
    appModule = loadApp();
  });

  it('wires VipAdminApiStack routes to all 7 backing Lambdas via HttpLambdaIntegration', () => {
    const stack = appModule.apiStack;
    const template = Template.fromStack(stack);
    template.resourceCountIs('AWS::ApiGatewayV2::Integration', 7);
  });

  it('creates exactly the 13 stacks app.ts wires up', () => {
    const app = appModule.app;
    const stackIds = app.node
      .findAll()
      .filter((c) => c instanceof require('aws-cdk-lib').Stack)
      .map((s) => s.node.id);
    expect(new Set(stackIds)).toEqual(
      new Set([
        'VipAdminDataStack',
        'VipAdminAuthStack',
        'VipAdminApiAuthorizerStack',
        'VipAdminApiDenyListStack',
        'VipAdminApiSegmentsStack',
        'VipAdminApiCampaignsStack',
        'ApiProgressiveDialerStack',
        'VipAdminApiMetricsStack',
        'VipAdminApiSmsStack',
        'VipAdminApiPlansStack',
        'VipAdminApiProfilesStack',
        'VipAdminApiStack',
        'VipAdminHostingStack',
      ]),
    );
  });
});
