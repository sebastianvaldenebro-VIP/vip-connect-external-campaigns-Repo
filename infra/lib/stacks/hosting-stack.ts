import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';

export interface HostingStackProps extends cdk.StackProps {
  readonly permissionsBoundaryName?: string;
}

/**
 * Hosts the SPA at a default CloudFront domain. The private S3 bucket holds
 * the built assets; CloudFront reaches it via Origin Access Control (OAC),
 * not via a public bucket policy.
 *
 * SPA routing works via a CustomErrorResponse that rewrites 404/403 back to
 * ``/index.html`` with a 200 — React Router takes it from there. That's the
 * pattern AWS documents for Vite/Next-Static/CRA hosted on CloudFront.
 */
export class HostingStack extends cdk.Stack {
  public readonly assetBucket: s3.Bucket;
  public readonly distribution: cloudfront.Distribution;

  constructor(scope: Construct, id: string, props: HostingStackProps) {
    super(scope, id, props);

    if (props.permissionsBoundaryName) {
      const boundary = iam.ManagedPolicy.fromManagedPolicyName(
        this,
        'PermissionsBoundary',
        props.permissionsBoundaryName,
      );
      iam.PermissionsBoundary.of(this).apply(boundary);
    }

    this.assetBucket = new s3.Bucket(this, 'AssetBucket', {
      bucketName: `vip-admin-ui-assets-${this.account}`,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      versioned: true,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // Origin Access Control — the modern replacement for Origin Access
    // Identity. Locks the bucket to CloudFront: nobody can GET an object
    // directly via s3.amazonaws.com.
    const oac = new cloudfront.S3OriginAccessControl(this, 'OAC', {
      originAccessControlName: 'vip-admin-ui-oac',
    });

    // Cache policies: long TTL for fingerprinted assets, no-cache for the
    // HTML shell so new deploys are picked up immediately. CloudFront rejects
    // enableAcceptEncoding* flags when caching is disabled, so we use the
    // AWS-managed CACHING_DISABLED policy for the shell.
    const shellCachePolicy = cloudfront.CachePolicy.CACHING_DISABLED;

    const securityHeaders = new cloudfront.ResponseHeadersPolicy(this, 'SecurityHeaders', {
      responseHeadersPolicyName: 'vip-admin-ui-security',
      securityHeadersBehavior: {
        strictTransportSecurity: {
          accessControlMaxAge: cdk.Duration.days(365),
          includeSubdomains: true,
          preload: true,
          override: true,
        },
        contentTypeOptions: { override: true },
        frameOptions: {
          frameOption: cloudfront.HeadersFrameOption.DENY,
          override: true,
        },
        referrerPolicy: {
          referrerPolicy: cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
          override: true,
        },
        xssProtection: { protection: true, modeBlock: true, override: true },
      },
    });

    const origin = origins.S3BucketOrigin.withOriginAccessControl(this.assetBucket, {
      originAccessControl: oac,
    });

    this.distribution = new cloudfront.Distribution(this, 'Distribution', {
      comment: 'VIP Admin UI SPA',
      defaultRootObject: 'index.html',
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
      minimumProtocolVersion: cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
      defaultBehavior: {
        origin,
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        responseHeadersPolicy: securityHeaders,
        compress: true,
      },
      additionalBehaviors: {
        'index.html': {
          origin,
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD,
          cachePolicy: shellCachePolicy,
          responseHeadersPolicy: securityHeaders,
        },
      },
      // React Router: rewrite S3 404/403 responses to /index.html so deep
      // links work on refresh.
      errorResponses: [
        {
          httpStatus: 403,
          responseHttpStatus: 200,
          responsePagePath: '/index.html',
          ttl: cdk.Duration.seconds(0),
        },
        {
          httpStatus: 404,
          responseHttpStatus: 200,
          responsePagePath: '/index.html',
          ttl: cdk.Duration.seconds(0),
        },
      ],
    });

    new cdk.CfnOutput(this, 'AssetBucketName', {
      value: this.assetBucket.bucketName,
      description: 'Upload built assets here with aws s3 sync',
    });
    new cdk.CfnOutput(this, 'DistributionId', {
      value: this.distribution.distributionId,
      description: 'Use with aws cloudfront create-invalidation',
    });
    new cdk.CfnOutput(this, 'DistributionDomain', {
      value: `https://${this.distribution.distributionDomainName}`,
      description: 'Open this URL after first upload',
    });
  }
}
