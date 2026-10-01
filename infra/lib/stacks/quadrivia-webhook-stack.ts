import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as apigatewayv2 from 'aws-cdk-lib/aws-apigatewayv2';
import * as integrations from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as path from 'path';
import { buildBundledPythonCode } from '../utils/python-bundling';
import { skipCheckovChecks } from '../utils/checkov-skip';

const APPLICATION = 'quadrivia-afterhours-callback';
const FUNCTION_NAME = 'vip-quadrivia-callback';
const TABLE_NAME = 'VipQuadriviaCallbackIdempotency';
// vip/quadrivia/webhook-hmac — now imported by ARN below (HmacSecret), not
// created by name; kept here only as a comment since the name itself is no
// longer referenced in code.
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

/**
 * Real facts about quadrivia-webhook.medwork.io, confirmed live via
 * `aws apigatewayv2 get-domain-name --domain-name quadrivia-webhook.medwork.io
 * --profile production` on 2026-09-28. This DomainName resource is NOT
 * created or owned by this stack (or any CDK app) — IT (Raymond) created it
 * directly against the VIP-Techsupport account, tagged `ManagedBy: manual`,
 * with a direct DNS record in the main medwork.io zone (same pattern as
 * unifi-alerts.medwork.io — no subzone delegation was needed after all).
 * Re-run the command above if these ever need re-verifying; do not guess
 * new values if the domain is ever recreated.
 */
const EXISTING_DOMAIN_NAME = 'quadrivia-webhook.medwork.io';
const EXISTING_DOMAIN_REGIONAL_NAME = 'd-xo30rcmxk8.execute-api.us-east-1.amazonaws.com';
const EXISTING_DOMAIN_HOSTED_ZONE_ID = 'Z1UJRXOUMOOFQ8';

/**
 * Wiring for the ALREADY-EXISTING quadrivia-webhook.medwork.io custom
 * domain. See the comment on `existingDomain` below for the mTLS
 * activation sequencing this depends on.
 */
