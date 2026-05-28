import { useState, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { Spinner } from '@/components/ui';
import { api, type PlanSummaryV2, type PlanTrigger } from '@/lib/api';

const DOW_STR = ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT'] as const;
const DOW_LABEL = ['S', 'M', 'T', 'W', 'T', 'F', 'S'];
const DOW_FULL = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];

function daysToIndices(days: string[]): number[] {
  return days
    .map((d) => DOW_STR.indexOf(d as (typeof DOW_STR)[number]))
    .filter((i) => i >= 0)
    .sort((a, b) => a - b);
}

function indicesToDays(indices: number[]): string[] {
  return indices.map((i) => DOW_STR[i]).filter(Boolean);
}

function fmtLastRun(iso?: string): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '—';
  return d.toLocaleString('en-US', {
    month: 'numeric',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

type RowLocal = {
  trigger: PlanTrigger;
  prevTrigger: PlanTrigger | null;
  whStart: string;
  whEnd: string;
  whDays: number[];
};

function initRow(plan: PlanSummaryV2): RowLocal {
  const wh = plan.workingHours;
  return {
    trigger: plan.trigger,
    prevTrigger: null,
    whStart: wh?.startTime ?? '08:00',
    whEnd: wh?.endTime ?? '19:00',
    whDays: daysToIndices(wh?.days ?? ['MON', 'TUE', 'WED', 'THU', 'FRI']),
  };
}

// ── Shared UI ─────────────────────────────────────────────────────────────────

function IOSToggle({ enabled, onChange }: { enabled: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      type="button"
      onClick={() => onChange(!enabled)}
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

function DOWPicker({ value, onChange }: { value: number[]; onChange: (v: number[]) => void }) {
  return (
    <div className="flex gap-0.5">
      {DOW_LABEL.map((l, i) => {
        const sel = value.includes(i);
        return (
          <button
            key={i}
            type="button"
            title={DOW_FULL[i]}
            onClick={() =>
              onChange(
                sel
                  ? value.filter((x) => x !== i)
                  : [...value, i].sort((a, b) => a - b),
              )
            }
            className={[
              'h-6 w-6 rounded text-[10px] font-semibold transition-colors',
              sel
                ? 'bg-blue-100 text-blue-700 border border-blue-300'
                : 'bg-white text-gray-400 border border-gray-200 hover:border-gray-300',
            ].join(' ')}
          >
            {l}
          </button>
        );
      })}
    </div>
  );
}

function TriggerTypeSelect({
  trigger,
  plans,
  planId,
  onChange,
}: {
  trigger: PlanTrigger;
  plans: PlanSummaryV2[];
  planId: string;
  onChange: (t: PlanTrigger) => void;
}) {
  const otherPlans = plans.filter((p) => p.planId !== planId);
  return (
    <div className="flex flex-col gap-1.5">
      <select
        value={trigger.type}
        onChange={(e) => {
          const v = e.target.value;
          if (v === 'time')
            onChange({ type: 'time', time: trigger.type === 'time' ? trigger.time : '08:00' });
          else if (v === 'on_plan_complete')
            onChange({
              type: 'on_plan_complete',
              planId:
                trigger.type === 'on_plan_complete'
                  ? trigger.planId
                  : (otherPlans[0]?.planId ?? ''),
              repeat: false,
            });
          else onChange({ type: 'manual' });
        }}
        className="border border-gray-200 rounded-md px-2 py-1 text-xs font-medium bg-white cursor-pointer focus:outline-none focus:ring-1 focus:ring-blue-300"
      >
        <option value="time">⏰ Scheduled</option>
        <option value="on_plan_complete">🔗 After plan</option>
        <option value="manual">▶ Manual</option>
      </select>
      {trigger.type === 'on_plan_complete' && (
        <select
          value={trigger.planId}
          onChange={(e) =>
            onChange({
              ...(trigger as Extract<PlanTrigger, { type: 'on_plan_complete' }>),
              planId: e.target.value,
            })
          }
          className="border border-gray-200 rounded-md px-2 py-1 text-[11px] bg-white font-mono cursor-pointer focus:outline-none focus:ring-1 focus:ring-blue-300 max-w-[180px]"
        >
          {otherPlans.map((p) => (
            <option key={p.planId} value={p.planId}>
              {p.name}
            </option>
          ))}
        </select>
      )}
    </div>
  );
}

// ── Scheduler row ─────────────────────────────────────────────────────────────

function SchedulerRow({
  plan,
  allPlans,
  row,
  saving,
  onToggle,
  onTriggerChange,
  onTimeChange,
  onWhStartChange,
  onWhEndChange,
  onWhDaysChange,
}: {
  plan: PlanSummaryV2;
  allPlans: PlanSummaryV2[];
  row: RowLocal;
  saving: boolean;
  onToggle: (enabled: boolean) => void;
  onTriggerChange: (t: PlanTrigger) => void;
  onTimeChange: (time: string) => void;
  onWhStartChange: (v: string) => void;
  onWhEndChange: (v: string) => void;
  onWhDaysChange: (indices: number[]) => void;
}) {
  const enabled = row.trigger.type !== 'manual';

  return (
    <tr
      className="border-b border-gray-100 last:border-0 transition-opacity"
      style={{ opacity: enabled ? 1 : 0.55 }}
    >
      {/* On */}
      <td className="px-4 py-3.5">
        <IOSToggle enabled={enabled} onChange={onToggle} />
      </td>

      {/* Plan */}
      <td className="px-4 py-3.5">
        <div className="text-sm font-semibold text-gray-900">{plan.name}</div>
        <div className="text-[11px] text-gray-400 mt-0.5">
          {plan.buckets.length} bucket{plan.buckets.length > 1 ? 's' : ''} ·{' '}
          {plan.buckets.reduce((s, b) => s + (b.campaigns?.length ?? 0), 0)} campaigns
        </div>
      </td>

      {/* Trigger */}
      <td className="px-4 py-3.5">
        <TriggerTypeSelect
          trigger={row.trigger}
          plans={allPlans}
          planId={plan.planId}
          onChange={onTriggerChange}
        />
      </td>

      {/* Start time */}
      <td className="px-4 py-3.5">
        {row.trigger.type === 'time' ? (
          <input
            type="time"
            value={row.trigger.time}
            onChange={(e) => onTimeChange(e.target.value)}
            className="border border-gray-200 rounded-md px-2 py-1 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-blue-300"
          />
        ) : row.trigger.type === 'on_plan_complete' ? (
          <span className="text-xs text-gray-400 italic">
            after{' '}
            {allPlans.find((p) => p.planId === (row.trigger as Extract<PlanTrigger, { type: 'on_plan_complete' }>).planId)?.name ?? '…'}
          </span>
        ) : (
          <span className="text-xs text-gray-400 italic">on demand</span>
        )}
      </td>

      {/* Working hours */}
      <td className="px-4 py-3.5">
        <div className="flex items-center gap-1.5">
          <input
            type="time"
            value={row.whStart}
            onChange={(e) => onWhStartChange(e.target.value)}
            className="border border-gray-200 rounded-md px-2 py-1 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-blue-300"
          />
          <span className="text-[11px] text-gray-400">to</span>
          <input
            type="time"
            value={row.whEnd}
            onChange={(e) => onWhEndChange(e.target.value)}
            className="border border-gray-200 rounded-md px-2 py-1 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-blue-300"
          />
        </div>
      </td>

      {/* Days */}
      <td className="px-4 py-3.5">
        <DOWPicker value={row.whDays} onChange={onWhDaysChange} />
      </td>

      {/* Last run */}
      <td className="px-4 py-3.5 text-xs text-gray-500 font-mono whitespace-nowrap">
        {saving ? <Spinner /> : fmtLastRun(plan.latestRun?.startedAt)}
      </td>
    </tr>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export function PlansScheduler(): ReactNode {
  const qc = useQueryClient();
  const list = useQuery({ queryKey: ['plans'], queryFn: () => api.plans.listV2() });

  const nonTemplatePlans = (list.data?.plans ?? [])
    .filter((p) => !p.isTemplate && !p.is_template)
    .sort((a, b) => {
      const aEnabled = a.trigger.type !== 'manual' ? 0 : 1;
      const bEnabled = b.trigger.type !== 'manual' ? 0 : 1;
      return aEnabled - bEnabled;
    });

  const [rowStates, setRowStates] = useState<Record<string, RowLocal>>({});
  const [savingIds, setSavingIds] = useState<Set<string>>(new Set());

  function getRow(plan: PlanSummaryV2): RowLocal {
    return rowStates[plan.planId] ?? initRow(plan);
  }

  function patchRow(planId: string, plan: PlanSummaryV2, patch: Partial<RowLocal>) {
    setRowStates((prev) => ({
      ...prev,
      [planId]: { ...(prev[planId] ?? initRow(plan)), ...patch },
    }));
  }

  const save = useMutation({
    mutationFn: ({
      planId,
      body,
    }: {
      planId: string;
      body: Parameters<typeof api.plans.updateV2>[1];
    }) => api.plans.updateV2(planId, body),
    onSuccess: (_res, { planId }) => {
      qc.invalidateQueries({ queryKey: ['plans'] });
      setSavingIds((s) => {
        const n = new Set(s);
        n.delete(planId);
        return n;
      });
      setRowStates((prev) => {
        const next = { ...prev };
        delete next[planId];
        return next;
      });
    },
    onError: (_err, { planId }) => {
      setSavingIds((s) => {
        const n = new Set(s);
        n.delete(planId);
        return n;
      });
    },
  });

  function updateTrigger(plan: PlanSummaryV2, trigger: PlanTrigger) {
    patchRow(plan.planId, plan, { trigger });
    setSavingIds((s) => new Set(s).add(plan.planId));
    save.mutate({ planId: plan.planId, body: { trigger } });
  }

  function updateWH(
    plan: PlanSummaryV2,
    patch: { start?: string; end?: string; days?: number[] },
  ) {
    const row = getRow(plan);
    const newStart = patch.start ?? row.whStart;
    const newEnd = patch.end ?? row.whEnd;
    const newDays = patch.days ?? row.whDays;
    patchRow(plan.planId, plan, { whStart: newStart, whEnd: newEnd, whDays: newDays });
    setSavingIds((s) => new Set(s).add(plan.planId));
    save.mutate({
      planId: plan.planId,
      body: {
        workingHours: {
          startTime: newStart,
          endTime: newEnd,
          days: indicesToDays(newDays),
        },
      },
    });
  }

  function handleToggle(plan: PlanSummaryV2, enabled: boolean) {
    const row = getRow(plan);
    if (!enabled) {
      patchRow(plan.planId, plan, {
        prevTrigger: row.trigger,
        trigger: { type: 'manual' },
      });
      setSavingIds((s) => new Set(s).add(plan.planId));
      save.mutate({ planId: plan.planId, body: { trigger: { type: 'manual' } } });
    } else {
      const restored: PlanTrigger =
        row.prevTrigger ?? { type: 'time', time: '08:00' };
      patchRow(plan.planId, plan, { trigger: restored, prevTrigger: null });
      setSavingIds((s) => new Set(s).add(plan.planId));
      save.mutate({ planId: plan.planId, body: { trigger: restored } });
    }
  }

  if (list.isLoading) {
    return (
      <div className="flex items-center justify-center py-20">
        <Spinner />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h2 className="text-xl font-semibold tracking-tight">Scheduler</h2>
        <p className="mt-1 text-sm text-muted-foreground max-w-2xl">
          Set when each plan can run. Working hours and days apply to every trigger type — a plan
          never runs outside its window, even if a clock time or upstream completion fires it.
        </p>
      </div>

      {nonTemplatePlans.length === 0 ? (
        <div className="rounded-xl border border-dashed border-border p-8 text-center text-sm text-muted-foreground">
          No daily plans yet.
        </div>
      ) : (
        <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-sm" style={{ minWidth: 1080 }}>
              <thead>
                <tr className="bg-gray-50 border-b border-gray-100">
                  {[
                    'On',
                    'Plan',
                    'Trigger',
                    'Start time',
                    'Working hours',
                    'Days',
                    'Last run',
                  ].map((h) => (
                    <th
                      key={h}
                      className="px-4 py-2.5 text-left text-[10px] font-semibold text-gray-500 uppercase tracking-wider whitespace-nowrap"
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {nonTemplatePlans.map((plan) => {
                  const row = getRow(plan);
                  return (
                    <SchedulerRow
                      key={plan.planId}
                      plan={plan}
                      allPlans={nonTemplatePlans}
                      row={row}
                      saving={savingIds.has(plan.planId)}
                      onToggle={(enabled) => handleToggle(plan, enabled)}
                      onTriggerChange={(t) => updateTrigger(plan, t)}
                      onTimeChange={(time) => {
                        if (row.trigger.type !== 'time') return;
                        updateTrigger(plan, { ...row.trigger, time });
                      }}
                      onWhStartChange={(v) => updateWH(plan, { start: v })}
                      onWhEndChange={(v) => updateWH(plan, { end: v })}
                      onWhDaysChange={(indices) => updateWH(plan, { days: indices })}
                    />
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="flex items-start gap-2 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2.5 text-xs text-amber-800">
        <span className="mt-0.5 shrink-0">ℹ</span>
        <div>
          <span className="font-semibold">How working hours interact with triggers: </span>
          If the trigger fires outside the working hours or on an excluded day, execution is held
          until the next valid window. After-plan-complete triggers and manual runs are queued the
          same way.
        </div>
      </div>
    </div>
  );
}
