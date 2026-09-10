import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import { HostingStack, HostingStackProps } from './hosting-stack';

const ACCOUNT = '165505826690';
const REGION = 'us-east-1';

function buildStack(overrides: Partial<HostingStackProps> = {}) {
  const app = new cdk.App();
  const props: HostingStackProps = {
    env: { account: ACCOUNT, region: REGION },
    ...overrides,
  };
  return new HostingStack(app, 'TestHostingStack', props);
}

describe('HostingStack — PermissionsBoundary', () => {
  it('does not create a PermissionsBoundary construct when permissionsBoundaryName is omitted', () => {
    const stack = buildStack();
    expect(stack.node.tryFindChild('PermissionsBoundary')).toBeUndefined();
  });

  it('resolves the boundary managed policy and synthesizes without throwing when permissionsBoundaryName is provided', () => {
    expect(() =>
      Template.fromStack(buildStack({ permissionsBoundaryName: 'TestBoundary' })),
    ).not.toThrow();
  });
});

describe('HostingStack — KMS CMK for asset bucket', () => {
  it('creates a KMS CMK with rotation enabled, RETAIN removal policy, and a CloudFront-decrypt resource policy scoped to this account', () => {
    const template = Template.fromStack(buildStack());
    template.resourceCountIs('AWS::KMS::Key', 1);
    template.hasResourceProperties('AWS::KMS::Key', {
      EnableKeyRotation: true,
      KeyPolicy: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: 'AllowCloudFrontDecrypt',
            Effect: 'Allow',
            Principal: { Service: 'cloudfront.amazonaws.com' },
            Action: 'kms:Decrypt',
            Resource: '*',
            Condition: { StringEquals: { 'aws:SourceAccount': ACCOUNT } },
          }),
        ]),
      },
    });
    template.hasResource('AWS::KMS::Key', { DeletionPolicy: 'Retain' });
  });
});

describe('HostingStack — S3 access-log bucket (AssetBucketLogs)', () => {
  it('is private, S3-managed-encrypted, SSL-enforced, versioned, with 90d/30d lifecycle and RETAIN policy', () => {
    const template = Template.fromStack(buildStack());
    template.hasResourceProperties('AWS::S3::Bucket', {
      BucketName: `vip-admin-ui-assets-logs-${ACCOUNT}`,
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
      BucketEncryption: {
        ServerSideEncryptionConfiguration: Match.arrayWith([
          Match.objectLike({
            ServerSideEncryptionByDefault: { SSEAlgorithm: 'AES256' },
          }),
        ]),
      },
      VersioningConfiguration: { Status: 'Enabled' },
      LifecycleConfiguration: {
        Rules: Match.arrayWith([
          Match.objectLike({
            Status: 'Enabled',
            ExpirationInDays: 90,
            NoncurrentVersionExpiration: { NoncurrentDays: 30 },
          }),
        ]),
      },
    });
    template.hasResource('AWS::S3::Bucket', {
      Properties: Match.objectLike({ BucketName: `vip-admin-ui-assets-logs-${ACCOUNT}` }),
      DeletionPolicy: 'Retain',
    });
  });

  it('gets ObjectWriter ownership (not BUCKET_OWNER_PREFERRED) — CDK auto-derives this from being used as AssetBucket.serverAccessLogsBucket, distinct from the explicit BUCKET_OWNER_PREFERRED on CloudFrontLogs', () => {
    // No `objectOwnership` is set explicitly in source for AssetBucketLogs, but
    // CDK's Bucket construct automatically sets AccessControl: LogDeliveryWrite +
    // OwnershipControls: ObjectWriter on any bucket passed as another bucket's
    // `serverAccessLogsBucket` (classic S3 access logging needs ACL-based delivery).
    const template = Template.fromStack(buildStack());
    template.hasResourceProperties('AWS::S3::Bucket', {
      BucketName: `vip-admin-ui-assets-logs-${ACCOUNT}`,
      AccessControl: 'LogDeliveryWrite',
      OwnershipControls: {
        Rules: [{ ObjectOwnership: 'ObjectWriter' }],
      },
    });
  });

  it('enforces SSL via a bucket policy denying non-HTTPS requests', () => {
    const template = Template.fromStack(buildStack());
    const buckets = template.findResources('AWS::S3::Bucket', {
      Properties: { BucketName: `vip-admin-ui-assets-logs-${ACCOUNT}` },
    });
    const [logicalId] = Object.keys(buckets);
    template.hasResourceProperties('AWS::S3::BucketPolicy', {
      Bucket: { Ref: logicalId },
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: 'Deny',
            Principal: { AWS: '*' },
            Condition: { Bool: { 'aws:SecureTransport': 'false' } },
          }),
        ]),
      },
    });
  });

  it('applies the CKV_AWS_18 checkov suppression (self-referential logging loop) with the access-log-bucket comment', () => {
    const template = Template.fromStack(buildStack());
    template.hasResource('AWS::S3::Bucket', {
      Properties: Match.objectLike({ BucketName: `vip-admin-ui-assets-logs-${ACCOUNT}` }),
      Metadata: Match.objectLike({
        checkov: {
          skip: Match.arrayWith([
            Match.objectLike({
              id: 'CKV_AWS_18',
              comment: Match.stringLikeRegexp('^This bucket IS the access-log destination for AssetBucket'),
            }),
          ]),
        },
      }),
    });
  });
});

