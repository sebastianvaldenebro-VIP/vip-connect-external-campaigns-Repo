import { useState, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';

import { Badge, Spinner } from '@/components/ui';
import { api, type PlanSummaryV2, type PlanTrigger, type PlanRunV2 } from '@/lib/api';
import { RunStatusBadge } from '@/pages/Plans';

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtDT(iso?: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  return d.toLocaleString('en-US', {
    month: 'numeric',
    day: 'numeric',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

// ── iOS-style toggle ──────────────────────────────────────────────────────────

function IOSToggle({ enabled, onChange }: { enabled: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        onChange(!enabled);
      }}
      title={enabled ? 'Enabled — click to disable' : 'Disabled — click to enable'}
      style={{
        width: 38,
        height: 22,
        borderRadius: 9999,
        background: enabled ? '#22c55e' : '#d1d5db',
        position: 'relative',
        cursor: 'pointer',
        transition: 'background 150ms',
        border: 0,
        padding: 0,
        flexShrink: 0,
        display: 'inline-block',
      }}
    >
      <span
        style={{
          position: 'absolute',
          top: 2,
          left: enabled ? 18 : 2,
          width: 18,
          height: 18,
          borderRadius: 9999,
          background: '#fff',
          boxShadow: '0 1px 2px rgba(0,0,0,0.1)',
          transition: 'left 150ms',
        }}
      />
    </button>
  );
}

// ── Trigger badge ─────────────────────────────────────────────────────────────

function TriggerBadge({ plan, planNameMap }: { plan: PlanSummaryV2; planNameMap: Record<string, string> }) {
  const t = plan.trigger;
  if (t.type === 'manual')
    return (
      <Badge tone="muted">
        <span className="mr-0.5">▶</span> Manual
      </Badge>
    );
  if (t.type === 'time')
    return (
      <Badge tone="default">
        <span className="mr-0.5">⏰</span> {t.time} COT
      </Badge>
    );
  if (t.type === 'on_plan_complete') {
    const upName = planNameMap[t.planId] ?? '…';
    return (
      <span className="inline-flex items-center rounded border border-purple-200 bg-purple-50 px-1.5 py-0.5 text-[11px] font-medium text-purple-700">
        <span className="mr-0.5">🔗</span> After {upName}
      </span>
    );
  }
  return null;
}

// ── Bucket chain pill row ─────────────────────────────────────────────────────

function BucketChain({ buckets }: { buckets: PlanSummaryV2['buckets'] }) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {buckets.map((b, i) => (
        <span key={b.id ?? i} className="flex items-center gap-1.5">
          <span className="inline-flex items-center gap-1.5 rounded-md border border-green-200 bg-green-50 px-2.5 py-0.5 text-[11px] font-medium text-green-700">
            <span>{b.name}</span>
            <span className="font-mono opacity-70">
              · {b.run_mode === 'time_based' ? `${b.duration_minutes}m` : 'auto'}
            </span>
            <span className="font-mono opacity-50">· {b.campaigns?.length ?? 0}c</span>
          </span>
          {i < buckets.length - 1 && (
            <span className="text-xs text-gray-300">→</span>
          )}
        </span>
      ))}
    </div>
  );
}

// ── Enhanced plan card ────────────────────────────────────────────────────────

