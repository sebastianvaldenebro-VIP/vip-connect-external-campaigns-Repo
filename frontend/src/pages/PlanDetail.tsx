import { type ReactNode, useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useParams } from 'react-router-dom';

import { Button, Spinner } from '@/components/ui';
import { StatusChip } from '@/components/ui/StatusChip';
import {
  api,
  type BrandedCampaignCounts,
  type BrandedMetricSnapshot,
  type BrandedQueueItem,
  type BrandedRunSummary,
  type BucketDefV2,
  type BucketStateV2,
  type CampaignDef,
  type CampaignState,
  type CampaignStatus,
  type PlanRunV2,
  type PlanSummaryV2,
  type PlanTrigger,
  type SmsCampaignRunRecord,
} from '@/lib/api';
import { buildChainMap } from '@/lib/chainMap';
import { fmtTime } from '@/lib/utils';
import { AgentAvailabilityPanel } from './AgentAvailabilityPanel';
import { DayActivityFeed } from './DayActivityFeed';
import { DayHeaderTimeline } from './DayHeaderTimeline';
import { formatReconcile, reconcileTone } from './reconcile';

function fmtDate(d: Date | string): string {
  const dt = typeof d === 'string' ? new Date(d) : d;
  return dt.toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' });
}

function elapsedMin(start?: string | null, end?: string | null): number {
  if (!start) return 0;
  const a = new Date(start).getTime();
  const b = end ? new Date(end).getTime() : Date.now();
  return Math.max(0, Math.floor((b - a) / 60_000));
}

function fmtElapsed(start?: string | null, end?: string | null): string {
  const m = elapsedMin(start, end);
  return `${m}m`;
}

// ── Campaign duration from run_type + bucket ──────────────────────────────────

const _LEGACY_RT_MIN: Record<string, number> = { time_30: 30, time_45: 45, time_60: 60, time_90: 90, time_120: 120 };

function campaignDurMin(campaignDef?: CampaignDef | null, bucketDef?: BucketDefV2 | null): number {
  const rt = campaignDef?.run_type ?? 'full';
  if (rt === 'custom' && campaignDef?.run_duration_minutes) return campaignDef.run_duration_minutes;
  if (rt in _LEGACY_RT_MIN) return _LEGACY_RT_MIN[rt];
  return bucketDef?.duration_minutes ?? 0;
}

// Compute the planned start time for a bucket in a sequential chain.
// For already-started buckets, returns the actual startedAt.
function computePlannedStart(
  plan: PlanSummaryV2,
  run: PlanRunV2,
  bucketIndex: number,
): Date {
  const bs = run.bucketStates[bucketIndex];
  if (bs?.startedAt) return new Date(bs.startedAt);

  // Estimate by walking the chain from run.startedAt
  let t = run.startedAt ? new Date(run.startedAt) : new Date();
  for (let i = 0; i < bucketIndex; i++) {
    const bDef = plan.buckets[i];
    const bState = run.bucketStates[i];
    if (bDef?.parallel) continue; // parallel bucket doesn't block the chain
    if (bState?.completedAt) {
      t = new Date(bState.completedAt);
    } else {
      const dur = bDef?.duration_minutes ?? 60;
      t = new Date(t.getTime() + dur * 60_000);
    }
  }
  return t;
}

// ── Status display mapping ────────────────────────────────────────────────────

const CAMPAIGN_STATUS_LABEL: Record<CampaignStatus | string, string> = {
  queued: 'waiting',
  warming: 'warming',
  running: 'running',
  completed: 'done',
  cancelled: 'cancelled',
  error: 'error',
  expired: 'expired',
};

const STATUS_DOT: Record<string, string> = {
  queued: 'text-gray-400',
  waiting: 'text-gray-400',
  warming: 'text-amber-500',
  running: 'text-blue-500',
  completed: 'text-green-500',
  done: 'text-green-500',
  cancelled: 'text-gray-300',
  error: 'text-red-500',
  expired: 'text-orange-400',
};

function StatusBadge({ status, className = '' }: { status: string; className?: string }) {
  const label = CAMPAIGN_STATUS_LABEL[status] ?? status;
  const dotColor = STATUS_DOT[label] ?? STATUS_DOT[status] ?? 'text-gray-400';
  return (
    <span className={`inline-flex items-center gap-1 text-xs text-gray-500 ${className}`}>
      <span className={`${dotColor} ${label === 'running' ? 'animate-pulse' : ''}`}>●</span>
      {label}
    </span>
  );
}

// ── Timing metadata strip ─────────────────────────────────────────────────────

function TimingMeta({
  items,
}: {
  items: { label: string; value: string }[];
}) {
  return (
    <div className="flex items-center gap-4">
      {items.map(({ label, value }) => (
        <span key={label} className="flex items-baseline gap-1">
          <span className="text-[10px] font-semibold tracking-widest text-gray-400 uppercase">
            {label}
          </span>
          <span className="text-xs font-medium text-gray-700">{value}</span>
        </span>
      ))}
    </div>
  );
}

// ── Campaign card ─────────────────────────────────────────────────────────────

function groupCategory(group: string): string {
  const slash = group.indexOf(' / ');
  return slash > 0 ? group.slice(0, slash) : group;
}

