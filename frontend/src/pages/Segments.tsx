import { useMemo, useState, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useNavigate } from 'react-router-dom';

import { Badge, Spinner } from '@/components/ui';
import { BulkDeletePanel } from '@/components/BulkDeletePanel';
import { EnableCampaignModal } from '@/components/EnableCampaignModal';
import {
  api,
  type ReconcileResult,
  type SegmentSummary,
  type VerifyResult,
} from '@/lib/api';
import { stateCodesFromSegmentGroups, stateCodesFromSegmentName, useLocationMapping } from '@/lib/stateLocationMap';
import { formatDateTime } from '@/lib/utils';

type SegSortKey = 'name' | 'created';
type SortDir = 'asc' | 'desc';

function SortHeader({
  label,
  sortKey,
  current,
  dir,
  onSort,
}: {
  label: string;
  sortKey: SegSortKey;
  current: SegSortKey;
  dir: SortDir;
  onSort: (key: SegSortKey) => void;
}): ReactNode {
  const active = current === sortKey;
  return (
    <th className="px-4 py-2.5 text-left text-[10px] font-semibold text-gray-500 uppercase tracking-wider whitespace-nowrap">
      <button
        type="button"
        className="inline-flex items-center gap-1 hover:text-gray-800"
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

type EstimateMap = Record<string, { totalCount: number | null; at: string } | 'pending'>;
type VerifyMap = Record<string, VerifyResult | 'pending' | undefined>;
type ReconcileMap = Record<string, 'pending' | undefined>;

export function Segments(): ReactNode {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [estimates, setEstimates] = useState<EstimateMap>({});
  const [verifications, setVerifications] = useState<VerifyMap>({});
  const [reconciling, setReconciling] = useState<ReconcileMap>({});
  const [lastReconcile, setLastReconcile] = useState<ReconcileResult | null>(null);
  const [showBulkDelete, setShowBulkDelete] = useState(false);
  const [search, setSearch] = useState('');
  const [sortKey, setSortKey] = useState<SegSortKey>('created');
  const [sortDir, setSortDir] = useState<SortDir>('desc');

  const list = useQuery({
    queryKey: ['segments'],
    queryFn: () => api.segments.list({ maxResults: 100 }),
  });

  function handleSort(key: SegSortKey) {
    if (sortKey === key) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortKey(key);
      setSortDir('asc');
    }
  }

  const segments = useMemo(() => {
    const raw = list.data?.segments ?? [];
    const q = search.trim().toLowerCase();
    const filtered = q
      ? raw.filter((s) => (s.displayName ?? s.name).toLowerCase().includes(q) || s.name.toLowerCase().includes(q))
      : raw;
    return [...filtered].sort((a, b) => {
      let cmp = 0;
      if (sortKey === 'name') cmp = (a.displayName ?? a.name).localeCompare(b.displayName ?? b.name);
      else if (sortKey === 'created') cmp = (a.createdAt ?? '').localeCompare(b.createdAt ?? '');
      return sortDir === 'asc' ? cmp : -cmp;
    });
  }, [list.data, search, sortKey, sortDir]);

  const refreshEstimate = useMutation({
    mutationFn: async (name: string) => {
      setEstimates((m) => ({ ...m, [name]: 'pending' }));
      const { estimateId } = await api.segments.createEstimate(name);
      return pollEstimate(name, estimateId);
    },
    onSuccess: (result, name) => {
      setEstimates((m) => ({
        ...m,
        [name]: { totalCount: result.totalCount, at: new Date().toISOString() },
      }));
    },
    onError: (_err, name) => {
      setEstimates((m) => {
        const copy = { ...m };
        delete copy[name];
        return copy;
      });
    },
  });

  const verify = useMutation({
    mutationFn: async (name: string) => {
      setVerifications((m) => ({ ...m, [name]: 'pending' }));
      return api.segments.verify(name);
    },
    onSuccess: (result, name) => setVerifications((m) => ({ ...m, [name]: result })),
    onError: (_err, name) =>
      setVerifications((m) => {
        const copy = { ...m };
        delete copy[name];
        return copy;
      }),
  });

  const reconcile = useMutation({
    mutationFn: async (name: string) => {
      setReconciling((m) => ({ ...m, [name]: 'pending' }));
      const result = await api.segments.reconcile(name);
      return { name, result };
    },
    onSettled: (_res, _err, name) =>
      setReconciling((m) => {
        const copy = { ...m };
        delete copy[name];
        return copy;
      }),
    onSuccess: async ({ name, result }) => {
      setLastReconcile(result);
      qc.invalidateQueries({ queryKey: ['segments'] });
      // Drop any UI state tied to the old segment name.
      setVerifications((m) => {
        const copy = { ...m };
        delete copy[name];
        return copy;
      });
      setEstimates((m) => {
        const copy = { ...m };
        delete copy[name];
        // The rebuild produced an exact Redis-matching set, so seed the
        // estimate tile for the new segment immediately without waiting for
        // a round-trip.
        copy[result.newSegmentName] = {
          totalCount: result.targetCount,
          at: result.completedAt,
        };
        return copy;
      });
      // Re-verify against the new segment version so the row shows ✓.
      const fresh = await api.segments.verify(result.newSegmentName);
      setVerifications((m) => ({ ...m, [result.newSegmentName]: fresh }));
    },
  });

  const remove = useMutation({
    mutationFn: (name: string) => api.segments.remove(name),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['segments'] }),
  });

  return (
    <div className="flex flex-col gap-5">
      {/* Header */}
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h2 className="text-xl font-semibold tracking-tight">Segments</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Define Customer Profiles segments; for manual-sync ones, verify against Redis and
            rebuild when the engine drifts.
          </p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          {/* Search */}
          <div className="relative flex-1 max-w-xs">
            <span className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 text-xs">🔍</span>
            <input
              type="search"
              placeholder="Search segments…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="w-full rounded-lg border border-gray-200 bg-white py-1.5 pl-8 pr-3 text-sm focus:outline-none focus:ring-1 focus:ring-blue-300"
            />
          </div>
          <button
            type="button"
            onClick={() => qc.invalidateQueries({ queryKey: ['segments'] })}
            disabled={list.isFetching}
            className="rounded-lg border border-gray-200 px-3 py-1.5 text-xs font-medium hover:bg-gray-50 transition-colors disabled:opacity-50"
          >
            {list.isFetching ? (
              <span className="inline-flex items-center gap-2"><Spinner /> refreshing…</span>
            ) : (
              'Refresh'
            )}
          </button>
          <button
            type="button"
            onClick={() => setShowBulkDelete((v) => !v)}
            className="rounded-lg border border-gray-200 px-3 py-1.5 text-xs font-medium hover:bg-gray-50 transition-colors"
          >
            {showBulkDelete ? 'Hide bulk delete' : 'Bulk delete'}
          </button>
          <button
            type="button"
            onClick={() => navigate('/segments/new')}
            className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 text-white px-3.5 py-2 text-sm font-medium hover:bg-blue-700 transition-colors"
          >
            <span className="text-xs">+</span> New segment
          </button>
        </div>
      </div>

      {showBulkDelete ? (
        <BulkDeletePanel
          items={(list.data?.segments ?? []).map((s) => ({
            id: s.name,
            label: s.name,
            dateKey: s.createdAt,
          }))}
          entityLabel="segment"
          onDelete={(name) => api.segments.remove(name)}
          onDone={() => {
            setShowBulkDelete(false);
            qc.invalidateQueries({ queryKey: ['segments'] });
          }}
        />
      ) : null}

      {lastReconcile ? (
        <div className="flex items-start gap-2 rounded-xl border border-green-200 bg-green-50 px-3 py-2.5 text-xs text-green-800">
          <span className="mt-0.5 shrink-0">✓</span>
          <div>
            Rebuilt <code>{lastReconcile.newSegmentName}</code> —{' '}
            {lastReconcile.targetCount.toLocaleString()} members (+
            {lastReconcile.added.toLocaleString()} / −{lastReconcile.removed.toLocaleString()}).
            {lastReconcile.campaignsUpdated.length > 0
              ? ` Retargeted ${lastReconcile.campaignsUpdated.length} campaign(s).`
              : null}
          </div>
        </div>
      ) : null}

      {/* Table */}
      <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="bg-gray-50 border-b border-gray-100">
                <SortHeader label="Name" sortKey="name" current={sortKey} dir={sortDir} onSort={handleSort} />
                <th className="px-4 py-2.5 text-left text-[10px] font-semibold text-gray-500 uppercase tracking-wider whitespace-nowrap">Member count</th>
                <th className="px-4 py-2.5 text-left text-[10px] font-semibold text-gray-500 uppercase tracking-wider whitespace-nowrap">Redis vs CP</th>
                <SortHeader label="Created" sortKey="created" current={sortKey} dir={sortDir} onSort={handleSort} />
                <th className="px-4 py-2.5 text-right text-[10px] font-semibold text-gray-500 uppercase tracking-wider whitespace-nowrap">Actions</th>
              </tr>
            </thead>
            <tbody>
              {list.isPending ? (
                <RowMessage colSpan={5} message="Loading segments…" />
              ) : list.isError ? (
                <RowMessage colSpan={5} message={(list.error as Error).message} tone="danger" />
              ) : segments.length === 0 ? (
                <RowMessage colSpan={5} message={search ? 'No segments match your search.' : 'No segments yet — create the first one.'} />
              ) : (
                segments.map((seg) => (
                  <SegmentRow
                    key={seg.name}
                    segment={seg}
                    estimate={estimates[seg.name]}
                    verification={verifications[seg.name]}
                    reconcilePending={reconciling[seg.name] === 'pending'}
                    onRefresh={() => refreshEstimate.mutate(seg.name)}
                    onVerify={() => verify.mutate(seg.name)}
                    onReconcile={() => reconcile.mutate(seg.name)}
                    onDelete={() => {
                      if (confirm(`Delete segment "${seg.name}"? This cannot be undone.`)) {
                        remove.mutate(seg.name);
                      }
                    }}
                  />
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
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
            ? 'px-4 py-8 text-center text-sm text-red-500'
            : 'px-4 py-8 text-center text-sm text-gray-400'
        }
      >
        {message}
      </td>
    </tr>
  );
}

function SegmentRow({
  segment,
  estimate,
  verification,
  reconcilePending,
  onRefresh,
  onVerify,
  onReconcile,
  onDelete,
}: {
  segment: SegmentSummary;
  estimate: EstimateMap[string] | undefined;
  verification: VerifyMap[string];
  reconcilePending: boolean;
  onRefresh: () => void;
  onVerify: () => void;
  onReconcile: () => void;
  onDelete: () => void;
}): ReactNode {
  const [enableOpen, setEnableOpen] = useState(false);

  // The list view doesn't carry the segment's segmentGroups, so fetch the
  // detail lazily when the operator clicks Enable Campaign — that's the only
  // way to derive state codes for area-code phone matching.
  const detail = useQuery({
    queryKey: ['segment', segment.name],
    queryFn: () => api.segments.get(segment.name),
    enabled: enableOpen,
  });
  const { locationMap } = useLocationMapping();
  const segmentStates = detail.data
    ? (stateCodesFromSegmentGroups(detail.data.segmentGroups, locationMap).length > 0
        ? stateCodesFromSegmentGroups(detail.data.segmentGroups, locationMap)
        : stateCodesFromSegmentName(segment.name, locationMap))
    : stateCodesFromSegmentName(segment.name, locationMap);

  return (
    <tr className="border-b border-gray-100 last:border-0 hover:bg-gray-50/50 transition-colors">
      <td className="px-4 py-3.5 align-top">
        <Link
          to={`/segments/${encodeURIComponent(segment.name)}`}
          className="font-medium text-gray-900 hover:underline"
        >
          {segment.displayName ?? segment.name}
        </Link>
        <div className="font-mono text-xs text-gray-400 mt-0.5">
          {segment.name}
          {segment.version ? (
            <span className="ml-2 rounded bg-gray-100 px-1.5 py-0.5 text-[10px] uppercase tracking-wide">
              v{segment.version}
            </span>
          ) : null}
        </div>
      </td>
      <td className="px-4 py-3.5 align-top">
        {estimate === 'pending' ? (
          <span className="inline-flex items-center gap-2 text-gray-400 text-xs">
            <Spinner /> computing…
          </span>
        ) : estimate ? (
          <div>
            <span className="font-medium text-gray-900">
              {estimate.totalCount?.toLocaleString() ?? '—'}
            </span>
            <div className="text-xs text-gray-400">
              as of {formatDateTime(estimate.at)}
            </div>
          </div>
        ) : (
          <Badge tone="muted">not computed</Badge>
        )}
      </td>
      <td className="px-4 py-3.5 align-top">
        <VerificationCell
          verification={verification}
          reconcilePending={reconcilePending}
          onVerify={onVerify}
          onReconcile={onReconcile}
        />
      </td>
      <td className="px-4 py-3.5 align-top font-mono text-xs text-gray-400">
        {formatDateTime(segment.createdAt)}
      </td>
      <td className="px-4 py-3.5 align-top">
        <div className="flex items-center justify-end gap-1.5">
          <button
            type="button"
            onClick={onRefresh}
            className="rounded-lg border border-gray-200 px-2.5 py-1.5 text-xs font-medium hover:bg-gray-50 transition-colors"
          >
            Refresh count
          </button>
          <button
            type="button"
            onClick={() => setEnableOpen(true)}
            className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 text-white px-2.5 py-1.5 text-xs font-medium hover:bg-blue-700 transition-colors"
          >
            Enable Campaign
          </button>
          <button
            type="button"
            onClick={onDelete}
            className="rounded-lg px-2 py-1.5 text-xs text-red-400 hover:text-red-600 hover:bg-red-50 transition-colors"
          >
            Delete
          </button>
        </div>
        <EnableCampaignModal
          open={enableOpen}
          onClose={() => setEnableOpen(false)}
          segmentName={segment.name}
          segmentArn={segment.segmentArn}
          segmentStates={segmentStates}
          segmentGroups={detail.data?.segmentGroups}
        />
      </td>
    </tr>
  );
}

function VerificationCell({
  verification,
  reconcilePending,
  onVerify,
  onReconcile,
}: {
  verification: VerifyMap[string];
  reconcilePending: boolean;
  onVerify: () => void;
  onReconcile: () => void;
}): ReactNode {
  if (verification === 'pending') {
    return (
      <span className="inline-flex items-center gap-2 text-gray-400 text-xs">
        <Spinner /> scanning Redis…
      </span>
    );
  }
  if (!verification) {
    return (
      <button
        type="button"
        onClick={onVerify}
        className="rounded-lg border border-gray-200 px-2.5 py-1.5 text-xs font-medium hover:bg-gray-50 transition-colors"
      >
        Verify
      </button>
    );
  }

  const missing = verification.missingCustomerIds.length;
  const extras = verification.extraCustomerIds.length;
  const inSync = missing === 0 && extras === 0;

  if (inSync) {
    return (
      <div className="flex flex-col gap-1">
        <Badge tone="success">
          {verification.redisCount.toLocaleString()} / {verification.segmentCount.toLocaleString()} ✓
        </Badge>
        <span className="text-xs text-gray-400">
          verified {formatDateTime(verification.verifiedAt)}
        </span>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="danger">
          {verification.redisCount.toLocaleString()} Redis · {verification.segmentCount.toLocaleString()} CP
        </Badge>
        <span className="text-xs text-red-500">
          {missing > 0 ? `+${missing} missing` : null}
          {missing > 0 && extras > 0 ? ' · ' : null}
          {extras > 0 ? `−${extras} extra` : null}
        </span>
      </div>
      <div className="flex items-center gap-1.5">
        <button
          type="button"
          onClick={onReconcile}
          disabled={reconcilePending}
          className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 text-white px-2.5 py-1.5 text-xs font-medium hover:bg-blue-700 transition-colors disabled:opacity-50"
        >
          {reconcilePending ? (
            <span className="inline-flex items-center gap-2">
              <Spinner /> rebuilding…
            </span>
          ) : (
            'Rebuild segment'
          )}
        </button>
        <button
          type="button"
          onClick={onVerify}
          className="rounded-lg border border-gray-200 px-2.5 py-1.5 text-xs font-medium hover:bg-gray-50 transition-colors"
        >
          Re-verify
        </button>
      </div>
    </div>
  );
}

async function pollEstimate(
  name: string,
  estimateId: string,
): Promise<{ totalCount: number | null }> {
  const deadline = Date.now() + 90_000;
  let delay = 1_500;
  while (Date.now() < deadline) {
    const res = await api.segments.getEstimate(name, estimateId);
    if (res.status === 'SUCCEEDED') {
      return { totalCount: res.estimate?.totalCount ?? null };
    }
    if (res.status === 'FAILED') {
      throw new Error(res.message ?? 'Estimate failed');
    }
    await new Promise((r) => setTimeout(r, delay));
    delay = Math.min(delay * 1.3, 5_000);
  }
  throw new Error('Estimate timed out');
}
