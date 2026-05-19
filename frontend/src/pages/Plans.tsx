import { useState, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';

import { Badge, Button, Card, Spinner } from '@/components/ui';
import { api, type PlanRunV2, type PlanSummaryV2, type PlanTrigger } from '@/lib/api';

function triggerLabel(trigger: PlanTrigger | undefined, upstreamName?: string): string {
  if (!trigger || trigger.type === 'manual') return '▶ Manual';
  if (trigger.type === 'time') return `⏰ ${trigger.time} COT`;
  return `⛓ ${upstreamName ?? 'On plan complete'}`;
}

type Tab = 'plans' | 'templates' | 'guide';

export function Plans(): ReactNode {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [tab, setTab] = useState<Tab>('plans');

  const list = useQuery({
    queryKey: ['plans'],
    queryFn: () => api.plans.listV2(),
  });

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

  const [cloningId, setCloningId] = useState<string | null>(null);
  const clone = useMutation({
    mutationFn: ({ tid, name }: { tid: string; name: string }) =>
      api.plans.cloneTemplate(tid, { name }),
    onSuccess: (plan) => {
      setCloningId(null);
      qc.invalidateQueries({ queryKey: ['plans'] });
      navigate(`/plans/${encodeURIComponent(plan.planId)}/edit`);
    },
    onError: () => setCloningId(null),
  });

  const plans = list.data?.plans ?? [];
  const dailyPlans = plans
    .filter((p) => !p.isTemplate && !p.is_template)
    .sort((a, b) => {
      const ta = a.latestRun?.startedAt ? +new Date(a.latestRun.startedAt) : 0;
      const tb = b.latestRun?.startedAt ? +new Date(b.latestRun.startedAt) : 0;
      return tb - ta;
    });
  const templates = plans.filter((p) => p.isTemplate || p.is_template);
  const planNameMap = Object.fromEntries(plans.map((p) => [p.planId, p.name]));

  const handleDuplicate = (plan: PlanSummaryV2) => {
    const name = prompt(`New plan name (copying "${plan.name}"):`, `${plan.name} (copy)`);
    if (!name?.trim()) return;
    setDuplicatingId(plan.planId);
    duplicate.mutate({ id: plan.planId, name: name.trim() });
  };

  const handleCloneTemplate = (plan: PlanSummaryV2) => {
    const name = prompt(`Name for new plan (from template "${plan.name}"):`, plan.name);
    if (!name?.trim()) return;
    setCloningId(plan.planId);
    clone.mutate({ tid: plan.planId, name: name.trim() });
  };

  const upstreamName = (plan: PlanSummaryV2) =>
    plan.trigger?.type === 'on_plan_complete'
      ? planNameMap[plan.trigger.planId]
      : undefined;

  return (
    <div className="flex flex-col gap-6">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Daily Plans</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Automated campaign workflows — each bucket executes its campaigns in DAG order.
          </p>
        </div>
        <Button onClick={() => navigate('/plans/new')}>New plan</Button>
      </header>

      {/* ── Tab bar ── */}
      <div className="flex gap-1 border-b">
        {(['plans', 'templates', 'guide'] as Tab[]).map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setTab(t)}
            className={`px-4 py-2 text-sm font-medium capitalize border-b-2 -mb-px transition-colors ${
              tab === t
                ? 'border-primary text-primary'
                : 'border-transparent text-muted-foreground hover:text-foreground'
            }`}
          >
            {t === 'plans'
              ? `Plans${dailyPlans.length ? ` (${dailyPlans.length})` : ''}`
              : t === 'templates'
                ? `Templates${templates.length ? ` (${templates.length})` : ''}`
                : '? How to use'}
          </button>
        ))}
      </div>

      {tab === 'guide' ? (
        <HowToUseTab />
      ) : list.isPending ? (
        <p className="inline-flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner /> Loading plans…
        </p>
      ) : list.isError ? (
        <p className="text-sm text-destructive">{(list.error as Error).message}</p>
      ) : tab === 'plans' ? (
        dailyPlans.length === 0 ? (
          <Card className="p-8 text-center text-sm text-muted-foreground">
            No plans yet. Create one or start from a template.
          </Card>
        ) : (
          <div className="flex flex-col gap-4">
            {dailyPlans.map((plan) => (
              <PlanCard
                key={plan.planId}
                plan={plan}
                upstreamName={upstreamName(plan)}
                isTriggering={trigger.isPending && trigger.variables === plan.planId}
                isDuplicating={duplicatingId === plan.planId}
                onRun={() => trigger.mutate(plan.planId)}
                onEdit={() => navigate(`/plans/${encodeURIComponent(plan.planId)}/edit`)}
                onDuplicate={() => handleDuplicate(plan)}
                onDelete={() => {
                  if (confirm(`Delete plan "${plan.name}"?`)) remove.mutate(plan.planId);
                }}
                onClick={() => navigate(`/plans/${encodeURIComponent(plan.planId)}`)}
              />
            ))}
          </div>
        )
      ) : (
        templates.length === 0 ? (
          <Card className="p-8 text-center text-sm text-muted-foreground">
            No templates yet. Mark a plan as a template to reuse its structure.
          </Card>
        ) : (
          <div className="flex flex-col gap-3">
            {templates.map((plan) => (
              <PlanCard
                key={plan.planId}
                plan={plan}
                upstreamName={upstreamName(plan)}
                isTriggering={false}
                isDuplicating={duplicatingId === plan.planId}
                isCloningTemplate={cloningId === plan.planId}
                isTemplate
                onRun={() => {}}
                onEdit={() => navigate(`/plans/${encodeURIComponent(plan.planId)}/edit`)}
                onDuplicate={() => handleDuplicate(plan)}
                onUseTemplate={() => handleCloneTemplate(plan)}
                onDelete={() => {
                  if (confirm(`Delete template "${plan.name}"?`)) remove.mutate(plan.planId);
                }}
                onClick={() => navigate(`/plans/${encodeURIComponent(plan.planId)}`)}
              />
            ))}
          </div>
        )
      )}

      {trigger.isError ? (
        <p className="text-sm text-destructive">{(trigger.error as Error).message}</p>
      ) : null}
    </div>
  );
}

