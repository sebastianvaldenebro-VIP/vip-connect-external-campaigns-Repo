import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';

import { Spinner } from '@/components/ui';
import { api, type PlanRunV2, type PlanSummaryV2, type SmsOriginationNumber } from '@/lib/api';
import { SMS_CAMPAIGN_TEMPLATE_VERSION, NO_PROMOTIONAL_SMS_NUMBER, isPromotionalSmsNumber } from '@/lib/smsCampaign';

function promotionalOrigins(plan: PlanSummaryV2): (string | undefined)[] {
  return plan.buckets.flatMap((bucket) => bucket.campaigns.flatMap((campaign) => {
    const config = campaign.campaignConfig ?? {};
    return campaign.deliveryType === 'sms' && config.smsTemplateVersion === SMS_CAMPAIGN_TEMPLATE_VERSION
      ? [config.smsOriginationNumberArn] : [];
  }));
}

const UNSUPPORTED_SMS_BUCKET = 'Configure the SMS template version on each campaign and remove it from the bucket before starting.';

function hasUnsupportedSmsBucket(plan: PlanSummaryV2): boolean {
  return plan.buckets.some((bucket) => 'smsTemplateVersion' in (bucket.campaignConfig ?? {})
    && bucket.campaigns.some((campaign) => campaign.deliveryType === 'sms'));
}

function compatibleOrigins(plan: PlanSummaryV2, numbers: SmsOriginationNumber[]): boolean {
  return promotionalOrigins(plan).every((arn) => numbers.some((number) => number.arn === arn && isPromotionalSmsNumber(number)));
}

export function isSmsOnlyPlan(plan: PlanSummaryV2): boolean {
  const campaigns = plan.buckets.flatMap((bucket) => bucket.campaigns ?? []);
  return !plan.isTemplate && !plan.is_template && plan.buckets.length > 0
    && plan.buckets.every((bucket) => (bucket.campaigns?.length ?? 0) > 0) && campaigns.length > 0
    && campaigns.every((campaign) => campaign.deliveryType === 'sms');
}

export function canEditSmsPlan(plan: PlanSummaryV2): boolean {
  const schedule = (plan as PlanSummaryV2 & { schedule?: { enabled?: boolean } }).schedule;
  if (!isSmsOnlyPlan(plan) || plan.isDefault || plan.buckets.length !== 1
      || plan.trigger?.type !== 'manual' || plan.loop || plan.workingHours || schedule?.enabled) return false;
  const bucket = plan.buckets[0]!;
  if (bucket.campaigns.length !== 1 || bucket.run_mode !== 'status_based'
      || bucket.cleanup !== false || bucket.prestart_next !== false
      || bucket.parallel || bucket.duration_minutes != null
      || Object.keys(bucket.campaignConfig ?? {}).length > 0) return false;
  const campaign = bucket.campaigns[0]!;
  const config = campaign.campaignConfig ?? {};
  return config.smsTemplateVersion === SMS_CAMPAIGN_TEMPLATE_VERSION
    && !!campaign.pinnedSegmentArn && campaign.run_type === 'full'
    && campaign.run_duration_minutes == null && campaign.maxLeadAgeMinutes == null
    && (campaign.dependsOn?.length ?? 0) === 0
    && (campaign.states?.length ?? 0) === 0 && (campaign.groups?.length ?? 0) === 0
    && Object.keys(config).every((key) => ['smsTemplateVersion', 'smsMessageTemplate', 'smsOriginationNumberArn', 'phiAcknowledged'].includes(key));
}

