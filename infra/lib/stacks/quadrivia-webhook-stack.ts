import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as apigatewayv2 from 'aws-cdk-lib/aws-apigatewayv2';
import * as integrations from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as certificatemanager from 'aws-cdk-lib/aws-certificatemanager';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as path from 'path';
import { buildBundledPythonCode } from '../utils/python-bundling';
import { skipCheckovChecks } from '../utils/checkov-skip';

const APPLICATION = 'quadrivia-afterhours-callback';
const FUNCTION_NAME = 'vip-quadrivia-callback';
const TABLE_NAME = 'VipQuadriviaCallbackIdempotency';
const SECRET_NAME = 'vip/quadrivia/webhook-hmac';
const WEBHOOK_PATH = '/callbacks';

const VPC_SKIP = {
  id: 'CKV_AWS_117',
  comment:
    'Deliberately not in a VPC. This Lambda reaches only public AWS APIs ' +
    '(Connect, DynamoDB, Secrets Manager) already protected by TLS+IAM, and ' +
    'nothing private-VPC-only. It also sits in the synchronous path of a live ' +
    'voice call, where an ENI-attachment cold start is a caller-visible delay; ' +
    'exposure is already bounded by mTLS on the custom domain plus inline ' +
    'HMAC verification, not by network placement.',
};

const DLQ_SKIP = {
  id: 'CKV_AWS_116',
  comment:
    'No DLQ — invoked only synchronously by API Gateway. Lambda DLQs apply ' +
    'exclusively to asynchronous invocations, so one here would be dead infra. ' +
    'A failed callback is surfaced to Quadrivia as a 5xx for it to retry, and ' +
    'the idempotency table makes that retry safe.',
};

/** mTLS custom-domain wiring. See the comment on `mtlsDomain` below. */
export interface QuadriviaMtlsDomainProps {
  /**
   * FQDN for the webhook, e.g. `quadrivia-webhook.<zone>`.
   * PLACEHOLDER-FREE ON PURPOSE: no default. Nothing in this repo owns a
   * hosted zone, so inventing one would produce a stack that deploys and
   * silently serves nothing.
   */
  readonly domainName: string;
  /** ACM cert ARN for `domainName` (must be in this stack's region). */
  readonly certificateArn: string;
  /**
   * Bucket holding the PEM truststore of the CA that signs Quadrivia's client
   * certificate. Imported by name — the truststore is a security artifact
   * whose upload/rotation is an operator action, not a CDK asset.
   */
  readonly truststoreBucketName: string;
  /** Key of the truststore object, e.g. `quadrivia/truststore.pem`. */
  readonly truststoreKey: string;
  /**
   * S3 object version of the truststore. Strongly recommended: pinning a
   * version makes a CA rotation an explicit, reviewable stack change instead
   * of an out-of-band bucket write that silently changes who can call in.
   */
  readonly truststoreVersion?: string;
  /**
   * Required by API Gateway when `certificateArn` is an imported or private-CA
   * certificate rather than an ACM-issued public one.
   */
  readonly ownershipCertificateArn?: string;
}

