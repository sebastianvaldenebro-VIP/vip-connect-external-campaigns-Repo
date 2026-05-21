import { useMemo, useState, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useNavigate } from 'react-router-dom';

import { Badge, Spinner } from '@/components/ui';
import { BulkDeletePanel } from '@/components/BulkDeletePanel';
import { api, type CampaignSummary } from '@/lib/api';
import { formatDateTime } from '@/lib/utils';

type LifecycleAction = 'start' | 'stop' | 'pause' | 'resume';

type SortKey = 'name' | 'state' | 'schedule';
type SortDir = 'asc' | 'desc';

type StatusFilter = 'all' | 'RUNNING' | 'PAUSED' | 'STOPPED' | 'FAILED' | 'INITIALIZED';

// ── Helpers ───────────────────────────────────────────────────────────────────

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

function statusBorderClass(state: string): string {
  switch (state) {
    case 'RUNNING':
      return 'border-l-4 border-l-blue-400';
    case 'PAUSED':
      return 'border-l-4 border-l-amber-400';
    case 'STOPPED':
    case 'INITIALIZED':
      return 'border-l-4 border-l-gray-200';
    case 'FAILED':
      return 'border-l-4 border-l-red-500';
    default:
      return 'border-l-4 border-l-gray-200';
  }
}

// ── Filter pill ───────────────────────────────────────────────────────────────

function FilterPill({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        'inline-flex items-center rounded-full border px-3 py-1.5 text-xs font-medium transition-colors',
        active
          ? 'border-blue-400 bg-blue-50 text-blue-700'
          : 'border-gray-200 bg-white text-gray-500 hover:border-gray-300',
      ].join(' ')}
    >
      {label}
    </button>
  );
}

// ── Sort header ───────────────────────────────────────────────────────────────

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
    <th className="px-4 py-2.5 text-left text-[10px] font-semibold text-gray-500 uppercase tracking-wider whitespace-nowrap">
      <button
        type="button"
        className="inline-flex items-center gap-1 hover:text-gray-700 transition-colors"
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

// ── Main component ────────────────────────────────────────────────────────────