function TodayPlanCard({
  plan,
  planNameMap,
  enabled,
  onToggle,
  onRun,
  onEdit,
  onDuplicate,
  onDelete,
  isTriggering,
  isDuplicating,
}: {
  plan: PlanSummaryV2;
  planNameMap: Record<string, string>;
  enabled: boolean;
  onToggle: (v: boolean) => void;
  onRun: () => void;
  onEdit: () => void;
  onDuplicate: () => void;
  onDelete: () => void;
  isTriggering: boolean;
  isDuplicating: boolean;
}) {
  const [showBuckets, setShowBuckets] = useState(false);
  const totalCampaigns = plan.buckets.reduce((s, b) => s + (b.campaigns?.length ?? 0), 0);
  const run = plan.latestRun as PlanRunV2 | undefined;
  const isRunning = run?.status === 'running';

  return (
    <div
      className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm transition-opacity"
      style={{ opacity: enabled ? 1 : 0.72 }}
    >
      <div className="grid items-center gap-3 px-4 py-3.5"
        style={{ gridTemplateColumns: 'auto 1fr auto' }}>
        {/* Toggle */}
        <IOSToggle enabled={enabled} onChange={onToggle} />

        {/* Info */}
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-[15px] font-semibold text-gray-900">{plan.name}</span>
            <TriggerBadge plan={plan} planNameMap={planNameMap} />
            {run && <RunStatusBadge status={run.status} />}
          </div>
          <div className="flex items-center gap-2 mt-1 text-xs text-gray-400 flex-wrap">
            <span>
              {plan.buckets.length} bucket{plan.buckets.length > 1 ? 's' : ''} · {totalCampaigns}{' '}
              campaign{totalCampaigns > 1 ? 's' : ''}
            </span>
            {run?.startedAt && (
              <span>· last run {fmtDT(run.startedAt)}</span>
            )}
            <button
              type="button"
              onClick={() => setShowBuckets((v) => !v)}
              className="text-blue-500 hover:underline inline-flex items-center gap-0.5"
            >
              {showBuckets ? 'Hide buckets' : 'Show buckets'}
              <span className="text-[10px]">{showBuckets ? ' ▲' : ' ▼'}</span>
            </button>
          </div>
        </div>

        {/* Actions */}
        <div className="flex items-center gap-1.5 shrink-0">
          <button
            type="button"
            onClick={onEdit}
            className="rounded-lg border border-gray-200 px-3 py-1.5 text-xs font-medium hover:bg-gray-50 transition-colors"
          >
            Edit
          </button>
          <button
            type="button"
            onClick={onDuplicate}
            disabled={isDuplicating}
            className="rounded-lg border border-gray-200 px-3 py-1.5 text-xs font-medium hover:bg-gray-50 transition-colors disabled:opacity-50"
          >
            {isDuplicating ? <Spinner /> : 'Duplicate'}
          </button>
          <button
            type="button"
            onClick={onRun}
            disabled={isRunning || isTriggering}
            className="inline-flex items-center gap-1 rounded-lg bg-blue-600 text-white px-3 py-1.5 text-xs font-medium hover:bg-blue-700 transition-colors disabled:opacity-50"
          >
            {isTriggering ? (
              <Spinner />
            ) : (
              <>
                <span className="text-[10px]">▶</span>
                {isRunning ? 'Running' : 'Run now'}
              </>
            )}
          </button>
          <button
            type="button"
            onClick={onDelete}
            className="rounded-lg px-2 py-1.5 text-xs text-red-400 hover:text-red-600 hover:bg-red-50 transition-colors"
            title="Delete plan"
          >
            ✕
          </button>
        </div>
      </div>

      {/* Bucket chain */}
      {showBuckets && (
        <div className="px-4 pb-3.5 pt-0.5 border-t border-gray-50 bg-gray-50/60">
          <BucketChain buckets={plan.buckets} />
        </div>
      )}
    </div>
  );
}

// ── Summary stat chip ─────────────────────────────────────────────────────────

function StatChip({
  label,
  value,
  color,
}: {
  label: string;
  value: number;
  color: string;
}) {
  return (
    <div className="flex items-center gap-2.5 rounded-xl border border-gray-200 bg-white px-3.5 py-2.5">
      <span
        className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-sm font-bold font-mono"
        style={{ background: color + '22', color }}
      >
        {value}
      </span>
      <span className="text-xs font-medium text-gray-600">{label}</span>
    </div>
  );
}

// ── Filter pill ───────────────────────────────────────────────────────────────

function FilterPill({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        'inline-flex items-center rounded-full border px-3 py-1.5 text-xs font-medium transition-colors',
        active
          ? 'border-blue-400 bg-blue-50 text-blue-700'
          : 'border-gray-200 bg-white text-gray-500 hover:border-gray-300',
      ].join(' ')}
    >
      {label}
    </button>
  );
}

