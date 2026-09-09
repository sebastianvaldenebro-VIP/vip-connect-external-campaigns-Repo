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
  /** EUM SMS Config Set name (created via CLI) */
  readonly smsConfigSetName: string;
  /** EUM SMS Opt-Out List name (created via CLI) */
  readonly smsOptOutListName: string;
  readonly permissionsBoundaryName?: string;
}

export class ApiSmsStack extends cdk.Stack {
  public readonly smsSenderFunction: lambda.Function;
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

    const sharedLayer = buildSharedLayer(this);
    const dataKey = kms.Key.fromKeyArn(this, 'DataKey', props.dataKeyArn);

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
      // ConcurrentExecutions = 1, 0 throttles — invoked once per SMS campaign run,
      // not per message (fans out via SQS to SmsProcessorFunction, which already
      // caps at 10). 5 is a generous margin given the near-zero real traffic and
      // the fact this function only enqueues, never calls a rate-limited API itself.
      reservedConcurrentExecutions: 5,
      environment: {
        SMS_CAMPAIGN_QUEUE_TABLE: this.smsCampaignQueueTable.tableName,
        SMS_CAMPAIGN_RUNS_TABLE: this.smsRunsTable.tableName,
        SMS_SQS_QUEUE_URL: this.smsSendQueue.queueUrl,
        PROFILES_DOMAIN_NAME: props.profilesDomainName,
      },
    });
    skipCheckovChecks(this.smsSenderFunction, [VPC_SKIP]);

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
    new cdk.CfnOutput(this, 'SmsProcessorFunctionArn', { value: this.smsProcessorFunction.functionArn });
    new cdk.CfnOutput(this, 'SmsCampaignQueueTableName', { value: this.smsCampaignQueueTable.tableName });
    new cdk.CfnOutput(this, 'SmsRunsTableName', { value: this.smsRunsTable.tableName });
    new cdk.CfnOutput(this, 'SmsSendQueueUrl', { value: this.smsSendQueue.queueUrl });
  }
}