export function Campaigns(): ReactNode {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [showBulkDelete, setShowBulkDelete] = useState(false);
  const [search, setSearch] = useState('');
  const [sortKey, setSortKey] = useState<SortKey>('name');
  const [sortDir, setSortDir] = useState<SortDir>('asc');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');

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
    const filtered = raw.filter((c) => {
      if (q && !c.name.toLowerCase().includes(q)) return false;
      if (statusFilter !== 'all') {
        const state = (c.status ?? '').toUpperCase();
        if (state !== statusFilter) return false;
      }
      return true;
    });
    return [...filtered].sort((a, b) => {
      let cmp = 0;
      if (sortKey === 'name') cmp = a.name.localeCompare(b.name);
      else if (sortKey === 'state') cmp = (a.status ?? '').localeCompare(b.status ?? '');
      else if (sortKey === 'schedule')
        cmp = (a.schedule?.startTime ?? '').localeCompare(b.schedule?.startTime ?? '');
      return sortDir === 'asc' ? cmp : -cmp;
    });
  }, [list.data, search, sortKey, sortDir, statusFilter]);

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
    <div className="flex flex-col gap-5">
      {/* Header */}
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h2 className="text-xl font-semibold tracking-tight">Campaigns</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Native Outbound Campaigns V2 driven by a Customer Profiles segment source.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setShowBulkDelete((v) => !v)}
            className="rounded-lg border border-gray-200 px-3 py-1.5 text-xs font-medium hover:bg-gray-50 transition-colors"
          >
            {showBulkDelete ? 'Hide bulk delete' : 'Bulk delete'}
          </button>
          <button
            type="button"
            onClick={() => navigate('/campaigns/new')}
            className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 text-white px-3.5 py-2 text-sm font-medium hover:bg-blue-700 transition-colors"
          >
            <span className="text-xs">+</span> New campaign
          </button>
        </div>
      </div>

      {/* Search + status filter */}
      <div className="flex items-center gap-2 flex-wrap">
        <div className="relative flex-1 max-w-xs">
          <span className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 text-xs">🔍</span>
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search campaigns…"
            className="w-full rounded-lg border border-gray-200 bg-white py-1.5 pl-8 pr-3 text-sm focus:outline-none focus:ring-1 focus:ring-blue-300"
          />
        </div>
        <FilterPill label="All" active={statusFilter === 'all'} onClick={() => setStatusFilter('all')} />
        <FilterPill label="Running" active={statusFilter === 'RUNNING'} onClick={() => setStatusFilter('RUNNING')} />
        <FilterPill label="Paused" active={statusFilter === 'PAUSED'} onClick={() => setStatusFilter('PAUSED')} />
        <FilterPill label="Stopped" active={statusFilter === 'STOPPED'} onClick={() => setStatusFilter('STOPPED')} />
        <FilterPill label="Failed" active={statusFilter === 'FAILED'} onClick={() => setStatusFilter('FAILED')} />
      </div>

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

      {/* Table */}
      {list.isPending ? (
        <div className="flex items-center justify-center py-20">
          <Spinner />
        </div>
      ) : list.isError ? (
        <div className="rounded-xl border border-dashed border-red-200 p-8 text-center text-sm text-red-400">
          {(list.error as Error).message}
        </div>
      ) : (
        <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="bg-gray-50 border-b border-gray-100">
                  <SortHeader label="Name" sortKey="name" current={sortKey} dir={sortDir} onSort={handleSort} />
                  <SortHeader label="State" sortKey="state" current={sortKey} dir={sortDir} onSort={handleSort} />
                  <SortHeader label="Schedule" sortKey="schedule" current={sortKey} dir={sortDir} onSort={handleSort} />
                  <th className="px-4 py-2.5 text-left text-[10px] font-semibold text-gray-500 uppercase tracking-wider whitespace-nowrap">
                    Channels
                  </th>
                  <th className="px-4 py-2.5 text-right text-[10px] font-semibold text-gray-500 uppercase tracking-wider whitespace-nowrap">
                    Actions
                  </th>
                </tr>
              </thead>
              <tbody>
                {campaigns.length === 0 ? (
                  <tr>
                    <td colSpan={5} className="px-4 py-12 text-center text-sm text-gray-400">
                      {search || statusFilter !== 'all'
                        ? 'No campaigns match your filters.'
                        : 'No campaigns yet.'}
                    </td>
                  </tr>
                ) : (
                  campaigns.map((c) => (
                    <CampaignRow
                      key={c.id}
                      campaign={c}
                      onAction={(action) => lifecycle.mutate({ id: c.id, action })}
                      onDelete={() => {
                        if (
                          confirm(
                            `Delete campaign "${c.name}"? Running campaigns will be stopped first.`,
                          )
                        ) {
                          remove.mutate(c.id);
                        }
                      }}
                    />
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Campaign row ──────────────────────────────────────────────────────────────

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
    <tr
      className={[
        'border-b border-gray-100 last:border-0 hover:bg-gray-50/50 transition-colors',
        statusBorderClass(state),
      ].join(' ')}
    >
      <td className="px-4 py-3.5">
        <Link
          to={`/campaigns/${encodeURIComponent(campaign.id)}`}
          className="font-medium text-gray-900 hover:underline"
        >
          {campaign.name}
        </Link>
        <div className="font-mono text-xs text-gray-400 mt-0.5">{campaign.id}</div>
      </td>
      <td className="px-4 py-3.5">
        <Badge tone={stateTone(state)}>{state}</Badge>
      </td>
      <td className="px-4 py-3.5">
        <div className="font-mono text-xs text-gray-400">
          <div>Start: {formatDateTime(campaign.schedule?.startTime)}</div>
          <div>End: {formatDateTime(campaign.schedule?.endTime)}</div>
        </div>
      </td>
      <td className="px-4 py-3.5 text-xs text-gray-500">
        {(campaign.channelSubtypes ?? []).join(', ') || '—'}
      </td>
      <td className="px-4 py-3.5">
        <div className="flex items-center justify-end gap-1.5">
          {state !== 'RUNNING' ? (
            <button
              type="button"
              onClick={() => onAction('start')}
              className="rounded-lg border border-gray-200 px-3 py-1.5 text-xs font-medium hover:bg-gray-50 transition-colors"
            >
              Start
            </button>
          ) : null}
          {state === 'RUNNING' ? (
            <button
              type="button"
              onClick={() => onAction('pause')}
              className="rounded-lg border border-gray-200 px-3 py-1.5 text-xs font-medium hover:bg-gray-50 transition-colors"
            >
              Pause
            </button>
          ) : null}
          {state === 'PAUSED' ? (
            <button
              type="button"
              onClick={() => onAction('resume')}
              className="rounded-lg border border-gray-200 px-3 py-1.5 text-xs font-medium hover:bg-gray-50 transition-colors"
            >
              Resume
            </button>
          ) : null}
          {state === 'RUNNING' || state === 'PAUSED' ? (
            <button
              type="button"
              onClick={() => onAction('stop')}
              className="rounded-lg border border-gray-200 px-3 py-1.5 text-xs font-medium hover:bg-gray-50 transition-colors"
            >
              Stop
            </button>
          ) : null}
          <button
            type="button"
            onClick={onDelete}
            className="rounded-lg px-2 py-1.5 text-xs text-red-400 hover:text-red-600 hover:bg-red-50 transition-colors"
          >
            Delete
          </button>
        </div>
      </td>
    </tr>
  );
}