export interface QuadriviaExistingDomainProps {
  /**
   * Bucket holding the PEM truststore of the CA that signs Quadrivia's client
   * certificate. Imported by name — the truststore is a security artifact
   * whose upload/rotation is an operator action, not a CDK asset. Needed to
   * run the manual mTLS-activation command documented at the mapping site
   * below, NOT used to configure the DomainName from this stack (this stack
   * does not own that resource — see EXISTING_DOMAIN_NAME above).
   */
  readonly truststoreBucketName: string;
  /** Key of the truststore object, e.g. `quadrivia/truststore.pem`. */
  readonly truststoreKey: string;
  /**
   * S3 object version of the truststore. Strongly recommended: pinning a
   * version makes a CA rotation an explicit, reviewable change instead of
   * an out-of-band bucket write that silently changes who can call in.
   */
  readonly truststoreVersion?: string;
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
  /**
   * The dedicated Connect flow the scheduled callback task runs. Deliberately
   * NOT a Task Template ID — publishing the first-ever Task Template on this
   * Connect instance forces every agent to pick a template for every
   * manually-created task from then on, an instance-wide behavior change
   * decided against on 2026-09-29. This flow is expected to be a plain
   * attribute-based router: read `is_billing_question` (-> PST queue) and
   * `patient_status` (`existing` -> existing-patient voicemail queue,
   * anything else -> agents/New Lead voicemail queue) and transfer
   * accordingly. It does NOT invoke SOPS-ConnectPatientLookup itself — see
   * `patientLookupFunctionArn` below for why that lookup happens in this
   * Lambda instead.
   */
  readonly contactFlowId: string;
  /**
   * ARN of the existing `SOPS-ConnectPatientLookup` Lambda that
   * *MainInboundVoice already uses to classify existing-patient vs
   * new-lead. Invoked directly from this Lambda (a plain lambda:Invoke,
   * not via a Connect flow) with a synthetic event shaped like a voice
   * contact's, because that Lambda reads the caller's number from
   * `Details.ContactData.CustomerEndpoint.Address` — a field
   * StartTaskContact has no way to populate for a Task contact (it isn't a
   * StartTaskContact parameter at all). A Connect flow invoking it directly
   * for our task would silently always get a missing-number "error"
   * response, which *MainInboundVoice's own closed-hours branch defaults to
   * the New Lead queue — every Quadrivia callback would be misclassified
   * as New Lead with no visible error. See handler.py's
   * `_lookup_patient_status` for the full reasoning.
   */
  readonly patientLookupFunctionArn: string;
  /**
   * The exact `subjectDN` Quadrivia's mTLS client certificate must present,
   * e.g. `CN=quadrivia-afterhours,OU=Integrations,O=Quadrivia,C=US`.
   * Quadrivia confirmed (2026-09-30) they operate a private CA used only
   * for this integration and will state the subject when they send the CA
   * certificate for the truststore. Required, not optional: the truststore
   * alone trusts any certificate that CA ever issues, not just theirs —
   * see handler.py's `_verify_client_certificate_subject` for the full
   * reasoning. Do not invent a placeholder value; leave this stack
   * unwired (as it already is, pending contactFlowId) until the real
   * subject is known.
   */
  readonly clientCertSubjectDn: string;
  /** Owner tag — email of the accountable engineer (SCP-mandated). */
  readonly ownerEmail: string;
  /** Team tag — one of `specialOps` | `engineering` | `medwork-devs`. */
  readonly team: string;
  /**
   * Connects this stack's HttpApi to the existing quadrivia-webhook.medwork.io
   * custom domain (see EXISTING_DOMAIN_NAME above — domain + cert + DNS
   * record are already live, created by IT on 2026-09-28). When omitted the
   * API has no domain mapping and `disableExecuteApiEndpoint: true`, i.e. it
   * is reachable from nowhere — fail-closed, on purpose.
   *
   * >>> DEPLOY-ORDER WARNING — read before setting this prop: <<<
   * This stack does NOT own the DomainName resource and therefore CANNOT
   * configure its mTLS truststore via CloudFormation — that is a manual,
   * out-of-band step against a resource IT created directly (tagged
   * `ManagedBy: manual`). If this prop is supplied and deployed BEFORE mTLS
   * is actually active on the domain, the webhook becomes reachable over
   * plain TLS with no client-certificate check at all — a real bypass of
   * layer 1, not a theoretical one. Sequence must be:
   *   1. Upload Quadrivia's client cert/CA to the truststore bucket+key
   *      given below.
   *   2. Run (once, manually, after step 1):
   *        aws apigatewayv2 update-domain-name \
   *          --domain-name quadrivia-webhook.medwork.io \
   *          --domain-name-configurations '[{"ApiGatewayDomainName":"'"$EXISTING_DOMAIN_REGIONAL_NAME"'","CertificateArn":"<existing cert arn — do not change>","EndpointType":"REGIONAL","SecurityPolicy":"TLS_1_2"}]' \
   *          --mutual-tls-authentication TruststoreUri=s3://<bucket>/<key>,TruststoreVersion=<version>
   *      (confirm with `aws apigatewayv2 get-domain-name` that
   *      MutualTlsAuthentication is present before proceeding)
   *   3. Only then deploy this stack with `existingDomain` set.
   */
  readonly existingDomain?: QuadriviaExistingDomainProps;
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
 *   1. mTLS on a custom domain (transport) — `existingDomain` prop, wired
 *      to the already-existing quadrivia-webhook.medwork.io (IT-owned).
 *   2. HMAC-SHA256 over the raw body + a ±5min timestamp window, verified
 *      INLINE in the business Lambda. Not a separate Lambda authorizer on
 *      purpose: an extra Lambda in the synchronous path of a live call adds a
 *      second cold start's worth of latency while a patient waits on the line.
 *   3. `X-Request-Id` idempotency in DynamoDB with a 1-hour TTL.
 */
export class QuadriviaWebhookStack extends cdk.Stack {
  public readonly httpApi: apigatewayv2.HttpApi;
  public readonly lambdaFunction: lambda.Function;
  public readonly idempotencyTable: dynamodb.ITable;
  public readonly hmacSecret: secretsmanager.ISecret;
  // IDomainName, not the concrete DomainName class: this is always an
  // imported reference (fromDomainNameAttributes) to a resource this stack
  // never creates — see EXISTING_DOMAIN_NAME above.
  public readonly domainName?: apigatewayv2.IDomainName;

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
    // IMPORTED, not created: the 2026-10-01 deploy attempt that first got
    // past the PassRole gap created this table fully (CREATE_COMPLETE: on
    // demand, requestId hash key, CMK-encrypted, TTL on `ttl`, PITR enabled)
    // before a later resource failed and rolled the stack back.
    // RemovalPolicy.RETAIN left the real, correctly-configured table in
    // place (DELETE_SKIPPED) — confirmed live via `aws dynamodb
    // describe-table` / `describe-time-to-live` / `describe-continuous-backups`
    // on 2026-10-01, all matching this stack's intent exactly. A second
    // `new dynamodb.Table` with the same name fails changeset validation
    // with "already exists".
    this.idempotencyTable = dynamodb.Table.fromTableAttributes(this, 'IdempotencyTable', {
      tableArn: `arn:aws:dynamodb:${this.region}:${this.account}:table/${TABLE_NAME}`,
      encryptionKey: props.dataKey,
    });