export function SmsCampaigns() {
  const queryClient = useQueryClient();
  const starting = useRef(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [started, setStarted] = useState<string | null>(null);
  const [acceptedRuns, setAcceptedRuns] = useState<Record<string, PlanRunV2>>({});
  const list = useQuery({ queryKey: ['plans'], queryFn: () => api.plans.listV2(), refetchInterval: 15_000 });
  const numbers = useQuery({ queryKey: ['sms', 'numbers'], queryFn: () => api.sms.listNumbers() });
  useEffect(() => {
    setAcceptedRuns((current) => {
      const finished = (list.data?.plans ?? []).filter((plan) => current[plan.planId]
        && plan.latestRun?.runId === current[plan.planId]!.runId && plan.latestRun.status !== 'running');
      if (finished.length === 0) return current;
      const next = { ...current };
      for (const plan of finished) delete next[plan.planId];
      return next;
    });
  }, [list.data]);
  const start = useMutation({
    mutationFn: async (plan: PlanSummaryV2) => {
      if (hasUnsupportedSmsBucket(plan)) throw new Error(UNSUPPORTED_SMS_BUCKET);
      if (promotionalOrigins(plan).length > 0) {
        const current = await api.sms.listNumbers();
        queryClient.setQueryData(['sms', 'numbers'], current);
        if (!compatibleOrigins(plan, current.originationNumbers)) {
          throw new Error('This campaign needs an active promotional origination number with SMS capability before it can start.');
        }
      }
      return api.plans.triggerRunV2(plan.planId);
    },
    retry: false,
    onSuccess: (run, plan) => {
      if (run.status === 'running') setAcceptedRuns((current) => ({ ...current, [plan.planId]: run }));
      queryClient.setQueryData<{ plans: PlanSummaryV2[] }>(['plans'], (current) => current && ({
        plans: current.plans.map((item) => item.planId === plan.planId ? { ...item, latestRun: run } : item),
      }));
      setStarted(plan.name);
      void queryClient.invalidateQueries({ queryKey: ['plans'] });
    },
    onError: (error: Error) => setStartError(error.message),
    onSettled: () => { starting.current = false; },
  });
  const plans = (list.data?.plans ?? []).filter(isSmsOnlyPlan);
  const hasPromotionalCampaigns = plans.some((plan) => promotionalOrigins(plan).length > 0);

  return <div className="mx-auto flex max-w-6xl flex-col gap-6">
    <div className="flex flex-wrap items-start justify-between gap-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">SMS campaigns</h1>
        <p className="mt-1 text-sm text-muted-foreground">Choose an audience, write a message, and start when ready.</p>
      </div>
      <Link to="/sms/new" className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground">New SMS campaign</Link>
    </div>
    {startError && <p role="alert" className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">{startError}</p>}
    {started && <p role="status" className="rounded-lg border border-green-200 bg-green-50 p-3 text-sm text-green-800">{started} started. View activity for progress.</p>}
    {hasPromotionalCampaigns && numbers.isError && <p role="alert" className="text-sm text-red-700">Could not verify promotional sending numbers. {numbers.error.message} <button type="button" className="underline" onClick={() => void numbers.refetch()}>Retry numbers</button></p>}
    {hasPromotionalCampaigns && numbers.isSuccess && !numbers.data.originationNumbers.some(isPromotionalSmsNumber)
      && <p role="alert" className="text-sm text-red-700">{NO_PROMOTIONAL_SMS_NUMBER}</p>}
    {list.isPending ? <div role="status" className="flex items-center gap-2 text-sm"><Spinner /> Loading SMS campaigns…</div>
      : list.isError ? <div role="alert" className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">
        <p>Could not load SMS campaigns. {list.error.message}</p>
        <button type="button" className="mt-2 underline" onClick={() => void list.refetch()}>Retry</button>
      </div>
        : plans.length === 0 ? <div className="rounded-xl border border-border bg-card p-8 text-center">
          <h2 className="font-semibold">No SMS campaigns yet</h2>
          <p className="mt-2 text-sm text-muted-foreground">Create a campaign to save your audience and message.</p>
        </div> : <div className="overflow-x-auto rounded-xl border border-border bg-card">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-border bg-muted/40 text-xs text-muted-foreground"><tr>
              <th className="px-5 py-3">Campaign</th><th className="px-5 py-3">Status</th><th className="px-5 py-3">Actions</th>
            </tr></thead>
            <tbody>{plans.map((plan) => {
              const active = plan.latestRun?.status === 'running' || !!acceptedRuns[plan.planId];
              const pending = start.isPending && start.variables?.planId === plan.planId;
              const needsPromotionalOrigin = promotionalOrigins(plan).length > 0;
              const unsupportedBucket = hasUnsupportedSmsBucket(plan);
              const originBlocked = unsupportedBucket || (needsPromotionalOrigin && (numbers.isPending || numbers.isError
                || !compatibleOrigins(plan, numbers.data?.originationNumbers ?? [])));
              return <tr key={plan.planId} className="border-b border-border last:border-0">
                <td className="px-5 py-4 font-medium">{plan.name}</td>
                <td className="px-5 py-4 capitalize">{active ? 'running' : plan.latestRun?.status ?? 'Not started'}</td>
                <td className="px-5 py-4"><div className="flex flex-wrap items-center gap-3">
                  <button type="button" disabled={active || start.isPending || originBlocked}
                    onClick={() => {
                      if (starting.current || active || originBlocked) return;
                      starting.current = true;
                      setStartError(null);
                      setStarted(null);
                      start.mutate(plan);
                    }}
                    className="rounded-lg bg-primary px-3 py-1.5 font-medium text-primary-foreground disabled:opacity-50">
                    {pending ? 'Starting…' : 'Start'}
                  </button>
                  <Link to={`/plans/${encodeURIComponent(plan.planId)}`} className="text-primary hover:underline">View activity</Link>
                  {canEditSmsPlan(plan) && !active && <Link to={`/sms/${encodeURIComponent(plan.planId)}/edit`} className="text-primary hover:underline">Edit</Link>}
                  {unsupportedBucket && !active && <p className="basis-full text-xs text-red-700">{UNSUPPORTED_SMS_BUCKET}</p>}
                  {!unsupportedBucket && originBlocked && !active && numbers.isSuccess && <p className="basis-full text-xs text-red-700">Select an active promotional SMS number in the campaign configuration before starting.</p>}
                </div></td>
              </tr>;
            })}</tbody>
          </table>
        </div>}
  </div>;
}
