import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';

export interface DataStackProps extends cdk.StackProps {
  readonly auditRetentionYears: number;
  readonly permissionsBoundaryName?: string;
}

export class DataStack extends cdk.Stack {
  public readonly dataKey: kms.Key;
  public readonly filtersTable: dynamodb.Table;
  public readonly trackingTable: dynamodb.Table;
  public readonly auditTable: dynamodb.Table;
  public readonly adminAuditTable: dynamodb.Table;
  public readonly segmentFilterConfigTable: dynamodb.Table;

  constructor(scope: Construct, id: string, props: DataStackProps) {
    super(scope, id, props);

    if (props.permissionsBoundaryName) {
      const boundary = iam.ManagedPolicy.fromManagedPolicyName(
        this,
        'PermissionsBoundary',
        props.permissionsBoundaryName,
      );
      iam.PermissionsBoundary.of(this).apply(boundary);
    }

    this.dataKey = new kms.Key(this, 'DataKey', {
      alias: 'alias/prod/external-campaigns/data',
      description: 'CMK for external campaigns data at rest',
      enableKeyRotation: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // CloudWatch Logs needs explicit permission on the CMK to encrypt log
    // groups; otherwise LogGroup creation fails with
    // "The specified KMS key does not exist or is not allowed to be used".
    // The context condition scopes this to log groups in this account/region.
    this.dataKey.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: 'AllowCloudWatchLogsEncryption',
        effect: iam.Effect.ALLOW,
        principals: [
          new iam.ServicePrincipal(`logs.${this.region}.amazonaws.com`),
        ],
        actions: [
          'kms:Encrypt*',
          'kms:Decrypt*',
          'kms:ReEncrypt*',
          'kms:GenerateDataKey*',
          'kms:DescribeKey',
        ],
        resources: ['*'],
        conditions: {
          ArnLike: {
            'kms:EncryptionContext:aws:logs:arn': `arn:aws:logs:${this.region}:${this.account}:log-group:*`,
          },
        },
      }),
    );

    this.filtersTable = new dynamodb.Table(this, 'FiltersTable', {
      tableName: 'ExternalCampaignFilters',
      partitionKey: { name: 'campaign_id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: this.dataKey,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      deletionProtection: true,
    });

    this.trackingTable = new dynamodb.Table(this, 'TrackingTable', {
      tableName: 'ExternalCampaignDialTracking',
      partitionKey: { name: 'lead_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'campaign_id_pushed_at', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: this.dataKey,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      deletionProtection: true,
    });

    this.trackingTable.addGlobalSecondaryIndex({
      indexName: 'GSI1_ReattemptSchedule',
      partitionKey: { name: 'campaign_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'retry_scheduled_at', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    this.auditTable = new dynamodb.Table(this, 'AuditTable', {
      tableName: 'ExternalCampaignAudit',
      partitionKey: { name: 'entity_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'timestamp', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: this.dataKey,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      deletionProtection: true,
    });

    this.adminAuditTable = new dynamodb.Table(this, 'AdminAuditTable', {
      tableName: 'AdminAuditLog',
      partitionKey: { name: 'entity_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'timestamp', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: this.dataKey,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      deletionProtection: true,
    });

    this.adminAuditTable.addGlobalSecondaryIndex({
      indexName: 'GSI1_ByActor',
      partitionKey: { name: 'actor_sub', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'timestamp', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    this.adminAuditTable.addGlobalSecondaryIndex({
      indexName: 'GSI2_ByAction',
      partitionKey: { name: 'action', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'timestamp', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // Holds the authoritative filter definition for manual-sync segments.
    // Enables verify/reconcile to keep working after a rebuild replaces the
    // segment's own segmentGroups with a static customerId list.
    this.segmentFilterConfigTable = new dynamodb.Table(this, 'SegmentFilterConfigTable', {
      tableName: 'VipAdminSegmentFilterConfig',
      partitionKey: { name: 'family', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: this.dataKey,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      deletionProtection: true,
    });

    new cdk.CfnOutput(this, 'DataKeyArn', { value: this.dataKey.keyArn });
    new cdk.CfnOutput(this, 'SegmentFilterConfigTableArn', {
      value: this.segmentFilterConfigTable.tableArn,
    });
    new cdk.CfnOutput(this, 'FiltersTableArn', { value: this.filtersTable.tableArn });
    new cdk.CfnOutput(this, 'TrackingTableArn', { value: this.trackingTable.tableArn });
    new cdk.CfnOutput(this, 'AuditTableArn', { value: this.auditTable.tableArn });
    new cdk.CfnOutput(this, 'AdminAuditTableArn', { value: this.adminAuditTable.tableArn });
    new cdk.CfnOutput(this, 'AuditRetentionYears', { value: String(props.auditRetentionYears) });
  }
}