export interface QuadriviaWebhookStackProps extends cdk.StackProps {
  /** KMS CMK used for the log group, the idempotency table, the secret and env vars. */
  readonly dataKey: kms.IKey;
  /**
   * Full ARN of the Amazon Connect instance, e.g.
   * `arn:aws:connect:us-east-1:<account>:instance/<instanceId>`.
   * Taken as a prop rather than rebuilt from a bare id so the IAM scope below
   * cannot silently widen if the account/region ever differ.
   */
  readonly connectInstanceArn: string;
  /** Task template the scheduled callback task is created from. */
  readonly taskTemplateId: string;
  /** Owner tag — email of the accountable engineer (SCP-mandated). */
  readonly ownerEmail: string;
  /** Team tag — one of `specialOps` | `engineering` | `medwork-devs`. */
  readonly team: string;
  /**
   * mTLS custom domain. When omitted the API is created with
   * `disableExecuteApiEndpoint: true` and no domain mapping, i.e. it is
   * reachable from nowhere — fail-closed, on purpose.
   *
   * >>> PENDING HUMAN DECISION (Sebastian): <<<
   * There is no hosted zone / ACM certificate / truststore bucket for this
   * webhook yet, and this repo owns none to borrow. Until that decision is
   * made this prop must be left undefined; supplying an invented FQDN would
   * create a real custom domain with a real (mis-issued) cert requirement and
   * defeat layer 1 of the defence-in-depth design. Also note: creating the
   * DomainName alone is not enough — the DNS ALIAS record pointing
   * `domainName` at `RegionalDomainName`/`RegionalHostedZoneId` (both emitted
   * as stack outputs) must be created in whichever zone is chosen, which is
   * intentionally NOT done here because that zone is unknown.
   */
  readonly mtlsDomain?: QuadriviaMtlsDomainProps;
  readonly permissionsBoundaryName?: string;
}

/**
 * Inbound webhook for Quadrivia's after-hours AI voice agent.
 *
 * Quadrivia answers VIP's calls outside business hours from another cloud.
 * When a caller asks for a callback, Quadrivia POSTs here and the Lambda
 * creates an Amazon Connect task with a `ScheduledTime` so a human agent
 * handles it once agents are back online.
 *
 * This is a machine-to-machine trust boundary and therefore gets its own
 * HttpApi — it does NOT reuse `vip-admin-ui-api` (ApiStack) or its Cognito
 * Lambda authorizer (ApiAuthorizerStack). Those authenticate interactive
 * human admins, cache authorizer results for 60s, and carry a documented
 * cross-route replay consideration in their own code. Sharing them would
 * couple a third party's availability to the internal admin UI's and widen
 * the blast radius of either one's misconfiguration.
 *
 * Three independent layers guard the endpoint:
 *   1. mTLS on a custom domain (transport) — `mtlsDomain` prop.
 *   2. HMAC-SHA256 over the raw body + a ±5min timestamp window, verified
 *      INLINE in the business Lambda. Not a separate Lambda authorizer on
 *      purpose: an extra Lambda in the synchronous path of a live call adds a
 *      second cold start's worth of latency while a patient waits on the line.
 *   3. `X-Request-Id` idempotency in DynamoDB with a 1-hour TTL.
 */
export class QuadriviaWebhookStack extends cdk.Stack {
  public readonly httpApi: apigatewayv2.HttpApi;
  public readonly lambdaFunction: lambda.Function;
  public readonly idempotencyTable: dynamodb.Table;
  public readonly hmacSecret: secretsmanager.Secret;
  public readonly domainName?: apigatewayv2.DomainName;

