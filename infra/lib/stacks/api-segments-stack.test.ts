import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import { ApiSegmentsStack, ApiSegmentsStackProps } from './api-segments-stack';

const ENV = { account: '165505826690', region: 'us-east-1' };
const DATA_KEY_ARN = 'arn:aws:kms:us-east-1:165505826690:key/11111111-1111-1111-1111-111111111111';
const ADMIN_AUDIT_TABLE_ARN = 'arn:aws:dynamodb:us-east-1:165505826690:table/AdminAuditLog';
const SEGMENT_FILTER_CONFIG_TABLE_ARN =
  'arn:aws:dynamodb:us-east-1:165505826690:table/VipAdminSegmentFilterConfig';
const PROFILES_DOMAIN_NAME = 'amazon-connect-vipmedicalgroup';
const REDIS_PASSWORD_SECRET_ARN =
  'arn:aws:secretsmanager:us-east-1:165505826690:secret:vip/redis/credentials-AbCdEf';

function buildStack(
  props: Partial<ApiSegmentsStackProps> = {},
  id = 'TestApiSegmentsStack',
): ApiSegmentsStack {
  const app = new cdk.App();
  const fixtures = new cdk.Stack(app, `${id}Fixtures`, { env: ENV });
  const dataKey = kms.Key.fromKeyArn(fixtures, 'DataKey', DATA_KEY_ARN);
  const adminAuditTable = dynamodb.Table.fromTableArn(fixtures, 'AdminAuditTable', ADMIN_AUDIT_TABLE_ARN);
  const segmentFilterConfigTable = dynamodb.Table.fromTableArn(
    fixtures,
    'SegmentFilterConfigTable',
    SEGMENT_FILTER_CONFIG_TABLE_ARN,
  );
  return new ApiSegmentsStack(app, id, {
    env: ENV,
    adminAuditTable,
    segmentFilterConfigTable,
    dataKey,
    profilesDomainName: PROFILES_DOMAIN_NAME,
    redisVpc: {
      vpcId: 'vpc-0d32b420acc84d370',
      subnetIds: ['subnet-06c7669b5e3e0e814', 'subnet-088367ac9fc0a2fec'],
      availabilityZones: ['us-east-1a', 'us-east-1b'],
      securityGroupId: 'sg-01d54d29c2a4785f1',
    },
    redis: {
      host: 'master.prod-medwork-api.jrdc0s.use1.cache.amazonaws.com',
      port: 6379,
      team: 'BASIC_TEAM',
      profileObjectType: 'leads-data-mapping',
    },
    ...props,
  });
}

