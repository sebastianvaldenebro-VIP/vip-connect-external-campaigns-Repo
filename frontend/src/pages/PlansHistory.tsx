import { useState, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';

import { Spinner } from '@/components/ui';
import { api } from '@/lib/api';
import { RunStatusBadge } from '@/pages/Plans';

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtDT(iso?: string): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '—';
  return d.toLocaleString('en-US', {
    month: 'numeric',
    day: 'numeric',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

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

const STATUS_OPTIONS = ['all', 'running', 'completed', 'failed', 'aborted'] as const;
type StatusFilter = (typeof STATUS_OPTIONS)[number];

// ── Main component ────────────────────────────────────────────────────────────

export function PlansHistory(): ReactNode {
  const navigate = useNavigate();

  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');

  const list = useQuery({ queryKey: ['plans'], queryFn: () => api.plans.list() });

  const withRuns = (list.data?.plans ?? [])
    .filter((p) => p.latestRun)
    .sort((a, b) => {
      const ta = a.latestRun?.startedAt ?? '';
      const tb = b.latestRun?.startedAt ?? '';
      return tb.localeCompare(ta);
    });

  const filtered = withRuns.filter((p) => {
    if (search && !p.name.toLowerCase().includes(search.toLowerCase())) return false;
    if (statusFilter !== 'all' && p.latestRun?.status !== statusFilter) return false;
    return true;
  });

  if (list.isPending) {
    return (
      <div className="flex items-center justify-center py-20">
        <Spinner />
      </div>
    );
  }
  if (list.isError) {
    return <p className="text-sm text-destructive">{(list.error as Error).message}</p>;
  }

  return (
    <div className="flex flex-col gap-5">
      {/* Header */}
      <div>
        <h2 className="text-xl font-semibold tracking-tight">History</h2>
        <p className="mt-1 text-sm text-muted-foreground">Most recent run per plan, newest first.</p>
      </div>

      {/* Search + filter */}
      <div className="flex items-center gap-2 flex-wrap">
        <div className="relative flex-1 max-w-xs">
          <span className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 text-xs">🔍</span>
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search by plan name…"
            className="w-full rounded-lg border border-gray-200 bg-white py-1.5 pl-8 pr-3 text-sm focus:outline-none focus:ring-1 focus:ring-blue-300"
          />
        </div>
        <div className="flex items-center gap-1.5 flex-wrap">
          {STATUS_OPTIONS.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => setStatusFilter(s)}
              className={[
                'inline-flex items-center rounded-full border px-3 py-1.5 text-xs font-medium transition-colors capitalize',
                statusFilter === s
                  ? 'border-blue-400 bg-blue-50 text-blue-700'
                  : 'border-gray-200 bg-white text-gray-500 hover:border-gray-300',
              ].join(' ')}
            >
              {s === 'all' ? 'All' : s}
            </button>
          ))}
        </div>
      </div>

      {filtered.length === 0 ? (
        <div className="rounded-xl border border-dashed border-gray-200 p-8 text-center text-sm text-gray-400">
          {withRuns.length === 0 ? 'No runs recorded yet.' : 'No runs match your filters.'}
        </div>
      ) : (
        <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="bg-gray-50 border-b border-gray-100">
                  {['Plan', 'Status', 'Buckets', 'Started', 'Completed'].map((h) => (
                    <th
                      key={h}
                      className="px-4 py-2.5 text-left text-[10px] font-semibold text-gray-500 uppercase tracking-wider whitespace-nowrap"
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {filtered.map((plan) => {
                  const run = plan.latestRun!;
                  const done = run.bucketStates.filter((b) => b.status === 'completed').length;
                  return (
                    <tr
                      key={plan.planId}
                      className={`border-b border-gray-100 last:border-0 hover:bg-gray-50/50 transition-colors cursor-pointer ${statusBorderClass(run.status)}`}
                      onClick={() => navigate(`/plans/${encodeURIComponent(plan.planId)}`)}
                    >
                      <td className="px-4 py-3.5">
                        <span className="text-sm font-semibold text-gray-900">{plan.name}</span>
                      </td>
                      <td className="px-4 py-3.5">
                        <RunStatusBadge status={run.status} />
                      </td>
                      <td className="px-4 py-3.5">
                        <span className="font-mono text-xs text-gray-500 tabular-nums">
                          {done}/{plan.buckets.length}
                        </span>
                      </td>
                      <td className="px-4 py-3.5">
                        <span className="font-mono text-xs text-gray-500">{fmtDT(run.startedAt)}</span>
                      </td>
                      <td className="px-4 py-3.5">
                        <span className="font-mono text-xs text-gray-500">{fmtDT(run.completedAt)}</span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