    // ── Layer 2: HMAC signing key ───────────────────────────────────────
    // IMPORTED, not created: the 2026-10-01 dry-run deploy got far enough to
    // fully create this secret (CREATE_COMPLETE) before a later resource
    // (FunctionRole, see below) hit the EngineeringPermissionBoundary deny
    // and rolled the stack back. RemovalPolicy.RETAIN meant CloudFormation
    // left the physical secret in place (DELETE_SKIPPED) instead of deleting
    // it, so a real random signing key already exists under this name — a
    // second `new secretsmanager.Secret` with the same `secretName` would
    // fail with ResourceExistsException on the next deploy. Importing by its
    // exact ARN adopts that already-generated key rather than discarding and
    // regenerating it. Confirmed live via `aws secretsmanager describe-secret
    // --secret-id vip/quadrivia/webhook-hmac --profile production` on
    // 2026-10-01 — re-run that command if this secret is ever recreated and
    // update the ARN below.
    this.hmacSecret = secretsmanager.Secret.fromSecretCompleteArn(
      this,
      'HmacSecret',
      'arn:aws:secretsmanager:us-east-1:165505826690:secret:vip/quadrivia/webhook-hmac-C2Sw6c',
    );

    // IMPORTED, not created: a prior rolled-back deploy attempt's
    // CreateLogGroup call physically succeeded against CloudWatch Logs
    // before CloudFormation's own bookkeeping marked the resource
    // "cancelled" during rollback — a known CFN race, not specific to this
    // stack. A fresh `new logs.LogGroup` with the same name fails with
    // AlreadyExists. Retention (365 days) was set to match this stack's
    // intended config via `aws logs put-retention-policy` on 2026-10-01
    // after confirming the group's real state with `describe-log-groups`.
    const logGroup = logs.LogGroup.fromLogGroupName(
      this,
      'FunctionLogs',
      `/aws/lambda/${FUNCTION_NAME}`,
    );

