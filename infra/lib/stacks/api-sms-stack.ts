import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import { SqsEventSource } from 'aws-cdk-lib/aws-lambda-event-sources';
import * as path from 'path';
import { buildSharedLayer } from '../utils/shared-layer';
import { skipCheckovChecks } from '../utils/checkov-skip';

const VPC_SKIP = {
  id: 'CKV_AWS_117',
  comment:
    'Not internet-reachable regardless of VPC config (invoked only via SQS event ' +
    'source or programmatic InvokeFunction, never a direct target). Talks only to ' +
    'AWS public APIs (DynamoDB, EUM SMS, Customer Profiles) already secured by ' +
    'TLS+IAM, not to any private-VPC-only resource.',
};

export interface ApiSmsStackProps extends cdk.StackProps {
  /** KMS CMK ARN — passed as string to avoid cross-stack Fn::ImportValue dependency */
  readonly dataKeyArn: string;
  /** Customer Profiles domain name */
  readonly profilesDomainName: string;
  /** Existing encrypted Customer Profiles snapshot infrastructure. */
  readonly snapshotBucketName: string;
  readonly snapshotRoleArn: string;
  readonly snapshotKeyArn: string;
  /** EUM SMS Config Set name (created via CLI) */
  readonly smsConfigSetName: string;
  /** EUM SMS Opt-Out List name (created via CLI) */
  readonly smsOptOutListName: string;
  readonly permissionsBoundaryName?: string;
}

export class ApiSmsStack extends cdk.Stack {
  public readonly smsSenderFunction: lambda.Function;
  public readonly smsRetryQuietHoursFunction: lambda.Function;
  public readonly smsProcessorFunction: lambda.Function;
  public readonly smsCampaignQueueTable: dynamodb.ITable;
  public readonly smsRunsTable: dynamodb.ITable;
  public readonly smsSendQueue: sqs.IQueue;

