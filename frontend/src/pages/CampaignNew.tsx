import { useState, type ReactNode } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useLocation, useNavigate } from 'react-router-dom';

import { Button, Card, Field, Input, Select } from '@/components/ui';
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
    <form onSubmit={onSubmit} className="flex flex-col gap-6">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">New campaign</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Segment-driven Outbound Campaigns V2.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button type="button" variant="outline" onClick={() => navigate('/campaigns')}>
            Cancel
          </Button>
          <Button type="submit" disabled={create.isPending}>
            {create.isPending ? 'Creating…' : 'Create campaign'}
          </Button>
        </div>
      </header>

      {error ? (
        <Card className="border-destructive/50 bg-destructive/5 text-sm text-destructive">
          {error}
        </Card>
      ) : null}

      <Card className="flex flex-col gap-4">
        <h2 className="text-sm font-semibold">Basics</h2>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <Field label="Name" htmlFor="cmp-name">
            <Input
              id="cmp-name"
              value={form.name}
              onChange={(e) => update('name', e.target.value)}
              required
            />
          </Field>
          <Field label="Segment" hint="Arn of the Customer Profiles segment to dial.">
            <Select
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
            </Select>
          </Field>
        </div>
      </Card>

      <Card className="flex flex-col gap-4">
        <h2 className="text-sm font-semibold">Connect routing</h2>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <Field label="Queue">
            <Select
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
            </Select>
          </Field>
          <Field label="Contact flow">
            <Select
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
            </Select>
          </Field>
          <Field label="Campaign flow">
            <Select
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
            </Select>
          </Field>
          <Field label="Source phone number">
            <Select
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
            </Select>
          </Field>
        </div>
      </Card>

      <Card className="flex flex-col gap-4">
        <h2 className="text-sm font-semibold">Dialer</h2>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          <Field label="Type">
            <Select
              value={form.dialerType}
              onChange={(e) => update('dialerType', e.target.value as DialerType)}
            >
              <option value="progressive">Progressive</option>
              <option value="predictive">Predictive</option>
              <option value="agentless">Agentless</option>
            </Select>
          </Field>
          <Field label="Bandwidth allocation" hint="0–1">
            <Input
              type="number"
              min="0"
              max="1"
              step="0.05"
              value={form.bandwidthAllocation}
              onChange={(e) => update('bandwidthAllocation', Number(e.target.value))}
            />
          </Field>
          <Field label="Dialing capacity" hint="0–1">
            <Input
              type="number"
              min="0"
              max="1"
              step="0.05"
              value={form.dialingCapacity}
              onChange={(e) => update('dialingCapacity', Number(e.target.value))}
            />
          </Field>
        </div>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <Field label="Answer machine detection">
            <Select
              value={String(form.amdEnabled)}
              onChange={(e) => update('amdEnabled', e.target.value === 'true')}
            >
              <option value="true">Enabled</option>
              <option value="false">Disabled</option>
            </Select>
          </Field>
          <Field label="Await prompt">
            <Select
              value={String(form.amdAwaitPrompt)}
              onChange={(e) => update('amdAwaitPrompt', e.target.value === 'true')}
            >
              <option value="true">Yes</option>
              <option value="false">No</option>
            </Select>
          </Field>
        </div>
      </Card>

      <Card className="flex flex-col gap-4">
        <h2 className="text-sm font-semibold">Schedule</h2>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          <Field
            label="Start"
            hint={
              form.startTime && new Date(form.startTime) < new Date(Date.now() + 5 * 60 * 1000)
                ? 'Must be at least 5 minutes from now'
                : undefined
            }
            hintTone="danger"
          >
            <Input
              type="datetime-local"
              value={form.startTime}
              onChange={(e) => update('startTime', e.target.value)}
              required
            />
          </Field>
          <Field label="End">
            <Input
              type="datetime-local"
              value={form.endTime}
              onChange={(e) => update('endTime', e.target.value)}
              required
            />
          </Field>
          <Field label="Timezone">
            <Input
              value={form.timezone}
              onChange={(e) => update('timezone', e.target.value)}
              placeholder="America/New_York"
            />
          </Field>
        </div>
      </Card>

      <Card className="flex flex-col gap-4">
        <h2 className="text-sm font-semibold">Communication limits (optional)</h2>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          <Field label="Max per day">
            <Input
              type="number"
              min="0"
              value={form.perDay}
              onChange={(e) => update('perDay', Number(e.target.value))}
            />
          </Field>
          <Field label="Max per week">
            <Input
              type="number"
              min="0"
              value={form.perWeek}
              onChange={(e) => update('perWeek', Number(e.target.value))}
            />
          </Field>
          <Field label="Max per month">
            <Input
              type="number"
              min="0"
              value={form.perMonth}
              onChange={(e) => update('perMonth', Number(e.target.value))}
            />
          </Field>
        </div>
      </Card>
    </form>
  );
}

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