  constructor(scope: Construct, id: string, props: QuadriviaWebhookStackProps) {
    super(scope, id, props);

    if (props.permissionsBoundaryName) {
      const boundary = iam.ManagedPolicy.fromManagedPolicyName(
        this,
        'PermissionsBoundary',
        props.permissionsBoundaryName,
      );
      iam.PermissionsBoundary.of(this).apply(boundary);
    }

    // Validated with an explicit pattern rather than cdk.Arn.split() so a bare
    // instance id fails with a message that says what was actually wrong,
    // instead of Arn.split's generic "ARNs must start with arn:".
    const arnMatch = /^arn:aws[a-z-]*:connect:[a-z0-9-]+:\d{12}:instance\/([a-f0-9-]+)$/.exec(
      props.connectInstanceArn,
    );
    if (!arnMatch) {
      throw new Error(
        'connectInstanceArn must be a full Connect instance ARN ' +
          '(arn:aws:connect:<region>:<account>:instance/<instanceId>), got: ' +
          props.connectInstanceArn,
      );
    }
    const connectInstanceId = arnMatch[1];

    // ── Layer 3: idempotency store ──────────────────────────────────────
    this.idempotencyTable = new dynamodb.Table(this, 'IdempotencyTable', {
      tableName: TABLE_NAME,
      partitionKey: { name: 'requestId', type: dynamodb.AttributeType.STRING },
      // On-demand: traffic is a handful of after-hours requests per night with
      // no predictable shape, and a provisioned floor would be paid 24/7 to
      // sit idle.
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: props.dataKey,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      // Matches IDEMPOTENCY_TTL_SECONDS in the handler.
      timeToLiveAttribute: 'ttl',
      // RETAIN, never DESTROY: this is account 165505826690 (production).
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // ── Layer 2: HMAC signing key ───────────────────────────────────────
    // Created empty-but-generated: no secret value in version control, and
    // the initial value is a real random key rather than a guessable
    // placeholder. Quadrivia gets the value out-of-band, and rotation is a
    // Secrets Manager operation — not a stack change.
    this.hmacSecret = new secretsmanager.Secret(this, 'HmacSecret', {
      secretName: SECRET_NAME,
      description: 'Shared HMAC-SHA256 signing key for the Quadrivia callback webhook',
      encryptionKey: props.dataKey,
      generateSecretString: {
        secretStringTemplate: JSON.stringify({}),
        generateStringKey: 'signingKey',
        passwordLength: 64,
        excludePunctuation: true,
      },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const logGroup = new logs.LogGroup(this, 'FunctionLogs', {
      logGroupName: `/aws/lambda/${FUNCTION_NAME}`,
      retention: logs.RetentionDays.ONE_YEAR,
      encryptionKey: props.dataKey,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // ── Least-privilege execution role ──────────────────────────────────
    // NOTE FOR WHOEVER DEPLOYS THIS: EngineeringPermissionBoundary denies
    // iam:CreateRole/PutRolePolicy to this app's CFN exec role, which is why
    // several sibling stacks import a hand-created role instead (see
    // api-authorizer-stack.ts and api-sms-stack.ts). The role is declared here
    // anyway so the intended least-privilege policy is version-controlled and
    // test-asserted; if the deploy hits that deny, mirror this exact policy
    // into a manually-created role and switch to Role.fromRoleArn — do not
    // widen it.
    const role = new iam.Role(this, 'FunctionRole', {
      roleName: `${FUNCTION_NAME}-role`,
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Execution role for the Quadrivia after-hours callback webhook',
    });

    // Log delivery only into this function's own log group — not the
    // AWSLambdaBasicExecutionRole managed policy, which allows
    // logs:CreateLogGroup account-wide.
    role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'WriteOwnLogs',
        effect: iam.Effect.ALLOW,
        actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
        resources: [logGroup.logGroupArn, `${logGroup.logGroupArn}:*`],
      }),
    );

    // StartTaskContact only, scoped to contacts of this one Connect instance.
    // No connect:StopContact / UpdateContact / ListTaskTemplates etc. — the
    // webhook creates and never reads or mutates.
    //
    // OPEN ITEM: AWS's StartTaskContact docs also discuss task-template
    // permissions; if a live invoke returns AccessDenied mentioning
    // GetTaskTemplate, add exactly that one action scoped to
    // `${connectInstanceArn}/task-template/${taskTemplateId}` — nothing
    // broader. Deliberately not pre-granted here, since granting an
    // unverified permission is the same mistake in the other direction.
    role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'StartScheduledCallbackTask',
        effect: iam.Effect.ALLOW,
        actions: ['connect:StartTaskContact'],
        resources: [`${props.connectInstanceArn}/contact/*`],
      }),
    );

    // Exactly PutItem + GetItem — NOT grantReadWriteData(), which would also
    // hand over Query/Scan/UpdateItem/DeleteItem/BatchWriteItem. PutItem does
    // the atomic conditional claim; GetItem is used only on the duplicate path
    // to return the original contactId alongside the 409.
    role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'IdempotencyClaim',
        effect: iam.Effect.ALLOW,
        // DeleteItem is for _release_reservation (handler.py): without it, a
        // Connect failure's cleanup delete silently AccessDenied's (caught
        // and only WARN-logged), so the release never actually happens and
        // the request_id stays claimed for the full TTL — exactly the bug
        // this cleanup exists to prevent.
        actions: ['dynamodb:PutItem', 'dynamodb:GetItem', 'dynamodb:DeleteItem'],
        resources: [this.idempotencyTable.tableArn],
      }),
    );

    // Written as explicit identity-policy statements rather than
    // `hmacSecret.grantRead(role)` / `dataKey.grantDecrypt(role)` on purpose.
    // Both of those helpers append this role's ARN to the *CMK's resource
    // policy*, and the CMK is owned by DataStack — that makes DataStack depend
    // on this stack's role while this stack depends on DataStack's key ARN,
    // which CloudFormation rejects as a cyclic reference (reproduced in this
    // stack's own test). The CMK's default key policy already delegates to IAM
    // for the account root, so identity-side grants are sufficient and stay
    // one-directional.
    role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'ReadWebhookSigningKey',
        effect: iam.Effect.ALLOW,
        actions: ['secretsmanager:GetSecretValue'],
        resources: [this.hmacSecret.secretArn],
      }),
    );
    // One CMK protects three things this role touches, and each needs a
    // different slice of KMS — under-granting here is invisible to unit tests
    // (the boto3 clients are mocked) and only surfaces as a runtime
    // AccessDenied in production:
    //   - Secrets Manager GetSecretValue on a CMK-encrypted secret → Decrypt
    //   - Lambda decrypting the encrypted environment variables at init
    //     → Decrypt (performed with the execution role's permissions)
    //   - DynamoDB PutItem into a CMK-encrypted table → GenerateDataKey* /
    //     Encrypt / ReEncrypt*, not just Decrypt (this is the same action set
    //     CDK's own grantEncryptDecrypt() emits for table.grantWriteData()).
    // Still scoped to this one key ARN. A `kms:ViaService` condition limiting
    // it to dynamodb/secretsmanager/lambda in this region would be tighter
    // still, but is left off until it can be verified against a real invoke
    // rather than guessed.
    role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'UseDataKey',
        effect: iam.Effect.ALLOW,
        actions: [
          'kms:Decrypt',
          'kms:DescribeKey',
          'kms:Encrypt',
          'kms:ReEncrypt*',
          'kms:GenerateDataKey*',
        ],
        resources: [props.dataKey.keyArn],
      }),
    );

    // ── Business Lambda (layers 2 + 3 live inside it) ───────────────────
    this.lambdaFunction = new lambda.Function(this, 'WebhookFunction', {
      functionName: FUNCTION_NAME,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      // Self-contained bundle, NOT the shared vip_shared layer: a bug shipped
      // in shared code must not be able to take down the only inbound path
      // for after-hours callbacks. Bundling also pins a boto3 new enough for
      // start_task_contact's ScheduledTime parameter.
      code: buildBundledPythonCode({
        assetRoot: path.join(__dirname, '../../../services/quadrivia-callback'),
        srcSubdir: 'src',
      }),
      role,
      logGroup,
      memorySize: 256,
      // Short on purpose: the only work is one HMAC comparison, one
      // conditional PutItem and one StartTaskContact. A live caller is
      // waiting, so failing fast beats hanging — and a long timeout would let
      // a flood of slow requests hold concurrency open.
      timeout: cdk.Duration.seconds(3),
      // Same value as ApiAuthorizerStack's front-door Lambda: enough headroom
      // that a burst cannot throttle legitimate callbacks, low enough that a
      // misbehaving caller cannot consume the account's whole concurrency
      // pool and starve the dialer Lambdas.
      reservedConcurrentExecutions: 100,
      environmentEncryption: props.dataKey,
      environment: {
        // Derived from the ARN rather than taken as a second, independently
        // settable prop — that way the IAM scope above and the instance the
        // Lambda actually calls can never drift apart.
        CONNECT_INSTANCE_ID: connectInstanceId,
        TASK_TEMPLATE_ID: props.taskTemplateId,
        IDEMPOTENCY_TABLE: this.idempotencyTable.tableName,
        HMAC_SECRET_ARN: this.hmacSecret.secretArn,
        LOG_LEVEL: 'INFO',
        POWERTOOLS_SERVICE_NAME: APPLICATION,
      },
    });
    skipCheckovChecks(this.lambdaFunction, [VPC_SKIP, DLQ_SKIP]);

    // ── Layer 1: dedicated HTTP API behind an mTLS custom domain ─────────
    const mtls = props.mtlsDomain;
    if (mtls) {
      this.domainName = new apigatewayv2.DomainName(this, 'WebhookDomain', {
        domainName: mtls.domainName,
        certificate: certificatemanager.Certificate.fromCertificateArn(
          this,
          'WebhookCertificate',
          mtls.certificateArn,
        ),
        ownershipCertificate: mtls.ownershipCertificateArn
          ? certificatemanager.Certificate.fromCertificateArn(
              this,
              'WebhookOwnershipCertificate',
              mtls.ownershipCertificateArn,
            )
          : undefined,
        securityPolicy: apigatewayv2.SecurityPolicy.TLS_1_2,
        mtls: {
          bucket: s3.Bucket.fromBucketName(
            this,
            'TruststoreBucket',
            mtls.truststoreBucketName,
          ),
          key: mtls.truststoreKey,
          version: mtls.truststoreVersion,
        },
      });
    }

    this.httpApi = new apigatewayv2.HttpApi(this, 'QuadriviaWebhookApi', {
      apiName: 'vip-quadrivia-callback-webhook',
      description: 'mTLS + HMAC webhook that schedules after-hours callback tasks in Connect',
      // No CORS block at all: this is server-to-server traffic. A CORS policy
      // here would only advertise the endpoint to browsers that have no
      // business calling it.
      //
      // Critical for layer 1: the default execute-api endpoint does NOT
      // enforce mTLS, so leaving it enabled would be a bypass of the whole
      // transport layer. With it disabled and no domain configured yet the API
      // is intentionally unreachable (fail-closed) rather than reachable
      // without a client certificate.
      disableExecuteApiEndpoint: true,
      defaultDomainMapping: this.domainName
        ? { domainName: this.domainName }
        : undefined,
    });

    this.httpApi.addRoutes({
      path: WEBHOOK_PATH,
      methods: [apigatewayv2.HttpMethod.POST],
      integration: new integrations.HttpLambdaIntegration(
        'WebhookIntegration',
        this.lambdaFunction,
      ),
      // No `authorizer`: authentication is mTLS (transport) + the inline HMAC
      // check in the handler. See the class docstring for why a second Lambda
      // authorizer was rejected.
    });

    // ── Access logging ──────────────────────────────────────────────────
    const accessLogGroup = new logs.LogGroup(this, 'AccessLogs', {
      logGroupName: `/aws/apigateway/${FUNCTION_NAME}-access`,
      retention: logs.RetentionDays.ONE_YEAR,
      encryptionKey: props.dataKey,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    // Same scoped grant pattern as ApiStack: API Gateway's log-delivery
    // principal needs explicit CMK access, narrowed to this account and this
    // log group rather than every stage in the account.
    props.dataKey.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: 'AllowQuadriviaApiGatewayLogDelivery',
        effect: iam.Effect.ALLOW,
        principals: [new iam.ServicePrincipal('apigateway.amazonaws.com')],
        actions: [
          'kms:Encrypt*',
          'kms:Decrypt*',
          'kms:ReEncrypt*',
          'kms:GenerateDataKey*',
          'kms:Describe*',
        ],
        resources: ['*'],
        conditions: {
          StringEquals: { 'aws:SourceAccount': this.account },
          ArnLike: {
            'aws:SourceArn': `arn:aws:logs:${this.region}:${this.account}:log-group:/aws/apigateway/${FUNCTION_NAME}-access:*`,
          },
        },
      }),
    );
    const defaultStage = this.httpApi.defaultStage!.node
      .defaultChild as apigatewayv2.CfnStage;
    // Gateway-level throttle as defense-in-depth on top of layers 1-3:
    // reservedConcurrentExecutions bounds the blast radius to the rest of
    // the account but does not itself rate-limit this endpoint. After-hours
    // callback volume is a handful of requests a night, so this has ample
    // headroom for legitimate traffic while still capping a client whose
    // HMAC key leaked or that is simply misbehaving.
    defaultStage.defaultRouteSettings = {
      throttlingRateLimit: 5,
      throttlingBurstLimit: 10,
    };
    defaultStage.accessLogSettings = {
      destinationArn: accessLogGroup.logGroupArn,
      // Request metadata only — no body, no headers. The body carries a
      // patient phone number and the headers carry the HMAC signature;
      // neither belongs in an access log.
      format: JSON.stringify({
        requestId: '$context.requestId',
        ip: '$context.identity.sourceIp',
        requestTime: '$context.requestTime',
        httpMethod: '$context.httpMethod',
        routeKey: '$context.routeKey',
        status: '$context.status',
        integrationErrorMessage: '$context.integrationErrorMessage',
        responseLatency: '$context.responseLatency',
        clientCertSubjectDN: '$context.identity.clientCert.subjectDN',
        clientCertSerial: '$context.identity.clientCert.serialNumber',
      }),
    };

    // ── SCP-mandated tagging (org policy, accounts incl. 165505826690) ───
    // Applied at stack scope with an explicit priority above the default so
    // these deliberately win over any broader app-level Tags.of(app) defaults
    // (infra/bin/app.ts sets a different, non-SCP tag set including
    // Owner=devaju, which is not the required email form).
    const SCP_TAG_PRIORITY = 200;
    const scpTags: Record<string, string> = {
      // Standard, not Clinical: nothing clinical is persisted by this stack.
      // The only stored data is an idempotency marker (requestId + contactId)
      // that self-deletes after 1 hour, and the whole thing is reconstructible
      // from code. The patient phone number transits this Lambda into Connect
      // — Connect is the system of record for the task and carries the
      // Clinical tier. A 7-year immutable second-region copy of one-hour
      // dedupe markers would be retention for its own sake.
      'Backup-Tier': 'Standard',
      Environment: 'prod',
      Owner: props.ownerEmail,
      Application: APPLICATION,
      // phi, despite nothing clinical being *persisted*: the request payload
      // carries a patient phone number, which is PHI in transit through this
      // Lambda and briefly present in the idempotency record's key space.
      DataClassification: 'phi',
      Team: props.team,
    };
    for (const [key, value] of Object.entries(scpTags)) {
      cdk.Tags.of(this).add(key, value, { priority: SCP_TAG_PRIORITY });
    }

    // ── Outputs ─────────────────────────────────────────────────────────
    new cdk.CfnOutput(this, 'HttpApiId', { value: this.httpApi.apiId });
    new cdk.CfnOutput(this, 'WebhookFunctionArn', {
      value: this.lambdaFunction.functionArn,
    });
    new cdk.CfnOutput(this, 'IdempotencyTableName', {
      value: this.idempotencyTable.tableName,
    });
    new cdk.CfnOutput(this, 'HmacSecretArn', { value: this.hmacSecret.secretArn });
    if (this.domainName) {
      // The two values needed to create the DNS ALIAS record in whichever
      // hosted zone is eventually chosen (see the mtlsDomain prop comment).
      new cdk.CfnOutput(this, 'RegionalDomainName', {
        value: this.domainName.regionalDomainName,
      });
      new cdk.CfnOutput(this, 'RegionalHostedZoneId', {
        value: this.domainName.regionalHostedZoneId,
      });
      new cdk.CfnOutput(this, 'WebhookUrl', {
        value: `https://${this.domainName.name}${WEBHOOK_PATH}`,
      });
    }
  }
}