  constructor(scope: Construct, id: string, props: ApiSmsStackProps) {
    super(scope, id, props);

    if (props.permissionsBoundaryName) {
      const boundary = iam.ManagedPolicy.fromManagedPolicyName(
        this,
        'PermissionsBoundary',
        props.permissionsBoundaryName,
      );
      iam.PermissionsBoundary.of(this).apply(boundary);
    }

    // ── DynamoDB: SMS Campaign Queue ──────────────────────────────────
    // imported — cfn-exec-role lacks kms:Decrypt on this CMK; table pre-created via CLI.
    // Schema: PK=campaignId(S), SK=sk(S), PAY_PER_REQUEST, KMS CMK, PITR enabled, TTL=ttl.
    this.smsCampaignQueueTable = dynamodb.Table.fromTableArn(
      this, 'SmsCampaignQueueTable',
      `arn:aws:dynamodb:${this.region}:${this.account}:table/VipSmsCampaignQueue`,
    );

    // ── DynamoDB: SMS Campaign Runs ───────────────────────────────────
    // imported — cfn-exec-role lacks kms:Decrypt on this CMK; table pre-created via CLI.
    // Schema: PK=planId(S), SK=runId#smsCampaignId(S), PAY_PER_REQUEST, KMS CMK, no TTL.
    this.smsRunsTable = dynamodb.Table.fromTableArn(
      this, 'SmsRunsTable',
      `arn:aws:dynamodb:${this.region}:${this.account}:table/VipSmsCampaignRuns`,
    );

    // ── SQS: SMS send queue (imported — pre-created via CLI) ──────────
    // Create via CLI before deploying (run these commands in order):
    //
    // 1. Get the CMK ARN (same key used for VipSmsCampaignQueue / VipSmsCampaignRuns):
    //   CMK_ARN=$(aws kms describe-key --key-id alias/vip-data-key \
    //     --query 'KeyMetadata.Arn' --output text --region us-east-1 --profile production)
    //
    // 2. Create DLQ with KMS encryption (B4: PHI phone numbers in SQS bodies must be encrypted):
    //   aws sqs create-queue --queue-name vip-sms-campaign-queue-dlq \
    //     --attributes "{\"MessageRetentionPeriod\":\"1209600\",\"KmsMasterKeyId\":\"$CMK_ARN\"}" \
    //     --region us-east-1 --profile production
    //
    //   DLQ_ARN=$(aws sqs get-queue-attributes \
    //     --queue-url $(aws sqs get-queue-url --queue-name vip-sms-campaign-queue-dlq \
    //       --query QueueUrl --output text --region us-east-1 --profile production) \
    //     --attribute-names QueueArn --query 'Attributes.QueueArn' --output text \
    //     --region us-east-1 --profile production)
    //
    // 3. Create main queue:
    //   - VisibilityTimeout=180 (B5: must be ≥ 6× Lambda timeout of 30s = 180s)
    //   - KmsMasterKeyId set (B4: PHI phones in SQS body must be KMS-encrypted)
    //   aws sqs create-queue --queue-name vip-sms-campaign-queue \
    //     --attributes "{\"VisibilityTimeout\":\"180\",\"KmsMasterKeyId\":\"$CMK_ARN\",\
    //       \"RedrivePolicy\":\"{\\\"deadLetterTargetArn\\\":\\\"$DLQ_ARN\\\",\\\"maxReceiveCount\\\":\\\"3\\\"}\"}" \
    //     --region us-east-1 --profile production
    //
    // 4. After creating, set VisibilityTimeout if queue already exists:
    //   aws sqs set-queue-attributes \
    //     --queue-url $(aws sqs get-queue-url --queue-name vip-sms-campaign-queue \
    //       --query QueueUrl --output text --region us-east-1 --profile production) \
    //     --attributes '{"VisibilityTimeout":"180"}' \
    //     --region us-east-1 --profile production
    this.smsSendQueue = sqs.Queue.fromQueueAttributes(this, 'SmsSendQueue', {
      queueArn: `arn:aws:sqs:${this.region}:${this.account}:vip-sms-campaign-queue`,
      queueUrl: `https://sqs.${this.region}.amazonaws.com/${this.account}/vip-sms-campaign-queue`,
      keyArn: props.dataKeyArn,
    });

    // This stack's Lambda needs `phonenumbers` (TCPA quiet-hours resolution)
    // which no other stack uses — pass the SMS-specific superset file so
    // only this stack's layer copy carries the extra ~48 MB.
    const sharedLayer = buildSharedLayer(this, 'SharedLayer', 'requirements-sms.txt');
    // Retain prior versions so an interrupted rollout can restore its reviewed
    // handler/layer pairing without depending on a deleted Lambda layer.
    sharedLayer.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);
    const dataKey = kms.Key.fromKeyArn(this, 'DataKey', props.dataKeyArn);
    const snapshotEnvironment = {
      SMS_SNAPSHOT_BUCKET: props.snapshotBucketName,
      SMS_SNAPSHOT_ROLE_ARN: props.snapshotRoleArn,
      SMS_SNAPSHOT_KEY_ARN: props.snapshotKeyArn,
    };

    // Both sender and processor roles below are imported with mutable:false —
    // every grant CDK would normally add for environmentEncryption /
    // deadLetterQueue (kms:Decrypt on dataKey, sqs:SendMessage on this DLQ)
    // silently no-ops (confirmed via `cdk synth`, same as LocationOnboardingGuard
    // in api-plans-stack.ts) and must be pre-attached via the existing CLI
    // policy-file flow documented above (SmsSenderPerms / SmsProcessorPerms):
    //   kms:Decrypt on <dataKeyArn>
    //   sqs:SendMessage on <DeadLetterQueue arn from `cdk synth` output>
    const dlq = new sqs.Queue(this, 'DeadLetterQueue', {
      queueName: 'vip-admin-sms-dlq',
      encryption: sqs.QueueEncryption.KMS,
      encryptionMasterKey: dataKey,
      retentionPeriod: cdk.Duration.days(14),
    });