describe('HostingStack — CloudFront standard-logs bucket (CloudFrontLogs)', () => {
  it('is private, S3-managed-encrypted, SSL-enforced, versioned, BUCKET_OWNER_PREFERRED, with 90d/30d lifecycle and RETAIN policy', () => {
    const template = Template.fromStack(buildStack());
    template.hasResourceProperties('AWS::S3::Bucket', {
      BucketName: `vip-admin-ui-cloudfront-logs-${ACCOUNT}`,
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
      OwnershipControls: {
        Rules: Match.arrayWith([Match.objectLike({ ObjectOwnership: 'BucketOwnerPreferred' })]),
      },
      BucketEncryption: {
        ServerSideEncryptionConfiguration: Match.arrayWith([
          Match.objectLike({
            ServerSideEncryptionByDefault: { SSEAlgorithm: 'AES256' },
          }),
        ]),
      },
      VersioningConfiguration: { Status: 'Enabled' },
      LifecycleConfiguration: {
        Rules: Match.arrayWith([
          Match.objectLike({
            Status: 'Enabled',
            ExpirationInDays: 90,
            NoncurrentVersionExpiration: { NoncurrentDays: 30 },
          }),
        ]),
      },
    });
    template.hasResource('AWS::S3::Bucket', {
      Properties: Match.objectLike({ BucketName: `vip-admin-ui-cloudfront-logs-${ACCOUNT}` }),
      DeletionPolicy: 'Retain',
    });
  });

  it('applies the CKV_AWS_18 checkov suppression with the CloudFront-logs-specific comment (distinct from AssetBucketLogs)', () => {
    const template = Template.fromStack(buildStack());
    template.hasResource('AWS::S3::Bucket', {
      Properties: Match.objectLike({ BucketName: `vip-admin-ui-cloudfront-logs-${ACCOUNT}` }),
      Metadata: Match.objectLike({
        checkov: {
          skip: Match.arrayWith([
            Match.objectLike({
              id: 'CKV_AWS_18',
              comment: Match.stringLikeRegexp('^This bucket IS a log destination \\(CloudFront standard logs\\)'),
            }),
          ]),
        },
      }),
    });
  });

  it('has exactly 2 log-destination buckets with BUCKET_OWNER_PREFERRED set on CloudFrontLogs only, never on AssetBucketLogs', () => {
    const template = Template.fromStack(buildStack());
    const withOwnerPreferred = template.findResources('AWS::S3::Bucket', {
      Properties: {
        OwnershipControls: {
          Rules: Match.arrayWith([Match.objectLike({ ObjectOwnership: 'BucketOwnerPreferred' })]),
        },
      },
    });
    expect(Object.keys(withOwnerPreferred)).toHaveLength(1);
    const [, resource] = Object.entries(withOwnerPreferred)[0];
    expect(resource.Properties.BucketName).toBe(`vip-admin-ui-cloudfront-logs-${ACCOUNT}`);
  });
});

