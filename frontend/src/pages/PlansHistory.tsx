import { type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';

import { Card, Spinner } from '@/components/ui';
import { api } from '@/lib/api';
import { RunStatusBadge } from '@/pages/Plans';

export function PlansHistory(): ReactNode {
  const navigate = useNavigate();

  const list = useQuery({ queryKey: ['plans'], queryFn: () => api.plans.list() });

  const withRuns = (list.data?.plans ?? [])
    .filter((p) => p.latestRun)
    .sort((a, b) => {
      const ta = a.latestRun?.startedAt ?? '';
      const tb = b.latestRun?.startedAt ?? '';
      return tb.localeCompare(ta);
    });

  if (list.isPending) {
    return <p className="inline-flex items-center gap-2 text-sm text-muted-foreground"><Spinner /> Loading…</p>;
  }
  if (list.isError) {
    return <p className="text-sm text-destructive">{(list.error as Error).message}</p>;
  }

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h2 className="text-lg font-semibold tracking-tight">History</h2>
        <p className="mt-0.5 text-sm text-muted-foreground">Most recent run per plan, newest first.</p>
      </div>

      {withRuns.length === 0 ? (
        <Card className="p-8 text-center text-sm text-muted-foreground">
          No runs recorded yet.
        </Card>
      ) : (
        <Card className="overflow-hidden p-0">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border bg-muted/40">
                <th className="px-4 py-2.5 text-left text-xs font-medium text-muted-foreground">Plan</th>
                <th className="px-4 py-2.5 text-left text-xs font-medium text-muted-foreground">Status</th>
                <th className="px-4 py-2.5 text-left text-xs font-medium text-muted-foreground">Buckets</th>
                <th className="px-4 py-2.5 text-left text-xs font-medium text-muted-foreground">Started</th>
                <th className="px-4 py-2.5 text-left text-xs font-medium text-muted-foreground">Completed</th>
              </tr>
            </thead>
            <tbody>
              {withRuns.map((plan) => {
                const run = plan.latestRun!;
                const done = run.bucketStates.filter((b) => b.status === 'completed').length;
                return (
                  <tr
                    key={plan.planId}
                    className="cursor-pointer border-b border-border/50 hover:bg-muted/30 transition-colors last:border-0"
                    onClick={() => navigate(`/plans/${encodeURIComponent(plan.planId)}`)}
                  >
                    <td className="px-4 py-2.5 font-medium">{plan.name}</td>
                    <td className="px-4 py-2.5">
                      <RunStatusBadge status={run.status} />
                    </td>
                    <td className="px-4 py-2.5 text-muted-foreground tabular-nums">
                      {done}/{plan.buckets.length}
                    </td>
                    <td className="px-4 py-2.5 text-muted-foreground">
                      {run.startedAt ? new Date(run.startedAt).toLocaleString() : '—'}
                    </td>
                    <td className="px-4 py-2.5 text-muted-foreground">
                      {run.completedAt ? new Date(run.completedAt).toLocaleString() : '—'}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}
