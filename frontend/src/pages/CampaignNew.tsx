import { useState, type ReactNode } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useLocation, useNavigate } from 'react-router-dom';

import { Spinner } from '@/components/ui';
import { api, type CreateCampaignBody } from '@/lib/api';

type DialerType = CreateCampaignBody['dialer']['type'];

type FormState = {
  name: string;
  segmentArn: string;
  queueId: string;
  contactFlowId: string;
  campaignFlowArn: string;
  sourcePhoneNumber: string;
  dialerType: DialerType;
  bandwidthAllocation: number;
  dialingCapacity: number;
  amdEnabled: boolean;
  amdAwaitPrompt: boolean;
  startTime: string;
  endTime: string;
  timezone: string;
  perDay: number;
  perWeek: number;
  perMonth: number;
};

const DEFAULTS: FormState = {
  name: '',
  segmentArn: '',
  queueId: '',
  contactFlowId: '',
  campaignFlowArn: '',
  sourcePhoneNumber: '',
  dialerType: 'progressive',
  bandwidthAllocation: 1,
  dialingCapacity: 1,
  amdEnabled: true,
  amdAwaitPrompt: true,
  startTime: '',
  endTime: '',
  timezone: 'America/New_York',
  perDay: 0,
  perWeek: 0,
  perMonth: 0,
};

// ── Reusable field wrapper ────────────────────────────────────────────────────

function FormField({
  label,
  hint,
  hintTone = 'muted',
  children,
}: {
  label: string;
  hint?: string;
  hintTone?: 'muted' | 'danger';
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1">
      <label className="block text-xs font-semibold text-gray-700">{label}</label>
      {children}
      {hint && (
        <p
          className={
            hintTone === 'danger' ? 'text-xs text-red-500' : 'text-xs text-gray-400'
          }
        >
          {hint}
        </p>
      )}
    </div>
  );
}

// ── Input style ───────────────────────────────────────────────────────────────

const inputCls =
  'w-full rounded-lg border border-gray-200 px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-300';

// ── Main component ────────────────────────────────────────────────────────────

