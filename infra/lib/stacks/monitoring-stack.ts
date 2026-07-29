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

    // ── Scheduled-run fallback counter ────────────────────────────────
    // Fires when prestart_check had to manually trigger scheduled_run() because a vip-sched-*
    // rule missed its Lambda invocation (resource policy statement wiped by a CDK deploy).
    // Metric is emitted per PlanId — SEARCH aggregates across all plans.
    // Self-healing: _ensure_scheduled_run_permission restores the missing statement at prestart,
    // so the next day's run will succeed; this alarm surfaces the incident for investigation.
    // NOTE: a CLI-created alarm "vip-plans-scheduled-run-fallback" may overlap — it can be deleted
    // once this CDK-managed alarm is confirmed working in production.
    {
      const fallbackAlarm = new cloudwatch.Alarm(this, 'ScheduledRunFallback', {
        metric: new cloudwatch.MathExpression({
          expression: "SUM(SEARCH('{VIPPlans,PlanId} ScheduledRunFallback', 'Sum', 300))",
          label: 'ScheduledRunFallback (all plans)',
          period: MIN5,
          usingMetrics: {},
        }),
        threshold: 0,
        evaluationPeriods: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        actionsEnabled: true,
        alarmDescription:
          '[CRITICAL] vip-sched-* rule missed Lambda invocation — prestart_check self-healed via fallback. ' +
          'Root cause: CDK deploy recreated api-plans Lambda and wiped the resource-policy statement. ' +
          'Verify: check CloudTrail InvokeFunction events + EventBridge FailedInvocations for the rule.',
      });
      fallbackAlarm.addAlarmAction(alarmAction);
      fallbackAlarm.addOkAction(alarmAction);
    }

    // ── vip-sched-* FailedInvocations (all per-plan scheduled-run rules) ─
    // Catches EventBridge AccessDenied (resource policy missing), throttles, or any pre-invocation
    // failure for dynamically-created per-plan schedule rules. SEARCH covers all rule names since
    // they are created at runtime and are not known at CDK synthesis time.
    // NOTE: a CLI-created alarm "vip-eventbridge-plan-1-1-failed-invocations" covers Plan 1.1 only —
    // this CDK alarm is generic and supersedes it for all plans.
    {
      const schedRuleAlarm = new cloudwatch.Alarm(this, 'ScheduledRunRuleFailedInvocations', {
        metric: new cloudwatch.MathExpression({
          expression: "SUM(SEARCH('{AWS/Events,RuleName} vip-sched FailedInvocations', 'Sum', 300))",
          label: 'vip-sched-* FailedInvocations (all plans)',
          period: MIN5,
          usingMetrics: {},
        }),
        threshold: 0,
        evaluationPeriods: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        actionsEnabled: true,
        alarmDescription:
          '[CRITICAL] vip-sched-* EventBridge rule failed to invoke api-plans Lambda. ' +
          'Root cause: Lambda resource-policy statement missing for this rule (wiped by CDK deploy). ' +
          'Fix: api.saveSettings() per affected plan to re-run upsert_schedule() + add_permission, ' +
          'or await next prestart_check which self-heals via _ensure_scheduled_run_permission.',
      });
      schedRuleAlarm.addAlarmAction(alarmAction);
      schedRuleAlarm.addOkAction(alarmAction);
    }

    // ── Branded campaign monitoring alarms ───────────────────────────────
    // Metrics emitted by vip-admin-branded-metrics-collector (1-min EventBridge schedule).
    makeAlarm(
      'BrandedCollectorErrors',
      lambdaMetric('vip-admin-branded-metrics-collector', 'Errors', MIN5),
      0, 2,
      '[WARNING] branded-metrics-collector: Lambda errors — disposition snapshots may be stale',
    );

    // ActiveBrandedCampaigns = 0 during business hours (7am-7pm COT = 12:00-23:59 UTC).
    // Only data points emitted during business hours — missing data at night is NOT_BREACHING.
    // This alarm fires when campaigns are expected but none are running.
    {
      const noCampaignsAlarm = new cloudwatch.Alarm(this, 'BrandedNoActiveCampaignsBizHours', {
        alarmName: 'vip-branded-no-active-campaigns-biz-hours',
        metric: new cloudwatch.Metric({
          namespace: 'VipBrandedMonitor',
          metricName: 'ActiveBrandedCampaigns',
          statistic: 'Maximum',
          period: cdk.Duration.minutes(5),
        }),
        threshold: 0,
        evaluationPeriods: 3,
        comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_OR_EQUAL_TO_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        actionsEnabled: true,
        alarmDescription:
          '[INFO] No active branded campaigns during business hours (7am-7pm COT). ' +
          'This is expected on non-campaign days; investigate if campaigns were scheduled.',
      });
      noCampaignsAlarm.addAlarmAction(alarmAction);
      noCampaignsAlarm.addOkAction(alarmAction);
    }

    // StuckBrandedCampaigns > 0 = items in VipActiveBrandedCampaigns older than 26h.
    // Indicates _stop_branded_campaign was not called after campaign completion.
    // Emitted on every collector tick (not restricted to business hours).
    makeAlarm(
      'BrandedStuckCampaigns',
      new cloudwatch.Metric({
        namespace: 'VipBrandedMonitor',
        metricName: 'StuckBrandedCampaigns',
        statistic: 'Maximum',
        period: MIN5,
      }),
      0, 2,
      '[CRITICAL] VipActiveBrandedCampaigns: campaign(s) stuck >26h — ' +
      '_stop_branded_campaign was not called. TTL may eventually clean up but run summary is missing. ' +
      'Fix: verify _force_finish_internal and other exit paths call _stop_branded_campaign.',
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