    // ── Lambda: SMS Sender ────────────────────────────────────────────
    // imported — cfn-exec-role lacks logs:DescribeIndexPolicies; log group pre-created via CLI
    const senderLogGroup = logs.LogGroup.fromLogGroupName(
      this, 'SmsSenderLogs', '/aws/lambda/vip-admin-sms-sender',
    );

    // imported — cfn-exec-role lacks iam:CreateRole + iam:GetRolePolicy; role pre-created via CLI.
    // mutable:false — all permissions pre-attached via:
    //   aws iam put-role-policy --role-name vip-sms-sender-role \
    //     --policy-name SmsSenderPerms --policy-document file:///<policy-file>.json
    // New campaign-v1 sends also validate the exact origination ARN through EUM.
    // Before deploying that guard, attach the separate read-only policy from
    // infra/config/sms-campaign-origination-read-policy.json as
    // SmsCampaignOriginationRead. Keep the existing policies and boundary;
    // this immutable imported role does not receive grants from CDK.
    const senderRole = iam.Role.fromRoleArn(
      this, 'SmsSenderRole',
      `arn:aws:iam::${this.account}:role/vip-sms-sender-role`,
      { mutable: false },
    );

    this.smsSenderFunction = new lambda.Function(this, 'SmsSenderFunction', {
      functionName: 'vip-admin-sms-sender',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'sms_sender_handler.lambda_handler',
      code: lambda.Code.fromAsset(
        path.join(__dirname, '../../../services/api-sms/src'),
      ),
      layers: [sharedLayer],
      role: senderRole,
      logGroup: senderLogGroup,
      timeout: cdk.Duration.minutes(5),
      memorySize: 512,
      environmentEncryption: dataKey,
      deadLetterQueue: dlq,
      // CloudWatch, 2026-09-09 (90d window): only 1 invocation total, max observed
      // ConcurrentExecutions = 1, 0 throttles. Initialization now also polls
      // pending snapshots and reads Customer Profiles; those APIs can throttle.
      // Keep the existing cap of 5. Delivery fans out through SQS to the
      // processor, whose concurrency cap is 10.
      reservedConcurrentExecutions: 5,
      environment: {
        SMS_CAMPAIGN_QUEUE_TABLE: this.smsCampaignQueueTable.tableName,
        SMS_CAMPAIGN_RUNS_TABLE: this.smsRunsTable.tableName,
        SMS_SQS_QUEUE_URL: this.smsSendQueue.queueUrl,
        PROFILES_DOMAIN_NAME: props.profilesDomainName,
        OPT_OUT_TABLE: 'VipConnectOptOutList',
        ...snapshotEnvironment,
        // Per-recipient TCPA window: full statutory hours (08:00-21:00 local),
        // stricter than statute on days (Mon-Sat, no Sunday — a VIP business
        // choice). Env vars so counsel can narrow either axis without a deploy
        // of new code. QUIET_HOURS_DAYS is Python weekday(): Monday=0..Sunday=6.
        QUIET_HOURS_START: '08:00',
        QUIET_HOURS_END: '21:00',
        QUIET_HOURS_DAYS: '0,1,2,3,4,5',
        QUIET_HOURS_DEFAULT_TZ: 'America/New_York',
      },
    });
    skipCheckovChecks(this.smsSenderFunction, [VPC_SKIP]);