export function CampaignNew(): ReactNode {
  const navigate = useNavigate();
  const location = useLocation();
  const prefilled = (location.state as { prefilledBody?: CreateCampaignBody } | null)
    ?.prefilledBody;
  const [form, setForm] = useState<FormState>(() =>
    prefilled ? prefilledToFormState(prefilled) : DEFAULTS,
  );
  const [error, setError] = useState<string | null>(null);

  const segments = useQuery({
    queryKey: ['segments', 'for-picker'],
    queryFn: () => api.segments.list({ maxResults: 200 }),
  });
  const queues = useQuery({
    queryKey: ['campaigns', 'queues'],
    queryFn: () => api.campaigns.queues(),
  });
  const flows = useQuery({
    queryKey: ['campaigns', 'contact-flows'],
    queryFn: () => api.campaigns.contactFlows(),
  });
  const phones = useQuery({
    queryKey: ['campaigns', 'phone-numbers'],
    queryFn: () => api.campaigns.phoneNumbers(),
  });

  const create = useMutation({
    mutationFn: () => {
      const body: CreateCampaignBody = {
        name: form.name,
        segmentArn: form.segmentArn || undefined,
        queueId: form.queueId,
        contactFlowId: form.contactFlowId,
        campaignFlowArn: form.campaignFlowArn || undefined,
        sourcePhoneNumber: form.sourcePhoneNumber,
        dialer: {
          type: form.dialerType,
          bandwidthAllocation: form.bandwidthAllocation,
          dialingCapacity: form.dialingCapacity,
        },
        answerMachineDetection: {
          enabled: form.amdEnabled,
          awaitPrompt: form.amdAwaitPrompt,
        },
        schedule: {
          startTime: toIso(form.startTime),
          endTime: toIso(form.endTime),
        },
        communicationTime: form.segmentArn ? { timezone: form.timezone } : undefined,
        communicationLimits:
          form.perDay || form.perWeek || form.perMonth
            ? {
                perDay: form.perDay || undefined,
                perWeek: form.perWeek || undefined,
                perMonth: form.perMonth || undefined,
              }
            : undefined,
      };
      return api.campaigns.create(body);
    },
    onSuccess: (res) => navigate(`/campaigns/${encodeURIComponent(res.id)}`),
    onError: (err: Error) => setError(err.message),
  });

  const update = <K extends keyof FormState>(key: K, value: FormState[K]) => {
    setForm((f) => ({ ...f, [key]: value }));
  };

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (form.startTime) {
      const start = new Date(form.startTime);
      const minStart = new Date(Date.now() + 5 * 60 * 1000);
      if (start < minStart) {
        setError('Start time must be at least 5 minutes from now.');
        return;
      }
    }
    create.mutate();
  };

  return (
    <form onSubmit={onSubmit} className="flex flex-col gap-5">
      {/* Header */}
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h2 className="text-xl font-semibold tracking-tight">New campaign</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Segment-driven Outbound Campaigns V2.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => navigate('/campaigns')}
            className="rounded-lg border border-gray-200 px-3 py-1.5 text-xs font-medium hover:bg-gray-50 transition-colors"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={create.isPending}
            className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 text-white px-3.5 py-2 text-sm font-medium hover:bg-blue-700 transition-colors disabled:opacity-50"
          >
            {create.isPending ? (
              <>
                <Spinner /> Creating…
              </>
            ) : (
              'Create campaign'
            )}
          </button>
        </div>
      </div>

      {/* Error banner */}
      {error ? (
        <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-600">
          {error}
        </div>
      ) : null}

      {/* Basics */}
      <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
        <h3 className="text-sm font-semibold text-gray-900 mb-4">Basics</h3>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <FormField label="Name">
            <input
              id="cmp-name"
              className={inputCls}
              value={form.name}
              onChange={(e) => update('name', e.target.value)}
              required
            />
          </FormField>
          <FormField label="Segment" hint="Arn of the Customer Profiles segment to dial.">
            <select
              className={inputCls}
              value={form.segmentArn}
              onChange={(e) => update('segmentArn', e.target.value)}
              required
            >
              <option value="">Select a segment…</option>
              {segments.data?.segments.map((s) => (
                <option key={s.name} value={s.segmentArn}>
                  {s.displayName ?? s.name}
                </option>
              ))}
            </select>
          </FormField>
        </div>
      </div>

      {/* Connect routing */}
      <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
        <h3 className="text-sm font-semibold text-gray-900 mb-4">Connect routing</h3>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <FormField label="Queue">
            <select
              className={inputCls}
              value={form.queueId}
              onChange={(e) => update('queueId', e.target.value)}
              required
            >
              <option value="">Select a queue…</option>
              {queues.data?.queues.map((q) => (
                <option key={q.id} value={q.id}>
                  {q.name}
                </option>
              ))}
            </select>
          </FormField>
          <FormField label="Contact flow">
            <select
              className={inputCls}
              value={form.contactFlowId}
              onChange={(e) => update('contactFlowId', e.target.value)}
              required
            >
              <option value="">Select a contact flow…</option>
              {flows.data?.contactFlows
                .filter((f) => f.contactFlowType === 'CONTACT_FLOW')
                .map((f) => (
                  <option key={f.id} value={f.id}>
                    {f.name}
                  </option>
                ))}
            </select>
          </FormField>
          <FormField label="Campaign flow">
            <select
              className={inputCls}
              value={form.campaignFlowArn}
              onChange={(e) => update('campaignFlowArn', e.target.value)}
              required
            >
              <option value="">Select a campaign flow…</option>
              {flows.data?.contactFlows
                .filter((f) => f.contactFlowType === 'CAMPAIGN')
                .map((f) => (
                  <option key={f.id} value={f.arn}>
                    {f.name}
                  </option>
                ))}
            </select>
          </FormField>
          <FormField label="Source phone number">
            <select
              className={inputCls}
              value={form.sourcePhoneNumber}
              onChange={(e) => update('sourcePhoneNumber', e.target.value)}
              required
            >
              <option value="">Select a caller ID…</option>
              {phones.data?.phoneNumbers.map((p) => (
                <option key={p.arn} value={p.number}>
                  {p.number}
                </option>
              ))}
            </select>
          </FormField>
        </div>
      </div>

      {/* Dialer */}
      <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
        <h3 className="text-sm font-semibold text-gray-900 mb-4">Dialer</h3>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          <FormField label="Type">
            <select
              className={inputCls}
              value={form.dialerType}
              onChange={(e) => update('dialerType', e.target.value as DialerType)}
            >
              <option value="progressive">Progressive</option>
              <option value="predictive">Predictive</option>
              <option value="agentless">Agentless</option>
            </select>
          </FormField>
          <FormField label="Bandwidth allocation" hint="0–1">
            <input
              type="number"
              min="0"
              max="1"
              step="0.05"
              className={inputCls}
              value={form.bandwidthAllocation}
              onChange={(e) => update('bandwidthAllocation', Number(e.target.value))}
            />
          </FormField>
          <FormField label="Dialing capacity" hint="0–1">
            <input
              type="number"
              min="0"
              max="1"
              step="0.05"
              className={inputCls}
              value={form.dialingCapacity}
              onChange={(e) => update('dialingCapacity', Number(e.target.value))}
            />
          </FormField>
        </div>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 mt-4">
          <FormField label="Answer machine detection">
            <select
              className={inputCls}
              value={String(form.amdEnabled)}
              onChange={(e) => update('amdEnabled', e.target.value === 'true')}
            >
              <option value="true">Enabled</option>
              <option value="false">Disabled</option>
            </select>
          </FormField>
          <FormField label="Await prompt">
            <select
              className={inputCls}
              value={String(form.amdAwaitPrompt)}
              onChange={(e) => update('amdAwaitPrompt', e.target.value === 'true')}
            >
              <option value="true">Yes</option>
              <option value="false">No</option>
            </select>
          </FormField>
        </div>
      </div>

      {/* Schedule */}
      <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
        <h3 className="text-sm font-semibold text-gray-900 mb-4">Schedule</h3>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          <FormField
            label="Start"
            hint={
              form.startTime && new Date(form.startTime) < new Date(Date.now() + 5 * 60 * 1000)
                ? 'Must be at least 5 minutes from now'
                : undefined
            }
            hintTone="danger"
          >
            <input
              type="datetime-local"
              className={inputCls}
              value={form.startTime}
              onChange={(e) => update('startTime', e.target.value)}
              required
            />
          </FormField>
          <FormField label="End">
            <input
              type="datetime-local"
              className={inputCls}
              value={form.endTime}
              onChange={(e) => update('endTime', e.target.value)}
              required
            />
          </FormField>
          <FormField label="Timezone">
            <input
              className={inputCls}
              value={form.timezone}
              onChange={(e) => update('timezone', e.target.value)}
              placeholder="America/New_York"
            />
          </FormField>
        </div>
      </div>

      {/* Communication limits */}
      <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
        <h3 className="text-sm font-semibold text-gray-900 mb-4">Communication limits (optional)</h3>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          <FormField label="Max per day">
            <input
              type="number"
              min="0"
              className={inputCls}
              value={form.perDay}
              onChange={(e) => update('perDay', Number(e.target.value))}
            />
          </FormField>
          <FormField label="Max per week">
            <input
              type="number"
              min="0"
              className={inputCls}
              value={form.perWeek}
              onChange={(e) => update('perWeek', Number(e.target.value))}
            />
          </FormField>
          <FormField label="Max per month">
            <input
              type="number"
              min="0"
              className={inputCls}
              value={form.perMonth}
              onChange={(e) => update('perMonth', Number(e.target.value))}
            />
          </FormField>
        </div>
      </div>
    </form>
  );
}

