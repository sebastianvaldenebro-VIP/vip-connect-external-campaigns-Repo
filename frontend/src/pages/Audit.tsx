import { useState, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';

import { Badge, Button, Card, Field, Input, Select } from '@/components/ui';
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
    <div className="flex flex-col gap-6">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Audit log</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Immutable record of every admin action. Retained 6 years per HIPAA.
          </p>
        </div>
      </header>

      <Card>
        <form onSubmit={onSubmit} className="grid grid-cols-1 gap-3 md:grid-cols-[1fr,160px,160px,auto,auto]">
          <Field label="Actor (Cognito sub)">
            <Input
              value={filters.actor}
              onChange={(e) => setFilters((f) => ({ ...f, actor: e.target.value }))}
              placeholder="uuid-or-email"
            />
          </Field>
          <Field label="Action">
            <Select
              value={filters.action}
              onChange={(e) => setFilters((f) => ({ ...f, action: e.target.value }))}
            >
              <option value="">All</option>
              {ACTIONS.map((a) => (
                <option key={a} value={a}>
                  {a}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Entity type">
            <Select
              value={filters.entityType}
              onChange={(e) => setFilters((f) => ({ ...f, entityType: e.target.value }))}
            >
              <option value="">All</option>
              {ENTITY_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </Select>
          </Field>
          <div className="flex items-end">
            <Button type="submit">Apply</Button>
          </div>
          <div className="flex items-end">
            <Button type="button" variant="ghost" onClick={onReset}>
              Reset
            </Button>
          </div>
        </form>
      </Card>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[2fr,1fr]">
        <Card className="p-0">
          <table className="w-full text-sm">
            <thead className="border-b border-border bg-muted/50 text-xs uppercase tracking-wide text-muted-foreground">
              <tr>
                <th className="px-4 py-2 text-left font-medium">When</th>
                <th className="px-4 py-2 text-left font-medium">Actor</th>
                <th className="px-4 py-2 text-left font-medium">Action</th>
                <th className="px-4 py-2 text-left font-medium">Entity</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {list.isPending ? (
                <tr>
                  <td colSpan={4} className="px-4 py-8 text-center text-muted-foreground">
                    Loading…
                  </td>
                </tr>
              ) : list.isError ? (
                <tr>
                  <td colSpan={4} className="px-4 py-8 text-center text-destructive">
                    {(list.error as Error).message}
                  </td>
                </tr>
              ) : list.data.entries.length === 0 ? (
                <tr>
                  <td colSpan={4} className="px-4 py-8 text-center text-muted-foreground">
                    No events match these filters.
                  </td>
                </tr>
              ) : (
                list.data.entries.map((entry) => (
                  <tr
                    key={`${entry.entityId}-${entry.timestamp}`}
                    className={
                      selected?.timestamp === entry.timestamp && selected?.entityId === entry.entityId
                        ? 'cursor-pointer bg-muted'
                        : 'cursor-pointer hover:bg-muted/50'
                    }
                    onClick={() => setSelected(entry)}
                  >
                    <td className="px-4 py-2 align-top text-xs text-muted-foreground">
                      {formatDateTime(entry.timestamp)}
                    </td>
                    <td className="px-4 py-2 align-top">
                      {entry.actorEmail ?? entry.actorSub ?? '—'}
                    </td>
                    <td className="px-4 py-2 align-top">
                      <Badge tone={actionTone(entry.action)}>{entry.action}</Badge>
                    </td>
                    <td className="px-4 py-2 align-top text-xs text-muted-foreground">
                      {entry.entityId}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </Card>

        <Card>
          {selected ? (
            <EntryDetail entry={selected} />
          ) : (
            <p className="text-sm text-muted-foreground">
              Click a row to see the full before/after diff.
            </p>
          )}
        </Card>
      </div>
    </div>
  );
}

function EntryDetail({ entry }: { entry: AuditEntry }): ReactNode {
  return (
    <div className="flex flex-col gap-3 text-sm">
      <div>
        <p className="text-xs uppercase tracking-wide text-muted-foreground">Entity</p>
        <p className="font-mono text-xs">{entry.entityId}</p>
      </div>
      <div>
        <p className="text-xs uppercase tracking-wide text-muted-foreground">Action</p>
        <Badge tone={actionTone(entry.action)}>{entry.action}</Badge>
      </div>
      <div>
        <p className="text-xs uppercase tracking-wide text-muted-foreground">Actor</p>
        <p>{entry.actorEmail ?? entry.actorSub ?? '—'}</p>
        {entry.ipAddress ? (
          <p className="text-xs text-muted-foreground">from {entry.ipAddress}</p>
        ) : null}
      </div>
      {entry.before ? (
        <div>
          <p className="text-xs uppercase tracking-wide text-muted-foreground">Before</p>
          <pre className="mt-1 max-h-64 overflow-auto rounded-md bg-muted p-2 text-xs">
            {JSON.stringify(entry.before, null, 2)}
          </pre>
        </div>
      ) : null}
      {entry.after ? (
        <div>
          <p className="text-xs uppercase tracking-wide text-muted-foreground">After</p>
          <pre className="mt-1 max-h-64 overflow-auto rounded-md bg-muted p-2 text-xs">
            {JSON.stringify(entry.after, null, 2)}
          </pre>
        </div>
      ) : null}
      {entry.extra ? (
        <div>
          <p className="text-xs uppercase tracking-wide text-muted-foreground">Extra</p>
          <pre className="mt-1 max-h-40 overflow-auto rounded-md bg-muted p-2 text-xs">
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
