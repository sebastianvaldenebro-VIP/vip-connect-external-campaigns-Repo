import type { ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';

import { Spinner } from '@/components/ui';
import { api, type AuditEntry, type CampaignSummary } from '@/lib/api';
import { formatDateTime } from '@/lib/utils';

// ── Helpers ───────────────────────────────────────────────────────────────────

function statusBorderClass(status: string): string {
  const s = (status ?? '').toLowerCase();
  if (s === 'running') return 'border-l-4 border-l-blue-400';
  if (s === 'completed') return 'border-l-4 border-l-green-400';
  if (s === 'failed' || s === 'error') return 'border-l-4 border-l-red-500';
  return 'border-l-4 border-l-gray-200';
}

export function Dashboard(): ReactNode {
  const segments = useQuery({
    queryKey: ['segments', { maxResults: 100 }],
    queryFn: () => api.segments.list({ maxResults: 100 }),
  });

  const campaigns = useQuery({
    queryKey: ['campaigns', { maxResults: 100 }],
    queryFn: () => api.campaigns.list({ maxResults: 100 }),
  });

  const audit = useQuery({
    queryKey: ['audit', { limit: 20 }],
    queryFn: () => api.audit.list({ limit: 20 }),
  });

  const running = (campaigns.data?.campaigns ?? []).filter(
    (c) => (c.status ?? '').toLowerCase() === 'running',
  );
  const paused = (campaigns.data?.campaigns ?? []).filter(
    (c) => (c.status ?? '').toLowerCase() === 'paused',
  );

  return (
    <div className="flex flex-col gap-6">
      {/* Page header */}
      <div>
        <h2 className="text-xl font-semibold tracking-tight">Dashboard</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Overview of segments, campaigns, and recent activity.
        </p>
      </div>

      {/* Stat cards */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div className="bg-blue-50/70 border border-blue-100 rounded-xl px-4 py-4 shadow-sm">
          <div className="text-[10px] font-semibold uppercase tracking-wider text-blue-400 mb-1">
            Active segments
          </div>
          <div className="text-3xl font-bold font-mono text-blue-700">
            {segments.isPending ? '—' : String(segments.data?.segments.length ?? '—')}
          </div>
          <div className="text-xs text-blue-400 mt-0.5">total loaded</div>
        </div>

        <div className="bg-green-50/70 border border-green-100 rounded-xl px-4 py-4 shadow-sm">
          <div className="text-[10px] font-semibold uppercase tracking-wider text-green-500 mb-1">
            Campaigns running
          </div>
          <div className="text-3xl font-bold font-mono text-green-700">
            {campaigns.isPending ? '—' : String(running.length)}
          </div>
          <div className="text-xs text-green-400 mt-0.5">currently active</div>
        </div>

        <div className="bg-amber-50/70 border border-amber-100 rounded-xl px-4 py-4 shadow-sm">
          <div className="text-[10px] font-semibold uppercase tracking-wider text-amber-500 mb-1">
            Campaigns paused
          </div>
          <div className="text-3xl font-bold font-mono text-amber-700">
            {campaigns.isPending ? '—' : String(paused.length)}
          </div>
          <div className="text-xs text-amber-400 mt-0.5">on hold</div>
        </div>

        <div className="bg-purple-50/70 border border-purple-100 rounded-xl px-4 py-4 shadow-sm">
          <div className="text-[10px] font-semibold uppercase tracking-wider text-purple-400 mb-1">
            Audit events
          </div>
          <div className="text-3xl font-bold font-mono text-purple-700">
            {audit.isPending ? '—' : String(audit.data?.entries.length ?? '—')}
          </div>
          <div className="text-xs text-purple-400 mt-0.5">recent (last 20)</div>
        </div>
      </div>

      {/* Bottom two-column grid */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <CampaignsCard items={running} loading={campaigns.isPending} />
        <AuditCard audit={audit.data?.entries ?? []} loading={audit.isPending} />
      </div>
    </div>
  );
}

function CampaignsCard({
  items,
  loading,
}: {
  items: CampaignSummary[];
  loading: boolean;
}): ReactNode {
  return (
    <div className="flex flex-col gap-3">
      {/* Section header */}
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-900">Running campaigns</h3>
        <Link to="/campaigns" className="text-xs text-blue-600 hover:underline">
          View all
        </Link>
      </div>

      <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
        {loading ? (
          <div className="flex items-center justify-center py-20">
            <Spinner />
          </div>
        ) : items.length === 0 ? (
          <div className="rounded-xl border border-dashed border-gray-200 p-8 text-center text-sm text-gray-400">
            Nothing to show yet.
          </div>
        ) : (
          <div className="divide-y divide-gray-100">
            {items.slice(0, 6).map((c) => (
              <div
                key={c.id}
                className={`flex items-center justify-between px-4 py-3 ${statusBorderClass(c.status ?? '')}`}
              >
                <div className="min-w-0">
                  <p className="text-sm font-medium text-gray-900 truncate">{c.name}</p>
                  <p className="text-xs text-muted-foreground">
                    {(c.channelSubtypes ?? ['TELEPHONY']).join(', ')}
                  </p>
                </div>
                <Link
                  to={`/campaigns/${encodeURIComponent(c.id)}`}
                  className="rounded-lg border border-gray-200 px-3 py-1.5 text-xs font-medium hover:bg-gray-50 transition-colors ml-3 shrink-0"
                >
                  Open
                </Link>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function AuditCard({
  audit,
  loading,
}: {
  audit: AuditEntry[];
  loading: boolean;
}): ReactNode {
  return (
    <div className="flex flex-col gap-3">
      {/* Section header */}
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-900">Recent activity</h3>
        <Link to="/audit" className="text-xs text-blue-600 hover:underline">
          View all
        </Link>
      </div>

      <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
        {loading ? (
          <div className="flex items-center justify-center py-20">
            <Spinner />
          </div>
        ) : audit.length === 0 ? (
          <div className="rounded-xl border border-dashed border-gray-200 p-8 text-center text-sm text-gray-400">
            Nothing to show yet.
          </div>
        ) : (
          <div className="divide-y divide-gray-100">
            {audit.slice(0, 8).map((entry, i) => (
              <div
                key={i}
                className="flex flex-col gap-0.5 px-4 py-3 border-l-4 border-l-gray-200"
              >
                <span className="text-sm">
                  <span className="font-medium text-gray-900">
                    {entry.actorEmail ?? entry.actorSub ?? '—'}
                  </span>{' '}
                  <span className="text-muted-foreground">{entry.action}</span>{' '}
                  <span className="text-gray-700">
                    {entry.entityType}:{entry.entityId}
                  </span>
                </span>
                <span className="font-mono text-xs text-gray-400">
                  {formatDateTime(entry.timestamp)}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
