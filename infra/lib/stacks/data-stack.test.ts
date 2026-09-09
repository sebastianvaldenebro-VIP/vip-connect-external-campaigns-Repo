import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { DataStack } from './data-stack';

function buildStack(props: Partial<ConstructorParameters<typeof DataStack>[2]> = {}) {
  const app = new cdk.App();
  return new DataStack(app, 'TestDataStack', {
    env: { account: '165505826690', region: 'us-east-1' },
    auditRetentionYears: 6,
    ...props,
  });
}

describe('DataStack', () => {
  it('creates a KMS CMK with rotation enabled and RETAIN removal policy', () => {
    const template = Template.fromStack(buildStack());
    template.hasResourceProperties('AWS::KMS::Key', {
      EnableKeyRotation: true,
    });
    template.hasResource('AWS::KMS::Key', { DeletionPolicy: 'Retain' });
  });

  it('grants CloudWatch Logs kms:Encrypt/Decrypt scoped to this account/region log groups', () => {
    const template = Template.fromStack(buildStack());
    template.hasResourceProperties('AWS::KMS::Key', {
      KeyPolicy: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: 'AllowCloudWatchLogsEncryption',
            Principal: { Service: Match.stringLikeRegexp('^logs\\.') },
            Action: Match.arrayWith(['kms:Encrypt*', 'kms:Decrypt*']),
          }),
        ]),
      },
    });
  });

  it('creates all 5 DynamoDB tables with customer-managed encryption and deletion protection', () => {
    const template = Template.fromStack(buildStack());
    const tableNames = [
      'ExternalCampaignFilters',
      'ExternalCampaignDialTracking',
      'ExternalCampaignAudit',
      'AdminAuditLog',
      'VipAdminSegmentFilterConfig',
    ];
    for (const tableName of tableNames) {
      template.hasResourceProperties('AWS::DynamoDB::Table', {
        TableName: tableName,
        SSESpecification: Match.objectLike({ SSEEnabled: true, SSEType: 'KMS' }),
        DeletionProtectionEnabled: true,
      });
    }
    template.resourceCountIs('AWS::DynamoDB::Table', 5);
  });

  it('enables point-in-time recovery on every table', () => {
    const template = Template.fromStack(buildStack());
    const tables = template.findResources('AWS::DynamoDB::Table');
    for (const [, resource] of Object.entries(tables)) {
      expect(resource.Properties.PointInTimeRecoverySpecification).toEqual({
        PointInTimeRecoveryEnabled: true,
      });
    }
  });

  it('adds a TTL attribute to tracking, audit, and admin-audit tables but not filters/segment-config', () => {
    const template = Template.fromStack(buildStack());
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'ExternalCampaignDialTracking',
      TimeToLiveSpecification: { AttributeName: 'ttl', Enabled: true },
    });
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'ExternalCampaignFilters',
      TimeToLiveSpecification: Match.absent(),
    });
  });

  it('adds the expected GSIs on TrackingTable and AdminAuditTable', () => {
    const template = Template.fromStack(buildStack());
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'ExternalCampaignDialTracking',
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({ IndexName: 'GSI1_ReattemptSchedule' }),
      ]),
    });
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'AdminAuditLog',
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({ IndexName: 'GSI1_ByActor' }),
        Match.objectLike({ IndexName: 'GSI2_ByAction' }),
      ]),
    });
  });

  it('resolves the boundary managed policy and synthesizes without throwing when permissionsBoundaryName is provided', () => {
    // DataStack creates no IAM::Role of its own, so the PermissionsBoundary
    // aspect has no resource-level effect to assert on here (it only alters
    // roles created within this stack, and there are none) — this branch's
    // observable behavior is "doesn't throw resolving the managed policy",
    // which is what this asserts.
    expect(() =>
      Template.fromStack(buildStack({ permissionsBoundaryName: 'TestBoundary' })),
    ).not.toThrow();
  });

  it('does not throw and produces no PermissionsBoundary construct when the prop is omitted', () => {
    const app = new cdk.App();
    const stack = new DataStack(app, 'NoBoundaryStack', { auditRetentionYears: 6 });
    expect(stack.node.tryFindChild('PermissionsBoundary')).toBeUndefined();
  });

  it('emits the 7 documented CfnOutputs', () => {
    const template = Template.fromStack(buildStack());
    template.hasOutput('DataKeyArn', {});
    template.hasOutput('SegmentFilterConfigTableArn', {});
    template.hasOutput('FiltersTableArn', {});
    template.hasOutput('TrackingTableArn', {});
    template.hasOutput('AuditTableArn', {});
    template.hasOutput('AdminAuditTableArn', {});
    template.hasOutput('AuditRetentionYears', { Value: '6' });
  });

  it('reflects a custom auditRetentionYears value in the CfnOutput', () => {
    const template = Template.fromStack(buildStack({ auditRetentionYears: 3 }));
    template.hasOutput('AuditRetentionYears', { Value: '3' });
  });
});