describe('ApiSegmentsStack', () => {
  describe('default configuration (no boundary, no redis password secret)', () => {
    let stack: ApiSegmentsStack;
    let template: Template;

    beforeAll(() => {
      stack = buildStack();
      template = Template.fromStack(stack);
    });

    it('creates no PermissionsBoundary construct and sets no boundary on any IAM::Role', () => {
      expect(stack.node.tryFindChild('PermissionsBoundary')).toBeUndefined();
      const roles = template.findResources('AWS::IAM::Role');
      for (const [, role] of Object.entries(roles)) {
        expect(role.Properties.PermissionsBoundary).toBeUndefined();
      }
    });

    it('creates the access-log bucket (S3-managed encryption, versioned, 90d/30d lifecycle) with the self-referential-logging Checkov skip', () => {
      template.hasResourceProperties('AWS::S3::Bucket', {
        BucketName: 'vip-admin-segment-snapshots-logs-165505826690',
        BucketEncryption: {
          ServerSideEncryptionConfiguration: [
            Match.objectLike({ ServerSideEncryptionByDefault: { SSEAlgorithm: 'AES256' } }),
          ],
        },
        VersioningConfiguration: { Status: 'Enabled' },
        PublicAccessBlockConfiguration: {
          BlockPublicAcls: true,
          BlockPublicPolicy: true,
          IgnorePublicAcls: true,
          RestrictPublicBuckets: true,
        },
        LifecycleConfiguration: {
          Rules: [
            Match.objectLike({
              ExpirationInDays: 90,
              NoncurrentVersionExpiration: { NoncurrentDays: 30 },
            }),
          ],
        },
      });
      template.hasResource('AWS::S3::Bucket', {
        Properties: Match.objectLike({ BucketName: 'vip-admin-segment-snapshots-logs-165505826690' }),
        Metadata: Match.objectLike({
          checkov: {
            skip: Match.arrayWith([
              Match.objectLike({ id: 'CKV_AWS_18', comment: Match.stringLikeRegexp('self-referential') }),
            ]),
          },
        }),
      });
    });

    it('creates the snapshot bucket with KMS encryption via the data key and access logging pointed at the log bucket', () => {
      template.hasResourceProperties('AWS::S3::Bucket', {
        BucketName: 'vip-admin-segment-snapshots-165505826690',
        BucketEncryption: {
          ServerSideEncryptionConfiguration: [
            Match.objectLike({
              ServerSideEncryptionByDefault: { SSEAlgorithm: 'aws:kms', KMSMasterKeyID: DATA_KEY_ARN },
            }),
          ],
        },
        LoggingConfiguration: Match.objectLike({ LogFilePrefix: 'snapshots/' }),
        LifecycleConfiguration: {
          Rules: [
            Match.objectLike({
              Id: 'expire-old-snapshots',
              ExpirationInDays: 90,
              NoncurrentVersionExpiration: { NoncurrentDays: 30 },
            }),
          ],
        },
      });
    });

    it('creates a snapshotRole assumable only by profile.amazonaws.com, named with the region, granted write on the snapshot bucket and KMS encrypt/decrypt', () => {
      template.hasResourceProperties('AWS::IAM::Role', {
        RoleName: 'VipAdminSnapshotRole-us-east-1',
        AssumeRolePolicyDocument: {
          Statement: [Match.objectLike({ Principal: { Service: 'profile.amazonaws.com' } })],
        },
      });
      const policies = template.findResources('AWS::IAM::Policy');
      const snapshotPolicy = Object.values(policies).find(
        (p) => p.Properties.PolicyName && String(p.Properties.PolicyName).startsWith('SnapshotRoleDefaultPolicy'),
      );
      expect(snapshotPolicy).toBeDefined();
      const statements = snapshotPolicy!.Properties.PolicyDocument.Statement as Array<{
        Action: unknown;
      }>;
      const flatActions = statements.flatMap((s) => (Array.isArray(s.Action) ? s.Action : [s.Action]));
      expect(flatActions).toEqual(
        expect.arrayContaining(['s3:PutObject', 'kms:Encrypt', 'kms:Decrypt']),
      );
    });

    it('creates the FunctionRole with the AWSLambdaVPCAccessExecutionRole managed policy attached', () => {
      template.hasResourceProperties('AWS::IAM::Role', {
        Description: 'Execution role for api-segments Lambda',
        ManagedPolicyArns: [
          {
            'Fn::Join': [
              '',
              ['arn:', { Ref: 'AWS::Partition' }, ':iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole'],
            ],
          },
        ],
      });
    });

    it('grants the exact CustomerProfilesSegments action set scoped to the domain and its segment-definitions', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            {
              Sid: 'CustomerProfilesSegments',
              Effect: 'Allow',
              Action: [
                'profile:ListSegmentDefinitions',
                'profile:GetSegmentDefinition',
                'profile:CreateSegmentDefinition',
                'profile:DeleteSegmentDefinition',
                'profile:CreateSegmentEstimate',
                'profile:GetSegmentEstimate',
                'profile:CreateSegmentSnapshot',
                'profile:GetSegmentSnapshot',
                'profile:GetSegmentMembership',
                'profile:TagResource',
              ],
              Resource: [
                `arn:aws:profile:us-east-1:165505826690:domains/${PROFILES_DOMAIN_NAME}`,
                `arn:aws:profile:us-east-1:165505826690:domains/${PROFILES_DOMAIN_NAME}/segment-definitions/*`,
              ],
            },
          ]),
        },
      });
    });

    it('grants OutboundCampaignsRetarget scoped to campaign/*', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            {
              Sid: 'OutboundCampaignsRetarget',
              Effect: 'Allow',
              Action: [
                'connect-campaigns:ListCampaigns',
                'connect-campaigns:UpdateCampaignSource',
                'connect-campaigns:DescribeCampaign',
              ],
              Resource: 'arn:aws:connect-campaigns:us-east-1:165505826690:campaign/*',
            },
          ]),
        },
      });
    });

    it('grants SnapshotBucketRead (GetObject + ListBucket) on the bucket and its objects', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Sid: 'SnapshotBucketRead',
              Effect: 'Allow',
              Action: ['s3:GetObject', 's3:ListBucket'],
            }),
          ]),
        },
      });
    });

    it('grants AuditWrite and SegmentFilterConfigRW on the correct tables', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            {
              Sid: 'AuditWrite',
              Effect: 'Allow',
              Action: 'dynamodb:PutItem',
              Resource: ADMIN_AUDIT_TABLE_ARN,
            },
            {
              Sid: 'SegmentFilterConfigRW',
              Effect: 'Allow',
              Action: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:DeleteItem'],
              Resource: SEGMENT_FILTER_CONFIG_TABLE_ARN,
            },
          ]),
        },
      });
    });

    it('grants PassSnapshotRoleToCustomerProfiles with the iam:PassedToService condition scoped to profile.amazonaws.com', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Sid: 'PassSnapshotRoleToCustomerProfiles',
              Effect: 'Allow',
              Action: 'iam:PassRole',
              Condition: { StringEquals: { 'iam:PassedToService': 'profile.amazonaws.com' } },
            }),
          ]),
        },
      });
    });

    it('grants full KMS encrypt/decrypt on the data key to the FunctionRole', () => {
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

    it('does NOT grant secretsmanager:GetSecretValue anywhere when redis.passwordSecretArn is absent (both if-branches skipped)', () => {
      const policies = template.findResources('AWS::IAM::Policy');
      const allStatements = Object.values(policies).flatMap(
        (p) => p.Properties.PolicyDocument.Statement as Array<{ Action: unknown; Sid?: string }>,
      );
      const hasSecretGrant = allStatements.some((s) => s.Sid === 'RedisPasswordSecret');
      expect(hasSecretGrant).toBe(false);
    });

    it('does NOT set REDIS_PASSWORD_SECRET_ARN in the Lambda environment when the prop is absent', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        Environment: {
          Variables: {
            REDIS_PASSWORD_SECRET_ARN: Match.absent(),
          },
        },
      });
    });

    it('creates a KMS-encrypted DLQ with a 14-day retention period', () => {
      template.hasResourceProperties('AWS::SQS::Queue', {
        QueueName: 'vip-admin-ui-api-segments-dlq',
        KmsMasterKeyId: DATA_KEY_ARN,
        MessageRetentionPeriod: 14 * 24 * 60 * 60,
      });
    });

    it('creates the Lambda wired into the provided VPC subnets and security group, with 1024MB/5min sizing', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'vip-admin-ui-api-segments',
        Runtime: 'python3.12',
        Handler: 'handler.lambda_handler',
        MemorySize: 1024,
        Timeout: 300,
        ReservedConcurrentExecutions: 10,
        KmsKeyArn: DATA_KEY_ARN,
        VpcConfig: {
          SecurityGroupIds: ['sg-01d54d29c2a4785f1'],
          SubnetIds: ['subnet-06c7669b5e3e0e814', 'subnet-088367ac9fc0a2fec'],
        },
        Environment: {
          Variables: Match.objectLike({
            PROFILES_DOMAIN_NAME: PROFILES_DOMAIN_NAME,
            PROFILE_OBJECT_TYPE: 'leads-data-mapping',
            AUDIT_TABLE: 'AdminAuditLog',
            SEGMENT_FILTER_CONFIG_TABLE: 'VipAdminSegmentFilterConfig',
            DATA_KEY_ARN: DATA_KEY_ARN,
            REDIS_HOST: 'master.prod-medwork-api.jrdc0s.use1.cache.amazonaws.com',
            REDIS_PORT: '6379',
            TEAM: 'BASIC_TEAM',
            LOG_LEVEL: 'INFO',
            POWERTOOLS_SERVICE_NAME: 'api-segments',
          }),
        },
      });
    });

    it('builds the VPC subnet list with exactly one Subnet construct per configured subnetId (map callback exercised twice)', () => {
      const fn = template.findResources('AWS::Lambda::Function');
      const [resource] = Object.values(fn);
      expect(resource.Properties.VpcConfig.SubnetIds).toHaveLength(2);
    });

    it('emits FunctionArn, SnapshotBucketName, and SharedLayerArn CfnOutputs', () => {
      template.hasOutput('FunctionArn', {});
      template.hasOutput('SnapshotBucketName', {});
      template.hasOutput('SharedLayerArn', {});
    });

    it('exposes lambdaFunction, snapshotBucket, and sharedLayer as public readonly properties', () => {
      expect(stack.lambdaFunction).toBeDefined();
      expect(stack.snapshotBucket).toBeDefined();
      expect(stack.sharedLayer).toBeDefined();
    });
  });

  describe('permissionsBoundaryName and redis.passwordSecretArn provided', () => {
    let stack: ApiSegmentsStack;
    let template: Template;

    beforeAll(() => {
      stack = buildStack(
        {
          permissionsBoundaryName: 'EngineeringPermissionBoundary',
          redis: {
            host: 'master.prod-medwork-api.jrdc0s.use1.cache.amazonaws.com',
            port: 6379,
            team: 'BASIC_TEAM',
            profileObjectType: 'leads-data-mapping',
            passwordSecretArn: REDIS_PASSWORD_SECRET_ARN,
          },
        },
        'BoundedApiSegmentsStack',
      );
      template = Template.fromStack(stack);
    });

    it('applies the PermissionsBoundary to every IAM::Role in the stack (FunctionRole and SnapshotRole)', () => {
      const roles = template.findResources('AWS::IAM::Role');
      const expectedBoundary = {
        'Fn::Join': [
          '',
          ['arn:', { Ref: 'AWS::Partition' }, ':iam::165505826690:policy/EngineeringPermissionBoundary'],
        ],
      };
      const roleEntries = Object.values(roles);
      expect(roleEntries.length).toBeGreaterThanOrEqual(2);
      for (const role of roleEntries) {
        expect(role.Properties.PermissionsBoundary).toEqual(expectedBoundary);
      }
    });

    it('grants RedisPasswordSecret (secretsmanager:GetSecretValue) scoped to the exact secret ARN', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            {
              Sid: 'RedisPasswordSecret',
              Effect: 'Allow',
              Action: 'secretsmanager:GetSecretValue',
              Resource: REDIS_PASSWORD_SECRET_ARN,
            },
          ]),
        },
      });
    });

    it('injects REDIS_PASSWORD_SECRET_ARN into the Lambda environment via addEnvironment', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        Environment: {
          Variables: Match.objectLike({ REDIS_PASSWORD_SECRET_ARN: REDIS_PASSWORD_SECRET_ARN }),
        },
      });
    });
  });
});
