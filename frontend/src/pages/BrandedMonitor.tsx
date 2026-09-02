import { type ReactNode, useState } from 'react';
import { useQuery, useQueries } from '@tanstack/react-query';

import {
  api,
  type AgentRosterEntry,
  type BrandedCampaignRecord,
  type BrandedMetricSnapshot,
  type BrandedTodaySummary,
  type RoutingProfileSummary,
} from '@/lib/api';
import { elapsedSeconds, elapsedMinutes, formatRuntime, fmtTime } from '@/lib/utils';
import { BRANDED_MONITOR_TEAMS, teamForProfile } from '@/lib/routingProfileTeams';
import { AgentAvailabilityCard } from '@/components/AgentAvailabilityCard';
import { aggregateByRoutingProfile, classifyStaffing, minAvailableFor, STAFFING_RISK_ORDER } from '@/lib/agentRoster';
import { AgentRoster } from './AgentRoster';

// ── Utilities ──────────────────────────────────────────────────────────────────

function todayISO(): string {
  return new Date().toISOString().slice(0, 10);
}

function queueLabel(queueArn: string): string {
  const parts = queueArn.split('/');
  return parts[parts.length - 1] ?? queueArn;
}

// ── Plan grouping ──────────────────────────────────────────────────────────────

type PlanRunStatus = 'RUNNING' | 'COMPLETED' | 'FAILED' | 'MIXED';

interface PlanRunGroup {
  key: string; // planId+runId
  planId: string;
  runId: string;
  planName: string;
  campaigns: BrandedCampaignRecord[];
  status: PlanRunStatus;
  startedAt: string;
}

function groupByPlanRun(campaigns: BrandedCampaignRecord[]): PlanRunGroup[] {
  const map = new Map<string, PlanRunGroup>();
  for (const c of campaigns) {
    const key = `${c.planId}#${c.runId}`;
    if (!map.has(key)) {
      map.set(key, {
        key,
        planId: c.planId,
        runId: c.runId,
        planName: c.planName || c.planId,
        campaigns: [],
        status: 'COMPLETED',
        startedAt: c.startedAt,
      });
    }
    const g = map.get(key)!;
    g.campaigns.push(c);
    if (c.startedAt < g.startedAt) g.startedAt = c.startedAt;
  }
  for (const g of map.values()) {
    const statuses = g.campaigns.map(c => c.status);
    if (statuses.some(s => s === 'RUNNING')) g.status = 'RUNNING';
    else if (statuses.every(s => s === 'ABORTED' || s === 'ERROR')) g.status = 'FAILED';
    else if (statuses.some(s => s === 'ABORTED' || s === 'ERROR')) g.status = 'MIXED';
    else g.status = 'COMPLETED';
  }
  return [...map.values()].sort((a, b) => {
    if (a.status === 'RUNNING' && b.status !== 'RUNNING') return -1;
    if (b.status === 'RUNNING' && a.status !== 'RUNNING') return 1;
    return b.startedAt.localeCompare(a.startedAt);
  });
}

function aggregateMetrics(metrics: (BrandedMetricSnapshot | undefined)[]): {
  placed: number; answered: number; voicemail: number; busy: number; noAnswer: number;
  answerRate: string; voicemailRate: string;
} {
  // API fields are typed as number but can arrive as numeric strings (Decimal
  // round-tripped through JSON) — Number(...) guards against 0 + "13" === "013"
  // string concatenation instead of numeric addition.
  const valid = metrics.filter(Boolean) as BrandedMetricSnapshot[];
  const placed    = valid.reduce((s, m) => s + Number(m.contactsPlaced), 0);
  const answered  = valid.reduce((s, m) => s + Number(m.contactsAnswered), 0);
  const voicemail = valid.reduce((s, m) => s + Number(m.contactsVoicemail), 0);
  const busy      = valid.reduce((s, m) => s + Number(m.contactsBusy), 0);
  const noAnswer  = valid.reduce((s, m) => s + Number(m.contactsNoAnswer), 0);
  return {
    placed, answered, voicemail, busy, noAnswer,
    answerRate:    placed > 0 ? ((answered / placed) * 100).toFixed(1) : '0.0',
    voicemailRate: placed > 0 ? ((voicemail / placed) * 100).toFixed(1) : '0.0',
  };
}

// ── Alert helpers ──────────────────────────────────────────────────────────────

function agentIdleAlert(agent: AgentRosterEntry): 'idle' | 'break' | null {
  const elapsed = elapsedMinutes(agent.statusStartTimestamp);
  if (agent.effectiveStatus === 'Available' && elapsed > 10) return 'idle';
  if (agent.effectiveStatus === 'Unavailable' && !agent.isIntentionalAbsence && elapsed > 20) return 'break';
  return null;
}

function slowPaceAlert(campaign: BrandedCampaignRecord, metric?: BrandedMetricSnapshot): boolean {
  const elapsed = elapsedMinutes(campaign.startedAt);
  const seeded  = campaign.totalSeeded ?? campaign.segmentSize ?? 0;
  const dialed  = campaign.totalDialed ?? metric?.contactsPlaced ?? 0;
  return elapsed > 90 && seeded > 0 && dialed / seeded < 0.1;
}

function lowAnswerRateAlert(metric?: BrandedMetricSnapshot): boolean {
  if (!metric) return false;
  return metric.contactsPlaced > 20 && parseFloat(metric.answerRate) < 5;
}

// ── Primitives ─────────────────────────────────────────────────────────────────