    // ── Lambda: SMS Retry (quiet-hours) ────────────────────────────────
    // Closes a 2026-09 adversarial-review finding: the pre-call SMS
    // quiet-hours check in sms_sender_handler.py runs once, at bucket
    // activation, while Connect Campaigns V2's own localTimeZoneDetection=
    // AREA_CODE + openHours check is re-evaluated continuously by Connect's
    // campaign engine for as long as the voice campaign runs — a recipient
    // outside their window at activation could get dialed hours later having
    // never received the text. This function re-attempts those sends,
    // invoked repeatedly from api-plans's tick() poll loop for as long as the
    // paired voice campaign stays "running" (see executor.py:
    // _invoke_sms_retry_quiet_hours).
    //
    // A second Function construct — not an `action` field dispatched inside
    // the existing, already-working lambda_handler — reusing the SAME code
    // asset (`sms_sender_handler.retry_quiet_hours_skipped`, no new
    // deployment package), the SAME layer, and the SAME senderRole (imported
    // above, mutable:false). Both entry points use the same recipient loader
    // and queue processing code. The imported role needs the permissions in
    // infra/config/precall-sms-sender-policy.json before deployment. In
    // particular, retries require Query/DeleteItem and their own log group;
    // membership resolution also needs profile reads and snapshot access.
    //
    // imported — same cfn-exec-role limitation as senderLogGroup above.
    // Pre-create before deploying this stack:
    //   CMK_ARN=$(aws kms describe-key --key-id alias/vip-data-key \
    //     --query 'KeyMetadata.Arn' --output text --region us-east-1 --profile production)
    //   aws logs create-log-group --log-group-name /aws/lambda/vip-admin-sms-retry-quiet-hours \
    //     --kms-key-id "$CMK_ARN" --region us-east-1 --profile production
    //   aws logs put-retention-policy --log-group-name /aws/lambda/vip-admin-sms-retry-quiet-hours \
    //     --retention-in-days 365 --region us-east-1 --profile production
    const retryLogGroup = logs.LogGroup.fromLogGroupName(
      this, 'SmsRetryQuietHoursLogs', '/aws/lambda/vip-admin-sms-retry-quiet-hours',
    );

    this.smsRetryQuietHoursFunction = new lambda.Function(this, 'SmsRetryQuietHoursFunction', {
      functionName: 'vip-admin-sms-retry-quiet-hours',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'sms_sender_handler.retry_quiet_hours_skipped',
      // Same source directory as SmsSenderFunction above (and SmsProcessorFunction
      // below, which already follows this same pattern) — CDK asset-hashes by
      // content, so this is the same underlying asset, not a new deployment
      // package.
      code: lambda.Code.fromAsset(
        path.join(__dirname, '../../../services/api-sms/src'),
      ),
      layers: [sharedLayer],
      role: senderRole,
      logGroup: retryLogGroup,
      timeout: cdk.Duration.minutes(5),
      memorySize: 512,
      environmentEncryption: dataKey,
      deadLetterQueue: dlq,
      // Invoked once per precall-SMS-enabled, still-"running" campaign, per
      // tick. Active runs re-check unresolved recipients and the delivery
      // ledger; a zero quiet-hours count alone cannot prove completion.
      reservedConcurrentExecutions: 5,
      environment: {
        SMS_CAMPAIGN_QUEUE_TABLE: this.smsCampaignQueueTable.tableName,
        SMS_CAMPAIGN_RUNS_TABLE: this.smsRunsTable.tableName,
        SMS_SQS_QUEUE_URL: this.smsSendQueue.queueUrl,
        PROFILES_DOMAIN_NAME: props.profilesDomainName,
        OPT_OUT_TABLE: 'VipConnectOptOutList',
        ...snapshotEnvironment,
        // Must match SmsSenderFunction's values exactly — this is a second
        // entry point into the same quiet-hours logic, not a second policy.
        QUIET_HOURS_START: '08:00',
        QUIET_HOURS_END: '21:00',
        QUIET_HOURS_DAYS: '0,1,2,3,4,5',
        QUIET_HOURS_DEFAULT_TZ: 'America/New_York',
      },
    });
    skipCheckovChecks(this.smsRetryQuietHoursFunction, [VPC_SKIP]);

    // ── Lambda: SMS Processor ─────────────────────────────────────────
    // imported — cfn-exec-role lacks logs:DescribeIndexPolicies; log group pre-created via CLI
    const processorLogGroup = logs.LogGroup.fromLogGroupName(
      this, 'SmsProcessorLogs', '/aws/lambda/vip-admin-sms-processor',
    );

