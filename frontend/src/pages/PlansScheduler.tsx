import { useState, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { Badge, Button, Card, Spinner } from '@/components/ui';
import { api, type PlanSchedule, type PlanSummaryV2 } from '@/lib/api';

const ALL_DAYS = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'] as const;
const DAY_LABELS: Record<string, string> = {
  MON: 'M', TUE: 'T', WED: 'W', THU: 'T', FRI: 'F', SAT: 'S', SUN: 'S',
};
const DAY_FULL: Record<string, string> = {
  MON: 'Mon', TUE: 'Tue', WED: 'Wed', THU: 'Thu', FRI: 'Fri', SAT: 'Sat', SUN: 'Sun',
};

type RowState = {
  enabled: boolean;
  hour: string;
  minute: string;
  days: string[];
  whEnabled: boolean;
  whDays: string[];
  whStartTime: string;
  whEndTime: string;
  dirty: boolean;
  saved: boolean;
};

function defaultRow(plan?: PlanSummaryV2): RowState {
  const wh = plan?.workingHours;
  return {
    enabled: false,
    hour: '08',
    minute: '00',
    days: ['MON', 'TUE', 'WED', 'THU', 'FRI'],
    whEnabled: !!wh,
    whDays: wh?.days ?? ['MON', 'TUE', 'WED', 'THU', 'FRI'],
    whStartTime: wh?.startTime ?? '08:00',
    whEndTime: wh?.endTime ?? '17:00',
    dirty: false,
    saved: false,
  };
}

function nextRunText(row: RowState): string {
  if (!row.enabled || row.days.length === 0) return '—';
  const hour = parseInt(row.hour, 10);
  const minute = parseInt(row.minute, 10);
  if (isNaN(hour) || isNaN(minute)) return '—';

  const now = new Date(new Date().toLocaleString('en-US', { timeZone: 'America/Bogota' }));
  const dayNames = ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT'];

  for (let offset = 0; offset <= 7; offset++) {
    const candidate = new Date(now);
    candidate.setDate(candidate.getDate() + offset);
    candidate.setHours(hour, minute, 0, 0);

    if (offset === 0 && candidate <= now) continue;

    const dayKey = dayNames[candidate.getDay()];
    if (!row.days.includes(dayKey)) continue;

    const label = offset === 0 ? 'Today' : offset === 1 ? 'Tomorrow' : DAY_FULL[dayKey];
    const hh = String(hour).padStart(2, '0');
    const mm = String(minute).padStart(2, '0');
    return `${label} ${hh}:${mm} COT`;
  }
  return '—';
}

function Toggle({ checked, disabled, onChange }: { checked: boolean; disabled: boolean; onChange: () => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={onChange}
      className={[
        'relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors',
        checked ? 'bg-primary' : 'bg-muted',
        disabled ? 'opacity-50 pointer-events-none' : '',
      ].join(' ')}
    >
      <span
        className={[
          'pointer-events-none inline-block h-4 w-4 rounded-full bg-white shadow transition-transform',
          checked ? 'translate-x-4' : 'translate-x-0',
        ].join(' ')}
      />
    </button>
  );
}

function DayButtons({ days, disabled, onToggle }: {
  days: string[];
  disabled: boolean;
  onToggle: (day: string) => void;
}) {
  return (
    <div className="flex gap-1">
      {ALL_DAYS.map((day) => (
        <button
          key={day}
          type="button"
          disabled={disabled}
          onClick={() => onToggle(day)}
          title={DAY_FULL[day]}
          className={[
            'h-6 w-6 rounded text-[10px] font-semibold transition-colors',
            days.includes(day)
              ? 'bg-primary text-primary-foreground'
              : 'bg-muted text-muted-foreground hover:bg-muted/80',
            disabled ? 'opacity-50 pointer-events-none' : '',
          ].join(' ')}
        >
          {DAY_LABELS[day]}
        </button>
      ))}
    </div>
  );
}

function SchedulerRow({ plan, row, onChange, onSave, saving }: {
  plan: PlanSummaryV2;
  row: RowState;
  onChange: (patch: Partial<RowState>) => void;
  onSave: () => void;
  saving: boolean;
}) {
  const canSave = row.dirty && (!row.enabled || row.days.length > 0) &&
    (!row.whEnabled || row.whDays.length > 0);

  return (
    <tr className="border-b border-border last:border-0">
      {/* Plan name */}
      <td className="px-4 py-3 text-sm font-medium">{plan.name}</td>

      {/* Schedule: Enabled toggle */}
      <td className="px-4 py-3">
        <Toggle
          checked={row.enabled}
          disabled={saving}
          onChange={() => onChange({ enabled: !row.enabled, dirty: true })}
        />
      </td>

      {/* Schedule: Days */}
      <td className="px-4 py-3">
        <DayButtons
          days={row.days}
          disabled={saving}
          onToggle={(day) => {
            const next = row.days.includes(day)
              ? row.days.filter((d) => d !== day)
              : [...row.days, day];
            onChange({ days: next, dirty: true });
          }}
        />
        {row.enabled && row.days.length === 0 && (
          <p className="mt-1 text-[11px] text-destructive">Select at least one day</p>
        )}
      </td>

      {/* Schedule: Time */}
      <td className="px-4 py-3">
        <div className="flex items-center gap-1">
          <input
            type="number"
            min={0}
            max={23}
            disabled={saving}
            value={row.hour}
            onChange={(e) => {
              const v = Math.min(23, Math.max(0, Number(e.target.value)));
              onChange({ hour: String(v).padStart(2, '0'), dirty: true });
            }}
            className="w-12 rounded border border-border bg-background px-2 py-1 text-center text-sm [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
          />
          <span className="text-muted-foreground">:</span>
          <input
            type="number"
            min={0}
            max={59}
            disabled={saving}
            value={row.minute}
            onChange={(e) => {
              const v = Math.min(59, Math.max(0, Number(e.target.value)));
              onChange({ minute: String(v).padStart(2, '0'), dirty: true });
            }}
            className="w-12 rounded border border-border bg-background px-2 py-1 text-center text-sm [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
          />
          <span className="text-[11px] text-muted-foreground">COT</span>
        </div>
      </td>

      {/* Next run */}
      <td className="px-4 py-3 text-sm text-muted-foreground">{nextRunText(row)}</td>

      {/* Working hours */}
      <td className="px-4 py-3">
        <div className="flex items-center gap-2">
          <Toggle
            checked={row.whEnabled}
            disabled={saving}
            onChange={() => onChange({ whEnabled: !row.whEnabled, dirty: true })}
          />
          <span className="text-xs text-muted-foreground">{row.whEnabled ? 'On' : 'Off'}</span>
        </div>
        {row.whEnabled && (
          <div className="mt-2 space-y-1.5">
            <DayButtons
              days={row.whDays}
              disabled={saving}
              onToggle={(day) => {
                const next = row.whDays.includes(day)
                  ? row.whDays.filter((d) => d !== day)
                  : [...row.whDays, day];
                onChange({ whDays: next, dirty: true });
              }}
            />
            {row.whDays.length === 0 && (
              <p className="text-[11px] text-destructive">Select at least one day</p>
            )}
            <div className="flex items-center gap-1">
              <input
                type="time"
                value={row.whStartTime}
                disabled={saving}
                onChange={(e) => onChange({ whStartTime: e.target.value, dirty: true })}
                className="rounded border border-border bg-background px-2 py-1 text-sm"
              />
              <span className="text-xs text-muted-foreground">–</span>
              <input
                type="time"
                value={row.whEndTime}
                disabled={saving}
                onChange={(e) => onChange({ whEndTime: e.target.value, dirty: true })}
                className="rounded border border-border bg-background px-2 py-1 text-sm"
              />
              <span className="text-[11px] text-muted-foreground">COT</span>
            </div>
          </div>
        )}
      </td>

      {/* Save */}
      <td className="px-4 py-3">
        {saving ? (
          <Spinner />
        ) : row.saved && !row.dirty ? (
          <Badge tone="success">Saved ✓</Badge>
        ) : canSave ? (
          <Button size="sm" onClick={onSave}>
            Save
          </Button>
        ) : null}
      </td>
    </tr>
  );
}

export function PlansScheduler(): ReactNode {
  const qc = useQueryClient();
  const list = useQuery({ queryKey: ['plans'], queryFn: () => api.plans.listV2() });

  const nonTemplatePlans = (list.data?.plans ?? []).filter((p) => !p.isTemplate && !p.is_template);

  const [rows, setRows] = useState<Record<string, RowState>>({});
  const [savingIds, setSavingIds] = useState<Set<string>>(new Set());

  function getRow(plan: PlanSummaryV2): RowState {
    return rows[plan.planId] ?? defaultRow(plan);
  }

  function patchRow(planId: string, patch: Partial<RowState>) {
    setRows((prev) => ({
      ...prev,
      [planId]: { ...(prev[planId] ?? defaultRow()), ...patch },
    }));
  }

  const save = useMutation({
    mutationFn: ({ planId, schedule, workingHours }: {
      planId: string;
      schedule: PlanSchedule;
      workingHours: { days: string[]; startTime: string; endTime: string } | null;
    }) => api.plans.update(planId, { schedule, workingHours }),
    onSuccess: (_res, { planId }) => {
      qc.invalidateQueries({ queryKey: ['plans'] });
      setSavingIds((s) => { const n = new Set(s); n.delete(planId); return n; });
      setRows((prev) => ({
        ...prev,
        [planId]: { ...prev[planId], dirty: false, saved: true },
      }));
      // After badge clears, remove local state so next render re-initializes from server
      setTimeout(() => {
        setRows((prev) => {
          const next = { ...prev };
          delete next[planId];
          return next;
        });
      }, 2000);
    },
    onError: (_err, { planId }) => {
      setSavingIds((s) => { const n = new Set(s); n.delete(planId); return n; });
    },
  });

  function handleSave(plan: PlanSummaryV2) {
    const row = getRow(plan);
    setSavingIds((s) => new Set(s).add(plan.planId));
    save.mutate({
      planId: plan.planId,
      schedule: {
        enabled: row.enabled,
        hour: parseInt(row.hour, 10),
        minute: parseInt(row.minute, 10),
        timezone: 'America/Bogota',
        days: row.days,
      },
      workingHours: row.whEnabled
        ? { days: row.whDays, startTime: row.whStartTime, endTime: row.whEndTime }
        : null,
    });
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
        <h2 className="text-xl font-semibold">Scheduler</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Configure automatic daily runs and working hours for each plan.
        </p>
      </div>

      {nonTemplatePlans.length === 0 ? (
        <Card>
          <p className="text-center text-sm text-muted-foreground py-8">No daily plans yet.</p>
        </Card>
      ) : (
        <Card className="p-0 overflow-hidden">
          <table className="w-full">
            <thead>
              <tr className="border-b border-border bg-muted/40">
                <th className="px-4 py-2.5 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground">Plan</th>
                <th className="px-4 py-2.5 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground">Enabled</th>
                <th className="px-4 py-2.5 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground">Days</th>
                <th className="px-4 py-2.5 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground">Time (COT)</th>
                <th className="px-4 py-2.5 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground">Next run</th>
                <th className="px-4 py-2.5 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground">Working hours</th>
                <th className="px-4 py-2.5" />
              </tr>
            </thead>
            <tbody>
              {nonTemplatePlans.map((plan) => {
                const row = getRow(plan);
                return (
                  <SchedulerRow
                    key={plan.planId}
                    plan={plan}
                    row={row}
                    onChange={(patch) => patchRow(plan.planId, patch)}
                    onSave={() => handleSave(plan)}
                    saving={savingIds.has(plan.planId)}
                  />
                );
              })}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}
