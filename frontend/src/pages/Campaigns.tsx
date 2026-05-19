import { useMemo, useState, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useNavigate } from 'react-router-dom';

import { Badge, Button, Card } from '@/components/ui';
import { BulkDeletePanel } from '@/components/BulkDeletePanel';
import { api, type CampaignSummary } from '@/lib/api';
import { formatDateTime } from '@/lib/utils';

type LifecycleAction = 'start' | 'stop' | 'pause' | 'resume';

type SortKey = 'name' | 'state' | 'schedule';
type SortDir = 'asc' | 'desc';

function SortHeader({
  label,
  sortKey,
  current,
  dir,
  onSort,
}: {
  label: string;
  sortKey: SortKey;
  current: SortKey;
  dir: SortDir;
  onSort: (key: SortKey) => void;
}): ReactNode {
  const active = current === sortKey;
  return (
    <th className="px-4 py-2 text-left font-medium">
      <button
        type="button"
        className="inline-flex items-center gap-1 hover:text-foreground"
        onClick={() => onSort(sortKey)}
      >
        {label}
        <span className="text-[10px]">
          {active ? (dir === 'asc' ? '▲' : '▼') : '⇅'}
        </span>
      </button>
    </th>
  );
}

export function Campaigns(): ReactNode {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [showBulkDelete, setShowBulkDelete] = useState(false);
  const [search, setSearch] = useState('');
  const [sortKey, setSortKey] = useState<SortKey>('name');
  const [sortDir, setSortDir] = useState<SortDir>('asc');

  const list = useQuery({
    queryKey: ['campaigns'],
    queryFn: () => api.campaigns.list({ maxResults: 100 }),
  });

  function handleSort(key: SortKey) {
    if (sortKey === key) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortKey(key);
      setSortDir('asc');
    }
  }

  const campaigns = useMemo(() => {
    const raw = list.data?.campaigns ?? [];
    const q = search.trim().toLowerCase();
    const filtered = q ? raw.filter((c) => c.name.toLowerCase().includes(q)) : raw;
    return [...filtered].sort((a, b) => {
      let cmp = 0;
      if (sortKey === 'name') cmp = a.name.localeCompare(b.name);
      else if (sortKey === 'state') cmp = (a.status ?? '').localeCompare(b.status ?? '');
      else if (sortKey === 'schedule') cmp = (a.schedule?.startTime ?? '').localeCompare(b.schedule?.startTime ?? '');
      return sortDir === 'asc' ? cmp : -cmp;
    });
  }, [list.data, search, sortKey, sortDir]);

  const lifecycle = useMutation({
    mutationFn: ({ id, action }: { id: string; action: LifecycleAction }) =>
      api.campaigns[action](id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['campaigns'] }),
  });

  const remove = useMutation({
    mutationFn: (id: string) => api.campaigns.remove(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['campaigns'] }),
  });

  const bulkItems = (list.data?.campaigns ?? []).map((c) => ({
    id: c.id,
    label: c.name,
    dateKey: c.schedule?.startTime,
  }));

  return (
    <div className="flex flex-col gap-6">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Campaigns</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Native Outbound Campaigns V2 driven by a Customer Profiles segment source.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <input
            type="search"
            placeholder="Search campaigns…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="h-9 w-56 rounded-md border border-border bg-background px-3 text-sm placeholder:text-muted-foreground"
          />
          <Button
            variant="outline"
            onClick={() => setShowBulkDelete((v) => !v)}
          >
            {showBulkDelete ? 'Hide bulk delete' : 'Bulk delete'}
          </Button>
          <Button onClick={() => navigate('/campaigns/new')}>New campaign</Button>
        </div>
      </header>

      {showBulkDelete ? (
        <BulkDeletePanel
          items={bulkItems}
          entityLabel="campaign"
          onDelete={(id) => api.campaigns.remove(id)}
          onDone={() => {
            setShowBulkDelete(false);
            qc.invalidateQueries({ queryKey: ['campaigns'] });
          }}
        />
      ) : null}

      <Card className="p-0">
        <table className="w-full text-sm">
          <thead className="border-b border-border bg-muted/50 text-xs uppercase tracking-wide text-muted-foreground">
            <tr>
              <SortHeader label="Name" sortKey="name" current={sortKey} dir={sortDir} onSort={handleSort} />
              <SortHeader label="State" sortKey="state" current={sortKey} dir={sortDir} onSort={handleSort} />
              <SortHeader label="Schedule" sortKey="schedule" current={sortKey} dir={sortDir} onSort={handleSort} />
              <th className="px-4 py-2 text-left font-medium">Channels</th>
              <th className="px-4 py-2 text-right font-medium">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {list.isPending ? (
              <RowMessage colSpan={5} message="Loading campaigns…" />
            ) : list.isError ? (
              <RowMessage colSpan={5} message={(list.error as Error).message} tone="danger" />
            ) : campaigns.length === 0 ? (
              <RowMessage colSpan={5} message={search ? 'No campaigns match your search.' : 'No campaigns yet.'} />
            ) : (
              campaigns.map((c) => (
                <CampaignRow
                  key={c.id}
                  campaign={c}
                  onAction={(action) => lifecycle.mutate({ id: c.id, action })}
                  onDelete={() => {
                    if (confirm(`Delete campaign "${c.name}"? Running campaigns will be stopped first.`)) {
                      remove.mutate(c.id);
                    }
                  }}
                />
              ))
            )}
          </tbody>
        </table>
      </Card>
    </div>
  );
}

