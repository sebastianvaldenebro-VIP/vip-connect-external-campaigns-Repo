import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as path from 'path';
import { buildSharedLayer } from '../utils/shared-layer';

export interface ApiSegmentsStackProps extends cdk.StackProps {
  readonly adminAuditTable: dynamodb.ITable;
  readonly segmentFilterConfigTable: dynamodb.ITable;
  readonly dataKey: kms.IKey;
  readonly profilesDomainName: string;
  readonly permissionsBoundaryName?: string;
  readonly sharedLayerArn?: string;
  /**
   * VPC + subnets + security group that can reach the Redis ElastiCache node.
   * Reusing the existing feeder networking avoids a second SG ingress rule.
   */
  readonly redisVpc: {
    vpcId: string;
    subnetIds: string[];
    availabilityZones: string[];
    securityGroupId: string;
  };
  readonly redis: {
    host: string;
    port: number;
    team: string;
    profileObjectType: string;
    passwordSecretArn?: string;
  };
}

export class ApiSegmentsStack extends cdk.Stack {
  public readonly lambdaFunction: lambda.Function;
  public readonly snapshotBucket: s3.Bucket;
  public readonly sharedLayer: lambda.LayerVersion;

  constructor(scope: Construct, id: string, props: ApiSegmentsStackProps) {
    super(scope, id, props);

    if (props.permissionsBoundaryName) {
      const boundary = iam.ManagedPolicy.fromManagedPolicyName(
        this,
        'PermissionsBoundary',
        props.permissionsBoundaryName,
      );
      iam.PermissionsBoundary.of(this).apply(boundary);
    }

    this.sharedLayer = buildSharedLayer(this, 'SharedLayer');

    // S3 bucket for segment snapshot exports
    this.snapshotBucket = new s3.Bucket(this, 'SnapshotBucket', {
      bucketName: `vip-admin-segment-snapshots-${this.account}`,
      encryption: s3.BucketEncryption.KMS,
      encryptionKey: props.dataKey,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      versioned: true,
      lifecycleRules: [
        {
          id: 'expire-old-snapshots',
          enabled: true,
          expiration: cdk.Duration.days(90),
          noncurrentVersionExpiration: cdk.Duration.days(30),
        },
      ],
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      enforceSSL: true,
    });

    // Dedicated role that Customer Profiles assumes to write snapshots to our
    // S3 bucket. The engineering permission boundary was updated to allow
    // iam:PassRole to profile.amazonaws.com, so we can pass this role from
    // the Lambda when calling CreateSegmentSnapshot.
    const snapshotRole = new iam.Role(this, 'SnapshotRole', {
      roleName: `VipAdminSnapshotRole-${this.region}`,
      assumedBy: new iam.ServicePrincipal('profile.amazonaws.com'),
      description: 'Role passed to Customer Profiles for segment snapshot writes',
    });
    this.snapshotBucket.grantWrite(snapshotRole);
    props.dataKey.grantEncryptDecrypt(snapshotRole);

    // CloudWatch log group (encrypted with CMK, retention 1 year)
    const logGroup = new logs.LogGroup(this, 'ApiSegmentsLogs', {
      logGroupName: '/aws/lambda/vip-admin-ui-api-segments',
      retention: logs.RetentionDays.ONE_YEAR,
      encryptionKey: props.dataKey,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const role = new iam.Role(this, 'FunctionRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Execution role for api-segments Lambda',
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          'service-role/AWSLambdaVPCAccessExecutionRole',
        ),
      ],
    });
    logGroup.grantWrite(role);

    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'CustomerProfilesSegments',
        actions: [
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
        resources: [
          `arn:aws:profile:${this.region}:${this.account}:domains/${props.profilesDomainName}`,
          `arn:aws:profile:${this.region}:${this.account}:domains/${props.profilesDomainName}/segment-definitions/*`,
        ],
      }),
    );

    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'OutboundCampaignsRetarget',
        actions: [
          'connect-campaigns:ListCampaigns',
          'connect-campaigns:UpdateCampaignSource',
          'connect-campaigns:DescribeCampaign',
        ],
        resources: [
          `arn:aws:connect-campaigns:${this.region}:${this.account}:campaign/*`,
        ],
      }),
    );

    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'SnapshotBucketRead',
        actions: ['s3:GetObject', 's3:ListBucket'],
        resources: [
          this.snapshotBucket.bucketArn,
          `${this.snapshotBucket.bucketArn}/*`,
        ],
      }),
    );

    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'AuditWrite',
        actions: ['dynamodb:PutItem'],
        resources: [props.adminAuditTable.tableArn],
      }),
    );

    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'SegmentFilterConfigRW',
        actions: [
          'dynamodb:GetItem',
          'dynamodb:PutItem',
          'dynamodb:UpdateItem',
          'dynamodb:DeleteItem',
        ],
        resources: [props.segmentFilterConfigTable.tableArn],
      }),
    );

    // PassRole to CP for snapshot writes — requires the boundary amendment
    // the security team approved (AllowPassRoleToCustomerProfiles).
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'PassSnapshotRoleToCustomerProfiles',
        actions: ['iam:PassRole'],
        resources: [snapshotRole.roleArn],
        conditions: {
          StringEquals: { 'iam:PassedToService': 'profile.amazonaws.com' },
        },
      }),
    );

    props.dataKey.grantEncryptDecrypt(role);

    // #003 — Redis AUTH: grant Secrets Manager read when a password secret is configured
    if (props.redis.passwordSecretArn) {
      role.addToPolicy(
        new iam.PolicyStatement({
          sid: 'RedisPasswordSecret',
          actions: ['secretsmanager:GetSecretValue'],
          resources: [props.redis.passwordSecretArn],
        }),
      );
    }

    // VPC wiring — reuse the existing feeder SG so Redis already allows us in.
    const vpc = ec2.Vpc.fromVpcAttributes(this, 'RedisVpc', {
      vpcId: props.redisVpc.vpcId,
      availabilityZones: props.redisVpc.availabilityZones,
      privateSubnetIds: props.redisVpc.subnetIds,
    });
    const sg = ec2.SecurityGroup.fromSecurityGroupId(
      this,
      'RedisSG',
      props.redisVpc.securityGroupId,
      { mutable: false },
    );

    this.lambdaFunction = new lambda.Function(this, 'FunctionSegments', {
      functionName: 'vip-admin-ui-api-segments',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: lambda.Code.fromAsset(
        path.join(__dirname, '../../../services/api-segments/src'),
      ),
      layers: [this.sharedLayer],
      memorySize: 1024,
      // Snapshot polling can take a few minutes on busy domains; verify +
      // reconcile both run synchronously so we need enough headroom.
      timeout: cdk.Duration.minutes(5),
      role,
      logGroup,
      reservedConcurrentExecutions: 10,
      vpc,
      vpcSubnets: {
        subnets: props.redisVpc.subnetIds.map((sid, i) =>
          ec2.Subnet.fromSubnetAttributes(this, `RedisSubnet${i}`, {
            subnetId: sid,
            availabilityZone: props.redisVpc.availabilityZones[i],
          }),
        ),
      },
      securityGroups: [sg],
      environment: {
        PROFILES_DOMAIN_NAME: props.profilesDomainName,
        PROFILE_OBJECT_TYPE: props.redis.profileObjectType,
        AUDIT_TABLE: props.adminAuditTable.tableName,
        SEGMENT_FILTER_CONFIG_TABLE: props.segmentFilterConfigTable.tableName,
        DATA_KEY_ARN: props.dataKey.keyArn,
        SNAPSHOT_BUCKET: this.snapshotBucket.bucketName,
        SNAPSHOT_ROLE_ARN: snapshotRole.roleArn,
        REDIS_HOST: props.redis.host,
        REDIS_PORT: String(props.redis.port),
        TEAM: props.redis.team,
        LOG_LEVEL: 'INFO',
        POWERTOOLS_SERVICE_NAME: 'api-segments',
      },
    });

    // #003 — inject Redis AUTH secret ARN when configured
    if (props.redis.passwordSecretArn) {
      this.lambdaFunction.addEnvironment('REDIS_PASSWORD_SECRET_ARN', props.redis.passwordSecretArn);
    }

    new cdk.CfnOutput(this, 'FunctionArn', { value: this.lambdaFunction.functionArn });
    new cdk.CfnOutput(this, 'SnapshotBucketName', { value: this.snapshotBucket.bucketName });
    new cdk.CfnOutput(this, 'SharedLayerArn', { value: this.sharedLayer.layerVersionArn });
  }
}