describe('HostingStack — WAFv2 WebACL', () => {
  it('scopes to CLOUDFRONT with default allow and exactly 3 rules', () => {
    const template = Template.fromStack(buildStack());
    template.resourceCountIs('AWS::WAFv2::WebACL', 1);
    template.hasResourceProperties('AWS::WAFv2::WebACL', {
      Name: 'vip-admin-ui-waf',
      Scope: 'CLOUDFRONT',
      DefaultAction: { Allow: {} },
    });
    const webAcls = template.findResources('AWS::WAFv2::WebACL');
    const [, resource] = Object.entries(webAcls)[0];
    expect(resource.Properties.Rules).toHaveLength(3);
  });

  it('rule 0 is the AWS Managed Common Rule Set at priority 0 with override none', () => {
    const template = Template.fromStack(buildStack());
    template.hasResourceProperties('AWS::WAFv2::WebACL', {
      Rules: Match.arrayWith([
        Match.objectLike({
          Name: 'AWS-AWSManagedRulesCommonRuleSet',
          Priority: 0,
          OverrideAction: { None: {} },
          Statement: {
            ManagedRuleGroupStatement: { VendorName: 'AWS', Name: 'AWSManagedRulesCommonRuleSet' },
          },
        }),
      ]),
    });
  });

  it('rule 1 is the AWS Managed Known Bad Inputs Rule Set at priority 1 with override none', () => {
    const template = Template.fromStack(buildStack());
    template.hasResourceProperties('AWS::WAFv2::WebACL', {
      Rules: Match.arrayWith([
        Match.objectLike({
          Name: 'AWS-AWSManagedRulesKnownBadInputsRuleSet',
          Priority: 1,
          OverrideAction: { None: {} },
          Statement: {
            ManagedRuleGroupStatement: {
              VendorName: 'AWS',
              Name: 'AWSManagedRulesKnownBadInputsRuleSet',
            },
          },
        }),
      ]),
    });
  });

  it('rule 2 is a rate-based block rule at priority 2 with limit 2000 aggregated by IP', () => {
    const template = Template.fromStack(buildStack());
    template.hasResourceProperties('AWS::WAFv2::WebACL', {
      Rules: Match.arrayWith([
        Match.objectLike({
          Name: 'RateLimit',
          Priority: 2,
          Action: { Block: {} },
          Statement: {
            RateBasedStatement: { Limit: 2000, AggregateKeyType: 'IP' },
          },
        }),
      ]),
    });
  });
});

describe('HostingStack — AssetBucket', () => {
  it('is KMS-encrypted with the CMK, bucket-keys enabled, private, versioned, SSL-enforced, RETAIN, and logs to AssetBucketLogs/assets/', () => {
    const template = Template.fromStack(buildStack());
    const kmsKeys = template.findResources('AWS::KMS::Key');
    const [keyLogicalId] = Object.keys(kmsKeys);
    const logBuckets = template.findResources('AWS::S3::Bucket', {
      Properties: { BucketName: `vip-admin-ui-assets-logs-${ACCOUNT}` },
    });
    const [logBucketLogicalId] = Object.keys(logBuckets);

    template.hasResourceProperties('AWS::S3::Bucket', {
      BucketName: `vip-admin-ui-assets-${ACCOUNT}`,
      BucketEncryption: {
        ServerSideEncryptionConfiguration: [
          Match.objectLike({
            ServerSideEncryptionByDefault: {
              SSEAlgorithm: 'aws:kms',
              KMSMasterKeyID: { 'Fn::GetAtt': [keyLogicalId, 'Arn'] },
            },
            BucketKeyEnabled: true,
          }),
        ],
      },
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
      VersioningConfiguration: { Status: 'Enabled' },
      LoggingConfiguration: {
        DestinationBucketName: { Ref: logBucketLogicalId },
        LogFilePrefix: 'assets/',
      },
    });
    template.hasResource('AWS::S3::Bucket', {
      Properties: Match.objectLike({ BucketName: `vip-admin-ui-assets-${ACCOUNT}` }),
      DeletionPolicy: 'Retain',
    });
  });

  it('has exactly 3 S3 buckets total (AssetBucket + AssetBucketLogs + CloudFrontLogs)', () => {
    const template = Template.fromStack(buildStack());
    template.resourceCountIs('AWS::S3::Bucket', 3);
  });
});