    // imported — cfn-exec-role lacks iam:CreateRole + iam:GetRolePolicy; role pre-created via CLI.
    // mutable:false — all permissions pre-attached via:
    //   aws iam put-role-policy --role-name vip-sms-processor-role \
    //     --policy-name SmsProcessorPerms --policy-document file:///<policy-file>.json
    const processorRole = iam.Role.fromRoleArn(
      this, 'SmsProcessorRole',
      `arn:aws:iam::${this.account}:role/vip-sms-processor-role`,
      { mutable: false },
    );

    this.smsProcessorFunction = new lambda.Function(this, 'SmsProcessorFunction', {
      functionName: 'vip-admin-sms-processor',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'sms_processor_handler.lambda_handler',
      code: lambda.Code.fromAsset(
        path.join(__dirname, '../../../services/api-sms/src'),
      ),
      // VIP-02: sms_processor_handler.py now imports
      // vip_shared.infrastructure.persistence.opt_out for the pre-send opt-out
      // recheck. Without this layer attached, that import raises
      // ModuleNotFoundError on every cold start — a 100% outage, not a
      // graceful degradation. This function previously had no vip_shared
      // dependency at all.
      layers: [sharedLayer],
      role: processorRole,
      logGroup: processorLogGroup,
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      // reservedConcurrentExecutions controls SMS send rate (= MPS cap).
      // 10 = safe default for 10DLC pools. Adjust per origination number type:
      //   TOLL_FREE: 3 | TEN_DLC: 10–100 | SHORT_CODE: up to 100
      reservedConcurrentExecutions: 10,
      environmentEncryption: dataKey,
      deadLetterQueue: dlq,
      environment: {
        SMS_CAMPAIGN_QUEUE_TABLE: this.smsCampaignQueueTable.tableName,
        SMS_CAMPAIGN_RUNS_TABLE: this.smsRunsTable.tableName,
        SMS_CONFIG_SET_NAME: props.smsConfigSetName,
        SMS_OPT_OUT_LIST_NAME: props.smsOptOutListName,
        // VIP-02: final strongly-consistent opt-out recheck immediately before
        // send (catches a STOP recorded between enqueue and send). Same shared
        // cross-channel table the sender already checks at enqueue time.
        //
        // IMPORTANT — this role is imported with mutable:false (see the
        // SmsProcessorPerms note above): CDK will NOT grant dynamodb:GetItem
        // on this table. Before deploying this change, add it to the
        // vip-sms-processor-role policy via the same CLI put-role-policy flow
        // used for SmsProcessorPerms, or every send will fail closed with
        // AccessDeniedException treated as a retryable error.
        OPT_OUT_TABLE: 'VipConnectOptOutList',
      },
    });
    skipCheckovChecks(this.smsProcessorFunction, [VPC_SKIP]);

    // SQS trigger — one message per invocation
    this.smsProcessorFunction.addEventSource(new SqsEventSource(this.smsSendQueue, {
      batchSize: 1,
    }));

    // All grants pre-attached via CLI (mutable:false — CDK skips IAM policy generation).

    // ── Outputs ───────────────────────────────────────────────────────
    new cdk.CfnOutput(this, 'SmsSenderFunctionArn', { value: this.smsSenderFunction.functionArn });
    new cdk.CfnOutput(this, 'SmsRetryQuietHoursFunctionArn', { value: this.smsRetryQuietHoursFunction.functionArn });
    new cdk.CfnOutput(this, 'SmsProcessorFunctionArn', { value: this.smsProcessorFunction.functionArn });
    new cdk.CfnOutput(this, 'SmsCampaignQueueTableName', { value: this.smsCampaignQueueTable.tableName });
    new cdk.CfnOutput(this, 'SmsRunsTableName', { value: this.smsRunsTable.tableName });
    new cdk.CfnOutput(this, 'SmsSendQueueUrl', { value: this.smsSendQueue.queueUrl });
  }
}
