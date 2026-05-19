import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useParams } from 'react-router-dom';

import { Button, Card, Field, Input, Select, Spinner } from '@/components/ui';
import {
  api,
  type BucketCampaignConfig,
  type BucketDefV2,
  type CampaignDef,
  type CampaignRunType,
  type ContactFlow,
  type PlanLoop,
  type PlanSummaryV2,
  type PlanTrigger,
  type Queue,
} from '@/lib/api';
import { STATE_DEFAULT_PHONES } from '@/lib/areaCodeMap';

// ── Constants ─────────────────────────────────────────────────────────────────

const STATE_CODES = ['NY', 'NJ', 'LI', 'CT', 'MD', 'TX', 'NCA', 'SCA'];

const RUN_TYPE_LABELS: Record<CampaignRunType, string> = {
  full: 'Until 7 PM EST',
  custom: 'Custom duration (min)',
};

const LEGACY_RUN_TYPE_MINUTES: Record<string, number> = {
  time_30: 30, time_45: 45, time_60: 60, time_90: 90, time_120: 120,
};

const GENERIC_QUEUE_ID = 'fc5e3102-44f1-4986-baaa-055ee92e0a98';        // agents outbound
const HIGH_PRIORITY_QUEUE_ID = '6aa59c69-1d1a-4ec1-9ed2-6730402b9437'; // high priority agents outbounds
const CANONICAL_QUEUE_IDS = new Set([GENERIC_QUEUE_ID, HIGH_PRIORITY_QUEUE_ID]);

// Production defaults — queueId and contactFlowId are fixed across all states.
// sourcePhoneNumber is auto-derived from campaign states via STATE_DEFAULT_PHONES.
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

// Canonical phone numbers per state — auto-picked when campaigns have states selected.
// Only override sourcePhoneNumber when current value is empty or is itself a canonical default,
// so manual overrides are never clobbered.
const CANONICAL_PHONES = new Set(Object.values(STATE_DEFAULT_PHONES));


function pickPhoneForCampaign(states: string[]): string {
  for (const state of states) {
    const phone = STATE_DEFAULT_PHONES[state];
    if (phone) return phone;
  }
  return '';
}

/** Derive a human-readable campaign name from states + groups.
 *  "NY-NJ" + ["New Lead / 1st Attempt"] → "NY-NJ-NL_1"
 *  "TX"   + ["Cancellation / 2nd Attempt", "Cancellation / 3rd Attempt"] → "TX-Can_2-3"
 *
 * Mirrors build_attempts_part() in builders.py exactly.
 */
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

// ── Group checkboxes (replaces MultiSelect for campaign groups) ───────────────

