import type { ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';

import { api, type AuditEntry, type CampaignSummary } from '@/lib/api';
import { formatDateTime } from '@/lib/utils';

type Tile = { label: string; value: string };

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

  const tiles: Tile[] = [
    { label: 'Active segments', value: String(segments.data?.segments.length ?? '—') },
    { label: 'Campaigns running', value: String(running.length) },
    { label: 'Campaigns paused', value: String(paused.length) },
    { label: 'Recent audit events', value: String(audit.data?.entries.length ?? '—') },
  ];

  return (
    <div className="flex flex-col gap-8">
      <header className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">Overview</h1>
      </header>

      <section className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {tiles.map((tile) => (
          <div
            key={tile.label}
            className="rounded-lg border border-border bg-card p-4 shadow-sm"
          >
            <p className="text-sm text-muted-foreground">{tile.label}</p>
            <p className="mt-2 text-3xl font-semibold tracking-tight">{tile.value}</p>
          </div>
        ))}
      </section>

      <section className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <CampaignsCard title="Running campaigns" items={running} emptyLabel="None running" />
        <AuditCard audit={audit.data?.entries ?? []} loading={audit.isPending} />
      </section>
    </div>
  );
}

function CampaignsCard({
  title,
  items,
  emptyLabel,
}: {
  title: string;
  items: CampaignSummary[];
  emptyLabel: string;
}): ReactNode {
  return (
    <div className="rounded-lg border border-border bg-card p-4 shadow-sm">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold">{title}</h2>
        <Link to="/campaigns" className="text-xs text-muted-foreground hover:text-foreground">
          View all →
        </Link>
      </div>
      <div className="mt-3 divide-y divide-border">
        {items.length === 0 ? (
          <p className="py-3 text-sm text-muted-foreground">{emptyLabel}</p>
        ) : (
          items.slice(0, 6).map((c) => (
            <div key={c.id} className="flex items-center justify-between py-2">
              <div>
                <p className="text-sm font-medium">{c.name}</p>
                <p className="text-xs text-muted-foreground">
                  {(c.channelSubtypes ?? ['TELEPHONY']).join(', ')}
                </p>
              </div>
              <Link
                to={`/campaigns/${encodeURIComponent(c.id)}`}
                className="text-xs text-muted-foreground hover:text-foreground"
              >
                Open →
              </Link>
            </div>
          ))
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
    <div className="rounded-lg border border-border bg-card p-4 shadow-sm">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold">Recent activity</h2>
        <Link to="/audit" className="text-xs text-muted-foreground hover:text-foreground">
          View all →
        </Link>
      </div>
      <div className="mt-3 divide-y divide-border">
        {loading ? (
          <p className="py-3 text-sm text-muted-foreground">Loading…</p>
        ) : audit.length === 0 ? (
          <p className="py-3 text-sm text-muted-foreground">No recent events</p>
        ) : (
          audit.slice(0, 8).map((entry, i) => (
            <div key={i} className="flex flex-col gap-0.5 py-2 text-sm">
              <span>
                <span className="font-medium">{entry.actorEmail ?? entry.actorSub ?? '—'}</span>{' '}
                <span className="text-muted-foreground">{entry.action}</span>{' '}
                <span>
                  {entry.entityType}:{entry.entityId}
                </span>
              </span>
              <span className="text-xs text-muted-foreground">
                {formatDateTime(entry.timestamp)}
              </span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
