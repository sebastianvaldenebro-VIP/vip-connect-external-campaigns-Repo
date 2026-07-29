import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as apigatewayv2 from 'aws-cdk-lib/aws-apigatewayv2';
import * as authorizers from 'aws-cdk-lib/aws-apigatewayv2-authorizers';
import * as integrations from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';

export interface ApiStackProps extends cdk.StackProps {
  readonly userPool: cognito.IUserPool;
  readonly userPoolClient: cognito.IUserPoolClient;
  readonly segmentsFunction: lambda.IFunction;
  readonly campaignsFunction: lambda.IFunction;
  readonly metricsFunction: lambda.IFunction;
  readonly profilesFunction: lambda.IFunction;
  readonly plansFunction: lambda.IFunction;
  readonly progressiveDialerSeedFunction: lambda.IFunction;
  readonly corsAllowOrigins: string[];
  readonly permissionsBoundaryName?: string;
}

export class ApiStack extends cdk.Stack {
  public readonly httpApi: apigatewayv2.HttpApi;
  public readonly apiUrl: string;

  constructor(scope: Construct, id: string, props: ApiStackProps) {
    super(scope, id, props);

    if (props.permissionsBoundaryName) {
      const boundary = iam.ManagedPolicy.fromManagedPolicyName(
        this,
        'PermissionsBoundary',
        props.permissionsBoundaryName,
      );
      iam.PermissionsBoundary.of(this).apply(boundary);
    }

    this.httpApi = new apigatewayv2.HttpApi(this, 'AdminApi', {
      apiName: 'vip-admin-ui-api',
      description: 'HTTP API fronting admin-ui Lambdas',
      corsPreflight: {
        allowOrigins: props.corsAllowOrigins,
        allowMethods: [
          apigatewayv2.CorsHttpMethod.GET,
          apigatewayv2.CorsHttpMethod.POST,
          apigatewayv2.CorsHttpMethod.PUT,
          apigatewayv2.CorsHttpMethod.DELETE,
          apigatewayv2.CorsHttpMethod.PATCH,
          apigatewayv2.CorsHttpMethod.OPTIONS,
        ],
        allowHeaders: ['Authorization', 'Content-Type', 'X-Amz-Date', 'X-Api-Key'],
        maxAge: cdk.Duration.hours(1),
        allowCredentials: false,
      },
    });

    const authorizer = new authorizers.HttpJwtAuthorizer(
      'CognitoJwtAuthorizer',
      `https://cognito-idp.${this.region}.amazonaws.com/${props.userPool.userPoolId}`,
      {
        jwtAudience: [props.userPoolClient.userPoolClientId],
        identitySource: ['$request.header.Authorization'],
      },
    );

    const segmentsIntegration = new integrations.HttpLambdaIntegration(
      'SegmentsIntegration',
      props.segmentsFunction,
    );
    const campaignsIntegration = new integrations.HttpLambdaIntegration(
      'CampaignsIntegration',
      props.campaignsFunction,
    );
    const metricsIntegration = new integrations.HttpLambdaIntegration(
      'MetricsIntegration',
      props.metricsFunction,
    );
    const profilesIntegration = new integrations.HttpLambdaIntegration(
      'ProfilesIntegration',
      props.profilesFunction,
    );
    const plansIntegration = new integrations.HttpLambdaIntegration(
      'PlansIntegration',
      props.plansFunction,
    );
    const progressiveDialerIntegration = new integrations.HttpLambdaIntegration(
      'ProgressiveDialerIntegration',
      props.progressiveDialerSeedFunction,
    );

    // ── Segments routes ─────────────────────────────────────────────
    this.httpApi.addRoutes({
      path: '/segments',
      methods: [apigatewayv2.HttpMethod.GET, apigatewayv2.HttpMethod.POST],
      integration: segmentsIntegration,
      authorizer,
    });
    this.httpApi.addRoutes({
      path: '/segments/{id}',
      methods: [
        apigatewayv2.HttpMethod.GET,
        apigatewayv2.HttpMethod.PATCH,
        apigatewayv2.HttpMethod.DELETE,
      ],
      integration: segmentsIntegration,
      authorizer,
    });
    this.httpApi.addRoutes({
      path: '/segments/{id}/verify',
      methods: [apigatewayv2.HttpMethod.POST],
      integration: segmentsIntegration,
      authorizer,
    });
    this.httpApi.addRoutes({
      path: '/segments/{id}/verify/extras',
      methods: [apigatewayv2.HttpMethod.POST],
      integration: segmentsIntegration,
      authorizer,
    });
    this.httpApi.addRoutes({
      path: '/segments/{id}/verify/extras/{snapshotId}',
      methods: [apigatewayv2.HttpMethod.GET],
      integration: segmentsIntegration,
      authorizer,
    });
    this.httpApi.addRoutes({
      path: '/segments/{id}/reconcile',
      methods: [apigatewayv2.HttpMethod.POST],
      integration: segmentsIntegration,
      authorizer,
    });
    this.httpApi.addRoutes({
      path: '/segments/preview-count',
      methods: [apigatewayv2.HttpMethod.POST],
      integration: segmentsIntegration,
      authorizer,
    });
    this.httpApi.addRoutes({
      path: '/leads/distinct-values',
      methods: [apigatewayv2.HttpMethod.GET],
      integration: segmentsIntegration,
      authorizer,
    });
    this.httpApi.addRoutes({
      path: '/segments/{id}/estimate',
      methods: [apigatewayv2.HttpMethod.POST],
      integration: segmentsIntegration,
      authorizer,
    });
    this.httpApi.addRoutes({
      path: '/segments/{id}/estimate/{estimateId}',
      methods: [apigatewayv2.HttpMethod.GET],
      integration: segmentsIntegration,
      authorizer,
    });
    this.httpApi.addRoutes({
      path: '/segments/{id}/snapshot',
      methods: [apigatewayv2.HttpMethod.POST],
      integration: segmentsIntegration,
      authorizer,
    });
    this.httpApi.addRoutes({
      path: '/segments/{id}/snapshot/{snapshotId}',
      methods: [apigatewayv2.HttpMethod.GET],
      integration: segmentsIntegration,
      authorizer,
    });

    // ── Campaigns routes ────────────────────────────────────────────
    this.httpApi.addRoutes({
      path: '/campaigns',
      methods: [apigatewayv2.HttpMethod.GET, apigatewayv2.HttpMethod.POST],
      integration: campaignsIntegration,
      authorizer,
    });
    this.httpApi.addRoutes({
      path: '/campaigns/{id}',
      methods: [
        apigatewayv2.HttpMethod.GET,
        apigatewayv2.HttpMethod.DELETE,
        apigatewayv2.HttpMethod.PATCH,
      ],
      integration: campaignsIntegration,
      authorizer,
    });
    for (const action of ['start', 'stop', 'pause', 'resume']) {
      this.httpApi.addRoutes({
        path: `/campaigns/{id}/${action}`,
        methods: [apigatewayv2.HttpMethod.POST],
        integration: campaignsIntegration,
        authorizer,
      });
    }
    for (const resource of ['queues', 'contact-flows', 'phone-numbers']) {
      this.httpApi.addRoutes({
        path: `/campaigns/resources/${resource}`,
        methods: [apigatewayv2.HttpMethod.GET],
        integration: campaignsIntegration,
        authorizer,
      });
    }

    // ── Audit routes ────────────────────────────────────────────────
    // Analytics endpoints were removed — operators do not use them. Audit
    // stays because Dashboard + Audit page depend on it, and the Lambda
    // that serves them is the same one the metrics endpoints used to share.
    this.httpApi.addRoutes({
      path: '/audit',
      methods: [apigatewayv2.HttpMethod.GET],
      integration: metricsIntegration,
      authorizer,
    });
    this.httpApi.addRoutes({
      path: '/audit/{entityId}',
      methods: [apigatewayv2.HttpMethod.GET],
      integration: metricsIntegration,
      authorizer,
    });

    // ── Branded Campaign Monitor routes ─────────────────────────────
    for (const path of [
      '/metrics/branded/today',
      '/metrics/branded/agents',
      '/metrics/branded/history',
    ]) {
      this.httpApi.addRoutes({
        path,
        methods: [apigatewayv2.HttpMethod.GET],
        integration: metricsIntegration,
        authorizer,
      });
    }
    this.httpApi.addRoutes({
      path: '/metrics/branded/campaigns/{brandedCampaignId}/metrics',
      methods: [apigatewayv2.HttpMethod.GET],
      integration: metricsIntegration,
      authorizer,
    });

    // ── Profiles routes ────────────────────────────────────────────
    this.httpApi.addRoutes({
      path: '/profiles/search',
      methods: [apigatewayv2.HttpMethod.GET],
      integration: profilesIntegration,
      authorizer,
    });
    this.httpApi.addRoutes({
      path: '/profiles/batch',
      methods: [apigatewayv2.HttpMethod.POST],
      integration: profilesIntegration,
      authorizer,
    });
    this.httpApi.addRoutes({
      path: '/profiles/{profileId}',
      methods: [apigatewayv2.HttpMethod.GET],
      integration: profilesIntegration,
      authorizer,
    });
    this.httpApi.addRoutes({
      path: '/profiles/{profileId}/objects',
      methods: [apigatewayv2.HttpMethod.GET],
      integration: profilesIntegration,
      authorizer,
    });
    this.httpApi.addRoutes({
      path: '/profiles/{profileId}/calculated-attributes',
      methods: [apigatewayv2.HttpMethod.GET],
      integration: profilesIntegration,
      authorizer,
    });
    this.httpApi.addRoutes({
      path: '/profiles/{profileId}/calculated-attributes/{attrName}',
      methods: [apigatewayv2.HttpMethod.GET],
      integration: profilesIntegration,
      authorizer,
    });

    // ── Plans routes ────────────────────────────────────────────────
    for (const [path, methods] of [
      ['/plans', [apigatewayv2.HttpMethod.GET, apigatewayv2.HttpMethod.POST]],
      ['/plans/{id}', [apigatewayv2.HttpMethod.GET, apigatewayv2.HttpMethod.PUT, apigatewayv2.HttpMethod.DELETE]],
      ['/templates', [apigatewayv2.HttpMethod.GET]],
      ['/plans/from-template/{tid}', [apigatewayv2.HttpMethod.POST]],
      ['/plans/{id}/runs', [apigatewayv2.HttpMethod.GET, apigatewayv2.HttpMethod.POST]],
      ['/plans/{id}/runs/{runId}', [apigatewayv2.HttpMethod.GET]],
      ['/plans/{id}/runs/{runId}/abort', [apigatewayv2.HttpMethod.POST]],
      ['/plans/{id}/runs/{runId}/force-finish', [apigatewayv2.HttpMethod.POST]],
      ['/plans/{id}/runs/{runId}/buckets/{bucketIndex}/force-start', [apigatewayv2.HttpMethod.POST]],
      ['/plans/{id}/runs/{runId}/buckets/{bucketIndex}/force-stop', [apigatewayv2.HttpMethod.POST]],
      ['/plans/{id}/runs/{runId}/buckets/{bucketIndex}/campaigns/{campaignIndex}/force-start', [apigatewayv2.HttpMethod.POST]],
      ['/plans/{id}/runs/{runId}/buckets/{bucketIndex}/campaigns/{campaignIndex}/force-stop', [apigatewayv2.HttpMethod.POST]],
    ] as [string, apigatewayv2.HttpMethod[]][]) {
      this.httpApi.addRoutes({
        path,
        methods,
        integration: plansIntegration,
        authorizer,
      });
    }

    // ── SMS routes ───────────────────────────────────────────────────
    for (const [path, methods] of [
      ['/sms/numbers', [apigatewayv2.HttpMethod.GET]],
      ['/plans/{id}/sms-runs', [apigatewayv2.HttpMethod.GET]],
      ['/location-mapping', [apigatewayv2.HttpMethod.GET]],
    ] as [string, apigatewayv2.HttpMethod[]][]) {
      this.httpApi.addRoutes({
        path,
        methods,
        integration: plansIntegration,
        authorizer,
      });
    }

    // ── Contact artifacts route ──────────────────────────────────────
    this.httpApi.addRoutes({
      path: '/contacts/{contactId}/artifacts',
      methods: [apigatewayv2.HttpMethod.GET],
      integration: plansIntegration,
      authorizer,
    });

    // ── Progressive Dialer routes ────────────────────────────────────
    this.httpApi.addRoutes({
      path: '/dialer/{id}/seed',
      methods: [apigatewayv2.HttpMethod.POST],
      integration: progressiveDialerIntegration,
      authorizer,
    });

    this.apiUrl = this.httpApi.apiEndpoint;

    // #002 — WAFv2 cannot associate with HTTP API v2 $default stages (WAFv2 ARN validator
    // rejects '$' in stage names — both CLI and CloudFormation fail with WAFInvalidParameterException).
    // WAF is applied at the CloudFront layer instead. See docs/waf-and-logging-deploy.sh.

    new cdk.CfnOutput(this, 'HttpApiId', { value: this.httpApi.apiId });
    new cdk.CfnOutput(this, 'HttpApiEndpoint', { value: this.apiUrl });
  }
}
