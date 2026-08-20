import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';

import { Badge, Button, Modal, Spinner } from '@/components/ui';
import { useEnableCampaign } from '@/hooks/useEnableCampaign';
import { api, type ContactFlow, type CreateCampaignBody } from '@/lib/api';
import { STATE_DEFAULT_PHONES, pickPhoneForStates } from '@/lib/areaCodeMap';
import { startTimeIso, today9pmNYIso } from '@/lib/utils';

const DEFAULT_QUEUE_NAME = 'agents outbound';
const HIGH_PRIORITY_QUEUE_NAME = 'high priority agents outbounds';
// The Connect flow name has a leading asterisk in our instance — same
// pattern as `*MainInboundVoice`. Connect treats it as part of the literal
// name, so we have to match it exactly.
const DEFAULT_FLOW_NAME = '*Agent-staffed Campaign AMD';

// State code → substrings to search for in campaign flow names.
// Canonical flows are named "campaign-<STATE>" (e.g. "campaign-NJ").
// Legacy entries handle the old hand-crafted flow names.
export const STATE_FLOW_PATTERNS: Record<string, string[]> = {
  NY:  ['campaign-NY', 'NY'],
  LI:  ['campaign-LI', 'LI', 'Long Island'],
  NJ:  ['campaign-NJ', 'NJ'],
  MD:  ['campaign-MD', 'MD'],
  CT:  ['campaign-CT', 'CT'],
  TX:  ['campaign-TX', 'TX'],
  NCA: ['campaign-NCA', 'NCA', 'North CA'],
  SCA: ['campaign-SCA', 'SCA', 'South CA'],
};

export function suggestCampaignFlow(
  campaignFlows: ContactFlow[],
  states: string[],
): ContactFlow | undefined {
  for (const state of states) {
    const patterns = STATE_FLOW_PATTERNS[state] ?? [`campaign-${state}`];
    // Try patterns in priority order: canonical `campaign-<STATE>` first,
    // legacy name substrings as fallback.
    for (const pattern of patterns) {
      const match = campaignFlows.find((f) =>
        f.name.toUpperCase().includes(pattern.toUpperCase()),
      );
      if (match) return match;
    }
  }
  return undefined;
}

/**
 * Resolve the campaign flow ARN for the given states, preferring the
 * backend's resolve_campaign_flow_arn (which auto-creates the canonical
 * flow if missing) over the client-side name-search heuristic. Falls back
 * to the client-side result if the backend call fails or finds nothing —
 * never regress below what suggestCampaignFlow alone already provided.
 */
export async function resolveCampaignFlowArn(
  campaignFlows: ContactFlow[],
  states: string[],
  callBackend: (states: string[]) => Promise<{ arn: string | null }>,
): Promise<string | undefined> {
  const clientArn = suggestCampaignFlow(campaignFlows, states)?.arn;
  try {
    const { arn } = await callBackend(states);
    if (arn) return arn;
  } catch {
    // fall through to the client-side result
  }
  return clientArn;
}

function isNewLeadNewLeadGroup(segmentGroups: unknown): boolean {
  // segmentGroups shape from CP API: { Groups: [{ Dimensions: [{ ProfileAttributes: { Attributes: { groups: { Values: string[] } } } }] }] }
  const groups = (segmentGroups as { Groups?: unknown[] } | null)?.Groups;
  if (!Array.isArray(groups)) return false;
  return groups.some((g: unknown) => {
    const dims = (g as { Dimensions?: unknown[] } | null)?.Dimensions;
    if (!Array.isArray(dims)) return false;
    return dims.some((d: unknown) => {
      const attrs = (d as { ProfileAttributes?: { Attributes?: Record<string, { Values?: string[] }> } } | null)
        ?.ProfileAttributes?.Attributes;
      const vals = attrs?.groups?.Values;
      return Array.isArray(vals) && vals.length === 1 && vals[0].toLowerCase().includes('new lead / new lead');
    });
  });
}

