import { useState, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';

import { Badge, Spinner } from '@/components/ui';
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
    <div className="flex flex-col gap-5">
      {/* Header */}
      <div>
        <h2 className="text-xl font-semibold tracking-tight">Profiles</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Search the Customer Profiles domain and inspect attributes + calculated values.
        </p>
      </div>

      {/* Search bar */}
      <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm p-4">
        <form onSubmit={onSubmit}>
          <div className="flex flex-wrap items-end gap-3">
            {/* Search by select */}
            <div className="flex flex-col gap-1">
              <span className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider">
                Search by
              </span>
              <select
                value={key}
                onChange={(e) => setKey(e.target.value as SearchKey)}
                className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm bg-white focus:outline-none focus:ring-1 focus:ring-blue-300 min-w-[140px]"
              >
                {SEARCH_KEYS.map((k) => (
                  <option key={k.value} value={k.value}>
                    {k.label}
                  </option>
                ))}
              </select>
            </div>

            {/* Value input */}
            <div className="flex flex-col gap-1 flex-1 min-w-[200px]">
              <span className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider">
                Value
              </span>
              <div className="relative">
                <span className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 text-xs">🔍</span>
                <input
                  value={value}
                  onChange={(e) => setValue(e.target.value)}
                  placeholder="e.g. +15551234567"
                  required
                  className="w-full rounded-lg border border-gray-200 bg-white py-1.5 pl-8 pr-3 text-sm focus:outline-none focus:ring-1 focus:ring-blue-300"
                />
              </div>
            </div>

            {/* Submit */}
            <button
              type="submit"
              disabled={!value.trim()}
              className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 text-white px-3.5 py-2 text-sm font-medium hover:bg-blue-700 transition-colors disabled:opacity-50 disabled:pointer-events-none"
            >
              Search
            </button>
          </div>
        </form>
      </div>

      {/* Results */}
      {applied ? (
        <div className="grid grid-cols-1 gap-5 lg:grid-cols-[1fr,2fr]">
          <ResultsList
            results={results.data?.profiles ?? []}
            loading={results.isPending}
            error={results.isError ? (results.error as Error).message : null}
            selectedId={selectedId}
            onSelect={setSelectedId}
          />
          {selectedId ? (
            <ProfilePane profileId={selectedId} />
          ) : (
            <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm p-6 flex items-center justify-center">
              <p className="text-sm text-gray-400">
                Select a profile on the left to see details.
              </p>
            </div>
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
    <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
      {/* Header row */}
      <div className="border-b border-gray-100 bg-gray-50 px-4 py-2.5">
        <span className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider">
          Results ({loading ? '…' : results.length})
        </span>
      </div>

      {loading ? (
        <div className="flex items-center justify-center py-20">
          <Spinner />
        </div>
      ) : error ? (
        <div className="p-4 text-sm text-destructive">{error}</div>
      ) : results.length === 0 ? (
        <div className="rounded-xl border border-dashed border-gray-200 m-4 p-8 text-center text-sm text-gray-400">
          No profiles found.
        </div>
      ) : (
        <div className="divide-y divide-gray-100">
          {results.map((p) => {
            const name = [p.firstName, p.lastName].filter(Boolean).join(' ') || '(no name)';
            const sub = p.phoneNumber ?? p.email ?? p.profileId;
            const isSelected = p.profileId === selectedId;
            return (
              <div
                key={p.profileId}
                onClick={() => onSelect(p.profileId)}
                className={[
                  'bg-white border-b border-gray-100 last:border-0 px-4 py-3 cursor-pointer transition-colors',
                  isSelected ? 'bg-blue-50' : 'hover:bg-gray-50/50',
                ].join(' ')}
              >
                <p className="text-sm font-semibold text-gray-900">{name}</p>
                <p className="font-mono text-xs text-gray-400 mt-0.5">{sub}</p>
              </div>
            );
          })}
        </div>
      )}
    </div>
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

  if (detail.isPending) {
    return (
      <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm flex items-center justify-center py-20">
        <Spinner />
      </div>
    );
  }
  if (detail.isError) {
    return (
      <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm p-4">
        <p className="text-sm text-destructive">{(detail.error as Error).message}</p>
      </div>
    );
  }

  const p = detail.data.profile;

  return (
    <div className="flex flex-col gap-4">
      {/* Identity card */}
      <div className="bg-white border border-gray-200 rounded-xl p-4 shadow-sm">
        <div className="flex items-start justify-between">
          <div>
            <p className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider mb-1">Profile</p>
            <h3 className="text-lg font-semibold text-gray-900">
              {[p.firstName, p.lastName].filter(Boolean).join(' ') || '(no name)'}
            </h3>
            <p className="mt-0.5 font-mono text-xs text-gray-400">{p.profileId}</p>
          </div>
          <Badge>{formatDateTime(p.lastUpdatedAt)}</Badge>
        </div>
        <div className="mt-4 grid grid-cols-1 gap-2 md:grid-cols-2">
          <KV k="Phone" v={p.phoneNumber} />
          <KV k="Email" v={p.email} />
          <KV k="Created" v={formatDateTime(p.createdAt)} />
          <KV k="Updated" v={formatDateTime(p.lastUpdatedAt)} />
        </div>
      </div>

      {/* Attributes */}
      <div className="bg-white border border-gray-200 rounded-xl p-4 shadow-sm">
        <h4 className="text-sm font-semibold text-gray-900 mb-3">Attributes</h4>
        {p.attributes && Object.keys(p.attributes).length > 0 ? (
          <dl className="grid grid-cols-1 gap-x-6 gap-y-2 md:grid-cols-2">
            {Object.entries(p.attributes).map(([k, v]) => (
              <div key={k} className="flex flex-col">
                <dt className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider">{k}</dt>
                <dd className="text-sm text-gray-700">{String(v)}</dd>
              </div>
            ))}
          </dl>
        ) : (
          <div className="rounded-xl border border-dashed border-gray-200 p-6 text-center text-sm text-gray-400">
            No attributes.
          </div>
        )}
      </div>

      {/* Calculated attributes */}
      <div className="bg-white border border-gray-200 rounded-xl p-4 shadow-sm">
        <h4 className="text-sm font-semibold text-gray-900 mb-2">Calculated attributes</h4>
        {calc.isPending ? (
          <div className="flex items-center justify-center py-8">
            <Spinner />
          </div>
        ) : calc.data && calc.data.calculatedAttributes.length > 0 ? (
          <pre className="mt-1 max-h-60 overflow-auto rounded-lg bg-gray-50 border border-gray-100 p-2.5 text-xs font-mono text-gray-600">
            {JSON.stringify(calc.data.calculatedAttributes, null, 2)}
          </pre>
        ) : (
          <div className="rounded-xl border border-dashed border-gray-200 p-6 text-center text-sm text-gray-400">
            None.
          </div>
        )}
      </div>

      {/* Profile objects */}
      <div className="bg-white border border-gray-200 rounded-xl p-4 shadow-sm">
        <h4 className="text-sm font-semibold text-gray-900 mb-2">
          Profile objects{' '}
          {objects.data ? (
            <span className="text-xs text-gray-400 font-normal">({objects.data.objectType})</span>
          ) : null}
        </h4>
        {objects.isPending ? (
          <div className="flex items-center justify-center py-8">
            <Spinner />
          </div>
        ) : objects.data && objects.data.objects.length > 0 ? (
          <pre className="mt-1 max-h-72 overflow-auto rounded-lg bg-gray-50 border border-gray-100 p-2.5 text-xs font-mono text-gray-600">
            {JSON.stringify(objects.data.objects, null, 2)}
          </pre>
        ) : (
          <div className="rounded-xl border border-dashed border-gray-200 p-6 text-center text-sm text-gray-400">
            No objects.
          </div>
        )}
      </div>
    </div>
  );
}

function KV({ k, v }: { k: string; v?: string | null }): ReactNode {
  return (
    <div className="flex flex-col">
      <span className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider">{k}</span>
      <span className="text-sm text-gray-700">{v || '—'}</span>
    </div>
  );
}