function StatusBadge({ status }: { status: string }): ReactNode {
  const variants: Record<string, string> = {
    RUNNING: 'bg-amber-100 text-amber-700', COMPLETED: 'bg-green-100 text-green-700',
    ABORTED: 'bg-gray-100 text-gray-600',   ERROR: 'bg-red-100 text-red-700',
    MIXED: 'bg-orange-100 text-orange-700',
    Available: 'bg-green-100 text-green-700', 'On Call': 'bg-blue-100 text-blue-700',
    ACW: 'bg-purple-100 text-purple-700',   Unavailable: 'bg-gray-100 text-gray-600',
    Offline: 'bg-gray-100 text-gray-400',
  };
  const labels: Record<string, string> = {
    RUNNING: 'Running', COMPLETED: 'Completed', ABORTED: 'Aborted', ERROR: 'Error', MIXED: 'Partial',
    Unavailable: 'Away', Offline: 'Offline',
  };
  return (
    <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium ${variants[status] ?? 'bg-gray-100 text-gray-600'}`}>
      {labels[status] ?? status}
    </span>
  );
}

function AlertChip({ label, color }: { label: string; color: 'red' | 'amber' }): ReactNode {
  const colors = { red: 'bg-red-100 text-red-700', amber: 'bg-amber-100 text-amber-700' };
  return <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium ${colors[color]}`}>{label}</span>;
}

// ── Daily KPI header ───────────────────────────────────────────────────────────

function KpiHeader({ summary, allMetrics }: {
  summary: BrandedTodaySummary;
  allMetrics: BrandedMetricSnapshot[];
}): ReactNode {
  const totalAnswered  = allMetrics.reduce((s, m) => s + Number(m.contactsAnswered), 0);
  const totalVoicemail = allMetrics.reduce((s, m) => s + Number(m.contactsVoicemail), 0);
  const totalAttempts  = allMetrics.reduce((s, m) => s + Number(m.contactsPlaced), 0) || summary.contactsDialed;
  const failed         = summary.campaigns.filter(c => c.status === 'ABORTED' || c.status === 'ERROR').length;
  const answerRate     = totalAttempts > 0 ? ((totalAnswered / totalAttempts) * 100).toFixed(1) : null;
  const vmRate         = totalAttempts > 0 ? ((totalVoicemail / totalAttempts) * 100).toFixed(1) : null;

  const kpis = [
    { label: 'CAMPAIGNS RUN',   value: summary.total.toString(),              sub: `${summary.completed} completed · ${summary.active} active${failed > 0 ? ` · ${failed} failed` : ''}`, cls: 'text-gray-900' },
    { label: 'CONTACTS DIALED', value: summary.contactsDialed.toLocaleString(), sub: 'today',             cls: 'text-gray-900' },
    { label: 'ATTEMPTS',        value: totalAttempts.toLocaleString(),          sub: 'dials placed',      cls: 'text-gray-900' },
    { label: 'ANSWER RATE',     value: answerRate ? `${answerRate}%` : '—',   sub: totalAnswered > 0 ? `${totalAnswered.toLocaleString()} answered` : 'no data yet', cls: 'text-green-600' },
    { label: 'VOICEMAIL RATE',  value: vmRate ? `${vmRate}%` : '—',           sub: totalVoicemail > 0 ? `${totalVoicemail.toLocaleString()} left` : 'no data yet',  cls: 'text-blue-600' },
  ];
  return (
    <div className="grid grid-cols-5 gap-3">
      {kpis.map(k => (
        <div key={k.label} className="bg-white rounded-xl border border-gray-200 p-4">
          <div className="text-[10px] font-semibold text-gray-400 tracking-widest uppercase">{k.label}</div>
          <div className={`text-2xl font-bold tabular-nums mt-1 ${k.cls}`}>{k.value}</div>
          <div className="text-xs text-gray-400 mt-0.5">{k.sub}</div>
        </div>
      ))}
    </div>
  );
}

// ── Disposition bar ────────────────────────────────────────────────────────────

function DispositionBar({ metric }: { metric: BrandedMetricSnapshot }): ReactNode {
  const placed = metric.contactsPlaced || 1;
  const segs = [
    { key: 'ans',  value: metric.contactsAnswered,   color: 'bg-green-400' },
    { key: 'vm',   value: metric.contactsVoicemail,  color: 'bg-blue-400' },
    { key: 'na',   value: metric.contactsNoAnswer,   color: 'bg-gray-300' },
    { key: 'busy', value: metric.contactsBusy,       color: 'bg-yellow-400' },
    { key: 'fail', value: metric.contactsFailed ?? 0, color: 'bg-red-400' },
  ];
  return (
    <div className="flex h-1.5 rounded-full overflow-hidden gap-px">
      {segs.map(s => s.value > 0 ? (
        <div key={s.key} className={`${s.color} transition-all`} style={{ width: `${Math.round((s.value / placed) * 100)}%` }} />
      ) : null)}
    </div>
  );
}

// ── Campaign card ──────────────────────────────────────────────────────────────

