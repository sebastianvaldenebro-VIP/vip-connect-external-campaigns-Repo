import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { AuthStack, AuthStackProps } from './auth-stack';

const ENV = { account: '165505826690', region: 'us-east-1' };

function buildStack(props: Partial<AuthStackProps> = {}, id = 'TestAuthStack') {
  const app = new cdk.App();
  return new AuthStack(app, id, {
    env: ENV,
    cognitoDomainPrefix: 'vip-admin-ui-165505826690',
    callbackUrls: [
      'http://localhost:5173/callback',
      'https://dprtjww5c9892.cloudfront.net/callback',
    ],
    logoutUrls: ['http://localhost:5173/', 'https://dprtjww5c9892.cloudfront.net/'],
    ...props,
  });
}

describe('AuthStack', () => {
  describe('default configuration (no permissions boundary)', () => {
    let stack: AuthStack;
    let template: Template;

    beforeAll(() => {
      stack = buildStack();
      template = Template.fromStack(stack);
    });

    it('creates a Cognito User Pool with email sign-in, no self-signup, and a RETAIN removal policy', () => {
      template.hasResourceProperties('AWS::Cognito::UserPool', {
        UserPoolName: 'vip-admin-ui-pool',
        UsernameAttributes: ['email'],
        AdminCreateUserConfig: { AllowAdminCreateUserOnly: true },
        Schema: Match.arrayWith([Match.objectLike({ Name: 'email', Required: true, Mutable: false })]),
      });
      template.hasResource('AWS::Cognito::UserPool', { DeletionPolicy: 'Retain' });
      template.hasResourceProperties('AWS::Cognito::UserPool', {
        DeletionProtection: 'ACTIVE',
      });
    });

    it('enforces a strong password policy (12 chars, all character classes, 1-day temp password validity)', () => {
      template.hasResourceProperties('AWS::Cognito::UserPool', {
        Policies: {
          PasswordPolicy: {
            MinimumLength: 12,
            RequireLowercase: true,
            RequireUppercase: true,
            RequireNumbers: true,
            RequireSymbols: true,
            TemporaryPasswordValidityDays: 1,
          },
        },
      });
    });

    it('requires MFA with both SMS and TOTP factors, and enables full threat protection', () => {
      template.hasResourceProperties('AWS::Cognito::UserPool', {
        MfaConfiguration: 'ON',
        EnabledMfas: Match.arrayWith(['SMS_MFA', 'SOFTWARE_TOKEN_MFA']),
        UserPoolAddOns: { AdvancedSecurityMode: 'ENFORCED' },
      });
    });

    it('suppresses CKV_AWS_111 on the CDK-auto-generated smsRole because sns:Publish has no resource ARN', () => {
      template.hasResource('AWS::IAM::Role', {
        Properties: Match.objectLike({
          AssumeRolePolicyDocument: {
            Statement: [
              Match.objectLike({
                Principal: { Service: 'cognito-idp.amazonaws.com' },
              }),
            ],
            Version: '2012-10-17',
          },
          Policies: Match.arrayWith([
            Match.objectLike({
              PolicyDocument: {
                Statement: [
                  Match.objectLike({
                    Action: 'sns:Publish',
                    Effect: 'Allow',
                    Resource: '*',
                  }),
                ],
                Version: '2012-10-17',
              },
            }),
          ]),
        }),
        Metadata: Match.objectLike({
          checkov: {
            skip: Match.arrayWith([
              Match.objectLike({
                id: 'CKV_AWS_111',
                comment: Match.stringLikeRegexp('sns:Publish'),
              }),
            ]),
          },
        }),
      });
    });

    it('does not set a PermissionsBoundary on the smsRole and creates no PermissionsBoundary construct', () => {
      template.hasResourceProperties('AWS::IAM::Role', {
        PermissionsBoundary: Match.absent(),
      });
      expect(stack.node.tryFindChild('PermissionsBoundary')).toBeUndefined();
    });

    it('creates an SPA user pool client with no secret, SRP auth, authorization-code OAuth flow, and 1h/1h/24h token validity', () => {
      template.hasResourceProperties('AWS::Cognito::UserPoolClient', {
        ClientName: 'vip-admin-ui-client',
        GenerateSecret: false,
        ExplicitAuthFlows: Match.arrayWith(['ALLOW_USER_SRP_AUTH']),
        AllowedOAuthFlows: ['code'],
        AllowedOAuthFlowsUserPoolClient: true,
        AllowedOAuthScopes: ['openid', 'email', 'profile'],
        CallbackURLs: [
          'http://localhost:5173/callback',
          'https://dprtjww5c9892.cloudfront.net/callback',
        ],
        LogoutURLs: ['http://localhost:5173/', 'https://dprtjww5c9892.cloudfront.net/'],
        AccessTokenValidity: 60,
        IdTokenValidity: 60,
        RefreshTokenValidity: 1440,
        EnableTokenRevocation: true,
        PreventUserExistenceErrors: 'ENABLED',
        SupportedIdentityProviders: ['COGNITO'],
      });
    });

    it('creates a Hosted UI domain using the configured domain prefix', () => {
      template.hasResourceProperties('AWS::Cognito::UserPoolDomain', {
        Domain: 'vip-admin-ui-165505826690',
      });
    });

    it('emits the 4 documented CfnOutputs, including a hand-built UserPoolDomain URL using this.region', () => {
      template.hasOutput('UserPoolId', {});
      template.hasOutput('UserPoolArn', {});
      template.hasOutput('UserPoolClientId', {});
      template.hasOutput('UserPoolDomain', {
        Value: 'https://vip-admin-ui-165505826690.auth.us-east-1.amazoncognito.com',
      });
    });

    it('exposes userPool, userPoolClient, and userPoolDomain as public readonly properties', () => {
      expect(stack.userPool).toBeDefined();
      expect(stack.userPoolClient).toBeDefined();
      expect(stack.userPoolDomain).toBeDefined();
    });
  });

  describe('permissionsBoundaryName provided', () => {
    it('applies the PermissionsBoundary to the auto-generated smsRole (the only IAM::Role in this stack)', () => {
      const stack = buildStack({ permissionsBoundaryName: 'EngineeringPermissionBoundary' }, 'BoundedAuthStack');
      const template = Template.fromStack(stack);
      template.hasResourceProperties('AWS::IAM::Role', {
        PermissionsBoundary: {
          'Fn::Join': [
            '',
            ['arn:', { Ref: 'AWS::Partition' }, ':iam::165505826690:policy/EngineeringPermissionBoundary'],
          ],
        },
      });
    });
  });
});
