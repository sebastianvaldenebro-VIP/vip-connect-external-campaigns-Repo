import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cw_actions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as sns_subs from 'aws-cdk-lib/aws-sns-subscriptions';

export interface MonitoringStackProps extends cdk.StackProps {
  readonly alertEmail: string;
  readonly permissionsBoundaryName?: string;
}

// ─────────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────────

function lambdaMetric(fnName: string, metricName: string, period: cdk.Duration, statistic = 'Sum'): cloudwatch.Metric {
  return new cloudwatch.Metric({
    namespace: 'AWS/Lambda',
    metricName,
    dimensionsMap: { FunctionName: fnName },
    statistic,
    period,
  });
}

function ddbMetric(tableName: string, metricName: string, period: cdk.Duration): cloudwatch.Metric {
  return new cloudwatch.Metric({
    namespace: 'AWS/DynamoDB',
    metricName,
    dimensionsMap: { TableName: tableName },
    statistic: 'Sum',
    period,
  });
}

function ebMetric(ruleName: string, metricName: string, period: cdk.Duration): cloudwatch.Metric {
  return new cloudwatch.Metric({
    namespace: 'AWS/Events',
    metricName,
    dimensionsMap: { RuleName: ruleName },
    statistic: 'Sum',
    period,
  });
}

// ─────────────────────────────────────────────────────────────────────────────

export class MonitoringStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: MonitoringStackProps) {
    super(scope, id, props);

    if (props.permissionsBoundaryName) {
      const boundary = iam.ManagedPolicy.fromManagedPolicyName(
        this, 'PermissionsBoundary', props.permissionsBoundaryName,
      );
      iam.PermissionsBoundary.of(this).apply(boundary);
    }

    // ── KMS key for alerting SNS topic ───────────────────────────────
    // CloudWatch Alarms need GenerateDataKey + Decrypt to publish to an
    // encrypted SNS topic. Granting the CW service principal directly in
    // the key policy is the recommended approach.
    const alertKey = new kms.Key(this, 'AlertKey', {
      description: 'CMK for VIP Admin alerts SNS topic',
      enableKeyRotation: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      policy: new iam.PolicyDocument({
        statements: [
          // Root account full control
          new iam.PolicyStatement({
            sid: 'RootFullControl',
            principals: [new iam.AccountRootPrincipal()],
            actions: ['kms:*'],
            resources: ['*'],
          }),
          // CloudWatch Alarms need these to publish encrypted messages
          new iam.PolicyStatement({
            sid: 'AllowCloudWatchAlarms',
            principals: [new iam.ServicePrincipal('cloudwatch.amazonaws.com')],
            actions: ['kms:GenerateDataKey*', 'kms:Decrypt'],
            resources: ['*'],
          }),
          // SNS service needs Decrypt to deliver messages
          new iam.PolicyStatement({
            sid: 'AllowSNSDecrypt',
            principals: [new iam.ServicePrincipal('sns.amazonaws.com')],
            actions: ['kms:GenerateDataKey*', 'kms:Decrypt'],
            resources: ['*'],
          }),
        ],
      }),
    });

    // ── SNS alerting topic ───────────────────────────────────────────
    const alertTopic = new sns.Topic(this, 'AlertTopic', {
      topicName: 'vip-admin-alerts',
      displayName: 'VIP Admin UI — Operational Alerts',
      masterKey: alertKey,
    });

    alertTopic.addSubscription(new sns_subs.EmailSubscription(props.alertEmail));

    const alarmAction = new cw_actions.SnsAction(alertTopic);

    // ── Convenience: create an alarm and wire both alarm + ok actions ─
    const makeAlarm = (
      id: string,
      metric: cloudwatch.Metric,
      threshold: number,
      evaluationPeriods: number,
      description: string,
      opts?: Partial<cloudwatch.AlarmProps>,
    ): cloudwatch.Alarm => {
      const alarm = new cloudwatch.Alarm(this, id, {
        metric,
        threshold,
        evaluationPeriods,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        alarmDescription: description,
        actionsEnabled: true,
        ...opts,
      });
      alarm.addAlarmAction(alarmAction);
      alarm.addOkAction(alarmAction); // notify when alarm recovers
      return alarm;
    };

    const MIN1 = cdk.Duration.minutes(1);
    const MIN5 = cdk.Duration.minutes(5);