function RowMessage({
  colSpan,
  message,
  tone = 'muted',
}: {
  colSpan: number;
  message: string;
  tone?: 'muted' | 'danger';
}): ReactNode {
  return (
    <tr>
      <td
        colSpan={colSpan}
        className={
          tone === 'danger'
            ? 'px-4 py-8 text-center text-sm text-destructive'
            : 'px-4 py-8 text-center text-sm text-muted-foreground'
        }
      >
        {message}
      </td>
    </tr>
  );
}

function CampaignRow({
  campaign,
  onAction,
  onDelete,
}: {
  campaign: CampaignSummary;
  onAction: (action: LifecycleAction) => void;
  onDelete: () => void;
}): ReactNode {
  const state = (campaign.status ?? 'UNKNOWN').toUpperCase();
  return (
    <tr>
      <td className="px-4 py-3 align-top">
        <Link
          to={`/campaigns/${encodeURIComponent(campaign.id)}`}
          className="font-medium hover:underline"
        >
          {campaign.name}
        </Link>
      </td>
      <td className="px-4 py-3 align-top">
        <Badge tone={stateTone(state)}>{state}</Badge>
      </td>
      <td className="px-4 py-3 align-top text-xs text-muted-foreground">
        <div>Start: {formatDateTime(campaign.schedule?.startTime)}</div>
        <div>End: {formatDateTime(campaign.schedule?.endTime)}</div>
      </td>
      <td className="px-4 py-3 align-top text-muted-foreground">
        {(campaign.channelSubtypes ?? []).join(', ') || '—'}
      </td>
      <td className="px-4 py-3 align-top">
        <div className="flex items-center justify-end gap-2">
          {state !== 'RUNNING' ? (
            <Button size="sm" variant="outline" onClick={() => onAction('start')}>
              Start
            </Button>
          ) : null}
          {state === 'RUNNING' ? (
            <Button size="sm" variant="outline" onClick={() => onAction('pause')}>
              Pause
            </Button>
          ) : null}
          {state === 'PAUSED' ? (
            <Button size="sm" variant="outline" onClick={() => onAction('resume')}>
              Resume
            </Button>
          ) : null}
          {state === 'RUNNING' || state === 'PAUSED' ? (
            <Button size="sm" variant="outline" onClick={() => onAction('stop')}>
              Stop
            </Button>
          ) : null}
          <Button size="sm" variant="ghost" onClick={onDelete}>
            Delete
          </Button>
        </div>
      </td>
    </tr>
  );
}

function stateTone(state: string): 'success' | 'warning' | 'muted' | 'danger' | 'default' {
  switch (state) {
    case 'RUNNING':
      return 'success';
    case 'PAUSED':
      return 'warning';
    case 'STOPPED':
    case 'INITIALIZED':
      return 'muted';
    case 'FAILED':
      return 'danger';
    default:
      return 'default';
  }
}