export function EnableCampaignModal({
  open,
  onClose,
  segmentName,
  segmentArn,
  segmentStates,
  segmentGroups,
  onSuccess,
}: {
  open: boolean;
  onClose: () => void;
  segmentName: string;
  segmentArn: string;
  segmentStates: string[];
  segmentGroups?: unknown;
  /** Called after a successful create+start so the parent can refresh state. */
  onSuccess?: (result: { id: string; state?: string }) => void;
}): ReactNode {
  const navigate = useNavigate();
  const enable = useEnableCampaign();

  const [overrideName, setOverrideName] = useState<string | null>(null);

  // Lazy: only fetch resources once the modal actually opens. Prevents the
  // segments-list page from making 3 calls per row on initial render.
  const queues = useQuery({
    queryKey: ['campaigns', 'queues'],
    queryFn: () => api.campaigns.queues(),
    enabled: open,
  });
  const flows = useQuery({
    queryKey: ['campaigns', 'flows', 'all'],
    queryFn: () => api.campaigns.contactFlows(),
    enabled: open,
  });
  const phones = useQuery({
    queryKey: ['campaigns', 'phones'],
    queryFn: () => api.campaigns.phoneNumbers(),
    enabled: open,
  });

  const resolved = useMemo(() => {
    const isHighPriority = isNewLeadNewLeadGroup(segmentGroups);
    const targetQueueName = isHighPriority ? HIGH_PRIORITY_QUEUE_NAME : DEFAULT_QUEUE_NAME;
    const queue = queues.data?.queues.find((q) => q.name === targetQueueName)
      ?? (isHighPriority ? queues.data?.queues.find((q) => q.name === DEFAULT_QUEUE_NAME) : undefined);
    const flow = flows.data?.contactFlows.find((f) => f.name === DEFAULT_FLOW_NAME);
    const phone = phones.data
      ? pickPhoneForStates(phones.data.phoneNumbers, segmentStates)
      : null;
    const campaignFlows = (flows.data?.contactFlows ?? []).filter(
      (f) => f.contactFlowType === 'CAMPAIGN',
    );
    const campaignFlow = suggestCampaignFlow(campaignFlows, segmentStates);
    return { queue, flow, phone, campaignFlow, isHighPriority };
  }, [queues.data, flows.data, phones.data, segmentStates, segmentGroups]);

  const [backendResolvedArn, setBackendResolvedArn] = useState<string | undefined>(undefined);

  useEffect(() => {
    if (!open || segmentStates.length === 0) return;
    let cancelled = false;
    // Clear the previous resolution immediately — otherwise, while this new
    // lookup is in flight, effectiveCampaignFlowArn would keep pointing at
    // the ARN resolved for the PRIOR segmentStates (e.g. the name-derived
    // fallback before the segment detail query resolves), inconsistent with
    // resolved.campaignFlow (synchronous, already reflecting the new state).
    setBackendResolvedArn(undefined);
    resolveCampaignFlowArn(
      (flows.data?.contactFlows ?? []).filter((f) => f.contactFlowType === 'CAMPAIGN'),
      segmentStates,
      (states) => api.plans.resolveCampaignFlow(states),
    ).then((arn) => {
      if (!cancelled) setBackendResolvedArn(arn);
    });
    return () => {
      cancelled = true;
    };
  }, [open, flows.data, segmentStates]);

  const effectiveCampaignFlowArn = backendResolvedArn ?? resolved.campaignFlow?.arn ?? '';

  const loading = queues.isPending || flows.isPending || phones.isPending;
  const errors: string[] = [];
  if (!loading) {
    if (!resolved.queue) errors.push(`Queue "${resolved.isHighPriority ? HIGH_PRIORITY_QUEUE_NAME : DEFAULT_QUEUE_NAME}" not found.`);
    if (!resolved.flow) errors.push(`Contact flow "${DEFAULT_FLOW_NAME}" not found.`);
    if (!resolved.phone) errors.push('No phone numbers available.');
    if (!effectiveCampaignFlowArn) errors.push('No campaign flow found for this state — use "Edit before" to pick one manually.');
  }

  const phoneDigits = resolved.phone?.number.replace(/\D/g, '') ?? '';
  const phoneMatchesState =
    phoneDigits.length > 0 &&
    segmentStates.length > 0 &&
    segmentStates.some((st) => {
      const canonical = STATE_DEFAULT_PHONES[st]?.replace(/\D/g, '');
      return canonical ? phoneDigits === canonical : false;
    });

  const defaultName = buildCampaignName(segmentName, segmentStates);
  const campaignName = overrideName ?? defaultName;
  const startTime = startTimeIso();
  const endTime = today9pmNYIso();

  const body: CreateCampaignBody | null =
    resolved.queue && resolved.flow && resolved.phone && effectiveCampaignFlowArn
      ? {
          name: campaignName,
          segmentArn,
          queueId: resolved.queue.id,
          contactFlowId: resolved.flow.id,
          campaignFlowArn: effectiveCampaignFlowArn,
          sourcePhoneNumber: resolved.phone.number,
          dialer: {
            type: 'progressive',
            bandwidthAllocation: 1.0,
            dialingCapacity: 1.0,
          },
          answerMachineDetection: { enabled: true, awaitPrompt: true },
          schedule: { startTime, endTime },
          communicationTime: { timezone: 'America/New_York' },
        }
      : null;

  const handleCreateAndStart = () => {
    if (!body) return;
    enable.mutate(body, {
      onSuccess: (res) => {
        onSuccess?.({ id: res.id, state: res.state });
        onClose();
        navigate(`/campaigns/${encodeURIComponent(res.id)}`);
      },
    });
  };

  const handleEditBefore = () => {
    if (!body) return;
    onClose();
    navigate('/campaigns/new', { state: { prefilledBody: body } });
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={`Enable Campaign for "${segmentName}"`}
      maxWidth="max-w-3xl"
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={enable.isPending}>
            Cancel
          </Button>
          <Button
            variant="outline"
            onClick={handleEditBefore}
            disabled={loading || !body || enable.isPending}
          >
            Edit before
          </Button>
          <Button
            onClick={handleCreateAndStart}
            disabled={loading || !body || enable.isPending}
          >
            {enable.isPending ? (
              <span className="inline-flex items-center gap-2">
                <Spinner /> creating…
              </span>
            ) : (
              'Create and start'
            )}
          </Button>
        </>
      }
    >
      {loading ? (
        <p className="inline-flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner /> Resolving defaults from your Connect instance…
        </p>
      ) : (
        <div className="flex flex-col gap-4">
          {errors.length > 0 ? (
            <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm">
              <p className="font-semibold text-destructive">
                Cannot create with defaults — {errors.length} issue
                {errors.length === 1 ? '' : 's'}:
              </p>
              <ul className="mt-1 list-disc pl-5 text-xs text-destructive">
                {errors.map((e) => (
                  <li key={e}>{e}</li>
                ))}
              </ul>
              <p className="mt-2 text-xs text-muted-foreground">
                Use "Edit before" to fix manually, or check that the named queue and
                contact flow exist in this Connect instance.
              </p>
            </div>
          ) : null}

          <div>
            <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Campaign name
            </label>
            <input
              type="text"
              value={campaignName}
              onChange={(e) => setOverrideName(e.target.value)}
              className="mt-1 h-9 w-full rounded-md border border-border bg-background px-3 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
            />
          </div>

          <div className="rounded-md border border-border bg-muted/30 p-3">
            <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              Defaults that will be applied
            </p>
            <dl className="mt-2 grid grid-cols-1 gap-2 text-sm md:grid-cols-2">
              <DefaultRow label="Segment" value={segmentName} />
              <DefaultRow
                label="Queue"
                value={resolved.queue ? resolved.queue.name : '—'}
                tone={resolved.queue ? 'ok' : 'bad'}
              />
              <DefaultRow
                label="Contact flow"
                value={resolved.flow ? resolved.flow.name : '—'}
                tone={resolved.flow ? 'ok' : 'bad'}
              />
              <DefaultRow
                label="Campaign flow"
                value={
                  effectiveCampaignFlowArn
                    ? resolved.campaignFlow?.arn === effectiveCampaignFlowArn
                      ? resolved.campaignFlow.name
                      : 'Resolved via backend'
                    : '—'
                }
                tone={effectiveCampaignFlowArn ? 'ok' : 'bad'}
              />
              <DefaultRow
                label="Phone number"
                value={resolved.phone?.number ?? '—'}
                tone={
                  !resolved.phone
                    ? 'bad'
                    : segmentStates.length > 0 && !phoneMatchesState
                    ? 'warn'
                    : 'ok'
                }
                hint={
                  resolved.phone && segmentStates.length > 0 && !phoneMatchesState
                    ? `${resolved.phone.number} is not the canonical number for ${segmentStates.join('/')} — using fallback. Verify the number is provisioned in Connect.`
                    : undefined
                }
              />
              <DefaultRow label="Dialer" value="Progressive · 100% bandwidth · 100% capacity" />
              <DefaultRow label="AMD" value="Enabled (await prompt)" />
              <DefaultRow
                label="Schedule"
                value={`Now → ${new Date(endTime).toLocaleString('en-US', {
                  timeZone: 'America/New_York',
                  hour: '2-digit',
                  minute: '2-digit',
                })} ET (today)`}
              />
            </dl>
          </div>

          {enable.isError ? (
            <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
              {(enable.error as Error).message}
            </div>
          ) : null}
          {enable.data?.startError ? (
            <div className="rounded-md border border-amber-400/50 bg-amber-50 p-3 text-sm text-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
              <strong>Created but not started:</strong> {enable.data.startError}. Open the
              campaign and click Start manually.
            </div>
          ) : null}
        </div>
      )}
    </Modal>
  );
}

