import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import { ApiCampaignsStack, ApiCampaignsStackProps } from './api-campaigns-stack';

const ENV = { account: '165505826690', region: 'us-east-1' };
const DATA_KEY_ARN = 'arn:aws:kms:us-east-1:165505826690:key/11111111-1111-1111-1111-111111111111';
const ADMIN_AUDIT_TABLE_ARN = 'arn:aws:dynamodb:us-east-1:165505826690:table/AdminAuditLog';
const CONNECT_INSTANCE_ID = '6b3f17ba-68a4-472a-9b20-db1991507009';
const PROFILES_DOMAIN_NAME = 'amazon-connect-vipmedicalgroup';

function buildStack(
  props: Partial<ApiCampaignsStackProps> = {},
  id = 'TestApiCampaignsStack',
): ApiCampaignsStack {
  const app = new cdk.App();
  const fixtures = new cdk.Stack(app, `${id}Fixtures`, { env: ENV });
  const dataKey = kms.Key.fromKeyArn(fixtures, 'DataKey', DATA_KEY_ARN);
  const adminAuditTable = dynamodb.Table.fromTableArn(fixtures, 'AdminAuditTable', ADMIN_AUDIT_TABLE_ARN);
  return new ApiCampaignsStack(app, id, {
    env: ENV,
    adminAuditTable,
    dataKey,
    connectInstanceId: CONNECT_INSTANCE_ID,
    profilesDomainName: PROFILES_DOMAIN_NAME,
    ...props,
  });
}

describe('ApiCampaignsStack', () => {
  describe('default configuration (no permissions boundary)', () => {
    let stack: ApiCampaignsStack;
    let template: Template;

    beforeAll(() => {
      stack = buildStack();
      template = Template.fromStack(stack);
    });

    it('creates a KMS-encrypted, 1-year-retention CloudWatch LogGroup with a RETAIN removal policy', () => {
      template.hasResourceProperties('AWS::Logs::LogGroup', {
        LogGroupName: '/aws/lambda/vip-admin-ui-api-campaigns',
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
        Description: 'Execution role for api-campaigns Lambda',
        PermissionsBoundary: Match.absent(),
      });
      expect(stack.node.tryFindChild('PermissionsBoundary')).toBeUndefined();
    });

    it('grants the role exactly the log write actions on the ApiCampaignsLogs log group', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: ['logs:CreateLogStream', 'logs:PutLogEvents'],
              Effect: 'Allow',
              Resource: Match.objectLike({ 'Fn::GetAtt': Match.arrayWith(['Arn']) }),
            }),
          ]),
        },
      });
    });

    it('grants the exact ConnectCampaignsV2 action set scoped to campaign/* in this account/region', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            {
              Sid: 'ConnectCampaignsV2',
              Effect: 'Allow',
              Action: [
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
                'connect-campaigns:TagResource',
                'connect-campaigns:UntagResource',
                'connect-campaigns:ListTagsForResource',
              ],
              Resource: 'arn:aws:connect-campaigns:us-east-1:165505826690:campaign/*',
            },
          ]),
        },
      });
    });

    it('grants ConnectReadInstanceResources scoped to the configured connectInstanceId', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            {
              Sid: 'ConnectReadInstanceResources',
              Effect: 'Allow',
              Action: [
                'connect:ListQueues',
                'connect:ListContactFlows',
                'connect:DescribeContactFlow',
                'connect:DescribeQueue',
                'connect:DescribeInstance',
              ],
              Resource: [
                `arn:aws:connect:us-east-1:165505826690:instance/${CONNECT_INSTANCE_ID}`,
                `arn:aws:connect:us-east-1:165505826690:instance/${CONNECT_INSTANCE_ID}/*`,
              ],
            },
          ]),
        },
      });
    });

    it('grants ConnectPhoneNumberV2 with account-level phone-number/* plus the instance ARN', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            {
              Sid: 'ConnectPhoneNumberV2',
              Effect: 'Allow',
              Action: ['connect:ListPhoneNumbersV2', 'connect:DescribePhoneNumber'],
              Resource: [
                'arn:aws:connect:us-east-1:165505826690:phone-number/*',
                `arn:aws:connect:us-east-1:165505826690:instance/${CONNECT_INSTANCE_ID}`,
              ],
            },
          ]),
        },
      });
    });

    it('grants AuditWrite (dynamodb:PutItem only) scoped to the admin audit table ARN', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            {
              Sid: 'AuditWrite',
              Effect: 'Allow',
              Action: 'dynamodb:PutItem',
              Resource: ADMIN_AUDIT_TABLE_ARN,
            },
          ]),
        },
      });
    });

    it('grants full KMS encrypt/decrypt on the data key (no Sid — grantEncryptDecrypt default)', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: ['kms:Decrypt', 'kms:Encrypt', 'kms:ReEncrypt*', 'kms:GenerateDataKey*'],
              Effect: 'Allow',
              Resource: DATA_KEY_ARN,
            }),
          ]),
        },
      });
    });

    it('creates a KMS-encrypted DLQ with a 14-day retention period', () => {
      template.hasResourceProperties('AWS::SQS::Queue', {
        QueueName: 'vip-admin-ui-api-campaigns-dlq',
        KmsMasterKeyId: DATA_KEY_ARN,
        MessageRetentionPeriod: 14 * 24 * 60 * 60,
      });
    });

    it('creates the Lambda with the exact expected runtime, sizing, concurrency, DLQ wiring, and environment', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'vip-admin-ui-api-campaigns',
        Runtime: 'python3.12',
        Handler: 'handler.lambda_handler',
        MemorySize: 512,
        Timeout: 30,
        ReservedConcurrentExecutions: 10,
        KmsKeyArn: DATA_KEY_ARN,
        DeadLetterConfig: Match.objectLike({
          TargetArn: Match.objectLike({ 'Fn::GetAtt': Match.arrayWith(['Arn']) }),
        }),
        Environment: {
          Variables: {
            CONNECT_INSTANCE_ID: CONNECT_INSTANCE_ID,
            AWS_ACCOUNT_ID: '165505826690',
            PROFILES_DOMAIN_NAME: PROFILES_DOMAIN_NAME,
            AUDIT_TABLE: 'AdminAuditLog',
            DATA_KEY_ARN: DATA_KEY_ARN,
            LOG_LEVEL: 'INFO',
            POWERTOOLS_SERVICE_NAME: 'api-campaigns',
          },
        },
      });
    });

    it('attaches exactly one Lambda layer (the shared layer)', () => {
      const fn = template.findResources('AWS::Lambda::Function');
      const [resource] = Object.values(fn);
      expect(resource.Properties.Layers).toHaveLength(1);
    });

    it('suppresses CKV_AWS_117 on the Lambda function metadata with the VPC-skip justification', () => {
      template.hasResource('AWS::Lambda::Function', {
        Metadata: Match.objectLike({
          checkov: {
            skip: Match.arrayWith([
              Match.objectLike({
                id: 'CKV_AWS_117',
                comment: Match.stringLikeRegexp('Not internet-reachable'),
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

  describe('permissionsBoundaryName provided', () => {
    it('applies the PermissionsBoundary to the FunctionRole', () => {
      const stack = buildStack(
        { permissionsBoundaryName: 'EngineeringPermissionBoundary' },
        'BoundedApiCampaignsStack',
      );
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