export function PlanCard({
  plan,
  upstreamName,
  isTriggering,
  isDuplicating,
  isCloningTemplate = false,
  isTemplate = false,
  onRun,
  onEdit,
  onDuplicate,
  onUseTemplate,
  onDelete,
  onClick,
}: {
  plan: PlanSummaryV2;
  upstreamName?: string;
  isTriggering: boolean;
  isDuplicating: boolean;
  isCloningTemplate?: boolean;
  isTemplate?: boolean;
  onRun: () => void;
  onEdit: () => void;
  onDuplicate: () => void;
  onUseTemplate?: () => void;
  onDelete: () => void;
  onClick: () => void;
}): ReactNode {
  const run = plan.latestRun;
  const isRunning = run?.status === 'running';

  return (
    <Card className="p-4">
      <div className="flex items-start justify-between gap-4">
        <button type="button" className="flex-1 text-left" onClick={onClick}>
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-medium text-sm">{plan.name}</span>
            {isTemplate ? <Badge tone="muted">template</Badge> : null}
            <span className="text-xs text-gray-400">{triggerLabel(plan.trigger, upstreamName)}</span>
            {run && !isTemplate ? <RunStatusBadge status={run.status} /> : null}
          </div>
          <p className="mt-0.5 text-xs text-muted-foreground">
            {plan.buckets.length} bucket{plan.buckets.length === 1 ? '' : 's'}
            {' · '}
            {plan.buckets.reduce((s, b) => s + (b.campaigns?.length ?? 0), 0)} campaigns
            {run && !isTemplate
              ? ` · last run ${run.startedAt ? new Date(run.startedAt).toLocaleString() : '—'}`
              : ''}
          </p>
        </button>

        <div className="flex items-center gap-2 shrink-0">
          <Button size="sm" variant="outline" onClick={onEdit}>
            Edit
          </Button>
          {isTemplate && onUseTemplate ? (
            <Button
              size="sm"
              onClick={onUseTemplate}
              disabled={isCloningTemplate}
            >
              {isCloningTemplate ? (
                <span className="inline-flex items-center gap-1"><Spinner />Creating…</span>
              ) : (
                'Use'
              )}
            </Button>
          ) : null}
          <Button
            size="sm"
            variant="outline"
            onClick={onDuplicate}
            disabled={isDuplicating}
          >
            {isDuplicating ? (
              <span className="inline-flex items-center gap-1"><Spinner />Copying…</span>
            ) : (
              'Duplicate'
            )}
          </Button>
          {!isTemplate ? (
            <Button size="sm" onClick={onRun} disabled={isRunning || isTriggering}>
              {isTriggering ? (
                <span className="inline-flex items-center gap-1"><Spinner />Starting…</span>
              ) : isRunning ? (
                'Running'
              ) : (
                'Run now'
              )}
            </Button>
          ) : null}
          <Button
            size="sm"
            variant="ghost"
            onClick={onDelete}
            className="text-destructive hover:text-destructive"
          >
            Delete
          </Button>
        </div>
      </div>

      <BucketPipelineV2 buckets={plan.buckets} runBucketStates={run?.bucketStates} />
    </Card>
  );
}