describe('HostingStack — Origin Access Control + Response Headers Policy', () => {
  it('creates an S3 Origin Access Control named vip-admin-ui-oac', () => {
    const template = Template.fromStack(buildStack());
    template.hasResourceProperties('AWS::CloudFront::OriginAccessControl', {
      OriginAccessControlConfig: Match.objectLike({
        Name: 'vip-admin-ui-oac',
        OriginAccessControlOriginType: 's3',
        SigningBehavior: 'always',
        SigningProtocol: 'sigv4',
      }),
    });
  });

  it('creates a ResponseHeadersPolicy with HSTS/frame/referrer/xss/CSP security headers', () => {
    const template = Template.fromStack(buildStack());
    template.hasResourceProperties('AWS::CloudFront::ResponseHeadersPolicy', {
      ResponseHeadersPolicyConfig: Match.objectLike({
        Name: 'vip-admin-ui-security',
        SecurityHeadersConfig: {
          StrictTransportSecurity: {
            AccessControlMaxAgeSec: 31536000,
            IncludeSubdomains: true,
            Preload: true,
            Override: true,
          },
          ContentTypeOptions: { Override: true },
          FrameOptions: { FrameOption: 'DENY', Override: true },
          ReferrerPolicy: {
            ReferrerPolicy: 'strict-origin-when-cross-origin',
            Override: true,
          },
          XSSProtection: { ModeBlock: true, Protection: true, Override: true },
          ContentSecurityPolicy: {
            ContentSecurityPolicy: [
              "default-src 'self'",
              "connect-src 'self' https://*.amazonaws.com https://*.amazoncognito.com",
              "script-src 'self'",
              "style-src 'self' 'unsafe-inline'",
              "img-src 'self' data:",
              "font-src 'self'",
              "frame-ancestors 'none'",
              "base-uri 'self'",
              "form-action 'self'",
            ].join('; '),
            Override: true,
          },
        },
      }),
    });
  });
});