function CampaignCard({
  campaign, metric, compact, selected, onClick, avgPaceSec,
}: {
  campaign: BrandedCampaignRecord;
  metric?: BrandedMetricSnapshot;
  compact?: boolean;
  selected?: boolean;
  onClick?: () => void;
  avgPaceSec?: number;
}): ReactNode {
  // Use segmentSize (original segment target) as denominator so campaigns
  // ended by time still show their full intended scope, not just what was dialed.
  const seeded     = campaign.segmentSize ?? campaign.totalSeeded ?? 0;
  const dialed     = campaign.totalDialed ?? metric?.contactsPlaced ?? 0;
  const pct        = seeded > 0 ? Math.round((dialed / seeded) * 100) : 0;
  const runtimeSec = campaign.status === 'RUNNING'
    ? elapsedSeconds(campaign.startedAt)
    : (campaign.durationSeconds ?? 0);
  const paceSec    = metric && metric.contactsPlaced > 0
    ? Math.round(runtimeSec / metric.contactsPlaced) : null;
  const isFailed   = campaign.status === 'ABORTED' || campaign.status === 'ERROR';
  const isRunning  = campaign.status === 'RUNNING';
  const isSlow     = slowPaceAlert(campaign, metric);
  const noAgents   = metric != null && metric.agentsAvailable === 0 && isRunning;
  const lowAnswer  = lowAnswerRateAlert(metric);
  const answerNum  = parseFloat(metric?.answerRate ?? '0');
  const answerCls  = answerNum >= 20 ? 'text-green-600' : answerNum >= 10 ? 'text-amber-600' : answerNum > 0 ? 'text-red-500' : 'text-gray-400';
  const borderCls  = selected
    ? 'border-amber-400 ring-2 ring-amber-300 bg-white'
    : isFailed ? 'border-red-300 bg-red-50' : noAgents ? 'border-red-300 bg-red-50' : isSlow ? 'border-amber-300 bg-amber-50' : 'border-gray-200 bg-white';
  const dotCls     = isFailed ? 'bg-red-500' : isSlow ? 'bg-amber-500' : isRunning ? 'bg-blue-500' : 'bg-green-500';
  const barCls     = isFailed ? 'bg-red-400' : isRunning ? 'bg-blue-400' : 'bg-green-400';

  return (
    <div
      className={`rounded-xl border p-4 space-y-3 transition-all ${borderCls} ${onClick ? 'cursor-pointer hover:shadow-md' : ''}`}
      onClick={onClick}
    >
      {/* Header */}
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-start gap-2 min-w-0">
          <div className={`mt-1.5 w-2 h-2 rounded-full shrink-0 ${dotCls}`} />
          <div className="min-w-0">
            <div className="font-semibold text-sm text-gray-900 truncate">{campaign.segmentName || campaign.campaignId}</div>
            <div className="text-xs text-gray-500 mt-0.5 truncate">{queueLabel(campaign.queueArn)}</div>
          </div>
        </div>
        <div className="flex flex-col items-end gap-1 shrink-0">
          <StatusBadge status={campaign.status} />
          <div className="flex gap-1 flex-wrap justify-end">
            {noAgents  && <AlertChip label="No agents"    color="red" />}
            {isSlow    && <AlertChip label="Slow pace"    color="amber" />}
            {lowAnswer && <AlertChip label="Low ans rate" color="amber" />}
          </div>
        </div>
      </div>

      {/* Runtime + contacts */}
      {!compact && (isRunning || runtimeSec > 0) && (
        <div className="grid grid-cols-2 gap-3">
          <div>
            <div className="text-[10px] text-gray-400 uppercase font-semibold">Runtime</div>
            <div className="text-lg font-bold tabular-nums text-gray-900">{formatRuntime(runtimeSec)}</div>
          </div>
          <div>
            <div className="text-[10px] text-gray-400 uppercase font-semibold">Contacts</div>
            <div className="text-lg font-bold tabular-nums text-gray-900">{metric?.contactsPlaced ?? dialed}</div>
          </div>
        </div>
      )}

      {!compact && (
        <div className="flex items-center gap-3 text-[11px] text-gray-500">
          <span>Start <span className="font-medium text-gray-700">{fmtTime(campaign.startedAt)}</span></span>
          <span>·</span>
          <span>End <span className="font-medium text-gray-700">{campaign.completedAt ? fmtTime(campaign.completedAt) : (isRunning ? 'Running' : '—')}</span></span>
        </div>
      )}

      {/* Pace */}
      {!compact && paceSec !== null && (
        <div className="text-xs flex items-center gap-2 flex-wrap">
          <span className="text-[10px] text-gray-400 uppercase font-semibold">Pace/contact</span>
          <span className={`font-bold ${isSlow ? 'text-amber-600' : 'text-gray-700'}`}>{paceSec}s</span>
          {avgPaceSec != null && avgPaceSec !== paceSec && (
            <span className={`text-[10px] font-medium ${paceSec <= avgPaceSec ? 'text-green-500' : 'text-amber-500'}`}>
              {paceSec <= avgPaceSec ? '↑ faster' : '↓ slower'} than avg ({avgPaceSec}s)
            </span>
          )}
        </div>
      )}

      {/* Progress */}
      <div>
        <div className="text-xs text-gray-500 mb-1.5">
          <span className="font-medium text-gray-800">{dialed.toLocaleString()} / {seeded > 0 ? seeded.toLocaleString() : '?'}</span>
          <span className="ml-1 text-gray-400">contacts · {pct}%</span>
        </div>
        <div className="h-1.5 rounded-full bg-gray-100 overflow-hidden">
          <div className={`h-full rounded-full transition-all ${barCls}`} style={{ width: `${pct}%` }} />
        </div>
      </div>

      {/* Metrics */}
      {metric && metric.contactsPlaced > 0 && (
        <>
          <div className="flex items-end gap-5">
            <div>
              <div className="text-[10px] text-gray-400 uppercase font-semibold">Attempts</div>
              <div className="font-bold text-gray-800 tabular-nums text-base">{metric.contactsPlaced}</div>
            </div>
            <div>
              <div className="text-[10px] text-gray-400 uppercase font-semibold">Answer</div>
              <div className={`font-bold tabular-nums text-base ${answerCls}`}>{metric.answerRate}%</div>
            </div>
            <div>
              <div className="text-[10px] text-gray-400 uppercase font-semibold">Voicemail</div>
              <div className="font-bold text-blue-600 tabular-nums text-base">{metric.voicemailRate}%</div>
            </div>
          </div>
          <DispositionBar metric={metric} />
        </>
      )}

      {/* Failed reason */}
      {isFailed && campaign.exitReason && (
        <div className="text-xs text-red-600 font-medium bg-red-100 rounded-lg px-2 py-1">
          Exit: {campaign.exitReason}
        </div>
      )}

      {/* Queue/agent status (non-compact, running only) */}
      {!compact && metric && isRunning && (
        <div className="text-xs text-gray-500 flex gap-4 pt-1 border-t border-gray-100">
          <span>
            Agents: <span className={`font-medium ${metric.agentsAvailable === 0 ? 'text-red-600' : 'text-gray-800'}`}>
              {metric.agentsAvailable}/{metric.agentsStaffed}
            </span>
          </span>
          {metric.contactsInQueue > 0 && (
            <span>In queue: <span className="font-medium text-gray-800">{metric.contactsInQueue}</span></span>
          )}
        </div>
      )}

      {onClick && !selected && (
        <div className="text-[10px] text-amber-600 font-medium">Tap to see full detail →</div>
      )}
    </div>
  );
}

// ── Plan run card (compact, shown in the main monitor list) ────────────────────

