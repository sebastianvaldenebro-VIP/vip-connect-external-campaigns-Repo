import { type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';

import { Spinner } from '@/components/ui';
import { api } from '@/lib/api';
import { RunStatusBadge } from '@/pages/Plans';

// ── Helpers ───────────────────────────────────────────────────────────────────

function statusBorderClass(status: string): string {
  switch (status) {
    case 'running':
      return 'border-l-4 border-l-blue-400';
    case 'completed':
      return 'border-l-4 border-l-green-400';
    case 'failed':
    case 'error':
      return 'border-l-4 border-l-red-500';
    case 'aborted':
      return 'border-l-4 border-l-red-300';
    case 'queued':
      return 'border-l-4 border-l-gray-200';
    default:
      return 'border-l-4 border-l-gray-200';
  }
}

// ── Stat chip ─────────────────────────────────────────────────────────────────

function StatChip({ label, value, color }: { label: string; value: number; color: string }) {
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

// ── Main component ────────────────────────────────────────────────────────────

export function PlansMonitor(): ReactNode {
  const navigate = useNavigate();

  const list = useQuery({
    queryKey: ['plans'],
    queryFn: () => api.plans.list(),
    refetchInterval: 5_000,
  });

  const running = (list.data?.plans ?? []).filter((p) => p.latestRun?.status === 'running');

  return (
    <div className="flex flex-col gap-5">
      {/* Header */}
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h2 className="text-xl font-semibold tracking-tight">Live monitor</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Plans currently executing — refreshes every 5 s.
          </p>
        </div>
        {list.isFetching && !list.isPending && <Spinner />}
      </div>

      {list.isPending ? (
        <div className="flex items-center justify-center py-20">
          <Spinner />
        </div>
      ) : list.isError ? (
        <p className="text-sm text-destructive">{(list.error as Error).message}</p>
      ) : (
        <>
          {/* Stat chip strip */}
          <div className="flex flex-wrap gap-2.5">
            <StatChip label="Active runs" value={running.length} color="#1d4ed8" />
          </div>

          {running.length === 0 ? (
            <div className="rounded-xl border border-dashed border-gray-200 p-8 text-center text-sm text-gray-400">
              No plans are running right now.
            </div>
          ) : (
            <div className="flex flex-col gap-3">
              {running.map((plan) => {
                const run = plan.latestRun!;
                const currentBucket = plan.buckets[run.currentBucketIndex];
                const done = run.bucketStates.filter((b) => b.status === 'completed').length;
                const pct = Math.round((done / Math.max(plan.buckets.length, 1)) * 100);

                return (
                  <div
                    key={plan.planId}
                    className={`bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm ${statusBorderClass(run.status)}`}
                  >
                    <button
                      type="button"
                      className="w-full cursor-pointer px-4 py-3.5 text-left hover:bg-gray-50/50 transition-colors"
                      onClick={() => navigate(`/plans/${encodeURIComponent(plan.planId)}`)}
                    >
                      <div className="flex items-center justify-between gap-4">
                        <div className="min-w-0">
                          <div className="flex items-center gap-2 flex-wrap">
                            <span className="text-sm font-semibold text-gray-900">{plan.name}</span>
                            <RunStatusBadge status={run.status} />
                          </div>
                          <div className="mt-0.5 flex items-center gap-1.5 flex-wrap">
                            <span className="font-mono text-xs text-gray-400">{run.runId}</span>
                            <span className="text-xs text-gray-400">·</span>
                            <span className="text-xs text-gray-400">
                              Bucket {run.currentBucketIndex + 1} of {plan.buckets.length}
                              {currentBucket ? ` — ${currentBucket.name}` : ''}
                            </span>
                            <span className="text-xs text-gray-400">·</span>
                            <span className="font-mono text-xs text-gray-500">
                              started{' '}
                              {new Date(run.startedAt).toLocaleTimeString([], {
                                hour: '2-digit',
                                minute: '2-digit',
                              })}
                            </span>
                          </div>
                        </div>
                        <span className="text-sm font-semibold tabular-nums text-blue-600 shrink-0">
                          {pct}%
                        </span>
                      </div>

                      {/* Progress bar */}
                      <div className="mt-3 h-1.5 w-full overflow-hidden rounded-full bg-gray-100">
                        <div
                          className="h-full bg-blue-500 transition-all"
                          style={{ width: `${pct}%` }}
                        />
                      </div>
                    </button>
                  </div>
                );
              })}
            </div>
          )}
        </>
      )}
    </div>
  );
}
