import { useState, type ReactNode } from 'react';
import { Button, Input } from '@/components/ui';

export type BulkItem = {
  id: string;
  label: string;
  /** ISO date string used for date-range filtering */
  dateKey?: string;
};

function etHourOf(isoDate: string): number {
  return parseInt(
    new Intl.DateTimeFormat('en-US', {
      hour: 'numeric',
      hour12: false,
      timeZone: 'America/New_York',
    }).format(new Date(isoDate)),
    10,
  );
}

function matchesFilter(
  dateKey: string,
  from: string,
  to: string,
  toBeforeHourET: number | null,
): boolean {
  const d = dateKey.slice(0, 10); // YYYY-MM-DD
  if (from && d < from) return false;
  if (to && d > to) return false;
  if (to && d === to && toBeforeHourET !== null && etHourOf(dateKey) >= toBeforeHourET) return false;
  return true;
}

function isoToEtLabel(isoDate: string): string {
  return new Date(isoDate).toLocaleString('en-US', {
    timeZone: 'America/New_York',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }) + ' ET';
}

function todayStr(): string {
  return new Date().toISOString().slice(0, 10);
}

function yesterdayStr(): string {
  const d = new Date();
  d.setDate(d.getDate() - 1);
  return d.toISOString().slice(0, 10);
}

const PRESETS = [
  { label: 'Yesterday', from: () => yesterdayStr(), to: () => yesterdayStr(), hour: null },
  { label: 'Today < 11 AM ET', from: () => todayStr(), to: () => todayStr(), hour: 11 },
  { label: 'Yesterday + Today < 11 AM ET', from: () => yesterdayStr(), to: () => todayStr(), hour: 11 },
] as const;

/** Inline panel that lets the operator delete items whose dateKey falls within a date range. */
export function BulkDeletePanel({
  items,
  entityLabel,
  onDelete,
  onDone,
}: {
  items: BulkItem[];
  entityLabel: string;
  onDelete: (id: string) => Promise<void>;
  onDone: () => void;
}): ReactNode {
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [toBeforeHourET, setToBeforeHourET] = useState<number | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [progress, setProgress] = useState<{ done: number; errors: number; total: number } | null>(null);
  const [error, setError] = useState<string | null>(null);

  function applyPreset(f: string, t: string, hour: number | null) {
    setFrom(f);
    setTo(t);
    setToBeforeHourET(hour);
    setError(null);
  }

  function clearTimeFilter() {
    setToBeforeHourET(null);
  }

  const matching = items.filter(
    (item) => item.dateKey && matchesFilter(item.dateKey, from, to, toBeforeHourET),
  );

  const noDateItems = items.filter((item) => !item.dateKey);

  const handleDelete = async () => {
    if (matching.length === 0) return;
    const timeNote =
      toBeforeHourET !== null ? ` (only before ${toBeforeHourET}:00 AM ET on ${to})` : '';
    const confirmed = window.confirm(
      `Delete ${matching.length} ${entityLabel}(s)?\nRange: ${from || '(any)'} → ${to || '(any)'}${timeNote}\n\nThis cannot be undone.`,
    );
    if (!confirmed) return;

    setDeleting(true);
    setProgress({ done: 0, errors: 0, total: matching.length });
    setError(null);

    let done = 0;
    let errCount = 0;
    const errorMessages: string[] = [];

    const CONCURRENCY = 5;
    for (let i = 0; i < matching.length; i += CONCURRENCY) {
      const chunk = matching.slice(i, i + CONCURRENCY);
      const results = await Promise.allSettled(chunk.map((item) => onDelete(item.id)));
      for (let j = 0; j < results.length; j++) {
        const r = results[j];
        if (r.status === 'fulfilled') {
          done++;
        } else {
          errCount++;
          errorMessages.push(
            `${chunk[j].label}: ${r.reason instanceof Error ? r.reason.message : 'error'}`,
          );
        }
      }
      setProgress({ done, errors: errCount, total: matching.length });
    }

    setDeleting(false);
    setProgress(null);

    if (errorMessages.length > 0) {
      setError(`${errorMessages.length} deletion(s) failed:\n${errorMessages.slice(0, 5).join('\n')}`);
    } else {
      onDone();
    }
  };

  return (
    <div className="rounded-md border border-border bg-muted/30 p-4">
      <p className="mb-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        Bulk delete by date range
      </p>

      <div className="mb-3 flex flex-wrap gap-2">
        {PRESETS.map((p) => (
          <button
            key={p.label}
            type="button"
            className="rounded border border-border bg-background px-2 py-1 text-xs hover:bg-muted disabled:opacity-50"
            disabled={deleting}
            onClick={() => applyPreset(p.from(), p.to(), p.hour)}
          >
            {p.label}
          </button>
        ))}
      </div>

      <div className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1">
          <label className="text-xs text-muted-foreground">From (inclusive)</label>
          <Input
            type="date"
            value={from}
            onChange={(e) => { setFrom(e.target.value); clearTimeFilter(); }}
            className="h-8 text-sm w-40"
            disabled={deleting}
          />
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-xs text-muted-foreground">To (inclusive)</label>
          <Input
            type="date"
            value={to}
            onChange={(e) => { setTo(e.target.value); clearTimeFilter(); }}
            className="h-8 text-sm w-40"
            disabled={deleting}
          />
        </div>
        <div className="flex items-center gap-2">
          <span className="text-sm text-muted-foreground">
            {matching.length > 0 ? (
              <span className="font-medium text-destructive">{matching.length} matched</span>
            ) : from || to ? (
              '0 matched'
            ) : (
              'Set a date range'
            )}
          </span>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="border-destructive/50 text-destructive hover:bg-destructive/10"
            disabled={matching.length === 0 || deleting}
            onClick={handleDelete}
          >
            {deleting && progress
              ? `Deleting ${progress.done + progress.errors}/${progress.total}…`
              : `Delete ${matching.length || ''} ${entityLabel}${matching.length !== 1 ? 's' : ''}`}
          </Button>
        </div>
      </div>

      {toBeforeHourET !== null ? (
        <p className="mt-2 text-xs text-amber-600 dark:text-amber-400">
          Time filter: only items created before {toBeforeHourET}:00 AM ET on {to} are included.
        </p>
      ) : null}

      {matching.length > 0 && !deleting ? (
        <div className="mt-3 max-h-40 overflow-auto rounded border border-border bg-background p-2">
          <p className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">
            Will delete ({matching.length}):
          </p>
          {matching.map((item) => (
            <p key={item.id} className="truncate text-xs text-foreground">
              {item.label}
              {item.dateKey ? (
                <span className="ml-1 text-muted-foreground">({isoToEtLabel(item.dateKey)})</span>
              ) : null}
            </p>
          ))}
        </div>
      ) : null}

      {noDateItems.length > 0 && (from || to) ? (
        <p className="mt-2 text-[10px] text-muted-foreground">
          {noDateItems.length} item(s) have no date and are excluded from bulk delete.
        </p>
      ) : null}

      {error ? (
        <pre className="mt-3 whitespace-pre-wrap rounded border border-destructive/30 bg-destructive/5 p-2 text-xs text-destructive">
          {error}
        </pre>
      ) : null}
    </div>
  );
}
