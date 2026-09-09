import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import * as kms from 'aws-cdk-lib/aws-kms';
import { ApiProfilesStack, ApiProfilesStackProps } from './api-profiles-stack';

const ENV = { account: '165505826690', region: 'us-east-1' };
const DATA_KEY_ARN = 'arn:aws:kms:us-east-1:165505826690:key/11111111-1111-1111-1111-111111111111';
const PROFILES_DOMAIN_NAME = 'amazon-connect-vipmedicalgroup';

function buildStack(
  props: Partial<ApiProfilesStackProps> = {},
  id = 'TestApiProfilesStack',
): ApiProfilesStack {
  const app = new cdk.App();
  const fixtures = new cdk.Stack(app, `${id}Fixtures`, { env: ENV });
  const dataKey = kms.Key.fromKeyArn(fixtures, 'DataKey', DATA_KEY_ARN);
  return new ApiProfilesStack(app, id, {
    env: ENV,
    dataKey,
    profilesDomainName: PROFILES_DOMAIN_NAME,
    ...props,
  });
}

describe('ApiProfilesStack', () => {
  describe('default configuration (no boundary, no profileObjectType override)', () => {
    let stack: ApiProfilesStack;
    let template: Template;

    beforeAll(() => {
      stack = buildStack();
      template = Template.fromStack(stack);
    });

    it('creates a KMS-encrypted, 1-year-retention CloudWatch LogGroup with a RETAIN removal policy', () => {
      template.hasResourceProperties('AWS::Logs::LogGroup', {
        LogGroupName: '/aws/lambda/vip-admin-ui-api-profiles',
        RetentionInDays: 365,
        KmsKeyId: DATA_KEY_ARN,
      });
      template.hasResource('AWS::Logs::LogGroup', { DeletionPolicy: 'Retain' });
    });

    it('creates a Lambda execution role assumable only by lambda.amazonaws.com, with no PermissionsBoundary', () => {
      template.hasResourceProperties('AWS::IAM::Role', {
        AssumeRolePolicyDocument: {
          Statement: [
            Match.objectLike({
              Action: 'sts:AssumeRole',
              Principal: { Service: 'lambda.amazonaws.com' },
            }),
          ],
        },
        Description: 'Execution role for api-profiles Lambda',
        PermissionsBoundary: Match.absent(),
      });
      expect(stack.node.tryFindChild('PermissionsBoundary')).toBeUndefined();
    });

    it('grants the exact CustomerProfilesRead action set scoped to the configured domain', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            {
              Sid: 'CustomerProfilesRead',
              Effect: 'Allow',
              Action: [
                'profile:SearchProfiles',
                'profile:BatchGetProfile',
                'profile:ListProfileObjects',
                'profile:GetCalculatedAttributeForProfile',
                'profile:ListCalculatedAttributesForProfile',
                'profile:GetProfileObjectType',
              ],
              Resource: [
                `arn:aws:profile:us-east-1:165505826690:domains/${PROFILES_DOMAIN_NAME}`,
                `arn:aws:profile:us-east-1:165505826690:domains/${PROFILES_DOMAIN_NAME}/*`,
              ],
            },
          ]),
        },
      });
    });

    it('grants only kms:Decrypt on the data key (grantDecrypt, not grantEncryptDecrypt)', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: 'kms:Decrypt',
              Effect: 'Allow',
              Resource: DATA_KEY_ARN,
            }),
          ]),
        },
      });
      // Negative check: encrypt actions must NOT appear anywhere in this role's policy.
      const policies = template.findResources('AWS::IAM::Policy');
      const [policy] = Object.values(policies);
      const statements = policy.Properties.PolicyDocument.Statement as Array<{ Action: unknown }>;
      const flatActions = statements.flatMap((s) => (Array.isArray(s.Action) ? s.Action : [s.Action]));
      expect(flatActions).not.toContain('kms:Encrypt');
      expect(flatActions).not.toContain('kms:GenerateDataKey*');
    });

    it('creates a KMS-encrypted DLQ with a 14-day retention period', () => {
      template.hasResourceProperties('AWS::SQS::Queue', {
        QueueName: 'vip-admin-ui-api-profiles-dlq',
        KmsMasterKeyId: DATA_KEY_ARN,
        MessageRetentionPeriod: 14 * 24 * 60 * 60,
      });
    });

    it('creates the Lambda with expected sizing/concurrency and defaults PROFILE_OBJECT_TYPE to leads-data-mapping', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'vip-admin-ui-api-profiles',
        Runtime: 'python3.12',
        Handler: 'handler.lambda_handler',
        MemorySize: 512,
        Timeout: 30,
        ReservedConcurrentExecutions: 10,
        KmsKeyArn: DATA_KEY_ARN,
        Environment: {
          Variables: {
            PROFILES_DOMAIN_NAME: PROFILES_DOMAIN_NAME,
            PROFILE_OBJECT_TYPE: 'leads-data-mapping',
            DATA_KEY_ARN: DATA_KEY_ARN,
            LOG_LEVEL: 'INFO',
            POWERTOOLS_SERVICE_NAME: 'api-profiles',
          },
        },
      });
    });

    it('suppresses CKV_AWS_117 on the Lambda function metadata with the VPC-skip justification', () => {
      template.hasResource('AWS::Lambda::Function', {
        Metadata: Match.objectLike({
          checkov: {
            skip: Match.arrayWith([
              Match.objectLike({
                id: 'CKV_AWS_117',
                comment: Match.stringLikeRegexp('Customer Profiles public API'),
              }),
            ]),
          },
        }),
      });
    });

    it('emits the FunctionArn CfnOutput', () => {
      template.hasOutput('FunctionArn', {});
    });

    it('exposes lambdaFunction as a public readonly property', () => {
      expect(stack.lambdaFunction).toBeDefined();
    });
  });

  describe('permissionsBoundaryName and profileObjectType provided', () => {
    let template: Template;

    beforeAll(() => {
      const stack = buildStack(
        { permissionsBoundaryName: 'EngineeringPermissionBoundary', profileObjectType: 'custom-mapping' },
        'BoundedApiProfilesStack',
      );
      template = Template.fromStack(stack);
    });

    it('applies the PermissionsBoundary to the FunctionRole', () => {
      template.hasResourceProperties('AWS::IAM::Role', {
        PermissionsBoundary: {
          'Fn::Join': [
            '',
            ['arn:', { Ref: 'AWS::Partition' }, ':iam::165505826690:policy/EngineeringPermissionBoundary'],
          ],
        },
      });
    });

    it('overrides PROFILE_OBJECT_TYPE with the provided value instead of the default', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        Environment: {
          Variables: Match.objectLike({ PROFILE_OBJECT_TYPE: 'custom-mapping' }),
        },
      });
    });
  });
});
