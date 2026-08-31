import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useParams } from 'react-router-dom';

import { Spinner } from '@/components/ui';
import {
  api,
  type BucketCampaignConfig,
  type BucketDefV2,
  type CampaignDef,
  type CampaignRunType,
  type ContactFlow,
  type PlanLoop,
  type PlanRunV2,
  type PlanSummaryV2,
  type PlanTrigger,
  type Queue,
  type SmsOriginationNumber,
} from '@/lib/api';
import { STATE_DEFAULT_PHONES } from '@/lib/areaCodeMap';
import { useLocationMapping } from '@/lib/stateLocationMap';

// ── Constants ─────────────────────────────────────────────────────────────────

const RUN_TYPE_LABELS: Record<CampaignRunType, string> = {
  full: 'Until 7 PM EST',
  custom: 'Custom duration (min)',
};

const LEGACY_RUN_TYPE_MINUTES: Record<string, number> = {
  time_30: 30, time_45: 45, time_60: 60, time_90: 90, time_120: 120,
};

const GENERIC_QUEUE_ID = 'fc5e3102-44f1-4986-baaa-055ee92e0a98';
const HIGH_PRIORITY_QUEUE_ID = '6aa59c69-1d1a-4ec1-9ed2-6730402b9437';
const CANONICAL_QUEUE_IDS = new Set([GENERIC_QUEUE_ID, HIGH_PRIORITY_QUEUE_ID]);

const DEFAULT_CAMPAIGN_CONFIG: BucketCampaignConfig = {
  queueId: GENERIC_QUEUE_ID,
  contactFlowId: '3d24320b-c1e3-40f3-90a2-b6867ef70c85',
  sourcePhoneNumber: '',
  dialerType: 'progressive',
  bandwidthAllocation: 1.0,
  dialingCapacity: 1.0,
  amdEnabled: true,
  amdAwaitPrompt: true,
};

const CANONICAL_PHONES = new Set(Object.values(STATE_DEFAULT_PHONES));

function pickPhoneForCampaign(states: string[]): string {
  for (const state of states) {
    const phone = STATE_DEFAULT_PHONES[state];
    if (phone) return phone;
  }
  return '';
}

function _abbreviate(text: string): string {
  const words = text.split(/[^a-zA-Z]+/).filter(Boolean);
  if (words.length >= 2) return words.map((w) => w[0].toUpperCase()).slice(0, 4).join('');
  if (words.length === 1) return words[0][0].toUpperCase() + words[0].slice(1, 3).toLowerCase();
  return 'att';
}

function buildAttemptsPart(groups: string[]): string {
  const grouped = new Map<string, string[]>();
  for (const value of groups) {
    const numMatch = value.match(/\d+/);
    const num = numMatch ? numMatch[0] : '';
    const parts = value.split('/').map((p) => p.trim()).filter(Boolean);
    const category = parts.find((p) => !/\d/.test(p)) ?? '';
    const abbr = _abbreviate(category || value);
    if (!abbr) continue;
    if (!grouped.has(abbr)) grouped.set(abbr, []);
    if (num && !grouped.get(abbr)!.includes(num)) grouped.get(abbr)!.push(num);
  }
  const out: string[] = [];
  for (const [abbr, nums] of grouped) {
    if (!nums.length) out.push(abbr);
    else if (nums.length === 1) out.push(`${abbr}_${nums[0]}`);
    else out.push(`${abbr}_${nums.join('-')}`);
  }
  return out.join('_');
}

function autoNameCampaign(states: string[], groups: string[]): string {
  const statesPart = states.join('-');
  if (!groups.length) return statesPart;
  const attemptsPart = buildAttemptsPart(groups);
  return [statesPart, attemptsPart].filter(Boolean).join('-');
}

function newCampaign(overrides?: Partial<CampaignDef>): CampaignDef {
  const { states = [], groups = [], name, campaignConfig, ...rest } = overrides ?? {};
  return {
    id: crypto.randomUUID(),
    name: name ?? autoNameCampaign(states, groups),
    states,
    groups,
    run_type: 'full',
    dependsOn: [],
    deliveryType: 'campaign',
    campaignConfig: campaignConfig ?? { ...DEFAULT_CAMPAIGN_CONFIG },
    ...rest,
  };
}

function newBucket(overrides?: Partial<BucketDefV2>): BucketDefV2 {
  return {
    id: crypto.randomUUID(),
    name: '',
    run_mode: 'status_based',
    cleanup: true,
    prestart_next: true,
    parallel: false,
    campaignConfig: { ...DEFAULT_CAMPAIGN_CONFIG },
    campaigns: [newCampaign()],
    ...overrides,
  };
}

// ── Kahn's stage assignment ───────────────────────────────────────────────────

function assignStages(campaigns: CampaignDef[]): Map<string, number> {
  const idSet = new Set(campaigns.map((c) => c.id));
  const inDegree = new Map<string, number>();
  campaigns.forEach((c) => inDegree.set(c.id, 0));
  campaigns.forEach((c) =>
    c.dependsOn.forEach((p) => {
      if (idSet.has(p)) inDegree.set(c.id, (inDegree.get(c.id) ?? 0) + 1);
    }),
  );

  const stage = new Map<string, number>();
  const queue = campaigns.filter((c) => (inDegree.get(c.id) ?? 0) === 0).map((c) => c.id);
  queue.forEach((id) => stage.set(id, 0));

  while (queue.length) {
    const id = queue.shift()!;
    campaigns
      .filter((c) => c.dependsOn.includes(id))
      .forEach((c) => {
        const newStage = (stage.get(id) ?? 0) + 1;
        if ((stage.get(c.id) ?? 0) < newStage) stage.set(c.id, newStage);
        inDegree.set(c.id, (inDegree.get(c.id) ?? 1) - 1);
        if ((inDegree.get(c.id) ?? 0) <= 0) queue.push(c.id);
      });
  }

  campaigns.forEach((c) => {
    if (!stage.has(c.id)) stage.set(c.id, 0);
  });
  return stage;
}

// ── Group checkboxes ──────────────────────────────────────────────────────────