function GroupCheckboxes({
  options,
  selected,
  onChange,
}: {
  options: string[];
  selected: string[];
  onChange: (next: string[]) => void;
}) {
  // Build category → items map preserving insertion order
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
    return <span className="text-xs text-muted-foreground">No groups available</span>;
  }

  return (
    <div className="space-y-3">
      {/* Selected summary pills */}
      {selected.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {selected.map((v) => (
            <button
              key={v}
              type="button"
              onClick={() => toggle(v)}
              className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-2 py-0.5 text-xs text-foreground hover:bg-primary/20"
            >
              <span>{v}</span>
              <span aria-hidden>×</span>
            </button>
          ))}
        </div>
      )}
      {/* Category sections */}
      {Array.from(categoryMap.entries()).map(([cat, items]) => (
        <div key={cat}>
          <div className="text-xs font-medium text-gray-500 mb-1">{cat}</div>
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

// ── Trigger editor ────────────────────────────────────────────────────────────

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
    <div className="border-t pt-3 space-y-2">
      <label className="flex items-center gap-2 text-xs text-gray-600 font-medium">
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
        <div className="space-y-2">
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
                className={`px-2 py-1 rounded text-xs font-medium border transition-colors ${
                  workingHours.days.includes(day)
                    ? 'bg-blue-600 text-white border-blue-600'
                    : 'bg-white text-gray-600 border-gray-300 hover:border-blue-400'
                }`}
              >
                {day}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-3">
            <Field label="From (COT)">
              <Input
                type="time"
                value={workingHours.startTime}
                onChange={(e) => onWorkingHoursChange({ ...workingHours, startTime: e.target.value })}
                className="w-28"
              />
            </Field>
            <Field label="Until (COT)">
              <Input
                type="time"
                value={workingHours.endTime}
                onChange={(e) => onWorkingHoursChange({ ...workingHours, endTime: e.target.value })}
                className="w-28"
              />
            </Field>
          </div>
          <p className="text-xs text-gray-400">
            Scheduled and chained runs outside this window will be skipped automatically.
          </p>
        </div>
      )}
    </div>
  );
}

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
    <Card className="p-4 space-y-3">
      <div className="font-medium text-sm text-gray-700">Trigger</div>
      <div className="flex gap-2">
        {(['manual', 'time', 'on_plan_complete'] as const).map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => {
              if (t === 'manual') onChange({ type: 'manual' });
              else if (t === 'time') onChange({ type: 'time', time: '08:00' });
              else onChange({ type: 'on_plan_complete', planId: '', repeat: true });
            }}
            className={`px-3 py-1.5 rounded text-xs font-medium border transition-colors ${
              trigger.type === t
                ? 'bg-blue-600 text-white border-blue-600'
                : 'bg-white text-gray-600 border-gray-300 hover:border-blue-400'
            }`}
          >
            {t === 'manual' ? '▶ Manual' : t === 'time' ? '⏰ Scheduled' : '⛓ After plan'}
          </button>
        ))}
      </div>

      {trigger.type === 'time' && (
        <Field label="Time (Colombia COT)">
          <Input
            type="time"
            value={trigger.time}
            onChange={(e) => onChange({ ...trigger, time: e.target.value })}
            className="w-32"
          />
        </Field>
      )}

      {trigger.type === 'on_plan_complete' && (
        <div className="space-y-2">
          <Field label="After plan">
            <Select
              value={trigger.planId}
              onChange={(e) => onChange({ ...trigger, planId: e.target.value, afterBucket: undefined, afterCampaign: undefined })}
            >
              <option value="">— pick a plan —</option>
              {allPlans
                .filter((p) => p.planId !== currentPlanId && !p.isTemplate)
                .map((p) => (
                  <option key={p.planId} value={p.planId}>
                    {p.name}
                  </option>
                ))}
            </Select>
          </Field>
          {trigger.planId && (() => {
            const upstreamBuckets = allPlans.find((p) => p.planId === trigger.planId)?.buckets ?? [];
            return upstreamBuckets.length > 1 ? (
              <Field label="Start after bucket (optional)">
                <Select
                  value={trigger.afterBucket != null ? String(trigger.afterBucket) : ''}
                  onChange={(e) =>
                    onChange({
                      ...trigger,
                      afterBucket: e.target.value !== '' ? Number(e.target.value) : undefined,
                      afterCampaign: undefined,
                    })
                  }
                >
                  <option value="">— wait for whole plan —</option>
                  {upstreamBuckets.map((b, i) => (
                    <option key={i} value={i}>
                      Bucket {i + 1}{b.name ? ` · ${b.name}` : ''}
                    </option>
                  ))}
                </Select>
              </Field>
            ) : null;
          })()}
          {trigger.planId && trigger.afterBucket != null && (() => {
            const upstreamBuckets = allPlans.find((p) => p.planId === trigger.planId)?.buckets ?? [];
            const campaigns = upstreamBuckets[trigger.afterBucket]?.campaigns ?? [];
            return campaigns.length > 1 ? (
              <Field label="Start after campaign (optional)">
                <Select
                  value={trigger.afterCampaign ?? ''}
                  onChange={(e) =>
                    onChange({
                      ...trigger,
                      afterCampaign: e.target.value !== '' ? e.target.value : undefined,
                    })
                  }
                >
                  <option value="">— any campaign in bucket —</option>
                  {campaigns.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name || c.id.slice(0, 8)}
                    </option>
                  ))}
                </Select>
              </Field>
            ) : null;
          })()}
          <label className="flex items-center gap-2 text-xs text-gray-600">
            <input
              type="checkbox"
              checked={trigger.repeat}
              onChange={(e) => onChange({ ...trigger, repeat: e.target.checked })}
            />
            Repeat every time upstream plan completes
          </label>
        </div>
      )}

      {/* ── Loop section — independent of trigger type ── */}
      <div className="border-t pt-3 space-y-2">
        <label className="flex items-center gap-2 text-xs text-gray-600 font-medium">
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
          <div className="flex items-center gap-3">
            <Field label="From (COT)">
              <Input
                type="time"
                value={loop.startTime ?? '00:00'}
                onChange={(e) => onLoopChange({ ...loop, startTime: e.target.value || undefined })}
                className="w-28"
              />
            </Field>
            <Field label="Until (COT)">
              <Input
                type="time"
                value={loop.endTime}
                onChange={(e) => onLoopChange({ ...loop, endTime: e.target.value })}
                className="w-28"
              />
            </Field>
          </div>
        )}
        {loop && (
          <p className="text-xs text-gray-400">
            After each run completes, the plan restarts if the current Colombia time is within the window.
            Downstream chained plans fire on every completion regardless of this setting.
          </p>
        )}
      </div>

      <WorkingHoursSection
        workingHours={workingHours}
        onWorkingHoursChange={onWorkingHoursChange}
      />
    </Card>
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
}) {
  const cfg = campaign.campaignConfig ?? { ...DEFAULT_CAMPAIGN_CONFIG };
  const [configOpen, setConfigOpen] = useState(false);

  // Track last auto-generated name so we know when to update it vs respect manual edits
  const prevAutoNameRef = useRef(autoNameCampaign(campaign.states, campaign.groups));

  // Campaigns available as dependencies: same or earlier buckets, excluding self
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
    const shouldUpdateName = isAutoNamed(); // capture BEFORE updating ref
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
    const shouldUpdateName = isAutoNamed(); // capture BEFORE updating ref
    prevAutoNameRef.current = newAutoName;
    // HIGH_PRIORITY only when the single selected group is exactly "New Lead / New Lead"
    const isOnlyNewLeadGroup = groups.length === 1 && groups[0] === 'New Lead / New Lead';
    const autoQueue = isOnlyNewLeadGroup ? HIGH_PRIORITY_QUEUE_ID : GENERIC_QUEUE_ID;
    const shouldUpdateQueue = !cfg.queueId || CANONICAL_QUEUE_IDS.has(cfg.queueId);
    onChange({
      ...campaign,
      groups,
      name: shouldUpdateName ? newAutoName : campaign.name,
      campaignConfig: {
        ...cfg,
        ...(shouldUpdateQueue ? { queueId: autoQueue } : {}),
      },
    });
  };

  const updateName = (name: string) => {
    // Mark as manual so future state/group changes don't overwrite
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
    <div className="border rounded-lg bg-white shadow-sm">
      <div
        className="flex items-center gap-2 px-3 py-2 cursor-pointer hover:bg-gray-50"
        onClick={onToggle}
      >
        <span className="text-gray-400 text-xs">{isExpanded ? '▼' : '▶'}</span>
        <span className="text-sm font-medium flex-1 truncate">
          {campaign.name || <span className="text-gray-400 italic">Untitled campaign</span>}
        </span>
        <span className="text-xs text-gray-400">
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
            className="text-red-400 hover:text-red-600 text-xs px-1"
          >
            ✕
          </button>
        )}
      </div>

      {isExpanded && (
        <div className="border-t px-3 py-3 space-y-3 bg-gray-50">
          <Field label="Name">
            <Input
              value={campaign.name}
              onChange={(e) => updateName(e.target.value)}
              placeholder={autoNameCampaign(campaign.states, campaign.groups) || 'e.g. NY New Lead 1'}
            />
          </Field>

          <Field label="States">
            <div className="flex flex-wrap gap-2">
              {STATE_CODES.map((code) => (
                <label key={code} className="flex items-center gap-1 text-xs">
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
                  {code}
                </label>
              ))}
            </div>
          </Field>

          <Field label="Groups">
            {groupData === undefined ? (
              <div className="flex h-9 items-center gap-2 text-xs text-muted-foreground">
                <Spinner /> loading…
              </div>
            ) : (
              <GroupCheckboxes
                options={groupOptions}
                selected={campaign.groups}
                onChange={updateGroups}
              />
            )}
          </Field>

          <Field label="Pinned segment">
            <Select
              value={campaign.pinnedSegmentArn ?? ''}
              onChange={(e) =>
                onChange({ ...campaign, pinnedSegmentArn: e.target.value || undefined })
              }
            >
              <option value="">— auto (build from Redis) —</option>
              {segmentOptions.map((s) => (
                <option key={s.segmentArn} value={s.segmentArn}>
                  {s.displayName ?? s.name}
                </option>
              ))}
            </Select>
            {campaign.pinnedSegmentArn && (
              <p className="mt-1 text-xs text-amber-600">
                States/groups ignored — pinned segment used as-is
              </p>
            )}
          </Field>

          <Field label="Run type">
            <Select
              value={campaign.run_type}
              onChange={(e) => onChange({ ...campaign, run_type: e.target.value as CampaignRunType })}
            >
              {(Object.entries(RUN_TYPE_LABELS) as [CampaignRunType, string][]).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </Select>
          </Field>

          {campaign.run_type === 'custom' && (
            <Field label="Duration (minutes)">
              <Input
                type="number"
                min={1}
                max={480}
                value={campaign.run_duration_minutes ?? ''}
                onChange={(e) => onChange({ ...campaign, run_duration_minutes: e.target.value ? parseInt(e.target.value, 10) : undefined })}
                placeholder="e.g. 45"
                className="w-28"
              />
            </Field>
          )}

          {availableDeps.length > 0 && (
            <div className="space-y-1">
              <div className="text-xs font-medium text-gray-700">
                Wait for these to complete first
              </div>
              <p className="text-xs text-gray-400">
                All checked campaigns must finish before this one starts. Leave all unchecked to start when the bucket opens.
              </p>
              <div className="flex flex-col gap-1 max-h-28 overflow-y-auto rounded border border-border bg-muted/20 p-2">
                {availableDeps.map(({ id, label }) => (
                  <label key={id} className="flex items-center gap-2 text-xs cursor-pointer hover:bg-muted/40 rounded px-1 py-0.5">
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

          {/* Per-campaign Connect config */}
          <div>
            <button
              type="button"
              onClick={() => setConfigOpen((o) => !o)}
              className="text-xs text-gray-500 hover:text-gray-700"
            >
              {configOpen ? '▼' : '▶'} Connect config (queue, flow, phone, dialer)
            </button>
            {configOpen && (
              <div className="mt-2 grid grid-cols-2 gap-3">
                <Field label="Queue">
                  <Select
                    value={cfg.queueId}
                    onChange={(e) => updateCfg({ queueId: e.target.value })}
                  >
                    <option value="">— select queue —</option>
                    {queues.map((q) => (
                      <option key={q.id} value={q.id}>
                        {q.name}
                      </option>
                    ))}
                  </Select>
                </Field>
                <Field label="Contact flow">
                  <Select
                    value={cfg.contactFlowId}
                    onChange={(e) => updateCfg({ contactFlowId: e.target.value })}
                  >
                    <option value="">— select flow —</option>
                    {contactFlows.map((f) => (
                      <option key={f.id} value={f.id}>
                        {f.name}
                      </option>
                    ))}
                  </Select>
                </Field>
                <Field label="Source phone">
                  <Input
                    value={cfg.sourcePhoneNumber}
                    onChange={(e) => updateCfg({ sourcePhoneNumber: e.target.value })}
                    placeholder="+1..."
                  />
                </Field>
                <Field label="Dialer type">
                  <Select
                    value={cfg.dialerType}
                    onChange={(e) => updateCfg({ dialerType: e.target.value })}
                  >
                    <option value="progressive">Progressive</option>
                    <option value="predictive">Predictive</option>
                    <option value="agentless">Agentless</option>
                  </Select>
                </Field>
                <Field label="AMD">
                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={cfg.amdEnabled}
                      onChange={(e) => updateCfg({ amdEnabled: e.target.checked })}
                    />
                    Enable answer machine detection
                  </label>
                </Field>
              </div>
            )}
          </div>
        </div>
      )}
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
}) {
  const stages = useMemo(() => assignStages(campaigns), [campaigns]);
  const maxStage = Math.max(0, ...Array.from(stages.values()));

  // Group campaigns by stage for column layout
  const columns: CampaignDef[][] = Array.from({ length: maxStage + 1 }, () => []);
  campaigns.forEach((c) => columns[stages.get(c.id) ?? 0].push(c));

  return (
    <div className="overflow-x-auto">
      <div className="flex gap-4 min-w-0">
        {columns.map((col, si) => (
          <div key={si} className="flex flex-col gap-2 min-w-[240px]">
            {si === 0 && (
              <div className="text-xs text-gray-400 font-medium mb-1">Stage {si + 1} (starts with bucket)</div>
            )}
            {si > 0 && (
              <div className="text-xs text-gray-400 font-medium mb-1">Stage {si + 1} (waits for parents)</div>
            )}
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
}: {
  bucket: BucketDefV2;
  bucketIndex: number;
  allBuckets: BucketDefV2[];
  onChange: (b: BucketDefV2) => void;
  onRemove: () => void;
  canRemove: boolean;
  queues: Queue[];
  contactFlows: ContactFlow[];
}) {
  const [collapsed, setCollapsed] = useState(false);
  const [expandedCampaignId, setExpandedCampaignId] = useState<string | null>(null);

  const toggleCampaign = (id: string) =>
    setExpandedCampaignId((prev) => (prev === id ? null : id));

  const updateCampaign = (ci: number, c: CampaignDef) =>
    onChange({ ...bucket, campaigns: bucket.campaigns.map((x, i) => (i === ci ? c : x)) });

  const removeCampaign = (ci: number) =>
    onChange({ ...bucket, campaigns: bucket.campaigns.filter((_, i) => i !== ci) });

  const addCampaign = () => {
    // Inherit config from last campaign so new campaign is pre-configured
    const prevCfg = bucket.campaigns[bucket.campaigns.length - 1]?.campaignConfig
      ?? bucket.campaignConfig
      ?? DEFAULT_CAMPAIGN_CONFIG;
    const c = newCampaign({ campaignConfig: { ...prevCfg } });
    onChange({ ...bucket, campaigns: [...bucket.campaigns, c] });
    setExpandedCampaignId(c.id);
  };

  return (
    <Card className="p-4 space-y-4">
      {/* ── Header row (always visible, clicking name/arrow collapses) ── */}
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={() => setCollapsed((c) => !c)}
          className="text-gray-400 text-xs w-4 shrink-0"
          aria-label={collapsed ? 'Expand bucket' : 'Collapse bucket'}
        >
          {collapsed ? '▶' : '▼'}
        </button>
        <Input
          value={bucket.name}
          onChange={(e) => onChange({ ...bucket, name: e.target.value })}
          placeholder="Bucket name"
          className="flex-1 font-medium"
          onClick={(e) => e.stopPropagation()}
        />
        {collapsed && (
          <span className="text-xs text-gray-400 shrink-0">
            {bucket.campaigns.length} campaign{bucket.campaigns.length !== 1 ? 's' : ''}
            {' · '}{bucket.run_mode === 'time_based' ? `${bucket.duration_minutes ?? 30} min` : 'status'}
            {bucket.parallel ? ' · parallel' : ''}
          </span>
        )}
        <div className="flex gap-1 shrink-0">
          {(['status_based', 'time_based'] as const).map((mode) => (
            <button
              key={mode}
              type="button"
              onClick={() => onChange({ ...bucket, run_mode: mode })}
              className={`px-2.5 py-1 rounded text-xs border transition-colors ${
                bucket.run_mode === mode
                  ? 'bg-blue-600 text-white border-blue-600'
                  : 'bg-white text-gray-600 border-gray-300 hover:border-blue-400'
              }`}
            >
              {mode === 'status_based' ? 'Status' : 'Time'}
            </button>
          ))}
        </div>
        {canRemove && (
          <button type="button" onClick={onRemove} className="text-red-400 hover:text-red-600 text-sm shrink-0">
            ✕
          </button>
        )}
      </div>

      {!collapsed && bucket.run_mode === 'time_based' && (
        <div className="flex gap-4 items-end">
          <Field label="Duration (minutes)">
            <Input
              type="number"
              min={5}
              value={bucket.duration_minutes ?? 30}
              onChange={(e) => onChange({ ...bucket, duration_minutes: Number(e.target.value) })}
              className="w-24"
            />
          </Field>
          <label className="flex items-center gap-1.5 text-xs text-gray-600 pb-1">
            <input
              type="checkbox"
              checked={bucket.prestart_next ?? true}
              onChange={(e) => onChange({ ...bucket, prestart_next: e.target.checked })}
            />
            Pre-start next bucket 5 min early
          </label>
        </div>
      )}

      {!collapsed && (
        <div className="flex flex-wrap gap-4">
          <label className="flex items-center gap-1.5 text-xs text-gray-600">
            <input
              type="checkbox"
              checked={bucket.cleanup}
              onChange={(e) => onChange({ ...bucket, cleanup: e.target.checked })}
            />
            Clean up campaigns after bucket
          </label>
          {bucketIndex > 0 && (
            <label className="flex items-center gap-1.5 text-xs text-gray-600">
              <input
                type="checkbox"
                checked={bucket.parallel ?? false}
                onChange={(e) => onChange({ ...bucket, parallel: e.target.checked })}
              />
              Run in parallel with previous bucket
            </label>
          )}
        </div>
      )}

      {/* Campaign DAG */}
      {!collapsed && <div>
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs font-medium text-gray-600">Campaigns</span>
          <button type="button" onClick={addCampaign} className="text-xs text-blue-600 hover:underline">
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
        />
      </div>}
    </Card>
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
      // Migrate legacy run_type values (time_45/90/120 → custom + run_duration_minutes)
      // and missing per-campaign campaignConfig
      const buckets = p.buckets.map((b) => ({
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
      setBuckets(buckets.length ? buckets : [newBucket()]);
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

  const overlaps = useMemo(() => detectOverlaps(buckets), [buckets]);

  const saveMutation = useMutation({
    mutationFn: (body: { name: string; description: string; trigger: PlanTrigger; loop: PlanLoop | null; workingHours: { days: string[]; startTime: string; endTime: string } | null; buckets: BucketDefV2[]; isTemplate: boolean }) =>
      isEdit
        ? api.plans.updateV2(id!, body)
        : api.plans.createV2(body),
    onSuccess: (plan) => {
      qc.invalidateQueries({ queryKey: ['plans'] });
      navigate(`/plans/${plan.planId}`);
    },
  });

  const handleSave = () => {
    setSaveError(null);
    if (!name.trim()) { setSaveError('Plan name is required.'); return; }
    if (buckets.length === 0) { setSaveError('Add at least one bucket.'); return; }
    for (let bi = 0; bi < buckets.length; bi++) {
      const b = buckets[bi];
      if (b.campaigns.length === 0) { setSaveError(`Bucket ${bi + 1} has no campaigns.`); return; }
      for (let ci = 0; ci < b.campaigns.length; ci++) {
        const c = b.campaigns[ci];
        if (c.states.length === 0) { setSaveError(`"${c.name || `Campaign ${ci + 1}`}" in bucket ${bi + 1} has no states selected.`); return; }
        if (c.run_type === 'custom' && (!c.run_duration_minutes || c.run_duration_minutes < 1)) {
          setSaveError(`"${c.name || `Campaign ${ci + 1}`}" in bucket ${bi + 1} needs a duration > 0 minutes.`); return;
        }
      }
    }
    if (trigger.type === 'on_plan_complete' && !trigger.planId) {
      setSaveError('Select a plan for the "After plan" trigger.'); return;
    }
    if (overlaps.length > 0 && !overlapAck) { setSaveError('Acknowledge the campaign overlap warning below.'); return; }

    saveMutation.mutate({ name: name.trim(), description, trigger, loop, workingHours, buckets, isTemplate });
  };

  const addBucket = () => setBuckets((prev) => [...prev, newBucket()]);

  const updateBucket = (i: number, b: BucketDefV2) =>
    setBuckets((prev) => prev.map((x, idx) => (idx === i ? b : x)));

  const removeBucket = (i: number) =>
    setBuckets((prev) => prev.filter((_, idx) => idx !== i));

  const moveBucket = (i: number, dir: -1 | 1) => {
    setBuckets((prev) => {
      const arr = [...prev];
      const j = i + dir;
      if (j < 0 || j >= arr.length) return arr;
      [arr[i], arr[j]] = [arr[j], arr[i]];
      // After swap, build campaignId → new bucket index map
      const bucketOf = new Map<string, number>();
      arr.forEach((b, bi) => b.campaigns.forEach((c) => bucketOf.set(c.id, bi)));
      // Strip dependsOn entries that now point to a same-or-later bucket
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

  if (isEdit && loadingPlan) return <Spinner />;

  return (
    <div className="max-w-4xl mx-auto p-6 space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">{isEdit ? 'Edit Plan' : 'New Plan'}</h1>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => navigate(-1)}>
            Cancel
          </Button>
          <Button onClick={handleSave} disabled={saveMutation.isPending || !name.trim()}>
            {saveMutation.isPending ? <Spinner /> : 'Save Plan'}
          </Button>
        </div>
      </div>

      {saveMutation.isError && (
        <div className="text-sm text-red-600 bg-red-50 border border-red-200 rounded p-3">
          {String((saveMutation.error as Error)?.message ?? 'Save failed')}
        </div>
      )}

      {/* Plan metadata */}
      <Card className="p-4 space-y-3">
        <Field label="Plan name">
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. NY Morning Wave"
            autoFocus
          />
        </Field>
        <Field label="Description (optional)">
          <Input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Brief description"
          />
        </Field>
        <label className="flex items-center gap-2 text-xs text-gray-600">
          <input type="checkbox" checked={isTemplate} onChange={(e) => setIsTemplate(e.target.checked)} />
          Save as template (cannot be run directly)
        </label>
      </Card>

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
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="font-medium text-gray-800">Buckets</h2>
          <button type="button" onClick={addBucket} className="text-sm text-blue-600 hover:underline">
            + Add bucket
          </button>
        </div>

        {buckets.map((bucket, bi) => (
          <div key={bucket.id} className="relative">
            <div className="flex items-start gap-2">
              <div className="flex flex-col gap-1 pt-4">
                <button
                  type="button"
                  onClick={() => moveBucket(bi, -1)}
                  disabled={bi === 0}
                  className="text-gray-400 hover:text-gray-600 disabled:opacity-30 text-xs"
                >
                  ↑
                </button>
                <button
                  type="button"
                  onClick={() => moveBucket(bi, 1)}
                  disabled={bi === buckets.length - 1}
                  className="text-gray-400 hover:text-gray-600 disabled:opacity-30 text-xs"
                >
                  ↓
                </button>
              </div>
              <div className="flex-1">
                <div className="text-xs text-gray-400 mb-1">Bucket {bi + 1}</div>
                <BucketEditor
                  bucket={bucket}
                  bucketIndex={bi}
                  allBuckets={buckets}
                  onChange={(b) => updateBucket(bi, b)}
                  onRemove={() => removeBucket(bi)}
                  canRemove={buckets.length > 1}
                  queues={queues}
                  contactFlows={contactFlows}
                />
              </div>
            </div>

            {bi < buckets.length - 1 && (
              <div className="flex items-center gap-2 my-2 ml-8">
                <div className="flex-1 border-t border-dashed border-gray-300" />
                <span className="text-xs text-gray-400">then</span>
                <div className="flex-1 border-t border-dashed border-gray-300" />
              </div>
            )}
          </div>
        ))}
      </div>

      {/* Overlap warning */}
      {overlaps.length > 0 && (
        <div className="bg-amber-50 border border-amber-300 rounded-lg p-4 space-y-2">
          <div className="text-sm font-medium text-amber-800">Overlapping campaign filters detected</div>
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
          <label className="flex items-center gap-2 text-xs text-amber-800 font-medium">
            <input type="checkbox" checked={overlapAck} onChange={(e) => setOverlapAck(e.target.checked)} />
            I acknowledge this overlap and want to proceed
          </label>
        </div>
      )}

      {(saveError || saveMutation.isError) && (
        <div className="text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg p-3">
          {saveError ?? String((saveMutation.error as Error)?.message ?? 'Save failed')}
        </div>
      )}

      <div className="flex justify-end gap-2 pb-8">
        <Button variant="outline" onClick={() => navigate(-1)}>
          Cancel
        </Button>
        <Button
          onClick={handleSave}
          disabled={saveMutation.isPending}
        >
          {saveMutation.isPending ? <Spinner /> : 'Save Plan'}
        </Button>
      </div>
    </div>
  );
}