// ── Sub-section header ────────────────────────────────────────────────────────

function SubSectionHeader({ label }: { label: string }) {
  return (
    <div className="inline-flex items-center rounded-full bg-gray-100 px-3 py-1 text-[11px] font-semibold uppercase tracking-wider text-gray-500 mb-2">
      {label}
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export function PlansToday(): ReactNode {
  const navigate = useNavigate();
  const qc = useQueryClient();

  const [search, setSearch] = useState('');
  const [filterType, setFilterType] = useState<'all' | 'time' | 'on_plan_complete' | 'manual'>('all');

  // Local state: planId → prevTrigger (saved when toggling off)
  const [prevTriggers, setPrevTriggers] = useState<Record<string, PlanTrigger>>({});

  const list = useQuery({ queryKey: ['plans'], queryFn: () => api.plans.listV2() });

  const remove = useMutation({
    mutationFn: (id: string) => api.plans.delete(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['plans'] }),
  });

  const trigger = useMutation({
    mutationFn: (id: string) => api.plans.triggerRunV2(id),
    onSuccess: (_run, id) => {
      qc.invalidateQueries({ queryKey: ['plans'] });
      navigate(`/plans/${encodeURIComponent(id)}`);
    },
  });

  const [duplicatingId, setDuplicatingId] = useState<string | null>(null);
  const duplicate = useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) => api.plans.duplicate(id, name),
    onSuccess: (plan) => {
      setDuplicatingId(null);
      qc.invalidateQueries({ queryKey: ['plans'] });
      navigate(`/plans/${encodeURIComponent(plan.planId)}/edit`);
    },
    onError: () => setDuplicatingId(null),
  });

  const updateTriggerMut = useMutation({
    mutationFn: ({ id, t }: { id: string; t: PlanTrigger }) =>
      api.plans.updateV2(id, { trigger: t }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['plans'] }),
  });

  const allPlans = (list.data?.plans ?? []).filter((p) => !p.isTemplate && !p.is_template);
  const planNameMap = Object.fromEntries(allPlans.map((p) => [p.planId, p.name]));

  // Filter
  const filtered = allPlans.filter((p) => {
    if (search && !p.name.toLowerCase().includes(search.toLowerCase())) return false;
    if (filterType !== 'all' && p.trigger.type !== filterType) return false;
    return true;
  });

  const scheduled = filtered.filter((p) => p.trigger.type === 'time');
  const chained = filtered.filter((p) => p.trigger.type === 'on_plan_complete');
  const manual = filtered.filter((p) => p.trigger.type === 'manual');

  // Summary counts (from allPlans, not filtered)
  const activeCount = allPlans.filter((p) => p.trigger.type !== 'manual').length;
  const scheduledCount = allPlans.filter((p) => p.trigger.type === 'time').length;
  const chainedCount = allPlans.filter((p) => p.trigger.type === 'on_plan_complete').length;
  const manualCount = allPlans.filter((p) => p.trigger.type === 'manual').length;

  function handleToggle(plan: PlanSummaryV2, enabled: boolean) {
    if (!enabled) {
      setPrevTriggers((prev) => ({ ...prev, [plan.planId]: plan.trigger }));
      updateTriggerMut.mutate({ id: plan.planId, t: { type: 'manual' } });
    } else {
      const restored = prevTriggers[plan.planId] ?? { type: 'time', time: '08:00' };
      updateTriggerMut.mutate({ id: plan.planId, t: restored as PlanTrigger });
    }
  }

  function handleDuplicate(plan: PlanSummaryV2) {
    const name = prompt(`New plan name (copying "${plan.name}"):`, `${plan.name} (copy)`);
    if (!name?.trim()) return;
    setDuplicatingId(plan.planId);
    duplicate.mutate({ id: plan.planId, name: name.trim() });
  }

  if (list.isPending) {
    return (
      <p className="inline-flex items-center gap-2 text-sm text-muted-foreground">
        <Spinner /> Loading…
      </p>
    );
  }
  if (list.isError) {
    return <p className="text-sm text-destructive">{(list.error as Error).message}</p>;
  }

  function renderCard(plan: PlanSummaryV2) {
    const enabled = plan.trigger.type !== 'manual';
    return (
      <TodayPlanCard
        key={plan.planId}
        plan={plan}
        planNameMap={planNameMap}
        enabled={enabled}
        onToggle={(v) => handleToggle(plan, v)}
        onRun={() => trigger.mutate(plan.planId)}
        onEdit={() => navigate(`/plans/${encodeURIComponent(plan.planId)}/edit`)}
        onDuplicate={() => handleDuplicate(plan)}
        onDelete={() => {
          if (confirm(`Delete plan "${plan.name}"?`)) remove.mutate(plan.planId);
        }}
        isTriggering={trigger.isPending && trigger.variables === plan.planId}
        isDuplicating={duplicatingId === plan.planId}
      />
    );
  }

  return (
    <div className="flex flex-col gap-5">
      {/* Header */}
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h2 className="text-lg font-semibold tracking-tight">Today's plan</h2>
          <p className="mt-0.5 text-sm text-muted-foreground">
            Daily outbound plans. Toggle a plan off to skip it today without deleting.
          </p>
        </div>
        <button
          type="button"
          onClick={() => navigate('/plans/new')}
          className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 text-white px-3.5 py-2 text-sm font-medium hover:bg-blue-700 transition-colors"
        >
          <span className="text-xs">+</span> New plan
        </button>
      </div>

      {/* Summary stats */}
      <div className="flex flex-wrap gap-2.5">
        <StatChip label="Active" value={activeCount} color="#15803d" />
        <StatChip label="Scheduled today" value={scheduledCount} color="#1d4ed8" />
        <StatChip label="Triggered by other plans" value={chainedCount} color="#6b21a8" />
        <StatChip label="Manual only" value={manualCount} color="#374151" />
      </div>

      {/* Search + filter */}
      <div className="flex items-center gap-2 flex-wrap">
        <div className="relative flex-1 max-w-xs">
          <span className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 text-xs">🔍</span>
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search plans…"
            className="w-full rounded-lg border border-gray-200 bg-white py-1.5 pl-8 pr-3 text-sm focus:outline-none focus:ring-1 focus:ring-blue-300"
          />
        </div>
        <FilterPill label="All" active={filterType === 'all'} onClick={() => setFilterType('all')} />
        <FilterPill
          label="Scheduled"
          active={filterType === 'time'}
          onClick={() => setFilterType('time')}
        />
        <FilterPill
          label="On plan complete"
          active={filterType === 'on_plan_complete'}
          onClick={() => setFilterType('on_plan_complete')}
        />
        <FilterPill
          label="Manual"
          active={filterType === 'manual'}
          onClick={() => setFilterType('manual')}
        />
      </div>

      {trigger.isError && (
        <p className="text-sm text-destructive">{(trigger.error as Error).message}</p>
      )}

      {/* Plan groups */}
      {filtered.length === 0 ? (
        <div className="rounded-xl border border-dashed border-gray-200 p-8 text-center text-sm text-gray-400">
          No plans match your filters.
        </div>
      ) : (
        <div className="flex flex-col gap-5">
          {scheduled.length > 0 && (
            <div>
              <SubSectionHeader label="⏰  Scheduled today" />
              <div className="flex flex-col gap-2.5">{scheduled.map(renderCard)}</div>
            </div>
          )}
          {chained.length > 0 && (
            <div>
              <SubSectionHeader label="🔗  Triggered by another plan" />
              <div className="flex flex-col gap-2.5">{chained.map(renderCard)}</div>
            </div>
          )}
          {manual.length > 0 && (
            <div>
              <SubSectionHeader label="▶  Manual only — run on demand" />
              <div className="flex flex-col gap-2.5">{manual.map(renderCard)}</div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