function GroupCheckboxes({
  options,
  selected,
  onChange,
}: {
  options: string[];
  selected: string[];
  onChange: (next: string[]) => void;
}) {
  const categoryMap = useMemo(() => {
    const map = new Map<string, string[]>();
    for (const opt of options) {
      const slash = opt.indexOf(' / ');
      const cat = slash > 0 ? opt.slice(0, slash) : 'Other';
      if (!map.has(cat)) map.set(cat, []);
      map.get(cat)!.push(opt);
    }
    return map;
  }, [options]);

  const toggle = (value: string) =>
    onChange(selected.includes(value) ? selected.filter((v) => v !== value) : [...selected, value]);

  if (options.length === 0) {
    return <span className="text-xs text-gray-400">No groups available</span>;
  }

  return (
    <div className="space-y-3">
      {selected.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {selected.map((v) => (
            <button
              key={v}
              type="button"
              onClick={() => toggle(v)}
              className="inline-flex items-center gap-1 rounded-full bg-blue-50 border border-blue-200 px-2 py-0.5 text-xs text-blue-700 hover:bg-blue-100 transition-colors"
            >
              <span>{v}</span>
              <span aria-hidden>×</span>
            </button>
          ))}
        </div>
      )}
      {Array.from(categoryMap.entries()).map(([cat, items]) => (
        <div key={cat}>
          <div className="text-xs font-semibold text-gray-500 mb-1">{cat}</div>
          <div className="flex flex-wrap gap-x-4 gap-y-1">
            {items.map((opt) => {
              const label = opt.includes(' / ') ? opt.split(' / ')[1] : opt;
              return (
                <label key={opt} className="flex items-center gap-1.5 text-xs cursor-pointer">
                  <input
                    type="checkbox"
                    checked={selected.includes(opt)}
                    onChange={() => toggle(opt)}
                  />
                  <span className={selected.includes(opt) ? 'text-blue-700 font-medium' : 'text-gray-700'}>
                    {label}
                  </span>
                </label>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Working hours section ─────────────────────────────────────────────────────

const DAYS_OF_WEEK = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'] as const;

function WorkingHoursSection({
  workingHours,
  onWorkingHoursChange,
}: {
  workingHours: { days: string[]; startTime: string; endTime: string } | null;
  onWorkingHoursChange: (wh: { days: string[]; startTime: string; endTime: string } | null) => void;
}) {
  const enabled = Boolean(workingHours);
  return (
    <div className="border-t border-gray-100 pt-3 space-y-2">
      <label className="flex items-center gap-2 text-xs text-gray-600 font-medium cursor-pointer">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(e) => {
            if (e.target.checked) {
              onWorkingHoursChange({ days: ['MON', 'TUE', 'WED', 'THU', 'FRI'], startTime: '08:00', endTime: '17:00' });
            } else {
              onWorkingHoursChange(null);
            }
          }}
        />
        Restrict execution to working hours
      </label>
      {workingHours && (
        <div className="space-y-2 pl-1">
          <div className="flex flex-wrap gap-1">
            {DAYS_OF_WEEK.map((day) => (
              <button
                key={day}
                type="button"
                onClick={() => {
                  const days = workingHours.days.includes(day)
                    ? workingHours.days.filter((d) => d !== day)
                    : [...workingHours.days, day];
                  onWorkingHoursChange({ ...workingHours, days });
                }}
                className={[
                  'px-2.5 py-1 rounded-lg text-xs font-medium border transition-colors',
                  workingHours.days.includes(day)
                    ? 'bg-blue-600 text-white border-blue-600'
                    : 'bg-white text-gray-600 border-gray-200 hover:border-gray-400',
                ].join(' ')}
              >
                {day}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-4">
            <div>
              <label className="block text-xs font-semibold text-gray-700 mb-1">From (COT)</label>
              <input
                type="time"
                value={workingHours.startTime}
                onChange={(e) => onWorkingHoursChange({ ...workingHours, startTime: e.target.value })}
                className="w-28 rounded-lg border border-gray-200 px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-300"
              />
            </div>
            <div>
              <label className="block text-xs font-semibold text-gray-700 mb-1">Until (COT)</label>
              <input
                type="time"
                value={workingHours.endTime}
                onChange={(e) => onWorkingHoursChange({ ...workingHours, endTime: e.target.value })}
                className="w-28 rounded-lg border border-gray-200 px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-300"
              />
            </div>
          </div>
          <p className="text-xs text-gray-400">
            Scheduled and chained runs outside this window will be skipped automatically.
          </p>
        </div>
      )}
    </div>
  );
}

// ── Trigger editor ────────────────────────────────────────────────────────────

function TriggerEditor({
  trigger,
  onChange,
  loop,
  onLoopChange,
  workingHours,
  onWorkingHoursChange,
  allPlans,
  currentPlanId,
}: {
  trigger: PlanTrigger;
  onChange: (t: PlanTrigger) => void;
  loop: PlanLoop | null;
  onLoopChange: (l: PlanLoop | null) => void;
  workingHours: { days: string[]; startTime: string; endTime: string } | null;
  onWorkingHoursChange: (wh: { days: string[]; startTime: string; endTime: string } | null) => void;
  allPlans: PlanSummaryV2[];
  currentPlanId?: string;
}) {
  return (
    <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm space-y-4">
      <h3 className="text-sm font-semibold text-gray-900">Trigger</h3>

      {/* Trigger type selector */}
      <div className="flex gap-2.5 flex-wrap">
        {(['manual', 'time', 'on_plan_complete'] as const).map((t) => (
          <label
            key={t}
            className={[
              'flex items-center gap-2.5 rounded-lg border px-3.5 py-2.5 cursor-pointer transition-colors',
              trigger.type === t ? 'border-blue-400 bg-blue-50' : 'border-gray-200 bg-white hover:border-gray-300',
            ].join(' ')}
          >
            <input
              type="radio"
              name="trigger-type"
              className="accent-blue-600"
              checked={trigger.type === t}
              onChange={() => {
                if (t === 'manual') onChange({ type: 'manual' });
                else if (t === 'time') onChange({ type: 'time', time: '08:00' });
                else onChange({ type: 'on_plan_complete', planId: '', repeat: true });
              }}
            />
            <span className="text-sm font-medium text-gray-700">
              {t === 'manual' ? '▶ Manual' : t === 'time' ? '⏰ Scheduled' : '⛓ After plan'}
            </span>
          </label>
        ))}
      </div>

      {trigger.type === 'time' && (
        <div>
          <label className="block text-xs font-semibold text-gray-700 mb-1">Time (Colombia COT)</label>
          <input
            type="time"
            value={trigger.time}
            onChange={(e) => onChange({ ...trigger, time: e.target.value })}
            className="w-32 rounded-lg border border-gray-200 px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-300"
          />
        </div>
      )}

      {trigger.type === 'on_plan_complete' && (
        <div className="space-y-3">
          <div>
            <label className="block text-xs font-semibold text-gray-700 mb-1">After plan</label>
            <select
              value={trigger.planId}
              onChange={(e) => onChange({ ...trigger, planId: e.target.value, afterBucket: undefined, afterCampaign: undefined })}
              className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm bg-white focus:outline-none focus:ring-1 focus:ring-blue-300"
            >
              <option value="">— pick a plan —</option>
              {allPlans
                .filter((p) => p.planId !== currentPlanId && !p.isTemplate)
                .map((p) => (
                  <option key={p.planId} value={p.planId}>
                    {p.name}
                  </option>
                ))}
            </select>
          </div>
          {trigger.planId && (() => {
            const upstreamBuckets = allPlans.find((p) => p.planId === trigger.planId)?.buckets ?? [];
            return upstreamBuckets.length > 1 ? (
              <div>
                <label className="block text-xs font-semibold text-gray-700 mb-1">Start after bucket (optional)</label>
                <select
                  value={trigger.afterBucket != null ? String(trigger.afterBucket) : ''}
                  onChange={(e) =>
                    onChange({
                      ...trigger,
                      afterBucket: e.target.value !== '' ? Number(e.target.value) : undefined,
                      afterCampaign: undefined,
                    })
                  }
                  className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm bg-white focus:outline-none focus:ring-1 focus:ring-blue-300"
                >
                  <option value="">— wait for whole plan —</option>
                  {upstreamBuckets.map((b, i) => (
                    <option key={i} value={i}>
                      Bucket {i + 1}{b.name ? ` · ${b.name}` : ''}
                    </option>
                  ))}
                </select>
              </div>
            ) : null;
          })()}
          {trigger.planId && trigger.afterBucket != null && (() => {
            const upstreamBuckets = allPlans.find((p) => p.planId === trigger.planId)?.buckets ?? [];
            const campaigns = upstreamBuckets[trigger.afterBucket]?.campaigns ?? [];
            return campaigns.length > 1 ? (
              <div>
                <label className="block text-xs font-semibold text-gray-700 mb-1">Start after campaign (optional)</label>
                <select
                  value={trigger.afterCampaign ?? ''}
                  onChange={(e) =>
                    onChange({
                      ...trigger,
                      afterCampaign: e.target.value !== '' ? e.target.value : undefined,
                    })
                  }
                  className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm bg-white focus:outline-none focus:ring-1 focus:ring-blue-300"
                >
                  <option value="">— any campaign in bucket —</option>
                  {campaigns.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name || c.id.slice(0, 8)}
                    </option>
                  ))}
                </select>
              </div>
            ) : null;
          })()}
          <label className="flex items-center gap-2 text-xs text-gray-600 cursor-pointer">
            <input
              type="checkbox"
              checked={trigger.repeat}
              onChange={(e) => onChange({ ...trigger, repeat: e.target.checked })}
            />
            Repeat every time upstream plan completes
          </label>
        </div>
      )}

      {/* Loop section */}
      <div className="border-t border-gray-100 pt-3 space-y-2">
        <label className="flex items-center gap-2 text-xs text-gray-600 font-medium cursor-pointer">
          <input
            type="checkbox"
            checked={Boolean(loop)}
            onChange={(e) => {
              if (e.target.checked) {
                onLoopChange({ startTime: '08:00', endTime: '19:00' });
              } else {
                onLoopChange(null);
              }
            }}
          />
          Loop — restart this plan after each completion
        </label>
        {loop && (
          <>
            <div className="flex items-center gap-4 pl-1">
              <div>
                <label className="block text-xs font-semibold text-gray-700 mb-1">From (COT)</label>
                <input
                  type="time"
                  value={loop.startTime ?? '00:00'}
                  onChange={(e) => onLoopChange({ ...loop, startTime: e.target.value || undefined })}
                  className="w-28 rounded-lg border border-gray-200 px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-300"
                />
              </div>
              <div>
                <label className="block text-xs font-semibold text-gray-700 mb-1">Until (COT)</label>
                <input
                  type="time"
                  value={loop.endTime}
                  onChange={(e) => onLoopChange({ ...loop, endTime: e.target.value })}
                  className="w-28 rounded-lg border border-gray-200 px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-300"
                />
              </div>
            </div>
            <p className="text-xs text-gray-400 pl-1">
              After each run completes, the plan restarts if the current Colombia time is within the window.
              Downstream chained plans fire on every completion regardless of this setting.
            </p>
          </>
        )}
      </div>

      <WorkingHoursSection
        workingHours={workingHours}
        onWorkingHoursChange={onWorkingHoursChange}
      />
    </div>
  );
}

// ── Campaign card (inline editor) ─────────────────────────────────────────────

function CampaignCard({
  campaign,
  bucketIndex,
  allBuckets,
  isExpanded,
  onToggle,
  onChange,
  onRemove,
  canRemove,
  queues,
  contactFlows,
  smsNumbers,
}: {
  campaign: CampaignDef;
  bucketIndex: number;
  allBuckets: BucketDefV2[];
  isExpanded: boolean;
  onToggle: () => void;
  onChange: (c: CampaignDef) => void;
  onRemove: () => void;
  canRemove: boolean;
  queues: Queue[];
  contactFlows: ContactFlow[];
  smsNumbers: SmsOriginationNumber[];
}) {
  const cfg = campaign.campaignConfig ?? { ...DEFAULT_CAMPAIGN_CONFIG };
  const [configOpen, setConfigOpen] = useState(false);
  const { locationMap } = useLocationMapping();
  const stateCodes = locationMap.map((g) => g.code);

  const prevAutoNameRef = useRef(autoNameCampaign(campaign.states, campaign.groups));

  const availableDeps = useMemo(() => {
    const result: { id: string; label: string }[] = [];
    for (let bi = 0; bi <= bucketIndex; bi++) {
      const bName = allBuckets[bi].name || `Bucket ${bi + 1}`;
      for (const c of allBuckets[bi].campaigns) {
        if (c.id !== campaign.id) {
          const cName = c.name || c.id.slice(0, 8);
          result.push({ id: c.id, label: `${bName} › ${cName}` });
        }
      }
    }
    return result;
  }, [allBuckets, bucketIndex, campaign.id]);

  const { data: groupData } = useQuery({
    queryKey: ['leads', 'distinct', 'groups'],
    queryFn: () => api.leads.distinctValues('groups'),
    staleTime: Infinity,
  });
  const groupOptions = groupData?.values ?? [];

  const { data: segmentData } = useQuery({
    queryKey: ['segments', 'list'],
    queryFn: () => api.segments.list(),
    staleTime: 60_000,
  });
  const segmentOptions = segmentData?.segments ?? [];

  const isAutoNamed = () =>
    !campaign.name || campaign.name === prevAutoNameRef.current;

  const updateStates = (states: string[]) => {
    const newAutoName = autoNameCampaign(states, campaign.groups);
    const shouldUpdateName = isAutoNamed();
    prevAutoNameRef.current = newAutoName;
    const autoPhone = pickPhoneForCampaign(states);
    const shouldUpdatePhone =
      autoPhone && (!cfg.sourcePhoneNumber || CANONICAL_PHONES.has(cfg.sourcePhoneNumber));
    onChange({
      ...campaign,
      states,
      name: shouldUpdateName ? newAutoName : campaign.name,
      campaignConfig: {
        ...cfg,
        ...(shouldUpdatePhone ? { sourcePhoneNumber: autoPhone } : {}),
      },
    });
  };

  const updateGroups = (groups: string[]) => {
    const newAutoName = autoNameCampaign(campaign.states, groups);
    const shouldUpdateName = isAutoNamed();
    prevAutoNameRef.current = newAutoName;
    const isOnlyNewLeadGroup = groups.length === 1 && groups[0] === 'New Lead / New Lead';
    const autoQueueId = isOnlyNewLeadGroup ? HIGH_PRIORITY_QUEUE_ID : GENERIC_QUEUE_ID;
    const shouldUpdateQueue = !cfg.queueId || CANONICAL_QUEUE_IDS.has(cfg.queueId);
    const autoQueueArn = queues.find((q) => q.id === autoQueueId)?.arn ?? '';
    const canonicalQueueArns = new Set(queues.filter((q) => CANONICAL_QUEUE_IDS.has(q.id)).map((q) => q.arn));
    const shouldUpdateQueueArn =
      campaign.deliveryType === 'branded' && (!cfg.queueArn || canonicalQueueArns.has(cfg.queueArn));
    onChange({
      ...campaign,
      groups,
      name: shouldUpdateName ? newAutoName : campaign.name,
      campaignConfig: {
        ...cfg,
        ...(shouldUpdateQueue ? { queueId: autoQueueId } : {}),
        ...(shouldUpdateQueueArn && autoQueueArn ? { queueArn: autoQueueArn } : {}),
      },
    });
  };

  const updateName = (name: string) => {
    prevAutoNameRef.current = '\0manual\0';
    onChange({ ...campaign, name });
  };

  const updateCfg = (patch: Partial<BucketCampaignConfig>) =>
    onChange({ ...campaign, campaignConfig: { ...cfg, ...patch } });

  const toggleDep = (depId: string) => {
    const deps = campaign.dependsOn.includes(depId)
      ? campaign.dependsOn.filter((d) => d !== depId)
      : [...campaign.dependsOn, depId];
    onChange({ ...campaign, dependsOn: deps });
  };

  return (
    <div className="flex items-center gap-3 rounded-lg border border-gray-100 bg-white overflow-hidden transition-colors">
      <div className="flex-1">
        {/* Header row */}
        <div
          className="flex items-center gap-3 px-3 py-2.5 cursor-pointer hover:bg-gray-50 transition-colors"
          onClick={onToggle}
        >
          <span className="text-gray-400 text-xs shrink-0">{isExpanded ? '▼' : '▶'}</span>
          <span className="text-sm font-medium text-gray-700 flex-1 truncate">
            {campaign.name || <span className="text-gray-400 italic">Untitled campaign</span>}
          </span>
          <span className="text-xs text-gray-400 shrink-0">
            {campaign.states.join(', ') || '—'} ·{' '}
            {campaign.groups.length > 0
              ? `${campaign.groups.length} group${campaign.groups.length !== 1 ? 's' : ''}`
              : 'no groups'}
          </span>
          {canRemove && (
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onRemove();
              }}
              className="rounded-lg px-2 py-1 text-xs text-red-400 hover:text-red-600 hover:bg-red-50 transition-colors shrink-0"
            >
              ✕
            </button>
          )}
        </div>

        {/* Expanded body */}
        {isExpanded && (
          <div className="border-t border-gray-100 px-3 py-3 space-y-3 bg-gray-50/60">
            {/* Name */}
            <div>
              <label className="block text-xs font-semibold text-gray-700 mb-1">Name</label>
              <input
                value={campaign.name}
                onChange={(e) => updateName(e.target.value)}
                placeholder={autoNameCampaign(campaign.states, campaign.groups) || 'e.g. NY New Lead 1'}
                className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-300"
              />
            </div>

            {/* States */}
            <div>
              <label className="block text-xs font-semibold text-gray-700 mb-1.5">States</label>
              <div className="flex flex-wrap gap-2">
                {stateCodes.map((code) => (
                  <label key={code} className="flex items-center gap-1 text-xs cursor-pointer">
                    <input
                      type="checkbox"
                      checked={campaign.states.includes(code)}
                      onChange={() => {
                        const states = campaign.states.includes(code)
                          ? campaign.states.filter((s) => s !== code)
                          : [...campaign.states, code];
                        updateStates(states);
                      }}
                    />
                    <span className="text-gray-700">{code}</span>
                  </label>
                ))}
              </div>
            </div>

            {/* Groups */}
            <div>
              <label className="block text-xs font-semibold text-gray-700 mb-1.5">Groups</label>
              {groupData === undefined ? (
                <div className="flex h-9 items-center gap-2 text-xs text-gray-400">
                  <Spinner /> loading…
                </div>
              ) : (
                <GroupCheckboxes
                  options={groupOptions}
                  selected={campaign.groups}
                  onChange={updateGroups}
                />
              )}
            </div>

            {/* Max lead age — only include leads created within this many minutes */}
            <div>
              <label className="block text-xs font-semibold text-gray-700 mb-1">Max lead age</label>
              <select
                value={campaign.maxLeadAgeMinutes ?? ''}
                onChange={(e) =>
                  onChange({
                    ...campaign,
                    maxLeadAgeMinutes: e.target.value ? Number(e.target.value) : undefined,
                  })
                }
                className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm bg-white focus:outline-none focus:ring-1 focus:ring-blue-300"
              >
                <option value="">None</option>
                {[10, 15, 20, 25, 30, 35].map((minutes) => (
                  <option key={minutes} value={minutes}>
                    {minutes} min
                  </option>
                ))}
              </select>
              <p className="mt-1 text-xs text-gray-400">
                Only include leads created within the selected window (Redis <code>createdAt</code>).
              </p>
            </div>

            {/* Pinned segment */}
            <div>
              <label className="block text-xs font-semibold text-gray-700 mb-1">Pinned segment</label>
              <select
                value={campaign.pinnedSegmentArn ?? ''}
                onChange={(e) =>
                  onChange({ ...campaign, pinnedSegmentArn: e.target.value || undefined })
                }
                className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm bg-white focus:outline-none focus:ring-1 focus:ring-blue-300"
              >
                <option value="">— auto (build from Redis) —</option>
                {segmentOptions.map((s) => (
                  <option key={s.segmentArn} value={s.segmentArn}>
                    {s.displayName ?? s.name}
                  </option>
                ))}
              </select>
              {campaign.pinnedSegmentArn && (
                <p className="mt-1 text-xs text-amber-600">
                  States/groups ignored — pinned segment used as-is
                </p>
              )}
            </div>

            {/* Delivery type */}
            <div>
              <label className="block text-xs font-semibold text-gray-700 mb-1">Delivery type</label>
              <select
                value={campaign.deliveryType ?? 'campaign'}
                onChange={(e) => {
                  const newType = e.target.value as 'campaign' | 'journey' | 'branded' | 'sms';
                  if (newType === 'branded') {
                    const isOnlyNewLead =
                      campaign.groups.length === 1 && campaign.groups[0] === 'New Lead / New Lead';
                    const autoQueueId = isOnlyNewLead ? HIGH_PRIORITY_QUEUE_ID : GENERIC_QUEUE_ID;
                    const autoQueueArn = queues.find((q) => q.id === autoQueueId)?.arn ?? '';
                    setConfigOpen(true);
                    onChange({
                      ...campaign,
                      deliveryType: newType,
                      campaignConfig: { ...cfg, ...(autoQueueArn ? { queueArn: autoQueueArn } : {}) },
                    });
                  } else {
                    onChange({ ...campaign, deliveryType: newType });
                  }
                }}
                className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm bg-white focus:outline-none focus:ring-1 focus:ring-blue-300"
              >
                <option value="campaign">Campaign</option>
                <option value="journey">Journey</option>
                <option value="branded">Branded (Progressive)</option>
                <option value="sms">SMS (Text)</option>
              </select>
            </div>

            {/* Run type */}
            <div>
              <label className="block text-xs font-semibold text-gray-700 mb-1">Run type</label>
              <select
                value={campaign.run_type}
                onChange={(e) => onChange({ ...campaign, run_type: e.target.value as CampaignRunType })}
                className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm bg-white focus:outline-none focus:ring-1 focus:ring-blue-300"
              >
                {(Object.entries(RUN_TYPE_LABELS) as [CampaignRunType, string][]).map(([k, v]) => (
                  <option key={k} value={k}>
                    {v}
                  </option>
                ))}
              </select>
            </div>

            {campaign.run_type === 'custom' && (
              <div>
                <label className="block text-xs font-semibold text-gray-700 mb-1">Duration (minutes)</label>
                <input
                  type="number"
                  min={1}
                  max={480}
                  value={campaign.run_duration_minutes ?? ''}
                  onChange={(e) => onChange({ ...campaign, run_duration_minutes: e.target.value ? parseInt(e.target.value, 10) : undefined })}
                  placeholder="e.g. 45"
                  className="w-28 rounded-lg border border-gray-200 px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-300"
                />
              </div>
            )}

            {/* Dependencies */}
            {availableDeps.length > 0 && (
              <div className="space-y-1">
                <div className="text-xs font-semibold text-gray-700">
                  Wait for these to complete first
                </div>
                <p className="text-xs text-gray-400">
                  All checked campaigns must finish before this one starts. Leave all unchecked to start when the bucket opens.
                </p>
                <div className="flex flex-col gap-1 max-h-28 overflow-y-auto rounded-lg border border-gray-200 bg-white p-2">
                  {availableDeps.map(({ id, label }) => (
                    <label key={id} className="flex items-center gap-2 text-xs cursor-pointer hover:bg-gray-50 rounded px-1 py-0.5">
                      <input
                        type="checkbox"
                        checked={campaign.dependsOn.includes(id)}
                        onChange={() => toggleDep(id)}
                      />
                      <span className={campaign.dependsOn.includes(id) ? 'text-blue-700 font-medium' : 'text-gray-700'}>
                        {label}
                      </span>
                    </label>
                  ))}
                </div>
              </div>
            )}

            {/* SMS config — shown only for deliveryType='sms' */}
            {campaign.deliveryType === 'sms' && (
              <div className="mt-2 rounded-lg border border-amber-200 bg-amber-50 p-3 space-y-3">
                <p className="text-xs font-semibold text-amber-700">SMS Configuration</p>
                <div className="space-y-1">
                  <label className="block text-xs font-medium text-gray-600">Origination Number</label>
                  <select
                    value={cfg.smsOriginationNumberArn ?? ''}
                    onChange={(e) => updateCfg({ smsOriginationNumberArn: e.target.value })}
                    className="w-full text-sm rounded-lg border border-gray-200 px-3 py-2 bg-white focus:outline-none focus:ring-1 focus:ring-amber-400"
                  >
                    <option value="">— Select origination number —</option>
                    {smsNumbers.map((n) => (
                      <option key={n.arn} value={n.arn}>
                        {n.phoneNumber} ({n.numberType})
                      </option>
                    ))}
                  </select>
                </div>
                <div className="space-y-1">
                  <label className="block text-xs font-medium text-gray-600">
                    Message Template{' '}
                    <span className="text-gray-400">({(cfg.smsMessageTemplate ?? '').length}/160)</span>
                  </label>
                  <textarea
                    maxLength={160}
                    value={cfg.smsMessageTemplate ?? ''}
                    onChange={(e) => updateCfg({ smsMessageTemplate: e.target.value })}
                    placeholder="Your appointment is confirmed. Reply STOP to opt out."
                    className="w-full text-sm rounded-lg border border-gray-200 px-3 py-2 h-20 resize-none focus:outline-none focus:ring-1 focus:ring-amber-400"
                  />
                  <p className="text-[10px] text-amber-600">
                    Do NOT include patient names, dates of birth, diagnoses, medications, or any identifying information.
                  </p>
                </div>
                <label className="flex items-start gap-2 text-xs cursor-pointer select-none">
                  <input
                    type="checkbox"
                    checked={cfg.phiAcknowledged ?? false}
                    onChange={(e) => updateCfg({ phiAcknowledged: e.target.checked })}
                    className="mt-0.5 accent-amber-500"
                  />
                  <span className="text-gray-600">
                    I confirm this message does <strong>not</strong> contain protected health information (PHI) —
                    no patient names, dates, diagnoses, medications, or account numbers.
                  </span>
                </label>
              </div>
            )}

            {/* Per-campaign Connect config — hidden for SMS */}
            {campaign.deliveryType !== 'sms' && <div>
              <button
                type="button"
                onClick={() => setConfigOpen((o) => !o)}
                className="text-xs text-gray-500 hover:text-gray-700 flex items-center gap-1"
              >
                <span>{configOpen ? '▼' : '▶'}</span>
                <span>Connect config (queue, flow, phone, dialer)</span>
              </button>
              {configOpen && (
                <div className="mt-2 grid grid-cols-2 gap-3">
                  {campaign.deliveryType === 'branded' ? (
                    <div>
                      <label className="block text-xs font-semibold text-gray-700 mb-1">Queue</label>
                      <select
                        value={cfg.queueArn ?? ''}
                        onChange={(e) => updateCfg({ queueArn: e.target.value })}
                        className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm bg-white focus:outline-none focus:ring-1 focus:ring-blue-300"
                      >
                        <option value="">— select queue —</option>
                        {queues.map((q) => (
                          <option key={q.id} value={q.arn}>
                            {q.name}
                          </option>
                        ))}
                      </select>
                    </div>
                  ) : (
                    <div>
                      <label className="block text-xs font-semibold text-gray-700 mb-1">Queue</label>
                      <select
                        value={cfg.queueId}
                        onChange={(e) => updateCfg({ queueId: e.target.value })}
                        className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm bg-white focus:outline-none focus:ring-1 focus:ring-blue-300"
                      >
                        <option value="">— select queue —</option>
                        {queues.map((q) => (
                          <option key={q.id} value={q.id}>
                            {q.name}
                          </option>
                        ))}
                      </select>
                    </div>
                  )}
                  <div>
                    <label className="block text-xs font-semibold text-gray-700 mb-1">Contact flow</label>
                    <select
                      value={cfg.contactFlowId}
                      onChange={(e) => updateCfg({ contactFlowId: e.target.value })}
                      className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm bg-white focus:outline-none focus:ring-1 focus:ring-blue-300"
                    >
                      <option value="">— select flow —</option>
                      {contactFlows.map((f) => (
                        <option key={f.id} value={f.id}>
                          {f.name}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label className="block text-xs font-semibold text-gray-700 mb-1">Source phone</label>
                    <input
                      value={cfg.sourcePhoneNumber}
                      onChange={(e) => updateCfg({ sourcePhoneNumber: e.target.value })}
                      placeholder="+1..."
                      className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-300"
                    />
                  </div>
                  {campaign.deliveryType !== 'branded' && (
                    <div>
                      <label className="block text-xs font-semibold text-gray-700 mb-1">Dialer type</label>
                      <select
                        value={cfg.dialerType}
                        onChange={(e) => updateCfg({ dialerType: e.target.value })}
                        className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm bg-white focus:outline-none focus:ring-1 focus:ring-blue-300"
                      >
                        <option value="progressive">Progressive</option>
                        <option value="predictive">Predictive</option>
                        <option value="agentless">Agentless</option>
                      </select>
                    </div>
                  )}
                  {campaign.deliveryType !== 'branded' && (
                    <div>
                      <label className="block text-xs font-semibold text-gray-700 mb-1">AMD</label>
                      <label className="flex items-center gap-2 text-sm cursor-pointer">
                        <input
                          type="checkbox"
                          checked={cfg.amdEnabled}
                          onChange={(e) => updateCfg({ amdEnabled: e.target.checked })}
                        />
                        <span className="text-gray-700">Enable answer machine detection</span>
                      </label>
                    </div>
                  )}
                </div>
              )}
            </div>}
          </div>
        )}
      </div>
    </div>
  );
}

// ── DAG Canvas ────────────────────────────────────────────────────────────────

function DagCanvas({
  campaigns,
  bucketIndex,
  allBuckets,
  expandedId,
  onToggle,
  onChange,
  onRemove,
  queues,
  contactFlows,
  smsNumbers,
}: {
  campaigns: CampaignDef[];
  bucketIndex: number;
  allBuckets: BucketDefV2[];
  expandedId: string | null;
  onToggle: (id: string) => void;
  onChange: (index: number, c: CampaignDef) => void;
  onRemove: (index: number) => void;
  queues: Queue[];
  contactFlows: ContactFlow[];
  smsNumbers: SmsOriginationNumber[];
}) {
  const stages = useMemo(() => assignStages(campaigns), [campaigns]);
  const maxStage = Math.max(0, ...Array.from(stages.values()));

  const columns: CampaignDef[][] = Array.from({ length: maxStage + 1 }, () => []);
  campaigns.forEach((c) => columns[stages.get(c.id) ?? 0].push(c));

  return (
    <div className="overflow-x-auto">
      <div className="flex gap-4 min-w-0">
        {columns.map((col, si) => (
          <div key={si} className="flex flex-col gap-2 min-w-[240px]">
            <div className="text-xs text-gray-400 font-medium mb-1">
              {si === 0 ? `Stage ${si + 1} (starts with bucket)` : `Stage ${si + 1} (waits for parents)`}
            </div>
            {col.map((c) => {
              const ci = campaigns.findIndex((x) => x.id === c.id);
              return (
                <CampaignCard
                  key={c.id}
                  campaign={c}
                  bucketIndex={bucketIndex}
                  allBuckets={allBuckets}
                  isExpanded={expandedId === c.id}
                  onToggle={() => onToggle(c.id)}
                  onChange={(updated) => onChange(ci, updated)}
                  onRemove={() => onRemove(ci)}
                  canRemove={campaigns.length > 1}
                  queues={queues}
                  contactFlows={contactFlows}
                  smsNumbers={smsNumbers}
                />
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Bucket editor ─────────────────────────────────────────────────────────────

function BucketEditor({
  bucket,
  bucketIndex,
  allBuckets,
  onChange,
  onRemove,
  canRemove,
  queues,
  contactFlows,
  smsNumbers,
}: {
  bucket: BucketDefV2;
  bucketIndex: number;
  allBuckets: BucketDefV2[];
  onChange: (b: BucketDefV2) => void;
  onRemove: () => void;
  canRemove: boolean;
  queues: Queue[];
  contactFlows: ContactFlow[];
  smsNumbers: SmsOriginationNumber[];
}) {
  const [collapsed, setCollapsed] = useState(false);
  const [expandedCampaignId, setExpandedCampaignId] = useState<string | null>(null);

  const toggleCampaign = (id: string) =>
    setExpandedCampaignId((prev) => (prev === id ? null : id));

  const updateCampaign = (ci: number, c: CampaignDef) =>
    onChange({ ...bucket, campaigns: bucket.campaigns.map((x, i) => (i === ci ? c : x)) });

  const removeCampaign = (ci: number) => {
    const removedId = bucket.campaigns[ci]?.id;
    const remaining = bucket.campaigns
      .filter((_, i) => i !== ci)
      .map((c) => ({
        ...c,
        dependsOn: removedId ? c.dependsOn.filter((d) => d !== removedId) : c.dependsOn,
      }));
    onChange({ ...bucket, campaigns: remaining });
  };

  const addCampaign = () => {
    const prevCfg = bucket.campaigns[bucket.campaigns.length - 1]?.campaignConfig
      ?? bucket.campaignConfig
      ?? DEFAULT_CAMPAIGN_CONFIG;
    const c = newCampaign({ campaignConfig: { ...prevCfg } });
    onChange({ ...bucket, campaigns: [...bucket.campaigns, c] });
    setExpandedCampaignId(c.id);
  };

  return (
    <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
      {/* Bucket header */}
      <div className="flex items-center gap-3 px-4 py-3 bg-gray-50 border-b border-gray-100">
        <span className="flex h-6 w-6 items-center justify-center rounded-full bg-gray-800 text-white text-xs font-bold shrink-0">
          {bucketIndex + 1}
        </span>
        <input
          value={bucket.name}
          onChange={(e) => onChange({ ...bucket, name: e.target.value })}
          placeholder="Bucket name"
          onClick={(e) => e.stopPropagation()}
          className="flex-1 rounded-lg border border-gray-200 px-3 py-1.5 text-sm font-semibold text-gray-900 focus:outline-none focus:ring-1 focus:ring-blue-300 bg-white"
        />
        {collapsed && (
          <span className="text-xs text-gray-400 shrink-0">
            {bucket.campaigns.length} campaign{bucket.campaigns.length !== 1 ? 's' : ''}
            {' · '}{bucket.run_mode === 'time_based' ? `${bucket.duration_minutes ?? 30} min` : 'status'}
            {bucket.parallel ? ' · parallel' : ''}
          </span>
        )}
        {/* Run mode toggle */}
        <div className="flex gap-1.5 shrink-0">
          {(['status_based', 'time_based'] as const).map((mode) => (
            <button
              key={mode}
              type="button"
              onClick={() => onChange({ ...bucket, run_mode: mode })}
              className={[
                'px-2.5 py-1 rounded-lg text-xs font-medium border transition-colors',
                bucket.run_mode === mode
                  ? 'bg-blue-600 text-white border-blue-600'
                  : 'bg-white text-gray-600 border-gray-200 hover:border-gray-400',
              ].join(' ')}
            >
              {mode === 'status_based' ? 'Status' : 'Time'}
            </button>
          ))}
        </div>
        <button
          type="button"
          onClick={() => setCollapsed((c) => !c)}
          className="text-gray-400 text-xs w-4 shrink-0 hover:text-gray-600"
          aria-label={collapsed ? 'Expand bucket' : 'Collapse bucket'}
        >
          {collapsed ? '▶' : '▼'}
        </button>
        {canRemove ? (
          <button
            type="button"
            onClick={onRemove}
            className="rounded-lg px-2 py-1 text-xs text-red-400 hover:text-red-600 hover:bg-red-50 transition-colors shrink-0"
          >
            ✕
          </button>
        ) : (
          <span
            title="A plan must have at least one bucket"
            className="rounded-lg px-2 py-1 text-xs text-gray-300 cursor-not-allowed shrink-0 select-none"
          >
            ✕
          </span>
        )}
      </div>

      {/* Bucket body */}
      {!collapsed && (
        <div className="p-4 space-y-4">
          {/* Time-based options */}
          {bucket.run_mode === 'time_based' && (
            <div className="flex gap-4 items-end">
              <div>
                <label className="block text-xs font-semibold text-gray-700 mb-1">Duration (minutes)</label>
                <input
                  type="number"
                  min={5}
                  value={bucket.duration_minutes ?? 30}
                  onChange={(e) => onChange({ ...bucket, duration_minutes: Number(e.target.value) })}
                  className="w-24 rounded-lg border border-gray-200 px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-300"
                />
              </div>
              <label className="flex items-center gap-1.5 text-xs text-gray-600 pb-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={bucket.prestart_next ?? true}
                  onChange={(e) => onChange({ ...bucket, prestart_next: e.target.checked })}
                />
                Pre-start next bucket 5 min early
              </label>
            </div>
          )}

          {/* Bucket flags */}
          <div className="flex flex-wrap gap-4">
            <label className="flex items-center gap-1.5 text-xs text-gray-600 cursor-pointer">
              <input
                type="checkbox"
                checked={bucket.cleanup}
                onChange={(e) => onChange({ ...bucket, cleanup: e.target.checked })}
              />
              Clean up campaigns after bucket
            </label>
            {bucketIndex > 0 && (
              <label className="flex items-center gap-1.5 text-xs text-gray-600 cursor-pointer">
                <input
                  type="checkbox"
                  checked={bucket.parallel ?? false}
                  onChange={(e) => onChange({ ...bucket, parallel: e.target.checked })}
                />
                Run in parallel with previous bucket
              </label>
            )}
          </div>

          {/* Campaigns */}
          <div>
            <div className="flex items-center justify-between mb-2">
              <span className="text-xs font-semibold text-gray-700">Campaigns</span>
              <button
                type="button"
                onClick={addCampaign}
                className="inline-flex items-center gap-1.5 rounded-lg border border-dashed border-gray-300 px-3 py-1.5 text-xs font-medium text-gray-500 hover:border-gray-400 hover:text-gray-700 transition-colors"
              >
                + Add campaign
              </button>
            </div>
            <DagCanvas
              campaigns={bucket.campaigns}
              bucketIndex={bucketIndex}
              allBuckets={allBuckets}
              expandedId={expandedCampaignId}
              onToggle={toggleCampaign}
              onChange={updateCampaign}
              onRemove={removeCampaign}
              queues={queues}
              contactFlows={contactFlows}
              smsNumbers={smsNumbers}
            />
          </div>
        </div>
      )}
    </div>
  );
}

// ── Overlap detection ─────────────────────────────────────────────────────────

type Overlap = { b1: number; b2: number; c1: number; c2: number; key: string };

function detectOverlaps(buckets: BucketDefV2[]): Overlap[] {
  const overlaps: Overlap[] = [];
  const all: { bi: number; ci: number; states: string[]; groups: string[] }[] = [];
  buckets.forEach((b, bi) =>
    b.campaigns.forEach((c, ci) =>
      all.push({ bi, ci, states: c.states, groups: c.groups }),
    ),
  );
  for (let i = 0; i < all.length; i++) {
    for (let j = i + 1; j < all.length; j++) {
      const a = all[i], b = all[j];
      if (a.groups.some((g) => b.groups.includes(g)) && a.states.some((s) => b.states.includes(s))) {
        overlaps.push({
          b1: a.bi, c1: a.ci, b2: b.bi, c2: b.ci,
          key: `${a.bi}-${a.ci}-${b.bi}-${b.ci}`,
        });
      }
    }
  }
  return overlaps;
}

// ── Main component ────────────────────────────────────────────────────────────

export function PlanNew() {
  const navigate = useNavigate();
  const { id } = useParams<{ id: string }>();
  const qc = useQueryClient();
  const isEdit = Boolean(id);

  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [trigger, setTrigger] = useState<PlanTrigger>({ type: 'manual' });
  const [loop, setLoop] = useState<PlanLoop | null>(null);
  const [workingHours, setWorkingHours] = useState<{ days: string[]; startTime: string; endTime: string } | null>(null);
  const [buckets, setBuckets] = useState<BucketDefV2[]>([newBucket()]);
  const [isTemplate, setIsTemplate] = useState(false);
  const [overlapAck, setOverlapAck] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const errorBannerRef = useRef<HTMLDivElement>(null);
  const overlapRef = useRef<HTMLDivElement>(null);

  // Load plan when editing
  const { data: existingData, isLoading: loadingPlan } = useQuery({
    queryKey: ['plans', id],
    queryFn: () => api.plans.getV2(id!),
    enabled: isEdit,
  });

  useEffect(() => {
    if (existingData?.plan) {
      const p = existingData.plan;
      setName(p.name);
      setDescription(p.description ?? '');
      setTrigger(p.trigger ?? { type: 'manual' });
      setLoop(p.loop ?? null);
      setWorkingHours((p as any).workingHours ?? null);
      const migratedBuckets = p.buckets.map((b) => ({
        ...b,
        campaigns: b.campaigns.map((c) => {
          const legacyMins = LEGACY_RUN_TYPE_MINUTES[c.run_type as string];
          return {
            ...c,
            run_type: legacyMins ? ('custom' as CampaignRunType) : c.run_type,
            run_duration_minutes: legacyMins ?? c.run_duration_minutes,
            campaignConfig: c.campaignConfig ?? b.campaignConfig ?? { ...DEFAULT_CAMPAIGN_CONFIG },
          };
        }),
      }));
      setBuckets(migratedBuckets.length ? migratedBuckets : [newBucket()]);
      setIsTemplate(p.isTemplate ?? false);
    }
  }, [existingData]);

  // Load all plans for trigger picker
  const { data: allPlansData } = useQuery({
    queryKey: ['plans'],
    queryFn: () => api.plans.listV2(),
  });
  const allPlans = allPlansData?.plans ?? [];

  const { data: queuesData } = useQuery({
    queryKey: ['queues'],
    queryFn: () => api.campaigns.queues(),
  });
  const queues = queuesData?.queues ?? [];

  const { data: contactFlowsData } = useQuery({
    queryKey: ['contactFlows'],
    queryFn: () => api.campaigns.contactFlows(),
  });
  const contactFlows = contactFlowsData?.contactFlows ?? [];

  const { data: smsNumbersData } = useQuery({
    queryKey: ['sms', 'numbers'],
    queryFn: () => api.sms.listNumbers(),
    staleTime: 5 * 60_000,
  });
  const smsNumbers = smsNumbersData?.originationNumbers ?? [];

  const overlaps = useMemo(() => detectOverlaps(buckets), [buckets]);

  const saveMutation = useMutation({
    mutationFn: (body: { name: string; description: string; trigger: PlanTrigger; loop: PlanLoop | null; workingHours: { days: string[]; startTime: string; endTime: string } | null; buckets: BucketDefV2[]; isTemplate: boolean }) =>
      isEdit
        ? api.plans.updateV2(id!, body)
        : api.plans.createV2(body),
    onSuccess: (plan) => {
      // Seed cache immediately so PlanDetail renders with fresh data on first paint,
      // avoiding a stale-data flash caused by the background refetch delay.
      const prev = qc.getQueryData<{ plan: PlanSummaryV2; latestRun?: PlanRunV2 }>(['plans', plan.planId]);
      qc.setQueryData(['plans', plan.planId], { plan, latestRun: prev?.latestRun });
      qc.invalidateQueries({ queryKey: ['plans'] });
      navigate(`/plans/${plan.planId}`);
    },
  });

  const setErrorAndScroll = (msg: string, scrollToOverlap = false) => {
    setSaveError(msg);
    if (scrollToOverlap && overlapRef.current) {
      overlapRef.current.scrollIntoView({ behavior: 'smooth', block: 'center' });
    } else {
      setTimeout(() => errorBannerRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' }), 50);
    }
  };

  const handleSave = () => {
    setSaveError(null);
    if (!name.trim()) { setErrorAndScroll('Plan name is required.'); return; }
    if (buckets.length === 0) { setErrorAndScroll('Add at least one bucket.'); return; }
    for (let bi = 0; bi < buckets.length; bi++) {
      const b = buckets[bi];
      if (b.campaigns.length === 0) { setErrorAndScroll(`Bucket ${bi + 1} has no campaigns.`); return; }
      for (let ci = 0; ci < b.campaigns.length; ci++) {
        const c = b.campaigns[ci];
        if (c.states.length === 0 && !c.pinnedSegmentArn) { setErrorAndScroll(`"${c.name || `Campaign ${ci + 1}`}" in bucket ${bi + 1} has no states selected.`); return; }
        if (c.run_type === 'custom' && (!c.run_duration_minutes || c.run_duration_minutes < 1)) {
          setErrorAndScroll(`"${c.name || `Campaign ${ci + 1}`}" in bucket ${bi + 1} needs a duration > 0 minutes.`); return;
        }
        if (c.deliveryType === 'sms') {
          const cfg = c.campaignConfig;
          if (!cfg?.smsOriginationNumberArn) { setErrorAndScroll(`"${c.name || `Campaign ${ci + 1}`}" in bucket ${bi + 1}: select an SMS origination number.`); return; }
          if (!cfg?.smsMessageTemplate) { setErrorAndScroll(`"${c.name || `Campaign ${ci + 1}`}" in bucket ${bi + 1}: SMS message template is required.`); return; }
          if (!cfg?.phiAcknowledged) { setErrorAndScroll(`"${c.name || `Campaign ${ci + 1}`}" in bucket ${bi + 1}: confirm the message contains no PHI before saving.`); return; }
        }
      }
    }
    if (trigger.type === 'on_plan_complete' && !trigger.planId) {
      setErrorAndScroll('Select a plan for the "After plan" trigger.'); return;
    }
    if (overlaps.length > 0 && !overlapAck) {
      setErrorAndScroll('There are overlapping campaign filters. Scroll down to review and acknowledge before saving.', true); return;
    }

    saveMutation.mutate({ name: name.trim(), description, trigger, loop, workingHours, buckets, isTemplate });
  };

  const addBucket = () => setBuckets((prev) => [...prev, newBucket()]);

  const updateBucket = (i: number, b: BucketDefV2) =>
    setBuckets((prev) => {
      const removedIds = new Set(
        prev[i].campaigns.map((c) => c.id).filter((id) => !b.campaigns.some((c2) => c2.id === id)),
      );
      return prev.map((x, idx) => {
        if (idx === i) return b;
        if (removedIds.size === 0) return x;
        return {
          ...x,
          campaigns: x.campaigns.map((c) => ({
            ...c,
            dependsOn: c.dependsOn.filter((depId) => !removedIds.has(depId)),
          })),
        };
      });
    });

  const removeBucket = (i: number) =>
    setBuckets((prev) => {
      const removedIds = new Set(prev[i].campaigns.map((c) => c.id));
      return prev
        .filter((_, idx) => idx !== i)
        .map((b) => ({
          ...b,
          campaigns: b.campaigns.map((c) => ({
            ...c,
            dependsOn: c.dependsOn.filter((depId) => !removedIds.has(depId)),
          })),
        }));
    });

  const moveBucket = (i: number, dir: -1 | 1) => {
    setBuckets((prev) => {
      const arr = [...prev];
      const j = i + dir;
      if (j < 0 || j >= arr.length) return arr;
      [arr[i], arr[j]] = [arr[j], arr[i]];
      const bucketOf = new Map<string, number>();
      arr.forEach((b, bi) => b.campaigns.forEach((c) => bucketOf.set(c.id, bi)));
      return arr.map((b, bi) => ({
        ...b,
        campaigns: b.campaigns.map((c) => ({
          ...c,
          dependsOn: c.dependsOn.filter((depId) => {
            const depBi = bucketOf.get(depId);
            return depBi !== undefined && depBi < bi;
          }),
        })),
      }));
    });
  };

  if (isEdit && loadingPlan) {
    return (
      <div className="flex items-center justify-center py-20">
        <Spinner />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      {/* Page header */}
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h2 className="text-xl font-semibold tracking-tight">
            {isEdit ? 'Edit Plan' : 'New Plan'}
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">
            {isEdit
              ? 'Update plan details, trigger, and bucket configuration.'
              : 'Configure a new outbound dialing plan with buckets and campaigns.'}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => navigate(-1)}
            className="rounded-lg border border-gray-200 px-4 py-2.5 text-sm font-medium hover:bg-gray-50 transition-colors"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleSave}
            disabled={saveMutation.isPending || !name.trim()}
            className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 text-white px-4 py-2.5 text-sm font-semibold hover:bg-blue-700 transition-colors disabled:opacity-50"
          >
            {saveMutation.isPending ? <Spinner /> : 'Save Plan'}
          </button>
        </div>
      </div>

      {/* Validation / save error — always visible near top */}
      {(saveError || saveMutation.isError) && (
        <div ref={errorBannerRef} className="text-sm text-red-600 bg-red-50 border border-red-200 rounded-xl p-3 flex items-start gap-2">
          <span className="text-red-500 mt-0.5 shrink-0">⚠</span>
          <span>{saveError ?? String((saveMutation.error as Error)?.message ?? 'Save failed')}</span>
        </div>
      )}

      {/* Plan metadata */}
      <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm space-y-4">
        <h3 className="text-sm font-semibold text-gray-900">Plan details</h3>
        <div>
          <label className="block text-xs font-semibold text-gray-700 mb-1">Plan name</label>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. NY Morning Wave"
            autoFocus
            className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-300"
          />
        </div>
        <div>
          <label className="block text-xs font-semibold text-gray-700 mb-1">Description (optional)</label>
          <input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Brief description"
            className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-300"
          />
        </div>
        <label className="flex items-center gap-2 text-xs text-gray-600 cursor-pointer">
          <input type="checkbox" checked={isTemplate} onChange={(e) => setIsTemplate(e.target.checked)} />
          Save as template (cannot be run directly)
        </label>
      </div>

      {/* Trigger */}
      <TriggerEditor
        trigger={trigger}
        onChange={setTrigger}
        loop={loop}
        onLoopChange={setLoop}
        workingHours={workingHours}
        onWorkingHoursChange={setWorkingHours}
        allPlans={allPlans}
        currentPlanId={id}
      />

      {/* Buckets */}
      <div className="flex flex-col gap-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-gray-900">Buckets</h3>
          <button
            type="button"
            onClick={addBucket}
            className="inline-flex items-center gap-1.5 rounded-lg border border-dashed border-gray-300 px-3 py-1.5 text-xs font-medium text-gray-500 hover:border-gray-400 hover:text-gray-700 transition-colors"
          >
            + Add bucket
          </button>
        </div>

        {buckets.map((bucket, bi) => (
          <div key={bucket.id} className="relative">
            <div className="flex items-start gap-2">
              {/* Up/down reorder arrows */}
              <div className="flex flex-col gap-1 pt-3.5">
                <button
                  type="button"
                  onClick={() => moveBucket(bi, -1)}
                  disabled={bi === 0}
                  className="text-gray-400 hover:text-gray-600 disabled:opacity-30 text-xs leading-none"
                >
                  ↑
                </button>
                <button
                  type="button"
                  onClick={() => moveBucket(bi, 1)}
                  disabled={bi === buckets.length - 1}
                  className="text-gray-400 hover:text-gray-600 disabled:opacity-30 text-xs leading-none"
                >
                  ↓
                </button>
              </div>
              <div className="flex-1">
                <BucketEditor
                  bucket={bucket}
                  bucketIndex={bi}
                  allBuckets={buckets}
                  onChange={(b) => updateBucket(bi, b)}
                  onRemove={() => removeBucket(bi)}
                  canRemove={buckets.length > 1}
                  queues={queues}
                  contactFlows={contactFlows}
                  smsNumbers={smsNumbers}
                />
              </div>
            </div>

            {/* Chain connector */}
            {bi < buckets.length - 1 && (
              <div className="flex items-center gap-2 my-2 ml-8">
                <div className="flex-1 border-t border-dashed border-gray-200" />
                <span className="text-xs text-gray-400 font-medium">then</span>
                <div className="flex-1 border-t border-dashed border-gray-200" />
              </div>
            )}
          </div>
        ))}
      </div>

      {/* Overlap warning */}
      {overlaps.length > 0 && (
        <div ref={overlapRef} className="bg-amber-50 border border-amber-200 rounded-xl p-4 space-y-2">
          <div className="text-sm font-semibold text-amber-800">Overlapping campaign filters detected</div>
          <ul className="text-xs text-amber-700 space-y-1">
            {overlaps.map((o) => {
              const c1 = buckets[o.b1].campaigns[o.c1];
              const c2 = buckets[o.b2].campaigns[o.c2];
              return (
                <li key={o.key}>
                  B{o.b1 + 1}:{c1?.name || 'campaign'} and B{o.b2 + 1}:{c2?.name || 'campaign'} share groups [
                  {c1?.groups.filter((g) => c2?.groups.includes(g)).join(', ')}] in states [
                  {c1?.states.filter((s) => c2?.states.includes(s)).join(', ')}]
                </li>
              );
            })}
          </ul>
          <label className="flex items-center gap-2 text-xs text-amber-800 font-medium cursor-pointer">
            <input type="checkbox" checked={overlapAck} onChange={(e) => setOverlapAck(e.target.checked)} />
            I acknowledge this overlap and want to proceed
          </label>
        </div>
      )}

      {/* Bottom action bar */}
      <div className="flex justify-end gap-2 pb-8">
        <button
          type="button"
          onClick={() => navigate(-1)}
          className="rounded-lg border border-gray-200 px-4 py-2.5 text-sm font-medium hover:bg-gray-50 transition-colors"
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={handleSave}
          disabled={saveMutation.isPending}
          className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 text-white px-4 py-2.5 text-sm font-semibold hover:bg-blue-700 transition-colors disabled:opacity-50"
        >
          {saveMutation.isPending ? <Spinner /> : 'Save Plan'}
        </button>
      </div>
    </div>
  );
}