function BucketPipelineV2({
  buckets,
  runBucketStates,
}: {
  buckets: PlanSummaryV2['buckets'];
  runBucketStates?: PlanRunV2['bucketStates'];
}): ReactNode {
  if (buckets.length === 0) return null;

  return (
    <div className="mt-3 flex flex-wrap items-center gap-1.5">
      {buckets.map((b, i) => {
        const state = runBucketStates?.[i];
        const status = state?.status ?? 'queued';
        return (
          <div key={b.id ?? i} className="flex items-center gap-1.5">
            <BucketPillV2 bucket={b} status={status} />
            {i < buckets.length - 1 ? (
              <span className="text-xs text-muted-foreground">→</span>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

function BucketPillV2({
  bucket,
  status,
}: {
  bucket: PlanSummaryV2['buckets'][number];
  status: string;
}): ReactNode {
  const colors: Record<string, string> = {
    queued: 'bg-muted text-muted-foreground border-border',
    warming: 'bg-yellow-50 text-yellow-800 border-yellow-300',
    running: 'bg-blue-50 text-blue-800 border-blue-300',
    completed: 'bg-green-50 text-green-800 border-green-300',
  };
  const cls = colors[status] ?? colors.queued;
  const duration = bucket.run_mode === 'time_based' && bucket.duration_minutes
    ? `${bucket.duration_minutes}min`
    : 'auto';
  const campaignCount = bucket.campaigns?.length ?? 0;

  return (
    <span className={`inline-flex items-center rounded border px-2 py-0.5 text-xs font-medium ${cls}`}>
      {bucket.name} · {duration} · {campaignCount}c
    </span>
  );
}

// ── How to use guide ─────────────────────────────────────────────────────────

function GuideSection({ title, children }: { title: string; children: ReactNode }): ReactNode {
  return (
    <section>
      <h2 className="text-base font-semibold text-gray-900 mb-3">{title}</h2>
      <div className="text-sm text-gray-600 space-y-2">{children}</div>
    </section>
  );
}

function Pill({ color, children }: { color: string; children: ReactNode }): ReactNode {
  return (
    <span className={`inline-flex items-center rounded border px-2 py-0.5 text-xs font-medium ${color}`}>
      {children}
    </span>
  );
}

function HowToUseTab(): ReactNode {
  return (
    <div className="max-w-3xl space-y-10">

      <GuideSection title="What is a Plan?">
        <p>
          A <strong>Plan</strong> is an automated workflow that runs one or more outbound
          campaign groups (called <em>buckets</em>) in sequence. Each bucket contains one or more
          Connect campaigns that execute in DAG order — campaigns can run in parallel and depend on
          each other.
        </p>
        <p>
          When a plan runs, it creates all Connect campaigns, builds the CP segments, and manages
          their lifecycle automatically. Everything stops at <strong>7 PM COT</strong> daily.
        </p>
      </GuideSection>

      <GuideSection title="Buckets">
        <p>
          A bucket is one wave of campaigns. Buckets run one after another (sequentially). Each
          bucket has two modes:
        </p>
        <ul className="list-disc list-inside space-y-1.5 ml-2">
          <li>
            <strong>Time-based</strong> — runs for a fixed number of minutes, then stops regardless
            of whether campaigns finished. The system can pre-warm the next bucket 5 minutes before
            the current one expires (enable <em>Pre-start next</em> on the bucket).
          </li>
          <li>
            <strong>Status-based</strong> — runs until all campaigns reach a terminal state
            (completed, cancelled, error, expired). No time limit.
          </li>
        </ul>
        <p className="mt-1">
          Each bucket also has a <strong>Campaign Config</strong>: the Connect queue, contact flow,
          phone number, dialer type, and capacity settings shared by all campaigns in that bucket.
        </p>
      </GuideSection>

      <GuideSection title="Campaigns inside a bucket">
        <p>
          Each campaign targets a specific combination of states, lead groups, and attempt numbers.
          It has two settings:
        </p>
        <ul className="list-disc list-inside space-y-1.5 ml-2">
          <li>
            <strong>Run type</strong>:
            <span className="ml-1">
              <em>Until 7 PM COT</em> — campaign end time is hard-capped at 7 PM.
              <em className="ml-2">Custom (N min)</em> — campaign runs for N minutes from when it starts.
            </span>
          </li>
          <li>
            <strong>Depends on</strong> — a list of other campaigns (in the same or earlier buckets)
            that must complete before this campaign starts. Leave empty to start immediately when
            its bucket activates. If any parent is cancelled or errors out, this campaign is
            automatically cascade-cancelled.
          </li>
        </ul>
        <p className="mt-1">
          Campaigns without dependencies run as <strong>stage 1</strong> (start immediately).
          Campaigns with dependencies start as soon as all parents complete — even if the parent
          is in a different bucket (cross-bucket dispatch).
        </p>
      </GuideSection>

      <GuideSection title="Campaign statuses">
        <div className="flex flex-wrap gap-2">
          <Pill color="bg-gray-100 text-gray-600 border-gray-200">waiting</Pill>
          <Pill color="bg-yellow-50 text-yellow-800 border-yellow-300">warming</Pill>
          <Pill color="bg-blue-50 text-blue-800 border-blue-300">running</Pill>
          <Pill color="bg-green-50 text-green-800 border-green-300">done</Pill>
          <Pill color="bg-gray-100 text-gray-400 border-gray-200">cancelled</Pill>
          <Pill color="bg-red-50 text-red-700 border-red-200">error</Pill>
          <Pill color="bg-orange-50 text-orange-700 border-orange-200">expired</Pill>
        </div>
        <ul className="list-disc list-inside space-y-1 ml-2 mt-2">
          <li><strong>waiting</strong> — queued, waiting for dependencies or bucket to activate</li>
          <li><strong>warming</strong> — Connect campaign created but not yet started (pre-start window)</li>
          <li><strong>running</strong> — actively dialing</li>
          <li><strong>done</strong> — Connect campaign reached end state normally</li>
          <li><strong>cancelled</strong> — skipped (empty segment, parent cancelled, bucket expired before starting, or manually cancelled)</li>
          <li><strong>error</strong> — segment or campaign creation failed</li>
          <li><strong>expired</strong> — was running when the time-based bucket expired; stopped mid-run</li>
        </ul>
      </GuideSection>

      <GuideSection title="Triggers">
        <ul className="list-disc list-inside space-y-1.5 ml-2">
          <li>
            <strong>▶ Manual</strong> — only starts when you click "Run now". No automatic trigger.
          </li>
          <li>
            <strong>⏰ Time (COT)</strong> — starts automatically every day at the specified time
            in Colombia time. Example: <code>07:55</code> starts at 7:55 AM COT.
          </li>
          <li>
            <strong>⛓ On plan complete</strong> — starts automatically when another plan finishes
            its last bucket. Used to chain plans back-to-back. Enable <em>Repeat</em> to chain
            every time that plan completes (not just once).
          </li>
        </ul>
        <p className="mt-1 text-xs text-gray-400">
          Templates cannot have a time or on-plan-complete trigger — they are only for cloning.
        </p>
      </GuideSection>

      <GuideSection title="How to create a plan">
        <ol className="list-decimal list-inside space-y-2 ml-2">
          <li>Click <strong>New plan</strong> in the top right.</li>
          <li>Enter a name and optional description.</li>
          <li>Choose a <strong>trigger</strong> (manual, time, or on plan complete).</li>
          <li>
            Add one or more <strong>buckets</strong>. For each bucket:
            <ul className="list-disc list-inside ml-6 mt-1 space-y-1 text-xs">
              <li>Set run mode (time-based or status-based).</li>
              <li>Configure the Campaign Config (queue, flow, phone, dialer settings).</li>
              <li>Add campaigns — select states, group, attempts, run type, and dependencies.</li>
            </ul>
          </li>
          <li>Review overlaps (amber warning if two campaigns share the same state + attempt group).</li>
          <li>Click <strong>Save</strong>.</li>
        </ol>
      </GuideSection>

      <GuideSection title="How to use a Template">
        <p>
          Templates are pre-configured plans that cannot be run directly. They are starting points
          for creating new plans quickly.
        </p>
        <ol className="list-decimal list-inside space-y-1.5 ml-2">
          <li>Go to the <strong>Templates</strong> tab.</li>
          <li>Find the template you want and click <strong>Use</strong>.</li>
          <li>Give the new plan a name.</li>
          <li>The new plan opens in edit mode — adjust as needed and save.</li>
        </ol>
        <p className="mt-1 text-xs text-gray-400">
          To create a template: create a plan normally, then check "Mark as template" in the edit form.
          Templates appear in the Templates tab, not in the Plans tab.
        </p>
      </GuideSection>

      <GuideSection title="Live monitor controls">
        <p>
          Click on any plan to open the <strong>Live monitor</strong>. When a run is active:
        </p>
        <ul className="list-disc list-inside space-y-1.5 ml-2">
          <li><strong>Abort</strong> — stops all campaigns immediately and marks the run aborted. Does not trigger chained plans.</li>
          <li><strong>Force Finish</strong> — stops all campaigns and marks the run completed. Chained plans will fire.</li>
          <li><strong>▶ Start now</strong> (on a bucket) — manually activates a queued or warming bucket, bypassing timing and dependency checks.</li>
          <li><strong>⏹ Stop bucket</strong> (on a bucket) — expires the current bucket immediately and advances to the next one.</li>
          <li><strong>▶</strong> (on a campaign card) — force-starts a single queued or cancelled campaign, bypassing its dependencies. Useful when a campaign got stuck or was cancelled by mistake.</li>
        </ul>
      </GuideSection>

      <GuideSection title="Daily cutoff">
        <p>
          All running plans are automatically force-finished at <strong>7 PM Colombia time (COT)</strong>.
          Campaign end times are also capped at 7 PM COT. This applies to both the automatic tick
          and to any campaign configured as "Until 7 PM COT".
        </p>
        <p className="text-xs text-gray-400">
          Colombia time = UTC-5, no daylight saving. 7 PM COT = midnight UTC.
        </p>
      </GuideSection>

    </div>
  );
}

export function RunStatusBadge({ status }: { status: string }): ReactNode {
  const toneMap: Record<string, 'default' | 'success' | 'warning' | 'danger' | 'muted'> = {
    running: 'default',
    completed: 'success',
    failed: 'danger',
    aborted: 'warning',
  };
  const tone = toneMap[status] ?? 'muted';
  return <Badge tone={tone}>{status}</Badge>;
}
