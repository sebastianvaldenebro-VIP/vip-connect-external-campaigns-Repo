import { useState, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';

import { Badge, Spinner } from '@/components/ui';
import { api, type AuditEntry } from '@/lib/api';
import { formatDateTime } from '@/lib/utils';

type Filters = {
  actor: string;
  action: string;
  entityType: string;
};

const EMPTY: Filters = { actor: '', action: '', entityType: '' };

const ACTIONS = [
  'create',
  'update',
  'delete',
  'estimate',
  'snapshot',
  'start',
  'stop',
  'pause',
  'resume',
];

const ENTITY_TYPES = ['segment', 'campaign'];

export function Audit(): ReactNode {
  const [filters, setFilters] = useState<Filters>(EMPTY);
  const [applied, setApplied] = useState<Filters>(EMPTY);
  const [selected, setSelected] = useState<AuditEntry | null>(null);

  const list = useQuery({
    queryKey: ['audit', applied],
    queryFn: () =>
      api.audit.list({
        actor: applied.actor || undefined,
        action: applied.action || undefined,
        entityType: applied.entityType || undefined,
        limit: 100,
      }),
  });

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setApplied(filters);
  };

  const onReset = () => {
    setFilters(EMPTY);
    setApplied(EMPTY);
  };

  return (
    <div className="flex flex-col gap-5">
      {/* Header */}
      <div>
        <h2 className="text-xl font-semibold tracking-tight">Audit log</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Immutable record of every admin action. Retained 6 years per HIPAA.
        </p>
      </div>

      {/* Filters */}
      <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm p-4">
        <form onSubmit={onSubmit}>
          <div className="flex flex-wrap items-end gap-3">
            {/* Actor input */}
            <div className="flex flex-col gap-1 flex-1 min-w-[180px]">
              <span className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider">
                Actor (Cognito sub)
              </span>
              <input
                value={filters.actor}
                onChange={(e) => setFilters((f) => ({ ...f, actor: e.target.value }))}
                placeholder="uuid-or-email"
                className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm bg-white focus:outline-none focus:ring-1 focus:ring-blue-300"
              />
            </div>

            {/* Action select */}
            <div className="flex flex-col gap-1">
              <span className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider">
                Action
              </span>
              <select
                value={filters.action}
                onChange={(e) => setFilters((f) => ({ ...f, action: e.target.value }))}
                className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm bg-white focus:outline-none focus:ring-1 focus:ring-blue-300 min-w-[130px]"
              >
                <option value="">All</option>
                {ACTIONS.map((a) => (
                  <option key={a} value={a}>
                    {a}
                  </option>
                ))}
              </select>
            </div>

            {/* Entity type select */}
            <div className="flex flex-col gap-1">
              <span className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider">
                Entity type
              </span>
              <select
                value={filters.entityType}
                onChange={(e) => setFilters((f) => ({ ...f, entityType: e.target.value }))}
                className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm bg-white focus:outline-none focus:ring-1 focus:ring-blue-300 min-w-[130px]"
              >
                <option value="">All</option>
                {ENTITY_TYPES.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            </div>

            {/* Buttons */}
            <button
              type="submit"
              className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 text-white px-3.5 py-2 text-sm font-medium hover:bg-blue-700 transition-colors"
            >
              Apply
            </button>
            <button
              type="button"
              onClick={onReset}
              className="rounded-lg border border-gray-200 px-3 py-1.5 text-xs font-medium hover:bg-gray-50 transition-colors"
            >
              Reset
            </button>
          </div>
        </form>
      </div>

      {/* Main content */}
      <div className="grid grid-cols-1 gap-5 lg:grid-cols-[2fr,1fr]">
        {/* Log table */}
        <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="bg-gray-50 border-b border-gray-100">
                  <th className="px-4 py-2.5 text-left text-[10px] font-semibold text-gray-500 uppercase tracking-wider whitespace-nowrap">
                    When
                  </th>
                  <th className="px-4 py-2.5 text-left text-[10px] font-semibold text-gray-500 uppercase tracking-wider whitespace-nowrap">
                    Actor
                  </th>
                  <th className="px-4 py-2.5 text-left text-[10px] font-semibold text-gray-500 uppercase tracking-wider whitespace-nowrap">
                    Action
                  </th>
                  <th className="px-4 py-2.5 text-left text-[10px] font-semibold text-gray-500 uppercase tracking-wider whitespace-nowrap">
                    Entity
                  </th>
                </tr>
              </thead>
              <tbody>
                {list.isPending ? (
                  <tr>
                    <td colSpan={4} className="px-4 py-8 text-center">
                      <div className="flex items-center justify-center py-12">
                        <Spinner />
                      </div>
                    </td>
                  </tr>
                ) : list.isError ? (
                  <tr>
                    <td colSpan={4} className="px-4 py-8 text-center text-destructive text-sm">
                      {(list.error as Error).message}
                    </td>
                  </tr>
                ) : list.data.entries.length === 0 ? (
                  <tr>
                    <td colSpan={4} className="px-4 py-8">
                      <div className="rounded-xl border border-dashed border-gray-200 p-8 text-center text-sm text-gray-400">
                        No events match these filters.
                      </div>
                    </td>
                  </tr>
                ) : (
                  list.data.entries.map((entry) => (
                    <tr
                      key={`${entry.entityId}-${entry.timestamp}`}
                      className={[
                        'border-b border-gray-100 last:border-0 transition-colors cursor-pointer',
                        selected?.timestamp === entry.timestamp && selected?.entityId === entry.entityId
                          ? 'bg-blue-50'
                          : 'hover:bg-gray-50/50',
                      ].join(' ')}
                      onClick={() => setSelected(entry)}
                    >
                      <td className="px-4 py-3.5 font-mono text-xs text-gray-400 align-top whitespace-nowrap">
                        {formatDateTime(entry.timestamp)}
                      </td>
                      <td className="px-4 py-3.5 align-top text-sm text-gray-700">
                        {entry.actorEmail ?? entry.actorSub ?? '—'}
                      </td>
                      <td className="px-4 py-3.5 align-top">
                        <Badge tone={actionTone(entry.action)}>{entry.action}</Badge>
                      </td>
                      <td className="px-4 py-3.5 align-top font-mono text-xs text-gray-400">
                        {entry.entityId}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>

        {/* Detail panel */}
        <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm p-4">
          {selected ? (
            <EntryDetail entry={selected} />
          ) : (
            <div className="flex items-center justify-center h-full py-8">
              <p className="text-sm text-gray-400 text-center">
                Click a row to see the full before/after diff.
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function EntryDetail({ entry }: { entry: AuditEntry }): ReactNode {
  return (
    <div className="flex flex-col gap-4 text-sm">
      <div>
        <p className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider mb-1">Entity</p>
        <p className="font-mono text-xs text-gray-600">{entry.entityId}</p>
      </div>
      <div>
        <p className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider mb-1">Action</p>
        <Badge tone={actionTone(entry.action)}>{entry.action}</Badge>
      </div>
      <div>
        <p className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider mb-1">Actor</p>
        <p className="text-sm text-gray-700">{entry.actorEmail ?? entry.actorSub ?? '—'}</p>
        {entry.ipAddress ? (
          <p className="text-xs text-gray-400 mt-0.5">from {entry.ipAddress}</p>
        ) : null}
      </div>
      {entry.before ? (
        <div>
          <p className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider mb-1">Before</p>
          <pre className="mt-1 max-h-64 overflow-auto rounded-lg bg-gray-50 border border-gray-100 p-2.5 text-xs font-mono text-gray-600">
            {JSON.stringify(entry.before, null, 2)}
          </pre>
        </div>
      ) : null}
      {entry.after ? (
        <div>
          <p className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider mb-1">After</p>
          <pre className="mt-1 max-h-64 overflow-auto rounded-lg bg-gray-50 border border-gray-100 p-2.5 text-xs font-mono text-gray-600">
            {JSON.stringify(entry.after, null, 2)}
          </pre>
        </div>
      ) : null}
      {entry.extra ? (
        <div>
          <p className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider mb-1">Extra</p>
          <pre className="mt-1 max-h-40 overflow-auto rounded-lg bg-gray-50 border border-gray-100 p-2.5 text-xs font-mono text-gray-600">
            {JSON.stringify(entry.extra, null, 2)}
          </pre>
        </div>
      ) : null}
    </div>
  );
}

function actionTone(action: string): 'success' | 'warning' | 'danger' | 'default' {
  switch (action) {
    case 'create':
    case 'start':
    case 'resume':
      return 'success';
    case 'pause':
    case 'estimate':
    case 'snapshot':
      return 'warning';
    case 'delete':
    case 'stop':
      return 'danger';
    default:
      return 'default';
  }
}