// ── Utilities ─────────────────────────────────────────────────────────────────

function toIso(local: string): string {
  if (!local) return '';
  const d = new Date(local);
  if (Number.isNaN(d.getTime())) return local;
  return d.toISOString();
}

/**
 * Convert an ISO Z timestamp to the `YYYY-MM-DDTHH:MM` shape that an HTML
 * `datetime-local` input expects, in the user's *local* browser timezone.
 * Used when CampaignNew is opened with a prefilled body coming from
 * EnableCampaignModal so the inputs aren't blank.
 */
function isoToLocalInput(iso: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const pad = (n: number) => String(n).padStart(2, '0');
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}`
  );
}

function prefilledToFormState(body: CreateCampaignBody): FormState {
  return {
    name: body.name,
    segmentArn: body.segmentArn ?? '',
    queueId: body.queueId,
    contactFlowId: body.contactFlowId,
    campaignFlowArn: body.campaignFlowArn ?? '',
    sourcePhoneNumber: body.sourcePhoneNumber,
    dialerType: body.dialer.type,
    bandwidthAllocation: body.dialer.bandwidthAllocation ?? 1,
    dialingCapacity: body.dialer.dialingCapacity ?? 1,
    amdEnabled: body.answerMachineDetection?.enabled ?? true,
    amdAwaitPrompt: body.answerMachineDetection?.awaitPrompt ?? true,
    startTime: isoToLocalInput(body.schedule.startTime),
    endTime: isoToLocalInput(body.schedule.endTime),
    timezone: body.communicationTime?.timezone ?? 'America/New_York',
    perDay: body.communicationLimits?.perDay ?? 0,
    perWeek: body.communicationLimits?.perWeek ?? 0,
    perMonth: body.communicationLimits?.perMonth ?? 0,
  };
}