describe('HostingStack — CloudFront Distribution', () => {
  it('wires the WebACL and the CloudFront log bucket', () => {
    // NOTE: minimumProtocolVersion: TLS_V1_2_2021 is passed to the Distribution
    // construct (per the class docstring's CKV_AWS_174 explanation), but since no
    // domainNames/certificate are configured, CDK's L2 Distribution omits
    // ViewerCertificate from the synthesized DistributionConfig entirely (verified
    // via a raw template dump — no ViewerCertificate key is present at all, not
    // even with CloudFrontDefaultCertificate:true). There is nothing to assert on
    // for that setting until a custom domain is added — genuinely untestable via
    // Template assertions in the current (no-custom-domain) configuration.
    const template = Template.fromStack(buildStack());
    const webAcls = template.findResources('AWS::WAFv2::WebACL');
    const [webAclLogicalId] = Object.keys(webAcls);
    const cfLogBuckets = template.findResources('AWS::S3::Bucket', {
      Properties: { BucketName: `vip-admin-ui-cloudfront-logs-${ACCOUNT}` },
    });
    const [cfLogBucketLogicalId] = Object.keys(cfLogBuckets);

    template.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({
        Comment: 'VIP Admin UI SPA',
        DefaultRootObject: 'index.html',
        PriceClass: 'PriceClass_100',
        WebACLId: { 'Fn::GetAtt': [webAclLogicalId, 'Arn'] },
        Logging: Match.objectLike({
          Bucket: { 'Fn::GetAtt': [cfLogBucketLogicalId, 'RegionalDomainName'] },
          Prefix: 'cloudfront/',
        }),
      }),
    });
  });

  it('default behavior redirects to HTTPS, allows GET/HEAD, uses the CACHING_OPTIMIZED managed policy, compresses, and applies the security headers policy', () => {
    const template = Template.fromStack(buildStack());
    const headerPolicies = template.findResources('AWS::CloudFront::ResponseHeadersPolicy');
    const [headerPolicyLogicalId] = Object.keys(headerPolicies);

    template.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({
        DefaultCacheBehavior: Match.objectLike({
          ViewerProtocolPolicy: 'redirect-to-https',
          AllowedMethods: ['GET', 'HEAD'],
          CachePolicyId: cloudfront.CachePolicy.CACHING_OPTIMIZED.cachePolicyId,
          ResponseHeadersPolicyId: { Ref: headerPolicyLogicalId },
          Compress: true,
        }),
      }),
    });
  });

  it('the index.html additional behavior uses the CACHING_DISABLED managed policy (no compress flag set)', () => {
    const template = Template.fromStack(buildStack());
    template.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({
        CacheBehaviors: Match.arrayWith([
          Match.objectLike({
            PathPattern: 'index.html',
            ViewerProtocolPolicy: 'redirect-to-https',
            AllowedMethods: ['GET', 'HEAD'],
            CachePolicyId: cloudfront.CachePolicy.CACHING_DISABLED.cachePolicyId,
          }),
        ]),
      }),
    });
  });

  it('rewrites both 403 and 404 to /index.html with a 200 and zero error-caching TTL (SPA routing)', () => {
    const template = Template.fromStack(buildStack());
    template.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({
        CustomErrorResponses: Match.arrayWith([
          Match.objectLike({
            ErrorCode: 403,
            ResponseCode: 200,
            ResponsePagePath: '/index.html',
            ErrorCachingMinTTL: 0,
          }),
          Match.objectLike({
            ErrorCode: 404,
            ResponseCode: 200,
            ResponsePagePath: '/index.html',
            ErrorCachingMinTTL: 0,
          }),
        ]),
      }),
    });
  });

  it('applies the CKV_AWS_174 checkov suppression to the Distribution with the accepted-risk comment', () => {
    const template = Template.fromStack(buildStack());
    template.hasResource('AWS::CloudFront::Distribution', {
      Metadata: Match.objectLike({
        checkov: {
          skip: Match.arrayWith([
            Match.objectLike({
              id: 'CKV_AWS_174',
              comment: Match.stringLikeRegexp('^Accepted risk \\(Sebastian, 2026-09-09\\)'),
            }),
          ]),
        },
      }),
    });
  });

  it('creates exactly one Distribution', () => {
    const template = Template.fromStack(buildStack());
    template.resourceCountIs('AWS::CloudFront::Distribution', 1);
  });
});

describe('HostingStack — CfnOutputs', () => {
  it('emits AssetBucketName, DistributionId, and DistributionDomain', () => {
    const stack = buildStack();
    const template = Template.fromStack(stack);
    template.hasOutput('AssetBucketName', {});
    template.hasOutput('DistributionId', {});
    template.hasOutput('DistributionDomain', {});

    const outputs = template.findOutputs('DistributionDomain');
    const value = Object.values(outputs)[0].Value;
    // "https://" + Fn::GetAtt Distribution.DomainName, joined via Fn::Join
    expect(value['Fn::Join'][1][0]).toBe('https://');
  });
});