function PlanGroupCard({
  group,
  metricsMap,
  onClick,
}: {
  group: PlanRunGroup;
  metricsMap: Map<string, BrandedMetricSnapshot>;
  onClick: () => void;
}): ReactNode {
  const groupMetrics   = group.campaigns.map(c => metricsMap.get(c.brandedCampaignId));
  const agg            = aggregateMetrics(groupMetrics);
  const runningCount   = group.campaigns.filter(c => c.status === 'RUNNING').length;
  const completedCount = group.campaigns.filter(c => c.status === 'COMPLETED').length;
  const failedCount    = group.campaigns.filter(c => c.status === 'ABORTED' || c.status === 'ERROR').length;
  const totalSeeded    = group.campaigns.reduce((s, c) => s + Number(c.segmentSize ?? c.totalSeeded ?? 0), 0);
  const isFailed       = group.status === 'FAILED';
  const isRunning      = group.status === 'RUNNING';
  const runtimeSec     = elapsedSeconds(group.startedAt);
  const answerNum      = parseFloat(agg.answerRate);
  const answerCls      = answerNum >= 20 ? 'text-green-600' : answerNum >= 10 ? 'text-amber-600' : answerNum > 0 ? 'text-red-500' : 'text-gray-400';
  const borderCls      = isFailed ? 'border-red-300 bg-red-50' : 'border-gray-200 bg-white';
  const dotCls         = isFailed ? 'bg-red-500' : isRunning ? 'bg-blue-500' : 'bg-green-500';

  return (
    <div
      className={`rounded-xl border p-4 cursor-pointer hover:shadow-md hover:border-amber-300 transition-all ${borderCls}`}
      onClick={onClick}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-start gap-3 min-w-0">
          <div className={`mt-1.5 w-2.5 h-2.5 rounded-full shrink-0 ${dotCls}`} />
          <div className="min-w-0">
            <div className="font-semibold text-sm text-gray-900 truncate">{group.planName}</div>
            <div className="text-xs text-gray-500 mt-0.5 flex items-center gap-2 flex-wrap">
              <span>{group.campaigns.length} campaign{group.campaigns.length > 1 ? 's' : ''}</span>
              {isRunning   && <span className="text-amber-600 font-medium">{runningCount} running</span>}
              {completedCount > 0 && <span className="text-green-600">{completedCount} done</span>}
              {failedCount > 0    && <span className="text-red-600">{failedCount} failed</span>}
              <span className="text-gray-300">·</span>
              <span>started {fmtTime(group.startedAt)}</span>
              {isRunning   && <span>{formatRuntime(runtimeSec)} elapsed</span>}
            </div>
          </div>
        </div>
        <div className="shrink-0"><StatusBadge status={group.status} /></div>
      </div>

      {/* Aggregate metrics */}
      {agg.placed > 0 && (
        <div className="mt-3 grid grid-cols-4 gap-3 pt-3 border-t border-gray-100">
          <div className="text-center">
            <div className="text-[10px] text-gray-400 uppercase font-semibold">Attempts</div>
            <div className="text-base font-bold tabular-nums text-gray-800">{agg.placed.toLocaleString()}</div>
            {totalSeeded > 0 && <div className="text-[10px] text-gray-400">/ {totalSeeded.toLocaleString()}</div>}
          </div>
          <div className="text-center">
            <div className="text-[10px] text-gray-400 uppercase font-semibold">Answer</div>
            <div className={`text-base font-bold tabular-nums ${answerCls}`}>{agg.answerRate}%</div>
            <div className="text-[10px] text-gray-400">{agg.answered.toLocaleString()}</div>
          </div>
          <div className="text-center">
            <div className="text-[10px] text-gray-400 uppercase font-semibold">Voicemail</div>
            <div className="text-base font-bold tabular-nums text-blue-600">{agg.voicemailRate}%</div>
            <div className="text-[10px] text-gray-400">{agg.voicemail.toLocaleString()}</div>
          </div>
          <div className="text-center">
            <div className="text-[10px] text-gray-400 uppercase font-semibold">No answer</div>
            <div className="text-base font-bold tabular-nums text-gray-500">
              {agg.placed > 0 ? ((agg.noAnswer / agg.placed) * 100).toFixed(1) : '0.0'}%
            </div>
            <div className="text-[10px] text-gray-400">{agg.noAnswer.toLocaleString()}</div>
          </div>
        </div>
      )}
      <div className="mt-2 text-[10px] text-amber-600 font-medium">View plan detail →</div>
    </div>
  );
}

// ── Plan Detail View ───────────────────────────────────────────────────────────

