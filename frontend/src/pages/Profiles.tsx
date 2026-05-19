import { useState, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';

import { Badge, Button, Card, Field, Input, Select } from '@/components/ui';
import { api, type Profile } from '@/lib/api';
import { formatDateTime } from '@/lib/utils';

type SearchKey = '_phone' | '_email' | '_fullName' | '_profileId' | 'customerid';

const SEARCH_KEYS: { value: SearchKey; label: string }[] = [
  { value: '_phone', label: 'Phone' },
  { value: '_email', label: 'Email' },
  { value: '_fullName', label: 'Full name' },
  { value: 'customerid', label: 'Customer ID' },
  { value: '_profileId', label: 'Profile ID' },
];

export function Profiles(): ReactNode {
  const [key, setKey] = useState<SearchKey>('_phone');
  const [value, setValue] = useState('');
  const [applied, setApplied] = useState<{ key: SearchKey; value: string } | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const results = useQuery({
    queryKey: ['profiles', 'search', applied],
    queryFn: () =>
      api.profiles.search({
        key: applied!.key,
        value: applied!.value,
        max: 25,
      }),
    enabled: Boolean(applied),
  });

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!value.trim()) return;
    setApplied({ key, value: value.trim() });
    setSelectedId(null);
  };

  return (
    <div className="flex flex-col gap-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Profiles</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Search the Customer Profiles domain and inspect attributes + calculated values.
        </p>
      </header>

      <Card>
        <form onSubmit={onSubmit} className="grid grid-cols-1 gap-3 md:grid-cols-[160px,1fr,auto]">
          <Field label="Search by">
            <Select value={key} onChange={(e) => setKey(e.target.value as SearchKey)}>
              {SEARCH_KEYS.map((k) => (
                <option key={k.value} value={k.value}>
                  {k.label}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Value">
            <Input
              value={value}
              onChange={(e) => setValue(e.target.value)}
              placeholder="e.g. +15551234567"
              required
            />
          </Field>
          <div className="flex items-end">
            <Button type="submit" disabled={!value.trim()}>
              Search
            </Button>
          </div>
        </form>
      </Card>

      {applied ? (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1fr,2fr]">
          <ResultsList
            results={results.data?.profiles ?? []}
            loading={results.isPending}
            error={results.isError ? (results.error as Error).message : null}
            selectedId={selectedId}
            onSelect={setSelectedId}
          />
          {selectedId ? <ProfilePane profileId={selectedId} /> : (
            <Card>
              <p className="text-sm text-muted-foreground">
                Select a profile on the left to see details.
              </p>
            </Card>
          )}
        </div>
      ) : null}
    </div>
  );
}

function ResultsList({
  results,
  loading,
  error,
  selectedId,
  onSelect,
}: {
  results: Profile[];
  loading: boolean;
  error: string | null;
  selectedId: string | null;
  onSelect: (id: string) => void;
}): ReactNode {
  return (
    <Card className="p-0">
      <div className="border-b border-border px-4 py-2 text-xs uppercase tracking-wide text-muted-foreground">
        Results ({loading ? '…' : results.length})
      </div>
      {loading ? (
        <p className="p-4 text-sm text-muted-foreground">Searching…</p>
      ) : error ? (
        <p className="p-4 text-sm text-destructive">{error}</p>
      ) : results.length === 0 ? (
        <p className="p-4 text-sm text-muted-foreground">No profiles found.</p>
      ) : (
        <ul className="divide-y divide-border">
          {results.map((p) => (
            <li
              key={p.profileId}
              className={
                p.profileId === selectedId
                  ? 'cursor-pointer bg-muted px-4 py-3'
                  : 'cursor-pointer px-4 py-3 hover:bg-muted/50'
              }
              onClick={() => onSelect(p.profileId)}
            >
              <p className="text-sm font-medium">
                {[p.firstName, p.lastName].filter(Boolean).join(' ') || '(no name)'}
              </p>
              <p className="text-xs text-muted-foreground">{p.phoneNumber ?? p.email ?? p.profileId}</p>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function ProfilePane({ profileId }: { profileId: string }): ReactNode {
  const detail = useQuery({
    queryKey: ['profile', profileId],
    queryFn: () => api.profiles.get(profileId),
  });

  const calc = useQuery({
    queryKey: ['profile', profileId, 'calculated'],
    queryFn: () => api.profiles.listCalculatedAttributes(profileId),
  });

  const objects = useQuery({
    queryKey: ['profile', profileId, 'objects'],
    queryFn: () => api.profiles.listObjects(profileId, { max: 10 }),
  });

  if (detail.isPending) return <Card>Loading…</Card>;
  if (detail.isError)
    return <Card><p className="text-destructive">{(detail.error as Error).message}</p></Card>;

  const p = detail.data.profile;

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <div className="flex items-start justify-between">
          <div>
            <p className="text-xs uppercase tracking-wide text-muted-foreground">Profile</p>
            <h2 className="text-xl font-semibold">
              {[p.firstName, p.lastName].filter(Boolean).join(' ') || '(no name)'}
            </h2>
            <p className="mt-1 font-mono text-xs text-muted-foreground">{p.profileId}</p>
          </div>
          <Badge>{formatDateTime(p.lastUpdatedAt)}</Badge>
        </div>
        <div className="mt-4 grid grid-cols-1 gap-2 md:grid-cols-2">
          <KV k="Phone" v={p.phoneNumber} />
          <KV k="Email" v={p.email} />
          <KV k="Created" v={formatDateTime(p.createdAt)} />
          <KV k="Updated" v={formatDateTime(p.lastUpdatedAt)} />
        </div>
      </Card>

      <Card>
        <h3 className="text-sm font-semibold">Attributes</h3>
        {p.attributes && Object.keys(p.attributes).length > 0 ? (
          <dl className="mt-3 grid grid-cols-1 gap-x-6 gap-y-2 md:grid-cols-2">
            {Object.entries(p.attributes).map(([k, v]) => (
              <div key={k} className="flex flex-col">
                <dt className="text-xs uppercase tracking-wide text-muted-foreground">{k}</dt>
                <dd className="text-sm">{String(v)}</dd>
              </div>
            ))}
          </dl>
        ) : (
          <p className="mt-2 text-sm text-muted-foreground">No attributes.</p>
        )}
      </Card>

      <Card>
        <h3 className="text-sm font-semibold">Calculated attributes</h3>
        {calc.isPending ? (
          <p className="mt-2 text-sm text-muted-foreground">Loading…</p>
        ) : calc.data && calc.data.calculatedAttributes.length > 0 ? (
          <pre className="mt-2 max-h-60 overflow-auto rounded-md bg-muted p-2 text-xs">
            {JSON.stringify(calc.data.calculatedAttributes, null, 2)}
          </pre>
        ) : (
          <p className="mt-2 text-sm text-muted-foreground">None.</p>
        )}
      </Card>

      <Card>
        <h3 className="text-sm font-semibold">
          Profile objects{' '}
          {objects.data ? (
            <span className="text-xs text-muted-foreground">({objects.data.objectType})</span>
          ) : null}
        </h3>
        {objects.isPending ? (
          <p className="mt-2 text-sm text-muted-foreground">Loading…</p>
        ) : objects.data && objects.data.objects.length > 0 ? (
          <pre className="mt-2 max-h-72 overflow-auto rounded-md bg-muted p-2 text-xs">
            {JSON.stringify(objects.data.objects, null, 2)}
          </pre>
        ) : (
          <p className="mt-2 text-sm text-muted-foreground">No objects.</p>
        )}
      </Card>
    </div>
  );
}

function KV({ k, v }: { k: string; v?: string | null }): ReactNode {
  return (
    <div className="flex flex-col">
      <span className="text-xs uppercase tracking-wide text-muted-foreground">{k}</span>
      <span className="text-sm">{v || '—'}</span>
    </div>
  );
}
