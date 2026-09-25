import { useRef, useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useNavigate, useParams } from 'react-router-dom';

import { Spinner } from '@/components/ui';
import { api, type BucketDefV2, type PlanSummaryV2, type SegmentSummary } from '@/lib/api';
import { useLocationMapping } from '@/lib/stateLocationMap';
import {
  DEFAULT_SMS_CAMPAIGN_TEMPLATE,
  SMS_CAMPAIGN_TEMPLATE_VERSION,
  NO_PROMOTIONAL_SMS_NUMBER,
  isPromotionalSmsNumber,
  previewSmsCampaign,
  smsCampaignMessageStats,
  validateSmsCampaignTemplate,
} from '@/lib/smsCampaign';
import { GroupCheckboxes } from './PlanNew';
import { canEditSmsPlan } from './SmsCampaigns';

async function loadAllSegments(): Promise<SegmentSummary[]> {
  const segments = new Map<string, SegmentSummary>();
  const seen = new Set<string>();
  let nextToken: string | undefined;
  do {
    const page = await api.segments.list({ maxResults: 100, ...(nextToken ? { nextToken } : {}) });
    for (const segment of page.segments) segments.set(segment.segmentArn, segment);
    nextToken = page.nextToken;
    if (nextToken && seen.has(nextToken)) throw new Error('Segment pagination did not advance. Please retry.');
    if (nextToken) seen.add(nextToken);
  } while (nextToken);
  return [...segments.values()];
}

export function SmsCampaignNew() {
  const { id } = useParams<{ id: string }>();
  const existing = useQuery({
    queryKey: ['plan', id], queryFn: () => api.plans.getV2(id!), enabled: !!id,
    refetchOnWindowFocus: false,
  });
  if (id && existing.isPending) return <p role="status" className="flex items-center gap-2"><Spinner /> Loading SMS campaign…</p>;
  if (id && existing.isError) return <div role="alert" className="space-y-3">
    <p>Could not load SMS campaign. {existing.error.message}</p>
    <button type="button" className="text-primary underline" onClick={() => void existing.refetch()}>Retry</button>
  </div>;
  const plan = id ? existing.data?.plan : undefined;
  if (id && (!plan || !canEditSmsPlan(plan) || existing.data?.latestRun?.status === 'running' || plan.latestRun?.status === 'running')) {
    return <div className="space-y-3">
      <h1 className="text-2xl font-semibold">Edit SMS campaign</h1>
      <p>This campaign cannot be edited here while running or with a multi-step or automatic configuration.</p>
      <Link className="text-primary underline" to={`/plans/${encodeURIComponent(id)}`}>View activity</Link>
    </div>;
  }
  return <SmsCampaignEditor key={id ?? 'new'} initialPlan={plan} />;
}