function buildCampaignName(segmentName: string, segmentStates: string[]): string {
  const now = new Date();
  const mm = String(now.getMonth() + 1).padStart(2, '0');
  const dd = String(now.getDate()).padStart(2, '0');

  const state = segmentStates[0] ?? '';

  // The attemptsPart may contain hyphens (e.g. Can_2-4-6_NS_6-2-4), so we
  // cannot naively split on '-'. Instead: strip the trailing -HHMM time
  // suffix, locate the state token with a regex, and take everything after.
  const withoutTime = segmentName.replace(/-\d{4}$/, '');

  let statePart = state;
  let groupsAttempts = '';
  if (state) {
    const re = new RegExp('(?:^|-)([^-]*' + state + '[^-]*)-', 'i');
    const matched = re.exec(withoutTime);
    if (matched) {
      statePart = matched[1];
      groupsAttempts = withoutTime.slice(matched.index + matched[0].length);
    }
  }

  return [statePart, groupsAttempts, `${mm}-${dd}`].filter(Boolean).join('-');
}

function DefaultRow({
  label,
  value,
  tone = 'ok',
  hint,
}: {
  label: string;
  value: string;
  tone?: 'ok' | 'warn' | 'bad';
  hint?: string;
}): ReactNode {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 flex items-center gap-2 text-sm">
        <span className="font-mono">{value}</span>
        {tone === 'warn' ? <Badge tone="warning">fallback</Badge> : null}
        {tone === 'bad' ? <Badge tone="danger">missing</Badge> : null}
      </dd>
      {hint ? <p className="mt-0.5 text-xs text-muted-foreground">{hint}</p> : null}
    </div>
  );
}