function PlanDetailView({
  group,
  onBack,
}: {
  group: PlanRunGroup;
  onBack: () => void;
}): ReactNode {
  const [selectedCampaignId, setSelectedCampaignId] = useState<string | null>(null);

  // Load metrics for ALL campaigns in this plan.
  // Running campaigns poll every 60s; completed ones load once.
  const campaignMetricsQueries = useQueries({
    queries: group.campaigns.map(c => ({
      queryKey: ['branded-metrics', c.brandedCampaignId],
      queryFn:  () => api.brandedMonitor.getCampaignMetrics(c.brandedCampaignId, 1),
      staleTime: 30_000,
      refetchInterval: c.status === 'RUNNING' ? (60_000 as number) : (false as const),
    })),
  });

  const metricsMap = new Map<string, BrandedMetricSnapshot>();
  group.campaigns.forEach((c, i) => {
    const m = campaignMetricsQueries[i]?.data?.metrics?.[0];
    if (m) metricsMap.set(c.brandedCampaignId, m);
  });

  const allMetrics    = [...metricsMap.values()];
  const agg           = aggregateMetrics(allMetrics);
  const totalSeeded   = group.campaigns.reduce((s, c) => s + Number(c.segmentSize ?? c.totalSeeded ?? 0), 0);
  const totalDialed   = group.campaigns.reduce(
    (s, c) => s + Number(c.totalDialed ?? metricsMap.get(c.brandedCampaignId)?.contactsPlaced ?? 0),
    0,
  );
  const allLoaded     = campaignMetricsQueries.every(q => !q.isLoading);

  // Average seconds-per-contact across all campaigns (for pace comparison).
  const avgPaceSec = (() => {
    const paces = group.campaigns.flatMap(c => {
      const m = metricsMap.get(c.brandedCampaignId);
      const rSec = c.status === 'RUNNING' ? elapsedSeconds(c.startedAt) : (c.durationSeconds ?? 0);
      return m && m.contactsPlaced > 0 && rSec > 0 ? [Math.round(rSec / m.contactsPlaced)] : [];
    });
    return paces.length > 1 ? Math.round(paces.reduce((a, b) => a + b, 0) / paces.length) : null;
  })();
  const answerNum     = parseFloat(agg.answerRate);
  const answerCls     = answerNum >= 20 ? 'text-green-600' : answerNum >= 10 ? 'text-amber-600' : answerNum > 0 ? 'text-red-500' : 'text-gray-400';

  const selectedCampaign = selectedCampaignId
    ? group.campaigns.find(c => c.brandedCampaignId === selectedCampaignId)
    : null;
  const selectedMetric = selectedCampaignId ? metricsMap.get(selectedCampaignId) : undefined;

  const sortedCampaigns = [...group.campaigns].sort((a, b) => {
    if (a.status === 'RUNNING' && b.status !== 'RUNNING') return -1;
    if (b.status === 'RUNNING' && a.status !== 'RUNNING') return 1;
    return a.startedAt.localeCompare(b.startedAt);
  });

  return (
    <div className="space-y-5">
      {/* Breadcrumb */}
      <div className="flex items-center gap-3">
        <button
          onClick={onBack}
          className="flex items-center gap-1.5 text-sm text-gray-500 hover:text-gray-800 font-medium transition-colors"
        >
          ← Live Monitor
        </button>
        <div className="h-4 w-px bg-gray-200" />
        <div className="min-w-0">
          <div className="font-semibold text-gray-900 truncate">{group.planName}</div>
          <div className="text-xs text-gray-500">
            {group.campaigns.length} campaigns · started {fmtTime(group.startedAt)} · run {group.runId.slice(0, 8)}
          </div>
        </div>
        <div className="ml-auto shrink-0"><StatusBadge status={group.status} /></div>
      </div>

      {/* Plan-level KPIs */}
      <div className="grid grid-cols-6 gap-3">
        {[
          { label: 'Campaigns',       value: String(group.campaigns.length),        cls: 'text-gray-900' },
          { label: 'Seeded',          value: totalSeeded.toLocaleString(),           cls: 'text-gray-900' },
          { label: 'Attempts',        value: agg.placed.toLocaleString(),            cls: 'text-gray-900' },
          { label: 'Answer rate',     value: `${agg.answerRate}%`,  sub: `${agg.answered} answered`,  cls: answerCls },
          { label: 'Voicemail rate',  value: `${agg.voicemailRate}%`, sub: `${agg.voicemail} left`,   cls: 'text-blue-600' },
          { label: 'No answer',       value: `${agg.placed > 0 ? ((agg.noAnswer / agg.placed) * 100).toFixed(1) : '0.0'}%`, sub: `${agg.noAnswer}`, cls: 'text-gray-500' },
        ].map(k => (
          <div key={k.label} className="bg-white rounded-xl border border-gray-200 p-3">
            <div className="text-[9px] font-semibold text-gray-400 tracking-widest uppercase">{k.label}</div>
            <div className={`text-xl font-bold tabular-nums mt-0.5 ${k.cls}`}>{k.value}</div>
            {'sub' in k && k.sub ? <div className="text-[10px] text-gray-400">{k.sub}</div> : null}
          </div>
        ))}
      </div>

      {/* Plan overall progress bar */}
      {totalSeeded > 0 && (
        <div className="bg-white rounded-xl border border-gray-200 p-4">
          <div className="flex items-center justify-between text-xs text-gray-600 mb-2">
            <span className="font-medium">Plan progress</span>
            <span className="tabular-nums font-medium text-gray-800">
              {totalDialed.toLocaleString()} / {totalSeeded.toLocaleString()} contacts · {Math.round((totalDialed / totalSeeded) * 100)}%
            </span>
          </div>
          <div className="h-2 rounded-full bg-gray-100 overflow-hidden">
            <div
              className="h-full rounded-full bg-blue-400 transition-all"
              style={{ width: `${Math.min(100, Math.round((totalDialed / totalSeeded) * 100))}%` }}
            />
          </div>
        </div>
      )}

      {/* Two-column layout: campaigns list + campaign detail panel */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Campaigns list */}
        <div className="space-y-3">
          <h2 className="text-sm font-semibold text-gray-800 flex items-center gap-2">
            Campaigns
            {!allLoaded && <span className="text-xs text-gray-400 font-normal animate-pulse">loading metrics…</span>}
          </h2>
          {sortedCampaigns.map(c => (
            <CampaignCard
              key={c.brandedCampaignId}
              campaign={c}
              metric={metricsMap.get(c.brandedCampaignId)}
              compact
              selected={selectedCampaignId === c.brandedCampaignId}
              onClick={() => setSelectedCampaignId(
                selectedCampaignId === c.brandedCampaignId ? null : c.brandedCampaignId
              )}
              avgPaceSec={avgPaceSec ?? undefined}
            />
          ))}
        </div>

        {/* Campaign detail panel */}
        <div>
          {!selectedCampaign && (
            <div className="rounded-xl border border-dashed border-gray-200 bg-gray-50 flex items-center justify-center h-52">
              <div className="text-center text-sm text-gray-400">
                <div className="text-2xl mb-2">📊</div>
                <div>Select a campaign to see its detail</div>
              </div>
            </div>
          )}

          {selectedCampaign && !selectedMetric && (
            <div className="rounded-xl border border-gray-200 bg-white p-6 text-center space-y-2">
              <div className="text-sm text-gray-600 font-medium">{selectedCampaign.segmentName}</div>
              <div className="text-xs text-gray-400">No metrics snapshot yet.</div>
              <div className="text-xs text-gray-400">Metrics are written every 60s while the campaign is running.</div>
            </div>
          )}

          {selectedCampaign && selectedMetric && (
            <div className="space-y-4 sticky top-4">
              <div className="flex items-center justify-between">
                <h2 className="text-sm font-semibold text-gray-800">Campaign detail</h2>
                <button onClick={() => setSelectedCampaignId(null)} className="text-xs text-gray-400 hover:text-gray-600">✕ Close</button>
              </div>

              {/* Full campaign card */}
              <CampaignCard campaign={selectedCampaign} metric={selectedMetric} avgPaceSec={avgPaceSec ?? undefined} />

              {/* Detailed disposition breakdown */}
              <div className="bg-white rounded-xl border border-gray-200 p-4 space-y-3">
                <h3 className="text-xs font-semibold text-gray-700 uppercase tracking-wide">Disposition Breakdown</h3>
                {[
                  { label: 'Answered',       value: selectedMetric.contactsAnswered,         color: 'bg-green-400',  textColor: 'text-green-700' },
                  { label: 'Voicemail',      value: selectedMetric.contactsVoicemail,        color: 'bg-blue-400',   textColor: 'text-blue-700' },
                  { label: 'No answer',      value: selectedMetric.contactsNoAnswer,         color: 'bg-gray-300',   textColor: 'text-gray-600' },
                  { label: 'Busy / rejected',value: selectedMetric.contactsBusy,             color: 'bg-yellow-400', textColor: 'text-yellow-700' },
                  { label: 'Failed',         value: selectedMetric.contactsFailed ?? 0,      color: 'bg-red-400',    textColor: 'text-red-700' },
                ].map(d => {
                  const pct = selectedMetric.contactsPlaced > 0
                    ? ((d.value / selectedMetric.contactsPlaced) * 100).toFixed(1) : '0.0';
                  return (
                    <div key={d.label}>
                      <div className="flex items-center justify-between text-xs mb-1">
                        <span className="flex items-center gap-2">
                          <div className={`w-2 h-2 rounded-full ${d.color}`} />
                          <span className="text-gray-600">{d.label}</span>
                        </span>
                        <span className={`font-bold tabular-nums ${d.textColor}`}>
                          {d.value} <span className="font-normal text-gray-400">({pct}%)</span>
                        </span>
                      </div>
                      <div className="h-1.5 rounded-full bg-gray-100 overflow-hidden">
                        <div className={`h-full rounded-full ${d.color}`} style={{ width: `${pct}%` }} />
                      </div>
                    </div>
                  );
                })}
              </div>

              {/* Queue status (only for running campaigns) */}
              {selectedCampaign.status === 'RUNNING' && (
                <div className="bg-white rounded-xl border border-gray-200 p-4">
                  <h3 className="text-xs font-semibold text-gray-700 uppercase tracking-wide mb-3">Queue status</h3>
                  <div className="grid grid-cols-3 gap-3 text-center">
                    <div>
                      <div className={`text-2xl font-bold tabular-nums ${selectedMetric.agentsAvailable === 0 ? 'text-red-600' : 'text-green-600'}`}>
                        {selectedMetric.agentsAvailable}
                      </div>
                      <div className="text-[10px] text-gray-500 uppercase">Available</div>
                    </div>
                    <div>
                      <div className="text-2xl font-bold tabular-nums text-blue-600">{selectedMetric.agentsStaffed}</div>
                      <div className="text-[10px] text-gray-500 uppercase">Staffed</div>
                    </div>
                    <div>
                      <div className="text-2xl font-bold tabular-nums text-gray-700">{selectedMetric.contactsInQueue}</div>
                      <div className="text-[10px] text-gray-500 uppercase">In queue</div>
                    </div>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Agent availability sidebar ─────────────────────────────────────────────────

function AgentAvailabilitySidebar({ agents, allRoutingProfiles, isLoading, lastUpdated, onSelectProfile, onViewAllProfiles }: {
  agents: AgentRosterEntry[];
  allRoutingProfiles: RoutingProfileSummary[];
  isLoading?: boolean;
  lastUpdated?: string;
  onSelectProfile: (profileId: string) => void;
  onViewAllProfiles: () => void;
}): ReactNode {
  const isBranded = (name: string) => (BRANDED_MONITOR_TEAMS as readonly string[]).includes(teamForProfile(name) ?? '');
  const brandedAgents = agents.filter((a) => isBranded(a.routingProfileName));
  const alertCount  = brandedAgents.filter(a => agentIdleAlert(a) !== null).length;

  const rows = aggregateByRoutingProfile(brandedAgents)
    .sort((a, b) =>
      STAFFING_RISK_ORDER[classifyStaffing(a.available, minAvailableFor(a.routingProfileName)).risk]
      - STAFFING_RISK_ORDER[classifyStaffing(b.available, minAvailableFor(b.routingProfileName)).risk],
    );
  const shownIds = new Set(rows.map((r) => r.routingProfileId));
  const zeroAgentCount = allRoutingProfiles.filter((p) => isBranded(p.name) && !shownIds.has(p.id)).length;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-800">Agent availability — Branded teams</h2>
        <div className="flex items-center gap-3">
          <button type="button" onClick={onViewAllProfiles} className="text-[10px] font-medium text-amber-600 hover:text-amber-700">
            All profiles →
          </button>
          {lastUpdated && !isLoading && (
            <span className="text-[10px] text-gray-400">
              {elapsedMinutes(lastUpdated) === 0 ? 'updated just now' : `updated ${elapsedMinutes(lastUpdated)}m ago`}
            </span>
          )}
          {isLoading && <span className="text-[10px] text-gray-400 animate-pulse">Loading…</span>}
        </div>
      </div>

      <div className="flex flex-wrap gap-3">
        {rows.map((row) => (
          <AgentAvailabilityCard key={row.routingProfileId} row={row} onClick={() => onSelectProfile(row.routingProfileId)} />
        ))}
      </div>

      {alertCount > 0 && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 p-3">
          <div className="text-xs text-amber-700 font-medium">
            {alertCount} agent{alertCount > 1 ? 's' : ''} need attention
          </div>
        </div>
      )}

      {zeroAgentCount > 0 && (
        <div className="text-center text-xs text-gray-400">+{zeroAgentCount} profile{zeroAgentCount > 1 ? 's' : ''}</div>
      )}

      {rows.length === 0 && !isLoading && (
        <div className="text-sm text-gray-400 text-center py-6 rounded-xl border border-gray-200 bg-white">No agents online</div>
      )}
    </div>
  );
}

// ── Live Monitor View ──────────────────────────────────────────────────────────

type CampaignTab = 'active' | 'completed' | 'failed' | 'all';

const CAMPAIGN_TABS: { key: CampaignTab; label: string }[] = [
  { key: 'active',    label: 'Active' },
  { key: 'completed', label: 'Completed' },
  { key: 'failed',    label: 'Failed' },
  { key: 'all',       label: 'All' },
];

const LEGEND = [
  { color: 'bg-green-400',  label: 'Answered' },
  { color: 'bg-blue-400',   label: 'Voicemail' },
  { color: 'bg-gray-300',   label: 'No answer' },
  { color: 'bg-yellow-400', label: 'Busy' },
  { color: 'bg-red-400',    label: 'Failed' },
];

function LiveView({ date, onDateChange, onSelectAgentProfile, onViewAllProfiles }: {
  date: string;
  onDateChange: (d: string) => void;
  onSelectAgentProfile: (profileId: string) => void;
  onViewAllProfiles: () => void;
}): ReactNode {
  const [campaignTab,    setCampaignTab]    = useState<CampaignTab>('active');
  const [selectedGroup,  setSelectedGroup]  = useState<PlanRunGroup | null>(null);
  const [plansCollapsed, setPlansCollapsed] = useState(false);

  const todayQuery = useQuery({
    queryKey: ['branded-today', date],
    queryFn:  () => api.brandedMonitor.getTodaySummary(date),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  const agentQuery = useQuery({
    queryKey: ['branded-agents'],
    queryFn:  () => api.brandedMonitor.getAgentRoster(),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  const summary      = todayQuery.data;
  const allCampaigns = summary?.campaigns ?? [];

  const byTab: Record<CampaignTab, BrandedCampaignRecord[]> = {
    active:    allCampaigns.filter(c => c.status === 'RUNNING'),
    completed: allCampaigns.filter(c => c.status === 'COMPLETED'),
    failed:    allCampaigns.filter(c => c.status === 'ABORTED' || c.status === 'ERROR'),
    all:       allCampaigns,
  };

  // Poll active campaign metrics at 60s; load completed campaign metrics once (for KPI header totals).
  const activeCampaigns    = byTab.active;
  const completedCampaigns = byTab.completed;

  const activeMetricsQueries = useQueries({
    queries: activeCampaigns.map(c => ({
      queryKey: ['branded-metrics', c.brandedCampaignId],
      queryFn:  () => api.brandedMonitor.getCampaignMetrics(c.brandedCampaignId, 1),
      refetchInterval: 60_000,
      staleTime: 30_000,
    })),
  });
  const completedMetricsQueries = useQueries({
    queries: completedCampaigns.map(c => ({
      queryKey: ['branded-metrics', c.brandedCampaignId],
      queryFn:  () => api.brandedMonitor.getCampaignMetrics(c.brandedCampaignId, 1),
      staleTime: 5 * 60_000,
      refetchInterval: false as const,
    })),
  });

  const metricsMap = new Map<string, BrandedMetricSnapshot>();
  activeCampaigns.forEach((c, i) => {
    const m = activeMetricsQueries[i]?.data?.metrics?.[0];
    if (m) metricsMap.set(c.brandedCampaignId, m);
  });
  completedCampaigns.forEach((c, i) => {
    const m = completedMetricsQueries[i]?.data?.metrics?.[0];
    if (m && !metricsMap.has(c.brandedCampaignId)) metricsMap.set(c.brandedCampaignId, m);
  });

  const allMetrics = [...metricsMap.values()];
  const planGroups = groupByPlanRun(byTab[campaignTab]);

  // Plan detail drill-down
  if (selectedGroup) {
    return <PlanDetailView group={selectedGroup} onBack={() => setSelectedGroup(null)} />;
  }

  return (
    <div className="space-y-4">
      {/* KPI header */}
      {summary && <KpiHeader summary={summary} allMetrics={allMetrics} />}

      {/* Toolbar */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-3 flex-wrap">
          <input
            type="date" value={date}
            onChange={e => onDateChange(e.target.value)}
            className="text-sm border border-gray-200 rounded-lg px-3 py-1.5"
          />
          <div className="flex rounded-lg border border-gray-200 overflow-hidden text-xs">
            {CAMPAIGN_TABS.map(t => {
              const count    = groupByPlanRun(byTab[t.key]).length;
              const isActive = campaignTab === t.key;
              const isFail   = t.key === 'failed' && byTab[t.key].length > 0;
              return (
                <button
                  key={t.key}
                  onClick={() => setCampaignTab(t.key)}
                  className={`px-3 py-1.5 font-medium transition-colors ${
                    isActive
                      ? isFail ? 'bg-red-50 text-red-700' : 'bg-amber-50 text-amber-700'
                      : 'bg-white text-gray-500 hover:text-gray-700'
                  }`}
                >
                  {t.label} <span className="ml-1 opacity-60">{count}</span>
                </button>
              );
            })}
          </div>
        </div>
        <div className="flex items-center gap-3 text-[10px] text-gray-500 flex-wrap">
          {LEGEND.map(l => (
            <div key={l.label} className="flex items-center gap-1">
              <div className={`w-2 h-2 rounded-full ${l.color}`} />
              {l.label}
            </div>
          ))}
          <button
            onClick={() => setPlansCollapsed(c => !c)}
            className="ml-auto text-xs text-gray-500 hover:text-gray-700 border border-gray-200 rounded-lg px-2.5 py-1 font-medium"
            title={plansCollapsed ? 'Show plans panel' : 'Collapse plans panel'}
          >
            {plansCollapsed ? '◀ Plans' : 'Plans ▶'}
          </button>
        </div>
      </div>

      {/* Main grid: plan groups + agent sidebar */}
      <div className={`grid grid-cols-1 ${plansCollapsed ? 'lg:grid-cols-1' : 'lg:grid-cols-3'} gap-4`}>
        {!plansCollapsed && (
          <div className="lg:col-span-2 space-y-3">
            {todayQuery.isLoading && (
              <div className="text-sm text-gray-400 py-10 text-center">Loading campaigns…</div>
            )}
            {!todayQuery.isLoading && planGroups.length === 0 && (
              <div className="text-sm text-gray-400 py-10 text-center">
                No {campaignTab === 'all' ? '' : campaignTab} plans today.
              </div>
            )}
            {planGroups.map(group => (
              <PlanGroupCard
                key={group.key}
                group={group}
                metricsMap={metricsMap}
                onClick={() => setSelectedGroup(group)}
              />
            ))}
          </div>
        )}
        <div className={plansCollapsed ? 'lg:col-span-1' : ''}>
          <AgentAvailabilitySidebar
            agents={agentQuery.data?.agents ?? []}
            allRoutingProfiles={agentQuery.data?.allRoutingProfiles ?? []}
            isLoading={agentQuery.isLoading}
            lastUpdated={agentQuery.data?.lastUpdated}
            onSelectProfile={onSelectAgentProfile}
            onViewAllProfiles={onViewAllProfiles}
          />
        </div>
      </div>
    </div>
  );
}

// ── History View ───────────────────────────────────────────────────────────────

function exitColor(reason?: string): string {
  if (reason === 'queue_drained')    return 'bg-green-50 text-green-700';
  if (reason === 'manually_stopped') return 'bg-amber-50 text-amber-700';
  return 'bg-red-50 text-red-700';
}

function HistoryView({ date, onDateChange, onNavigateLive }: {
  date: string;
  onDateChange: (d: string) => void;
  onNavigateLive: () => void;
}): ReactNode {
  const todayQuery = useQuery({
    queryKey: ['branded-today', date],
    queryFn:  () => api.brandedMonitor.getTodaySummary(date),
    staleTime: 60_000,
  });
  const campaigns = todayQuery.data?.campaigns ?? [];
  const finished  = campaigns.filter(c => c.status !== 'RUNNING');

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <input
          type="date" value={date}
          onChange={e => onDateChange(e.target.value)}
          className="text-sm border border-gray-200 rounded-lg px-3 py-1.5"
        />
        <span className="text-sm text-gray-500">{finished.length} campaign{finished.length !== 1 ? 's' : ''} completed</span>
      </div>
      <div className="overflow-x-auto rounded-xl border border-gray-200 bg-white">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 border-b border-gray-200">
            <tr>
              {['Plan', 'Segment', 'Started', 'Duration', 'Seeded', 'Dialed', 'Result'].map(h => (
                <th key={h} className="px-4 py-2.5 text-left text-xs font-medium text-gray-500">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {finished.map(c => (
              <tr
                key={c.sk}
                className="hover:bg-gray-50 transition-colors cursor-pointer"
                onClick={onNavigateLive}
                title="View in Live Monitor"
              >
                <td className="px-4 py-2.5 font-medium text-gray-900 hover:text-amber-700">{c.planName || c.campaignId}</td>
                <td className="px-4 py-2.5 text-gray-500 max-w-[200px] truncate">{c.segmentName}</td>
                <td className="px-4 py-2.5 tabular-nums text-gray-700">{fmtTime(c.startedAt)}</td>
                <td className="px-4 py-2.5 tabular-nums text-gray-700">{c.durationSeconds != null ? `${Math.round(c.durationSeconds / 60)}m` : '—'}</td>
                <td className="px-4 py-2.5 tabular-nums text-gray-700">{c.totalSeeded ?? '—'}</td>
                <td className="px-4 py-2.5 tabular-nums text-gray-700">{c.totalDialed ?? '—'}</td>
                <td className="px-4 py-2.5">
                  <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium ${exitColor(c.exitReason)}`}>
                    {c.exitReason ?? c.status}
                  </span>
                </td>
              </tr>
            ))}
            {finished.length === 0 && (
              <tr>
                <td colSpan={7} className="px-4 py-8 text-center text-sm text-gray-500">
                  No completed campaigns for this date.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Page root ──────────────────────────────────────────────────────────────────

type Tab = 'live' | 'agents' | 'history';
const TAB_LABELS: Record<Tab, string> = { live: 'Live Monitor', agents: 'Agents', history: 'History' };

export function BrandedMonitor(): ReactNode {
  const [tab, setTab] = useState<Tab>(() => {
    const sp = new URLSearchParams(window.location.search);
    const t = sp.get('tab');
    return t === 'agents' || t === 'history' || t === 'live' ? t : 'live';
  });
  const [agentsPreset, setAgentsPreset] = useState<{ team?: string; profileId?: string }>(() => {
    const sp = new URLSearchParams(window.location.search);
    return { team: sp.get('team') ?? undefined, profileId: sp.get('profile') ?? undefined };
  });
  const [date, setDate] = useState(todayISO());

  return (
    <div className="p-6 space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold text-gray-900">Branded Monitor</h1>
          <p className="text-xs text-gray-500 mt-0.5">Live metrics and audit trail for branded dialer campaigns</p>
        </div>
        <div className="flex rounded-lg border border-gray-200 overflow-hidden text-sm">
          {(Object.keys(TAB_LABELS) as Tab[]).map(t => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`px-4 py-1.5 font-medium transition-colors ${tab === t ? 'bg-amber-50 text-amber-700' : 'bg-white text-gray-500 hover:text-gray-700'}`}
            >
              {TAB_LABELS[t]}
            </button>
          ))}
        </div>
      </div>

      {tab === 'live'    && (
        <LiveView
          date={date}
          onDateChange={setDate}
          onSelectAgentProfile={(profileId) => {
            setAgentsPreset({ profileId });
            setTab('agents');
          }}
          onViewAllProfiles={() => {
            setAgentsPreset({});
            setTab('agents');
          }}
        />
      )}
      {tab === 'agents'  && (
        <AgentRoster initialTeamFilter={agentsPreset.team} initialProfileFilter={agentsPreset.profileId} />
      )}
      {tab === 'history' && <HistoryView date={date} onDateChange={setDate} onNavigateLive={() => setTab('live')} />}
    </div>
  );
}