    // ── api-plans alarms (most critical — plan orchestration) ─────────
    makeAlarm(
      'PlansErrors',
      lambdaMetric('vip-admin-ui-api-plans', 'Errors', MIN1),
      0, 1,
      '[CRITICAL] api-plans: Lambda errors — plan tick or prestart_check failed',
    );
    makeAlarm(
      'PlansThrottles',
      lambdaMetric('vip-admin-ui-api-plans', 'Throttles', MIN1),
      0, 1,
      '[CRITICAL] api-plans: Throttled — 5 reserved concurrency exhausted, plan ticks are being dropped',
    );
    makeAlarm(
      'PlansDuration',
      lambdaMetric('vip-admin-ui-api-plans', 'Duration', MIN5, 'p99'),
      240_000, 1, // 4 min → 80 % of 5 min timeout
      '[WARNING] api-plans: p99 duration > 4 min (approaching 5 min timeout)',
    );

    // ── feeder alarms (critical — data pump for Connect campaigns) ────
    makeAlarm(
      'FeederErrors',
      lambdaMetric('vip-external-campaigns-feeder', 'Errors', MIN1),
      0, 1,
      '[CRITICAL] feeder: Lambda errors — lead push to Connect campaigns failed',
    );
    makeAlarm(
      'FeederThrottles',
      lambdaMetric('vip-external-campaigns-feeder', 'Throttles', MIN1),
      0, 1,
      '[CRITICAL] feeder: Throttled — 1 reserved concurrency in use, no data flowing to Connect',
    );
    makeAlarm(
      'FeederDuration',
      lambdaMetric('vip-external-campaigns-feeder', 'Duration', MIN5, 'p99'),
      240_000, 1,
      '[WARNING] feeder: p99 duration > 4 min (approaching 5 min timeout)',
    );

    // ── api-segments alarms ───────────────────────────────────────────
    makeAlarm(
      'SegmentsErrors',
      lambdaMetric('vip-admin-ui-api-segments', 'Errors', MIN5),
      0, 2,
      '[WARNING] api-segments: Lambda errors — segment rebuild or filter config failed',
    );
    makeAlarm(
      'SegmentsDuration',
      lambdaMetric('vip-admin-ui-api-segments', 'Duration', MIN5, 'p99'),
      240_000, 1,
      '[WARNING] api-segments: p99 duration > 4 min (approaching 5 min timeout)',
    );

    // ── api-campaigns alarms ──────────────────────────────────────────
    makeAlarm(
      'CampaignsErrors',
      lambdaMetric('vip-admin-ui-api-campaigns', 'Errors', MIN5),
      0, 2,
      '[WARNING] api-campaigns: Lambda errors — Connect campaign CRUD or lifecycle operation failed',
    );

    // ── api-metrics / api-profiles (lower severity) ───────────────────
    makeAlarm(
      'MetricsErrors',
      lambdaMetric('vip-admin-ui-api-metrics', 'Errors', MIN5),
      3, 3, // sustained errors, not one-off
      '[INFO] api-metrics: Elevated error rate',
    );
    makeAlarm(
      'ProfilesErrors',
      lambdaMetric('vip-admin-ui-api-profiles', 'Errors', MIN5),
      3, 3,
      '[INFO] api-profiles: Elevated error rate',
    );

    // ── DynamoDB — VipAdminPlans table ───────────────────────────────
    makeAlarm(
      'PlansTableSystemErrors',
      ddbMetric('VipAdminPlans', 'SystemErrors', MIN1),
      0, 1,
      '[CRITICAL] VipAdminPlans DynamoDB: System errors — table availability issue',
    );
    makeAlarm(
      'PlansTableThrottles',
      ddbMetric('VipAdminPlans', 'ThrottledRequests', MIN1),
      0, 1,
      '[WARNING] VipAdminPlans DynamoDB: Throttled requests',
    );

    // ── EventBridge — failed invocations of plan rules ────────────────
    // Covers both prestart_check and dynamic per-run tick rules (vip-plan-*, vip-sched-*)
    makeAlarm(
      'PrestartCheckFailedInvocations',
      ebMetric('vip-plans-prestart-check', 'FailedInvocations', MIN5),
      0, 1,
      '[WARNING] vip-plans-prestart-check EventBridge rule: failed to invoke api-plans Lambda',
    );

    // Dashboard created via CLI (CFN exec role lacks cloudwatch:PutDashboard).
    // See: deploy-cli.sh pattern used for EventBridge rules.
    // Command stored in infra/lib/stacks/monitoring-stack.ts comments:
    //   aws cloudwatch put-dashboard --dashboard-name VipConnect-Admin-UI --dashboard-body file://infra/monitoring-dashboard.json

    // ── Outputs ───────────────────────────────────────────────────────
    new cdk.CfnOutput(this, 'AlertTopicArn', {
      value: alertTopic.topicArn,
      description: 'SNS topic ARN — subscribe additional channels here',
    });
    new cdk.CfnOutput(this, 'DashboardUrl', {
      value: `https://${this.region}.console.aws.amazon.com/cloudwatch/home?region=${this.region}#dashboards:name=VipConnect-Admin-UI`,
      description: 'CloudWatch dashboard URL',
    });
  }
}