    // ── Least-privilege execution role ──────────────────────────────────
    // IMPORTED, not created: EngineeringPermissionBoundary denies
    // iam:CreateRole to this app's CFN exec role (reproduced live during the
    // 2026-10-01 dry-run deploy — HandlerErrorCode: UnauthorizedTaggingOperation
    // / explicit deny on iam:CreateRole), same pattern as api-authorizer-stack.ts
    // and api-sms-stack.ts. The role was created manually via the AWS console
    // on 2026-10-01 with the exact statements below mirrored into its inline
    // policy by hand — see the policy JSON kept alongside this stack's docs
    // for the source of truth. If this role is ever recreated, re-apply that
    // same policy; do not widen it.
    // mutable: false — nothing in this stack may call addToPrincipalPolicy /
    // grantXxx on this role. Those would synthesize their own
    // AWS::IAM::Policy against the CFN exec role, hitting the exact same
    // PutRolePolicy deny this import exists to avoid. All permissions this
    // Lambda needs already live in the role's manually-created inline policy.
    const role = iam.Role.fromRoleArn(
      this,
      'FunctionRole',
      'arn:aws:iam::165505826690:role/vip-quadrivia-callback-role',
      { mutable: false },
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
      // Short on purpose, but not as short as the original 3s: an HMAC
      // comparison, a conditional PutItem, and StartTaskContact are all
      // fast, but the SOPS-ConnectPatientLookup invoke (see
      // patientLookupFunctionArn) has its own 1s connect / 1.5s read
      // timeout in handler.py — worst case ~2.5s spent there alone before
      // it gives up and falls back to "error". 6s leaves real headroom
      // above that worst case instead of racing it. A live caller is still
      // waiting, so this stays well short of a "long" timeout, and a flood
      // of slow requests still can't hold concurrency open indefinitely.
      timeout: cdk.Duration.seconds(6),
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
        CONTACT_FLOW_ID: props.contactFlowId,
        PATIENT_LOOKUP_FUNCTION_ARN: props.patientLookupFunctionArn,
        QUADRIVIA_CLIENT_CERT_SUBJECT_DN: props.clientCertSubjectDn,
        IDEMPOTENCY_TABLE: this.idempotencyTable.tableName,
        HMAC_SECRET_ARN: this.hmacSecret.secretArn,
        LOG_LEVEL: 'INFO',
        POWERTOOLS_SERVICE_NAME: APPLICATION,
      },
    });
    skipCheckovChecks(this.lambdaFunction, [VPC_SKIP, DLQ_SKIP]);

    // ── Layer 1: dedicated HTTP API behind an mTLS custom domain ─────────
    // Imported, not created: quadrivia-webhook.medwork.io + its ACM cert +
    // DNS record already exist, owned by IT (see EXISTING_DOMAIN_NAME above).
    // `fromDomainNameAttributes` produces no AWS::ApiGatewayV2::DomainName in
    // this stack's template — CloudFormation never manages, and cannot
    // configure mTLS on, a resource it did not create. See the
    // DEPLOY-ORDER WARNING on `existingDomain` for why mTLS must already be
    // active before this is wired.
    const existingDomain = props.existingDomain;
    if (existingDomain) {
      this.domainName = apigatewayv2.DomainName.fromDomainNameAttributes(this, 'WebhookDomain', {
        name: EXISTING_DOMAIN_NAME,
        regionalDomainName: EXISTING_DOMAIN_REGIONAL_NAME,
        regionalHostedZoneId: EXISTING_DOMAIN_HOSTED_ZONE_ID,
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
    // IMPORTED, not created — same orphaned-by-a-rolled-back-deploy reason as
    // FunctionLogs above; see that comment.
    const accessLogGroup = logs.LogGroup.fromLogGroupName(
      this,
      'AccessLogs',
      `/aws/apigateway/${FUNCTION_NAME}-access`,
    );
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
      // DNS is already live (IT created the record directly in the main
      // medwork.io zone) — these are sanity-check outputs to confirm the
      // imported domain resolved to what EXISTING_DOMAIN_NAME above expects,
      // not something a deploy needs to act on.
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