function SmsCampaignEditor({ initialPlan }: { initialPlan?: PlanSummaryV2 }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const originalBucket = initialPlan?.buckets[0];
  const originalCampaign = originalBucket?.campaigns[0];
  const originalConfig = originalCampaign?.campaignConfig;
  const { locationMap } = useLocationMapping();
  const stateCodes = locationMap.map((g) => g.code);
  const [name, setName] = useState(initialPlan?.name ?? '');
  const [states, setStates] = useState<string[]>(originalCampaign?.states ?? []);
  const [groups, setGroups] = useState<string[]>(originalCampaign?.groups ?? []);
  const [pinnedSegmentArn, setPinnedSegmentArn] = useState(originalCampaign?.pinnedSegmentArn ?? '');
  const [originArn, setOriginArn] = useState(originalConfig?.smsOriginationNumberArn ?? '');
  const [template, setTemplate] = useState(originalConfig?.smsMessageTemplate ?? DEFAULT_SMS_CAMPAIGN_TEMPLATE);
  const [acknowledged, setAcknowledged] = useState(originalConfig?.phiAcknowledged ?? false);
  const [errors, setErrors] = useState<string[]>([]);
  const saving = useRef(false);
  const groupsQuery = useQuery({ queryKey: ['leads', 'distinct', 'groups'], queryFn: () => api.leads.distinctValues('groups') });
  const segments = useQuery({ queryKey: ['sms', 'segment-options'], queryFn: loadAllSegments });
  const numbers = useQuery({ queryKey: ['sms', 'numbers'], queryFn: () => api.sms.listNumbers() });
  const segmentChoices = new Map((segments.data ?? []).map((segment) => [segment.segmentArn, segment]));
  const origins = (numbers.data?.originationNumbers ?? []).filter(isPromotionalSmsNumber);
  const selectedOriginCompatible = origins.some((number) => number.arn === originArn);
  const stats = smsCampaignMessageStats(template);
  const save = useMutation({
    mutationFn: async (buckets: BucketDefV2[]) => {
      const current = await api.sms.listNumbers();
      queryClient.setQueryData(['sms', 'numbers'], current);
      const arn = buckets[0]?.campaigns[0]?.campaignConfig?.smsOriginationNumberArn;
      if (!current.originationNumbers.some((number) => number.arn === arn && isPromotionalSmsNumber(number))) {
        throw new Error('Select an active promotional origination number with SMS capability before saving.');
      }
      return initialPlan
        ? api.plans.updateV2(initialPlan.planId, { name: name.trim(), buckets })
        : api.plans.createV2({ name: name.trim(), trigger: { type: 'manual' }, isTemplate: false, buckets });
    },
    retry: false,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['plans'] });
      if (initialPlan) void queryClient.invalidateQueries({ queryKey: ['plan', initialPlan.planId] });
      navigate('/sms');
    },
    onError: (error: Error) => setErrors([error.message]),
    onSettled: () => { saving.current = false; },
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (saving.current) return;
    const invalid = validateSmsCampaignTemplate(template);
    if (!name.trim()) invalid.unshift('Enter a campaign name.');
    if (states.length === 0 && !pinnedSegmentArn) invalid.push('Select at least one state, or pin an existing segment.');
    if (pinnedSegmentArn && !segmentChoices.has(pinnedSegmentArn)) invalid.push('The pinned segment is unavailable — clear it or pick another.');
    if (!selectedOriginCompatible) invalid.push('Select an active promotional origination number with SMS capability.');
    if (!acknowledged) invalid.push('Confirm the message contains no protected health information.');
    if (segments.isError || numbers.isError) invalid.push('Resolve the loading error before saving.');
    setErrors(invalid);
    if (invalid.length) return;
    const bucket: BucketDefV2 = {
      ...originalBucket,
      id: originalBucket?.id ?? crypto.randomUUID(), name: originalBucket?.name ?? 'SMS',
      run_mode: 'status_based', cleanup: false, prestart_next: false, campaignConfig: {},
      campaigns: [{
        ...originalCampaign,
        id: originalCampaign?.id ?? crypto.randomUUID(), name: name.trim(),
        deliveryType: 'sms', states, groups, dependsOn: [], run_type: 'full',
        pinnedSegmentArn: pinnedSegmentArn || undefined,
        campaignConfig: {
          smsOriginationNumberArn: originArn, smsMessageTemplate: template,
          smsTemplateVersion: SMS_CAMPAIGN_TEMPLATE_VERSION, phiAcknowledged: acknowledged,
        },
      }],
    };
    saving.current = true;
    save.mutate([bucket]);
  };

  const control = 'w-full rounded-lg border border-gray-200 px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-300';
  return <form onSubmit={submit} className="mx-auto flex max-w-4xl flex-col gap-6">
    <div className="flex flex-wrap items-start justify-between gap-4">
      <div>
        <h2 className="text-xl font-semibold tracking-tight">{initialPlan ? 'Edit SMS campaign' : 'New SMS campaign'}</h2>
        <p className="mt-1 text-sm text-muted-foreground">Save your audience and message, then start the campaign from the list.</p>
      </div>
      <div className="flex items-center gap-2">
        <Link to="/sms" className="rounded-lg border border-gray-200 px-4 py-2.5 text-sm font-medium hover:bg-gray-50 transition-colors">
          Back to SMS campaigns
        </Link>
        <button type="submit" disabled={save.isPending || segments.isPending || numbers.isPending || numbers.isError
          || origins.length === 0 || (!!originArn && !selectedOriginCompatible)}
          className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 text-white px-4 py-2.5 text-sm font-semibold hover:bg-blue-700 transition-colors disabled:opacity-50">
          {save.isPending ? <Spinner /> : initialPlan ? 'Save changes' : 'Save campaign'}
        </button>
      </div>
    </div>
    {errors.length > 0 && <div role="alert" className="text-sm text-red-600 bg-red-50 border border-red-200 rounded-xl p-3 flex items-start gap-2">
      <span className="text-red-500 mt-0.5 shrink-0">⚠</span>
      <ul className="list-inside list-disc">{errors.map((error) => <li key={error}>{error}</li>)}</ul>
    </div>}
    <section className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm space-y-4">
      <h3 className="text-sm font-semibold text-gray-900">Campaign details</h3>
      <div>
        <label className="block text-xs font-semibold text-gray-700 mb-1" htmlFor="sms-campaign-name">Campaign name</label>
        <input id="sms-campaign-name" value={name} onChange={(event) => setName(event.target.value)} className={control} />
      </div>

      {/* States — same ad-hoc Redis filter as Plans, no Customer Profiles segment required */}
      <div>
        <label className="block text-xs font-semibold text-gray-700 mb-1.5">States</label>
        <div className="flex flex-wrap gap-2">
          {stateCodes.map((code) => (
            <label key={code} className="flex items-center gap-1 text-xs cursor-pointer">
              <input
                type="checkbox"
                checked={states.includes(code)}
                onChange={() => setStates((prev) => prev.includes(code) ? prev.filter((s) => s !== code) : [...prev, code])}
              />
              <span className="text-gray-700">{code}</span>
            </label>
          ))}
        </div>
      </div>

      {/* Groups */}
      <div>
        <label className="block text-xs font-semibold text-gray-700 mb-1.5">Groups</label>
        {groupsQuery.isPending ? (
          <div className="flex h-9 items-center gap-2 text-xs text-gray-400">
            <Spinner /> loading…
          </div>
        ) : (
          <GroupCheckboxes options={groupsQuery.data?.values ?? []} selected={groups} onChange={setGroups} />
        )}
      </div>

      {/* Pinned segment — optional override; skips the states/groups filter above entirely */}
      <div>
        <label className="block text-xs font-semibold text-gray-700 mb-1" htmlFor="sms-audience">Pinned segment (optional)</label>
        <select id="sms-audience" value={pinnedSegmentArn} onChange={(event) => setPinnedSegmentArn(event.target.value)}
          disabled={segments.isPending || segments.isError} className={`${control} bg-white`}>
          <option value="">— auto (build from states/groups) —</option>
          {pinnedSegmentArn && !segmentChoices.has(pinnedSegmentArn) && <option value={pinnedSegmentArn} disabled>Previously selected segment is unavailable</option>}
          {[...segmentChoices.values()].map((segment) => <option key={segment.segmentArn} value={segment.segmentArn}>{segment.displayName ?? segment.name}</option>)}
        </select>
        {pinnedSegmentArn && <p className="mt-1 text-xs text-amber-600">States/groups above are ignored — pinned segment used as-is.</p>}
        {segments.isError && <p role="alert" className="mt-2 text-xs text-red-600">Could not load audience segments. {segments.error.message} <button type="button" className="underline" onClick={() => void segments.refetch()}>Retry segments</button></p>}
      </div>

      <div>
        <label className="block text-xs font-semibold text-gray-700 mb-1" htmlFor="sms-origin">Origination number</label>
        <select id="sms-origin" value={originArn} onChange={(event) => setOriginArn(event.target.value)}
          disabled={numbers.isPending || numbers.isError} className={`${control} bg-white`}>
          <option value="">{numbers.isPending ? 'Loading numbers…' : 'Select a number'}</option>
          {originArn && !selectedOriginCompatible && <option value={originArn} disabled>Previously selected number is unavailable for promotional SMS</option>}
          {origins.map((number) => <option key={number.arn} value={number.arn}>{number.phoneNumber} ({number.numberType})</option>)}
        </select>
        {numbers.isError && <p role="alert" className="mt-2 text-xs text-red-600">Could not load origination numbers. {numbers.error.message} <button type="button" className="underline" onClick={() => void numbers.refetch()}>Retry numbers</button></p>}
        {numbers.isSuccess && origins.length === 0 && <p role="alert" className="mt-2 text-xs text-red-600">{NO_PROMOTIONAL_SMS_NUMBER}</p>}
        {numbers.isSuccess && origins.length > 0 && originArn && !selectedOriginCompatible && <p role="alert" className="mt-2 text-xs text-red-600">The saved origination number cannot send promotional SMS. Select a compatible number before saving.</p>}
      </div>
    </section>
    <section className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm space-y-4">
      <h3 className="text-sm font-semibold text-gray-900">Message</h3>
      <div>
        <label className="block text-xs font-semibold text-gray-700 mb-1" htmlFor="sms-message">Message</label>
        <textarea id="sms-message" value={template} onChange={(event) => { setTemplate(event.target.value); setAcknowledged(false); }}
          rows={10} className={`${control} min-h-32 [field-sizing:content]`} aria-describedby="sms-message-help sms-message-stats" />
        <p id="sms-message-help" className="mt-2 text-xs text-gray-500">Use {'{{FirstName}}'} for each recipient&apos;s first name. You can edit the message and keep the supplied Luma booking link.</p>
        <p id="sms-message-stats" className="mt-1 text-xs text-gray-400">{stats.characters} characters · Estimated {stats.parts} SMS {stats.parts === 1 ? 'part' : 'parts'} · {stats.unicode ? 'Unicode' : 'GSM-7'} (20-character name estimate; actual parts may vary)</p>
      </div>
      <div className="rounded-lg border border-gray-100 bg-gray-50/60 p-4">
        <label htmlFor="sms-preview" className="block text-xs font-semibold text-gray-700 mb-1">Message preview</label>
        <p className="mb-2 text-xs text-gray-500">Alex is an example. The recipient&apos;s name is filled in when sending.</p>
        <textarea id="sms-preview" readOnly value={previewSmsCampaign(template)} rows={8} className="min-h-32 w-full resize-none border-0 bg-transparent text-sm [field-sizing:content] focus:outline-none" />
      </div>
      <label className="flex items-start gap-2 text-xs text-gray-600 cursor-pointer">
        <input type="checkbox" checked={acknowledged} onChange={(event) => setAcknowledged(event.target.checked)} className="mt-0.5" />
        <span>I confirm this message contains no protected health information: no patient details, dates of birth, diagnoses, medications, or account numbers.</span>
      </label>
    </section>
    <div className="flex items-center justify-end gap-2 pb-8">
      <Link to="/sms" className="rounded-lg border border-gray-200 px-4 py-2.5 text-sm font-medium hover:bg-gray-50 transition-colors">Cancel</Link>
      <button type="submit" disabled={save.isPending || segments.isPending || numbers.isPending || numbers.isError
        || origins.length === 0 || (!!originArn && !selectedOriginCompatible)}
        className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 text-white px-4 py-2.5 text-sm font-semibold hover:bg-blue-700 transition-colors disabled:opacity-50">
        {save.isPending ? <Spinner /> : initialPlan ? 'Save changes' : 'Save campaign'}
      </button>
    </div>
  </form>;
}