function CampaignCard({
  cs,
  campaignDef,
  bucketDef,
  plannedStart,
  parentNames,
  isMerge,
  onForceStart,
  onForceStop,
  onSkip,
  brandedCounts,
  brandedQueue,
  smsRun,
}: {
  cs: CampaignState;
  campaignDef?: CampaignDef | null;
  bucketDef?: BucketDefV2 | null;
  plannedStart: Date;
  parentNames?: string[];
  isMerge?: boolean;
  onForceStart?: () => void;
  onForceStop?: () => void;
  onSkip?: () => void;
  brandedCounts?: BrandedCampaignCounts;
  brandedQueue?: BrandedQueueItem[];
  smsRun?: SmsCampaignRunRecord;
}) {
  const durMin = campaignDurMin(campaignDef, bucketDef);
  const isTimeBased = bucketDef?.run_mode === 'time_based';

  const actualStart = cs.startedAt ? new Date(cs.startedAt) : plannedStart;
  const etaDate = durMin > 0 ? new Date(actualStart.getTime() + durMin * 60_000) : null;

  const states = campaignDef?.states ?? [];
  const groups = campaignDef?.groups ?? [];
  const category = groups.length > 0 ? groupCategory(groups[0]) : null;
  const subtitle = [states.join('-'), category].filter(Boolean).join(' · ');

  const timingItems: { label: string; value: string }[] = [
    { label: 'Start', value: fmtTime(actualStart) },
    ...(etaDate ? [{ label: 'ETA', value: fmtTime(etaDate) }] : []),
    ...(isTimeBased && durMin > 0 ? [{ label: 'Dur', value: `${durMin}m` }] : []),
  ];

  // Fetch live metrics for active branded campaigns (answer/voicemail rate)
  const brandedCampaignId = (cs as CampaignState & { brandedCampaignId?: string }).brandedCampaignId;
  const isBrandedRunning = campaignDef?.deliveryType === 'branded' && cs.status === 'running';
  const brandedMetricQuery = useQuery({
    queryKey: ['branded-metrics-detail', brandedCampaignId],
    queryFn: () => api.brandedMonitor.getCampaignMetrics(brandedCampaignId!, 1),
    enabled: isBrandedRunning && !!brandedCampaignId,
    refetchInterval: 30_000,
    staleTime: 30_000,
  });
  const latestMetric: BrandedMetricSnapshot | undefined = brandedMetricQuery.data?.metrics?.[0];

  const isError = cs.status === 'error';
  const statusBorderLeft: Record<string, string> = {
    running: 'border-l-blue-400',
    completed: 'border-l-green-400',
    warming: 'border-l-amber-400',
    cancelled: 'border-l-gray-300',
    expired: 'border-l-gray-300',
    error: 'border-l-red-500',
    queued: 'border-l-gray-200',
  };
  const borderLeft = statusBorderLeft[cs.status] ?? 'border-l-gray-200';
  const borderColor = isError
    ? `border-red-300 ${borderLeft} bg-red-50`
    : `border-gray-200 ${borderLeft}`;

  return (
    <div className={`border rounded-xl p-4 space-y-2 border-l-4 ${borderColor} ${isError ? '' : 'bg-white'}`}>
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-1.5 min-w-0">
          <span className="text-sm font-semibold text-gray-800 leading-tight truncate">
            {cs.name || cs.campaignId.slice(0, 8)}
          </span>
          {campaignDef?.deliveryType === 'journey' && (
            <span className="shrink-0 text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-purple-100 text-purple-700 leading-none">
              Journey
            </span>
          )}
          {campaignDef?.deliveryType === 'branded' && (
            <span className="shrink-0 text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-amber-100 text-amber-700 leading-none">
              Branded
            </span>
          )}
          {campaignDef?.deliveryType === 'sms' && (
            <span className="shrink-0 text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-teal-100 text-teal-700 leading-none">
              SMS
            </span>
          )}
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <StatusBadge status={cs.status} />
          {onForceStart && (cs.status === 'queued' || cs.status === 'cancelled' || cs.status === 'error') && (
            <button
              type="button"
              onClick={onForceStart}
              className={
                cs.status === 'error'
                  ? 'text-[10px] text-red-600 border border-red-300 rounded px-1.5 py-0.5 hover:bg-red-50 leading-none'
                  : 'text-[10px] text-blue-600 border border-blue-300 rounded px-1.5 py-0.5 hover:bg-blue-50 leading-none'
              }
              title={cs.status === 'error' ? 'Retry segment creation' : 'Force-start this campaign now'}
            >
              {cs.status === 'error' ? '↺' : '▶'}
            </button>
          )}
          {onForceStop && cs.status === 'running' && (
            <button
              type="button"
              onClick={onForceStop}
              className="text-[10px] text-orange-600 border border-orange-300 rounded px-1.5 py-0.5 hover:bg-orange-50 leading-none"
              title="Stop this campaign now"
            >
              ⏹
            </button>
          )}
          {onSkip && (cs.status === 'queued' || cs.status === 'warming' || cs.status === 'running') && (
            <button
              type="button"
              onClick={onSkip}
              className="text-[10px] text-gray-500 border border-gray-300 rounded px-1.5 py-0.5 hover:bg-gray-100 leading-none"
              title="Skip this campaign — children will not be cascade-cancelled"
            >
              ⏭
            </button>
          )}
        </div>
      </div>
      {subtitle && (
        <div className="text-xs text-gray-400">{subtitle}</div>
      )}
      {parentNames && parentNames.length > 0 && (
        <div className="text-[10px] text-gray-400 flex items-center gap-1">
          <span className="text-gray-300">{isMerge ? '⑂' : '↳'}</span>
          <span>after {parentNames.join(isMerge ? ' + ' : ', ')}</span>
        </div>
      )}
      {cs.leadCount != null && (
        <div className="text-xs text-gray-400">{cs.leadCount.toLocaleString()} leads</div>
      )}
      {campaignDef?.deliveryType === 'branded' && (
        brandedCounts != null ? (
          <div className="space-y-1">
            {/* Bug 5 fix: clamp pct to [0, 100] so a backend counting race (dialed > total)
                never renders ">100%" text or a bar wider than its container. */}
            {(() => {
              const pct = brandedCounts.total > 0
                ? Math.min(100, Math.round((brandedCounts.dialed / brandedCounts.total) * 100))
                : 0;
              return (
                <>
                  <div className="flex items-center justify-between text-[10px] text-gray-500">
                    <span>
                      <span className="font-medium text-green-600">{brandedCounts.dialed}</span>
                      {' dialed · '}
                      <span className="font-medium text-amber-600">{brandedCounts.pending}</span>
                      {' pending'}
                    </span>
                    {brandedCounts.total > 0 && (
                      <span className="text-gray-400">{pct}%</span>
                    )}
                  </div>
                  {brandedCounts.total > 0 && (
                    <div className="w-full h-1 bg-gray-100 rounded-full overflow-hidden">
                      <div
                        className="h-full bg-green-400 rounded-full transition-all duration-500"
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                  )}
                  {latestMetric && (
                    <div className="flex items-center gap-3 text-[11px] mt-0.5">
                      <span className="text-green-600 font-medium">Answer {latestMetric.answerRate}%</span>
                      <span className="text-blue-600">VM {latestMetric.voicemailRate}%</span>
                      <span className="text-gray-400">
                        Agents {latestMetric.agentsAvailable}/{latestMetric.agentsStaffed}
                      </span>
                    </div>
                  )}
                </>
              );
            })()}
            {brandedQueue && brandedQueue.length > 0 && (
              <div className="mt-1 max-h-[128px] overflow-y-auto rounded border border-gray-100">
                <table className="w-full text-[10px] border-collapse">
                  <thead className="sticky top-0 bg-white">
                    <tr className="text-gray-400 border-b border-gray-100">
                      <th className="text-left px-1.5 py-0.5 font-medium">Status</th>
                      <th className="text-left px-1.5 py-0.5 font-medium">Phone</th>
                      <th className="text-right px-1.5 py-0.5 font-medium">Time</th>
                    </tr>
                  </thead>
                  <tbody>
                    {brandedQueue.map((item, i) => (
                      <tr key={i} className="border-b border-gray-50 last:border-0">
                        <td className="px-1.5 py-0.5">
                          {item.status === 'DIALED' ? (
                            <span className="text-green-600 font-medium">✓ DIALED</span>
                          ) : item.status === 'DISPATCHING' ? (
                            <span className="text-amber-600 font-medium">● CALLING</span>
                          ) : (
                            <span className="text-gray-400">PENDING</span>
                          )}
                        </td>
                        <td className="px-1.5 py-0.5 font-mono text-gray-600">···-{item.phone_last4}</td>
                        <td className="px-1.5 py-0.5 text-right text-gray-400">{fmtTime(item.seededAt)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        ) : (
          <div className="text-[10px] text-gray-400 animate-pulse">Loading progress…</div>
        )
      )}
      {campaignDef?.deliveryType === 'sms' && smsRun && (
        <div className="text-[10px] text-gray-500 space-y-1">
          <div className="flex items-center gap-3">
            <span>
              <span className="font-medium text-green-600">{smsRun.totalSent}</span>
              {' sent · '}
              <span className="font-medium text-red-500">{smsRun.totalFailed}</span>
              {' failed · '}
              <span className="text-gray-400">{smsRun.totalOptedOut}</span>
              {' opted out'}
            </span>
            {smsRun.totalEnqueued > 0 && (
              <span className="text-gray-400 ml-auto">
                {Math.round(((smsRun.totalSent + smsRun.totalFailed + smsRun.totalOptedOut) / smsRun.totalEnqueued) * 100)}%
              </span>
            )}
          </div>
          {smsRun.totalEnqueued > 0 && (
            <div className="w-full h-1 bg-gray-100 rounded-full overflow-hidden">
              <div
                className="h-full bg-green-400 rounded-full transition-all duration-500"
                style={{ width: `${Math.min(100, Math.round(((smsRun.totalSent + smsRun.totalFailed + smsRun.totalOptedOut) / smsRun.totalEnqueued) * 100))}%` }}
              />
            </div>
          )}
        </div>
      )}
      {cs.exitReason && cs.exitReason !== 'completed' && (
        <div className="text-xs text-orange-500">{cs.exitReason}</div>
      )}
      {cs.errorDetail && (
        <div className="text-xs text-red-500 truncate" title={cs.errorDetail}>
          {cs.errorDetail}
        </div>
      )}
      {cs.reconcile && (
        <StatusChip tone={reconcileTone(cs.reconcile)} label={formatReconcile(cs.reconcile)} mono />
      )}
      <TimingMeta items={timingItems} />
    </div>
  );
}

// ── Bucket section ────────────────────────────────────────────────────────────

function modeLabel(bucketDef?: BucketDefV2 | null): string {
  if (!bucketDef) return '';
  if (bucketDef.run_mode === 'time_based') return `${bucketDef.duration_minutes ?? 0}m`;
  return 'until done';
}

function BucketSection({
  bs,
  bucketDef,
  index,
  plan,
  run,
  onForceStart,
  onForceStop,
  onForceStartCampaign,
  onForceStopCampaign,
  onSkipCampaign,
  brandedProgress,
  brandedQueue,
  smsRunsMap,
}: {
  bs: BucketStateV2;
  bucketDef?: BucketDefV2 | null;
  index: number;
  plan: PlanSummaryV2;
  run: PlanRunV2;
  onForceStart?: () => void;
  onForceStop?: () => void;
  onForceStartCampaign?: (campaignIndex: number) => void;
  onForceStopCampaign?: (campaignIndex: number) => void;
  onSkipCampaign?: (campaignIndex: number) => void;
  brandedProgress?: Record<string, BrandedCampaignCounts>;
  brandedQueue?: Record<string, BrandedQueueItem[]>;
  smsRunsMap?: Record<string, SmsCampaignRunRecord>;
}) {
  const plannedStart = computePlannedStart(plan, run, index);
  const durMin = bucketDef?.duration_minutes ?? 0;

  // Chain map: campaignId → { chainIndex, isMerge, parentNames }
  const chainMap = buildChainMap(plan);

  const campaignStates = bs.campaignStates ?? [];
  const campaigns = bucketDef?.campaigns ?? [];

  // Sort campaigns in this bucket by chain index so same-chain campaigns
  // always appear in the same column across all buckets
  const sorted = [...campaignStates]
    .map((cs) => {
      const def = campaigns.find((c) => c.id === cs.campaignId) ?? campaigns[campaignStates.indexOf(cs)];
      const chain = chainMap.get(cs.campaignId) ?? { chainIndex: 0, isMerge: false, parentNames: [] };
      return { cs, def, chain };
    })
    .sort((a, b) => a.chain.chainIndex - b.chain.chainIndex);

  const etaDate =
    bucketDef?.run_mode === 'time_based' && durMin > 0
      ? new Date(plannedStart.getTime() + durMin * 60_000)
      : null;

  const bucketTimingItems: { label: string; value: string }[] = [
    { label: 'Start', value: fmtTime(plannedStart) },
    ...(etaDate ? [{ label: 'ETA', value: fmtTime(etaDate) }] : []),
    { label: 'Elapsed', value: fmtElapsed(bs.startedAt, bs.completedAt) },
  ];

  return (
    <div className="space-y-3">
      {/* Bucket header */}
      <div className="flex items-center gap-4">
        {/* Number circle */}
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-gray-900 text-sm font-bold text-white">
          {index + 1}
        </div>

        {/* Name + status */}
        <div className="flex flex-1 items-center gap-3 min-w-0">
          <span className="text-base font-semibold text-gray-900 truncate">
            {bs.name || bucketDef?.name || `Bucket ${index + 1}`}
          </span>
          <StatusBadge status={bs.status} />
          {modeLabel(bucketDef) && (
            <span className="text-xs text-gray-400">{modeLabel(bucketDef)}</span>
          )}
          {bucketDef?.parallel && index > 0 && (
            <span className="text-xs text-indigo-400">parallel</span>
          )}
        </div>

        {/* Timing */}
        <div className="shrink-0">
          <TimingMeta items={bucketTimingItems} />
        </div>

        {/* Manual bucket controls */}
        {onForceStart && (bs.status === 'queued' || bs.status === 'warming') && (
          <button
            type="button"
            onClick={onForceStart}
            className="shrink-0 text-xs text-blue-600 border border-blue-300 rounded px-2 py-0.5 hover:bg-blue-50"
          >
            ▶ Start now
          </button>
        )}
        {onForceStop && (bs.status === 'running' || bs.status === 'warming') && (
          <button
            type="button"
            onClick={onForceStop}
            className="shrink-0 text-xs text-orange-600 border border-orange-300 rounded px-2 py-0.5 hover:bg-orange-50"
          >
            ⏹ Stop bucket
          </button>
        )}
      </div>

      {/* Campaign grid — responsive auto-fill */}
      {sorted.length > 0 && (
        <div
          className="ml-12 grid gap-3"
          style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))' }}
        >
          {sorted.map(({ cs, def, chain }) => {
            const spanClass = chain.isMerge ? 'col-span-full' : '';
            const ci = campaignStates.indexOf(cs);
            return (
              <div key={cs.campaignId} className={spanClass}>
                <CampaignCard
                  cs={cs}
                  campaignDef={def}
                  bucketDef={bucketDef}
                  plannedStart={plannedStart}
                  parentNames={chain.parentNames}
                  isMerge={chain.isMerge}
                  onForceStart={onForceStartCampaign ? () => onForceStartCampaign(ci) : undefined}
                  onForceStop={onForceStopCampaign ? () => onForceStopCampaign(ci) : undefined}
                  onSkip={onSkipCampaign ? () => onSkipCampaign(ci) : undefined}
                  brandedCounts={brandedProgress?.[cs.campaignId]}
                  brandedQueue={brandedQueue?.[cs.campaignId]}
                  smsRun={smsRunsMap?.[(cs as CampaignState & { smsCampaignId?: string }).smsCampaignId ?? '']}
                />
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ── Run status header bar ─────────────────────────────────────────────────────

function RunStatusBar({ run, plan }: { run?: PlanRunV2; plan: PlanSummaryV2 }) {
  if (!run) return null;

  const activeBucketIndex = run.bucketStates.findIndex(
    (bs) => bs.status === 'running' || bs.status === 'warming',
  );
  const currentNum = activeBucketIndex >= 0
    ? activeBucketIndex + 1
    : typeof run.currentBucketIndex === 'number'
      ? run.currentBucketIndex + 1
      : '—';
  const totalBuckets = run.bucketStates.length;

  const hasLoop = Boolean(plan.loop);
  const statusLabel =
    run.status === 'running'
      ? hasLoop
        ? 'Cycle running'
        : 'Running'
      : run.status === 'completed'
        ? 'Completed'
        : run.status === 'aborted'
          ? 'Aborted'
          : run.status;

  const dotColor =
    run.status === 'running'
      ? 'text-blue-500 animate-pulse'
      : run.status === 'completed'
        ? 'text-green-500'
        : 'text-gray-400';

  return (
    <span className="inline-flex items-center gap-1.5 text-sm text-gray-600">
      <span className={`text-base ${dotColor}`}>●</span>
      <span className="font-medium">{statusLabel}</span>
      {run.status === 'running' && (
        <>
          <span className="text-gray-300">·</span>
          <span>bucket {currentNum} of {totalBuckets}</span>
        </>
      )}
    </span>
  );
}

// ── Trigger label ─────────────────────────────────────────────────────────────

function triggerLabel(trigger: PlanTrigger | undefined, allPlans: PlanSummaryV2[]): string {
  if (!trigger || trigger.type === 'manual') return '▶ Manual';
  if (trigger.type === 'time') return `⏰ ${trigger.time} COT`;
  if (trigger.type === 'on_plan_complete') {
    const upstreamName = allPlans.find((p) => p.planId === trigger.planId)?.name ?? trigger.planId;
    if (trigger.afterCampaign) {
      const bucketPart = trigger.afterBucket != null ? ` → bucket ${trigger.afterBucket + 1}` : '';
      // Find campaign name in the upstream plan's bucket
      const bucket = trigger.afterBucket != null
        ? allPlans.find((p) => p.planId === trigger.planId)?.buckets[trigger.afterBucket!]
        : undefined;
      const campaignName = bucket?.campaigns.find((c) => c.id === trigger.afterCampaign)?.name
        ?? trigger.afterCampaign;
      return `⛓ After "${upstreamName}"${bucketPart} → "${campaignName}"`;
    }
    return trigger.afterBucket != null
      ? `⛓ After "${upstreamName}" → bucket ${trigger.afterBucket + 1}`
      : `⛓ After "${upstreamName}"`;
  }
  return '⛓ On plan complete';
}

// ── Main component ────────────────────────────────────────────────────────────

export function PlanDetail(): ReactNode {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [showAbortConfirm, setShowAbortConfirm] = useState(false);
  const [showBucketPicker, setShowBucketPicker] = useState(false);
  const bucketPickerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!showBucketPicker) return;
    function handleClick(e: MouseEvent) {
      if (bucketPickerRef.current && !bucketPickerRef.current.contains(e.target as Node)) {
        setShowBucketPicker(false);
      }
    }
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [showBucketPicker]);

  const allPlansQuery = useQuery({
    queryKey: ['plans'],
    queryFn: () => api.plans.listV2(),
    staleTime: 60_000,
  });
  const allPlans: PlanSummaryV2[] = allPlansQuery.data?.plans ?? [];

  const planQuery = useQuery({
    queryKey: ['plans', id],
    queryFn: () => api.plans.getV2(id!),
    refetchInterval: (query) =>
      query.state.data?.latestRun?.status === 'running' ? 5_000 : false,
  });

  const runsQuery = useQuery({
    queryKey: ['plans', id, 'runs'],
    queryFn: () => api.plans.listRunsV2(id!),
    refetchInterval: planQuery.data?.latestRun?.status === 'running' ? 5_000 : false,
  });

  const isRunActive = planQuery.data?.latestRun?.status === 'running';

  // Bug 2 fix: derive hasBrandedCampaigns from the run's frozen planSnapshot (not the live
  // plan), so mid-run plan edits don't silence polling while campaigns are still dialing.
  // Fall back to live plan only when no snapshot is available (e.g., pre-run).
  const snapshotBuckets = (planQuery.data?.latestRun?.planSnapshot as PlanSummaryV2 | undefined)?.buckets
    ?? planQuery.data?.plan?.buckets
    ?? [];
  const hasBrandedCampaigns = isRunActive && snapshotBuckets.some(
    (b) => b.campaigns?.some((c) => c.deliveryType === 'branded'),
  );
  const hasSmsCampaigns = snapshotBuckets.some(
    (b) => b.campaigns?.some((c) => c.deliveryType === 'sms'),
  );

  const brandedProgressQuery = useQuery({
    queryKey: ['branded-progress', id, planQuery.data?.latestRun?.runId],
    // Bug 4 fix: replace non-null assertions with optional chaining + early return to
    // prevent TypeError if planQuery is mid-refetch when this queryFn fires.
    queryFn: () => {
      const runId = planQuery.data?.latestRun?.runId;
      if (!id || !runId) return Promise.resolve({ progress: {} });
      return api.plans.getBrandedProgressV2(id, runId);
    },
    enabled: !!id && hasBrandedCampaigns && !!planQuery.data?.latestRun?.runId,
    refetchInterval: isRunActive ? 10_000 : false,
  });

  const brandedQueueQuery = useQuery({
    queryKey: ['branded-queue', id, planQuery.data?.latestRun?.runId],
    queryFn: () => {
      const runId = planQuery.data?.latestRun?.runId;
      if (!id || !runId) return Promise.resolve({ items: {} });
      return api.plans.getBrandedQueueV2(id, runId);
    },
    enabled: !!id && hasBrandedCampaigns && !!planQuery.data?.latestRun?.runId,
    refetchInterval: isRunActive ? 10_000 : false,
  });

  const brandedHistoryQuery = useQuery({
    queryKey: ['branded-history', id],
    queryFn: () => id ? api.plans.getBrandedHistoryV2(id) : Promise.resolve({ history: [] }),
    enabled: !!id && hasBrandedCampaigns,
    staleTime: 60_000,
  });

  const smsRunsQuery = useQuery({
    queryKey: ['sms-runs', id],
    queryFn: () => api.sms.getSmsRuns(id!),
    enabled: !!id && hasSmsCampaigns,
    refetchInterval: isRunActive ? 15_000 : false,
    staleTime: 10_000,
  });
  const smsRunsMap: Record<string, SmsCampaignRunRecord> = {};
  for (const run of smsRunsQuery.data?.runs ?? []) {
    smsRunsMap[run.smsCampaignId] = run;
  }

  const triggerMutation = useMutation({
    mutationFn: (startBucketIndex?: number) => api.plans.triggerRunV2(id!, startBucketIndex),
    onSuccess: () => {
      setShowBucketPicker(false);
      qc.invalidateQueries({ queryKey: ['plans', id] });
      qc.invalidateQueries({ queryKey: ['plans', id, 'runs'] });
    },
  });

  const abortMutation = useMutation({
    mutationFn: (runId: string) => api.plans.abortRunV2(id!, runId),
    onSuccess: () => {
      setActionError(null);
      setShowAbortConfirm(false);
      qc.invalidateQueries({ queryKey: ['plans', id] });
      qc.invalidateQueries({ queryKey: ['plans', id, 'runs'] });
    },
    onError: (err: Error) => {
      setActionError(err.message || 'Abort failed — try again');
    },
  });

  const forceFinishMutation = useMutation({
    mutationFn: (runId: string) => api.plans.forceFinishRunV2(id!, runId),
    onSuccess: () => {
      setActionError(null);
      qc.invalidateQueries({ queryKey: ['plans', id] });
      qc.invalidateQueries({ queryKey: ['plans', id, 'runs'] });
    },
    onError: (err: Error) => {
      setActionError(err.message || 'Force finish failed — try again');
    },
  });

  const bucketActionMutation = useMutation({
    mutationFn: ({ runId, bucketIndex, action }: { runId: string; bucketIndex: number; action: 'start' | 'stop' }) =>
      action === 'start'
        ? api.plans.forceStartBucketV2(id!, runId, bucketIndex)
        : api.plans.forceStopBucketV2(id!, runId, bucketIndex),
    onSuccess: () => {
      setActionError(null);
      qc.invalidateQueries({ queryKey: ['plans', id] });
      qc.invalidateQueries({ queryKey: ['plans', id, 'runs'] });
    },
    onError: (err: Error) => {
      setActionError(err.message || 'Action failed — try again');
    },
  });

  const [actionError, setActionError] = useState<string | null>(null);

  const campaignActionMutation = useMutation({
    mutationFn: ({ runId, bucketIndex, campaignIndex, action }: { runId: string; bucketIndex: number; campaignIndex: number; action: 'start' | 'stop' | 'skip' }) =>
      action === 'start'
        ? api.plans.forceStartCampaignV2(id!, runId, bucketIndex, campaignIndex)
        : action === 'skip'
          ? api.plans.skipCampaignV2(id!, runId, bucketIndex, campaignIndex)
          : api.plans.forceStopCampaignV2(id!, runId, bucketIndex, campaignIndex),
    onSuccess: () => {
      setActionError(null);
      qc.invalidateQueries({ queryKey: ['plans', id] });
      qc.invalidateQueries({ queryKey: ['plans', id, 'runs'] });
    },
    onError: (err: Error) => {
      setActionError(err.message || 'Action failed — try again');
    },
  });

  const applySnapshotMutation = useMutation({
    mutationFn: (runId: string) => api.plans.applySnapshotV2(id!, runId),
    onSuccess: () => {
      setActionError(null);
      qc.invalidateQueries({ queryKey: ['plans', id] });
      qc.invalidateQueries({ queryKey: ['plans', id, 'runs'] });
    },
    onError: (err: Error) => {
      setActionError(err.message || 'Apply failed — try again');
    },
  });

  if (planQuery.isLoading) return <Spinner />;
  if (!planQuery.data?.plan) return <div className="p-6 text-gray-500">Plan not found.</div>;

  const plan = planQuery.data.plan as PlanSummaryV2;
  const latestRun = planQuery.data.latestRun as PlanRunV2 | undefined;
  const runs = (runsQuery.data?.runs ?? []) as PlanRunV2[];

  const displayRun = selectedRunId
    ? (runs.find((r) => r.runId === selectedRunId) ?? latestRun)
    : latestRun;

  // Use planSnapshot from run when available (immutable at trigger time)
  const planForDisplay = (displayRun?.planSnapshot as PlanSummaryV2 | undefined) ?? plan;

  const isRunning = latestRun?.status === 'running';

  // True when the live plan definition has diverged from the active run's snapshot
  const hasSnapshotDiff = Boolean(
    isRunning &&
    displayRun?.planSnapshot &&
    JSON.stringify((displayRun.planSnapshot as PlanSummaryV2).buckets) !== JSON.stringify(plan.buckets),
  );

  return (
    <div className="min-h-screen bg-gray-50">
      {/* ── Page header ─────────────────────────────────────────────────────── */}
      <div className="bg-white border-b border-gray-100 px-8 py-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => navigate('/plans')}
              className="text-gray-400 hover:text-gray-600 text-sm"
            >
              ←
            </button>
            <div>
              <span className="font-semibold text-gray-900">Live monitor</span>
              <span className="text-gray-300 mx-2">·</span>
              <span className="text-sm text-gray-500">{fmtDate(new Date())}</span>
            </div>
          </div>

          <div className="flex items-center gap-4">
            <RunStatusBar run={displayRun} plan={planForDisplay} />
            <div className="flex gap-2">
              <Button variant="outline" size="sm" onClick={() => navigate(`/plans/${id}/edit`)}>
                Edit
              </Button>
              {hasSnapshotDiff && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => displayRun && applySnapshotMutation.mutate(displayRun.runId)}
                  disabled={applySnapshotMutation.isPending}
                >
                  {applySnapshotMutation.isPending ? <Spinner /> : 'Apply to run'}
                </Button>
              )}
              {isRunning && !showAbortConfirm && (
                <Button variant="destructive" size="sm" onClick={() => setShowAbortConfirm(true)}>
                  Abort
                </Button>
              )}
              {isRunning && !showAbortConfirm && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => displayRun && forceFinishMutation.mutate(displayRun.runId)}
                  disabled={forceFinishMutation.isPending}
                  title="Mark run as completed now — stops all campaigns and fires chaining"
                >
                  {forceFinishMutation.isPending ? <Spinner /> : 'Force Finish'}
                </Button>
              )}
              {isRunning && showAbortConfirm && (
                <div className="flex items-center gap-2">
                  <span className="text-xs text-red-600 font-medium">Abort this run?</span>
                  <Button
                    variant="destructive"
                    size="sm"
                    onClick={() => displayRun && abortMutation.mutate(displayRun.runId)}
                    disabled={abortMutation.isPending}
                  >
                    {abortMutation.isPending ? <Spinner /> : 'Confirm'}
                  </Button>
                  <Button variant="outline" size="sm" onClick={() => setShowAbortConfirm(false)}>
                    Cancel
                  </Button>
                </div>
              )}
              {!isRunning && !plan.isTemplate && (
                <div className="relative" ref={bucketPickerRef}>
                  <div className="flex">
                    <Button
                      size="sm"
                      className="rounded-r-none pr-2"
                      onClick={() => triggerMutation.mutate(undefined)}
                      disabled={triggerMutation.isPending}
                    >
                      {triggerMutation.isPending ? <Spinner /> : 'Run Now'}
                    </Button>
                    <Button
                      size="sm"
                      className="rounded-l-none border-l border-primary-foreground/30 px-1.5"
                      onClick={() => setShowBucketPicker(v => !v)}
                      disabled={triggerMutation.isPending}
                      title="Start from a specific bucket"
                    >
                      ▾
                    </Button>
                  </div>
                  {showBucketPicker && (
                    <div className="absolute right-0 top-full mt-1 z-50 bg-white border border-gray-200 rounded-lg shadow-lg min-w-48 py-1">
                      <div className="px-3 py-1.5 text-xs text-gray-400 font-medium uppercase tracking-wide">Start from bucket</div>
                      {plan.buckets.map((bucket, i) => (
                        <button
                          key={bucket.id}
                          type="button"
                          className="w-full text-left px-3 py-2 text-sm hover:bg-gray-50 flex items-center gap-2"
                          onClick={() => triggerMutation.mutate(i)}
                        >
                          <span className="text-gray-400 text-xs w-4">{i + 1}</span>
                          <span className="truncate">{bucket.name || `Bucket ${i + 1}`}</span>
                          {i === 0 && <span className="ml-auto text-xs text-gray-300">default</span>}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Plan name + meta */}
        <div className="mt-3">
          <h1 className="text-lg font-semibold text-gray-900">{plan.name}</h1>
          {plan.description && (
            <p className="text-sm text-gray-400 mt-0.5">{plan.description}</p>
          )}
          <div className="flex items-center gap-3 mt-1 text-xs text-gray-400">
            <span>{triggerLabel(plan.trigger, allPlans)}</span>
            <span>·</span>
            <span>{plan.buckets.length} bucket{plan.buckets.length !== 1 ? 's' : ''}</span>
            <span>·</span>
            <span>
              {plan.buckets.reduce((s, b) => s + (b.campaigns?.length ?? 0), 0)} campaigns
            </span>
            {plan.isTemplate && (
              <>
                <span>·</span>
                <span className="text-indigo-400">template</span>
              </>
            )}
          </div>
        </div>

        {displayRun && (
          <div className="mt-3">
            <DayHeaderTimeline plan={plan} run={displayRun} />
          </div>
        )}
      </div>

      {/* ── Error banners ────────────────────────────────────────────────────── */}
      {triggerMutation.isError && (
        <div className="mx-8 mt-4 text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg p-3">
          {String((triggerMutation.error as Error)?.message ?? 'Failed to start run')}
        </div>
      )}
      {actionError && (
        <div className="mx-8 mt-4 flex items-center justify-between text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg p-3">
          <span>{actionError}</span>
          <button type="button" onClick={() => setActionError(null)} className="ml-3 text-red-400 hover:text-red-600">✕</button>
        </div>
      )}

      {/* ── Main content ─────────────────────────────────────────────────────── */}
      <div className="px-8 py-6">
        <div className={`grid grid-cols-1 gap-6 ${displayRun ? 'lg:grid-cols-[minmax(0,1fr)_320px]' : ''}`}>
          <div className="space-y-6">

            {/* Active / selected run — bucket sections */}
            {displayRun ? (
              <div className="space-y-6">
                {/* Run meta strip */}
                <div className="flex items-center gap-3 text-xs text-gray-400">
                  <span>Run {displayRun.runId.slice(0, 16)}</span>
                  <span>·</span>
                  <span>started {fmtTime(displayRun.startedAt)}</span>
                  {displayRun.triggeredBy && displayRun.triggeredBy !== 'manual' && (
                    <>
                      <span>·</span>
                      <span>via {displayRun.triggeredBy}</span>
                    </>
                  )}
                  {displayRun.completedAt && (
                    <>
                      <span>·</span>
                      <span>finished {fmtTime(displayRun.completedAt)}</span>
                      <span>·</span>
                      <span>{fmtElapsed(displayRun.startedAt, displayRun.completedAt)} total</span>
                    </>
                  )}
                  {selectedRunId && selectedRunId !== latestRun?.runId && (
                    <button
                      type="button"
                      className="ml-2 text-blue-500 hover:underline"
                      onClick={() => setSelectedRunId(null)}
                    >
                      ← Back to latest
                    </button>
                  )}
                </div>

                {/* Bucket sections */}
                <div className="space-y-6">
                  {displayRun.bucketStates.map((bs, bi) => {
                    const bucketDef = planForDisplay.buckets[bi] ?? null;
                    const canControl = isRunning && displayRun.runId === latestRun?.runId;
                    // Bug 1 + 3 fix: only pass brandedProgress when the user is viewing the
                    // active latestRun AND it is currently running. This prevents:
                    //   - Bug 1: latestRun counts bleeding into historical run cards
                    //   - Bug 3: stale counts persisting in React Query cache after run completes
                    const isCurrentRunDisplayed = isRunActive && displayRun.runId === latestRun?.runId;
                    return (
                      <BucketSection
                        key={bs.bucketId}
                        bs={bs}
                        bucketDef={bucketDef}
                        index={bi}
                        plan={planForDisplay}
                        run={displayRun}
                        onForceStart={canControl && (bs.status === 'queued' || bs.status === 'warming')
                          ? () => bucketActionMutation.mutate({ runId: displayRun.runId, bucketIndex: bi, action: 'start' })
                          : undefined}
                        onForceStop={canControl && (bs.status === 'running' || bs.status === 'warming')
                          ? () => bucketActionMutation.mutate({ runId: displayRun.runId, bucketIndex: bi, action: 'stop' })
                          : undefined}
                        onForceStartCampaign={canControl
                          ? (ci) => campaignActionMutation.mutate({ runId: displayRun.runId, bucketIndex: bi, campaignIndex: ci, action: 'start' })
                          : undefined}
                        onForceStopCampaign={canControl
                          ? (ci) => campaignActionMutation.mutate({ runId: displayRun.runId, bucketIndex: bi, campaignIndex: ci, action: 'stop' })
                          : undefined}
                        onSkipCampaign={canControl
                          ? (ci) => campaignActionMutation.mutate({ runId: displayRun.runId, bucketIndex: bi, campaignIndex: ci, action: 'skip' })
                          : undefined}
                        brandedProgress={isCurrentRunDisplayed ? brandedProgressQuery.data?.progress : undefined}
                        brandedQueue={isCurrentRunDisplayed ? brandedQueueQuery.data?.items : undefined}
                        smsRunsMap={hasSmsCampaigns ? smsRunsMap : undefined}
                      />
                    );
                  })}
                </div>
              </div>
            ) : (
              <div className="rounded-2xl border-2 border-dashed border-gray-200 py-16 text-center text-gray-400 text-sm">
                No runs yet.{' '}
                {!plan.isTemplate && (
                  <button
                    type="button"
                    className="text-blue-500 hover:underline"
                    onClick={() => triggerMutation.mutate(undefined)}
                  >
                    Start the first run
                  </button>
                )}
              </div>
            )}

            {/* ── Run history ────────────────────────────────────────────────────── */}
            {runs.length > 0 && (
              <details className="group" open>
                <summary className="cursor-pointer text-sm font-medium text-gray-600 hover:text-gray-800 select-none list-none flex items-center gap-2">
                  <span className="group-open:rotate-90 transition-transform inline-block text-gray-400">▶</span>
                  Run history ({runs.length})
                </summary>
                <div className="mt-3 bg-white rounded-xl border border-gray-200 overflow-hidden">
                  <table className="w-full text-sm">
                    <thead className="bg-gray-50 border-b border-gray-100">
                      <tr>
                        {['Run ID', 'Status', 'Triggered by', 'Date', 'Started', 'Duration'].map((h) => (
                          <th
                            key={h}
                            className="px-4 py-2.5 text-left text-[11px] font-semibold tracking-wider text-gray-400 uppercase"
                          >
                            {h}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-50">
                      {runs.map((r) => {
                        const startDt = r.startedAt ? new Date(r.startedAt) : null;
                        const dateStr = startDt
                          ? startDt.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
                          : '—';
                        return (
                          <tr
                            key={r.runId}
                            className={`cursor-pointer hover:bg-gray-50 transition-colors ${
                              r.runId === displayRun?.runId ? 'bg-blue-50' : ''
                            }`}
                            onClick={() =>
                              setSelectedRunId((prev) => (prev === r.runId ? null : r.runId))
                            }
                          >
                            <td className="px-4 py-2.5 text-xs font-mono text-gray-500">
                              {r.runId.slice(0, 16)}
                            </td>
                            <td className="px-4 py-2.5">
                              <StatusBadge status={r.status} />
                            </td>
                            <td className="px-4 py-2.5 text-xs text-gray-500">
                              {r.triggeredBy ?? 'manual'}
                            </td>
                            <td className="px-4 py-2.5 text-xs text-gray-500">
                              {dateStr}
                            </td>
                            <td className="px-4 py-2.5 text-xs text-gray-500">
                              {fmtTime(r.startedAt)}
                            </td>
                            <td className="px-4 py-2.5 text-xs text-gray-500">
                              {fmtElapsed(r.startedAt, r.completedAt)}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </details>
            )}

            {/* ── Branded campaign history ─────────────────────────────────────────── */}
            {hasBrandedCampaigns && (brandedHistoryQuery.data?.history?.length ?? 0) > 0 && (
              <details className="group" open>
                <summary className="cursor-pointer text-sm font-medium text-gray-600 hover:text-gray-800 select-none list-none flex items-center gap-2">
                  <span className="group-open:rotate-90 transition-transform inline-block text-gray-400">▶</span>
                  Branded dialer history ({brandedHistoryQuery.data!.history.length})
                </summary>
                <div className="mt-3 bg-white rounded-xl border border-gray-200 overflow-hidden">
                  <table className="w-full text-sm">
                    <thead className="bg-gray-50 border-b border-gray-100">
                      <tr>
                        {['Run', 'Campaign', 'Dialed', 'Total', '%', 'Resultado', 'Duración'].map((h) => (
                          <th key={h} className="px-4 py-2.5 text-left text-[11px] font-semibold tracking-wider text-gray-400 uppercase">
                            {h}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-50">
                      {brandedHistoryQuery.data!.history.map((r: BrandedRunSummary) => {
                        const pct = r.totalSeeded > 0
                          ? Math.min(100, Math.round((r.totalDialed / r.totalSeeded) * 100))
                          : 0;
                        return (
                          <tr key={`${r.runId}#${r.campaignId}`} className="hover:bg-gray-50">
                            <td className="px-4 py-2.5 font-mono text-xs text-gray-400">{r.runId.slice(0, 13)}</td>
                            <td className="px-4 py-2.5 text-xs text-gray-700">{r.campaignId}</td>
                            <td className="px-4 py-2.5 text-xs font-medium text-green-600">{r.totalDialed}</td>
                            <td className="px-4 py-2.5 text-xs text-gray-500">{r.totalSeeded}</td>
                            <td className="px-4 py-2.5 text-xs text-gray-500">{r.totalSeeded > 0 ? `${pct}%` : '—'}</td>
                            <td className="px-4 py-2.5">
                              <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium ${
                                r.exitReason === 'queue_drained'
                                  ? 'bg-green-100 text-green-700'
                                  : r.exitReason === 'manually_stopped'
                                    ? 'bg-amber-100 text-amber-700'
                                    : 'bg-gray-100 text-gray-500'
                              }`}>
                                {r.exitReason || '—'}
                              </span>
                            </td>
                            <td className="px-4 py-2.5 text-xs text-gray-400">{fmtElapsed(r.startedAt, r.completedAt)}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </details>
            )}

            {/* ── Plan definition ─────────────────────────────────────────────────── */}
            <details>
              <summary className="cursor-pointer text-xs text-gray-400 hover:text-gray-600 select-none list-none flex items-center gap-1">
                <span className="text-gray-300">▶</span> Plan definition (JSON)
              </summary>
              <pre className="mt-2 text-xs bg-white border border-gray-100 rounded-xl p-4 overflow-x-auto max-h-96 text-gray-600">
                {JSON.stringify(plan, null, 2)}
              </pre>
            </details>
          </div>

          {displayRun && (
            <div className="space-y-6">
              <AgentAvailabilityPanel />
              <DayActivityFeed planId={id!} runId={displayRun.runId} />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
