import { type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';

import { Card, Spinner } from '@/components/ui';
import { api } from '@/lib/api';
import { RunStatusBadge } from '@/pages/Plans';

export function PlansMonitor(): ReactNode {
  const navigate = useNavigate();

  const list = useQuery({
    queryKey: ['plans'],
    queryFn: () => api.plans.list(),
    refetchInterval: 5_000,
  });

  const running = (list.data?.plans ?? []).filter((p) => p.latestRun?.status === 'running');

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold tracking-tight">Live monitor</h2>
          <p className="mt-0.5 text-sm text-muted-foreground">Plans currently executing — refreshes every 5 s.</p>
        </div>
        {list.isFetching ? <Spinner /> : null}
      </div>

      {list.isPending ? (
        <p className="inline-flex items-center gap-2 text-sm text-muted-foreground"><Spinner /> Loading…</p>
      ) : list.isError ? (
        <p className="text-sm text-destructive">{(list.error as Error).message}</p>
      ) : running.length === 0 ? (
        <Card className="p-8 text-center text-sm text-muted-foreground">
          No plans are running right now.
        </Card>
      ) : (
        <div className="flex flex-col gap-3">
          {running.map((plan) => {
            const run = plan.latestRun!;
            const currentBucket = plan.buckets[run.currentBucketIndex];
            const done = run.bucketStates.filter((b) => b.status === 'completed').length;
            const pct = Math.round((done / plan.buckets.length) * 100);
            return (
              <Card key={plan.planId} className="p-0 overflow-hidden">
                <button
                  type="button"
                  className="w-full cursor-pointer p-4 text-left hover:bg-muted/30 transition-colors"
                  onClick={() => navigate(`/plans/${encodeURIComponent(plan.planId)}`)}
                >
                <div className="flex items-center justify-between gap-4">
                  <div>
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-sm">{plan.name}</span>
                      <RunStatusBadge status={run.status} />
                    </div>
                    <p className="mt-0.5 text-xs text-muted-foreground">
                      Bucket {run.currentBucketIndex + 1} of {plan.buckets.length}
                      {currentBucket ? ` — ${currentBucket.name}` : ''}
                      {' · '}started {new Date(run.startedAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                    </p>
                  </div>
                  <span className="text-sm font-semibold tabular-nums text-primary">{pct}%</span>
                </div>
                <div className="mt-3 h-1.5 w-full overflow-hidden rounded-full bg-muted">
                  <div className="h-full bg-primary transition-all" style={{ width: `${pct}%` }} />
                </div>
                </button>
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}
