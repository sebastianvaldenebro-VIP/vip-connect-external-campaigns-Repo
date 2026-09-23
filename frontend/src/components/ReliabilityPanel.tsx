import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { useAuth } from '@/hooks/useAuth';
import { api } from '@/lib/api';
import { observationsFromAudit, observationStatus } from '@/lib/reliability';
import { usePreferences } from './UserPreferencesProvider';

export function ReliabilityPanel({
  segmentName,
  campaign = false,
}: {
  segmentName?: string;
  campaign?: boolean;
}) {
  const { user, groups, loading } = useAuth();
  const { preferences } = usePreferences();
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(timer);
  }, []);
  const allowed = !loading && !!user && groups.includes('Admin');
  const query = useQuery({
    queryKey: [
      'reliability-observations',
      user?.userId,
      'segment',
      segmentName,
    ],
    enabled: allowed && !!segmentName,
    staleTime: 60_000,
    retry: false,
    queryFn: async () => {
      // Minimize the query cache: no emails, profile IDs, samples, or raw audit events.
      const result = await api.audit.entityHistory(`segment/${segmentName}`);
      return observationsFromAudit(result.entries);
    },
  });
  if (!allowed) return null;
  return (
    <section
      className="rounded-xl border border-border bg-card p-5 shadow-sm"
      aria-label="Segment reliability"
    >
      <div className="flex items-center justify-between gap-4">
        <div>
          <h3 className="text-sm font-semibold">Segment reliability</h3>
          <p className="mt-1 text-xs text-muted-foreground">
            Recorded count comparisons. Matching counts do not prove matching
            members.
          </p>
        </div>
        <button
          type="button"
          disabled={query.isFetching || !segmentName}
          className="text-xs text-primary disabled:opacity-50"
          onClick={() => void query.refetch()}
        >
          Refresh
        </button>
      </div>
      {campaign && !segmentName ? (
        <p className="mt-4 text-sm text-muted-foreground">
          No Customer Profiles segment is linked to this campaign.
        </p>
      ) : !segmentName ? (
        <p className="mt-4 text-sm text-muted-foreground">
          Select a segment to view its recorded observations.
        </p>
      ) : query.isPending ? (
        <p className="mt-4 text-sm" role="status">
          Loading observations…
        </p>
      ) : query.isError ? (
        <p className="mt-4 text-sm text-red-700" role="alert">
          Verification history is unavailable. Refresh to retry.
        </p>
      ) : query.data?.length === 0 ? (
        <p className="mt-4 text-sm text-muted-foreground">
          No verification was found in the returned history. This does not
          establish segment health.
        </p>
      ) : (
        <div className="mt-4 overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-border">
                {[
                  'Segment',
                  'Redis count',
                  'Connect estimate',
                  'Observation',
                  'Checked at',
                ].map((label) => (
                  <th key={label} scope="col" className="px-2 py-3">
                    {label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {query.data?.map((row) => (
                <tr key={row.segment} className="border-b border-border">
                  <td className="px-2 py-3">
                    <Link
                      className="text-primary"
                      to={`/segments/${encodeURIComponent(row.segment)}`}
                    >
                      {row.segment}
                    </Link>
                  </td>
                  <td className="px-2 py-3">{row.redisCount ?? 'Unknown'}</td>
                  <td className="px-2 py-3">{row.segmentCount ?? 'Unknown'}</td>
                  <td className="px-2 py-3">
                    {observationStatus(row, now, preferences.freshnessMinutes)}
                  </td>
                  <td className="px-2 py-3">
                    {new Date(row.checkedAt).toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="mt-3 text-xs text-muted-foreground">
        Latest observations among at most 100 returned audit records. Missing
        and extra members are not measured here. Refresh reads history; it does
        not run verification or reconciliation.
      </p>
    </section>
  );
}

export function ReliabilityOverview({
  segmentNames,
}: {
  segmentNames: string[];
}) {
  const { user, groups, loading } = useAuth();
  const [selected, setSelected] = useState('');
  if (loading || !user || !groups.includes('Admin')) return null;
  const segmentName = segmentNames.includes(selected) ? selected : undefined;
  return (
    <div className="flex flex-col gap-3">
      <label className="text-sm">
        Reliability segment
        <select
          className="ml-3 rounded border border-border bg-background p-2"
          value={segmentName ?? ''}
          onChange={(event) => setSelected(event.target.value)}
        >
          <option value="">Select a segment</option>
          {segmentNames.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
      </label>
      <ReliabilityPanel segmentName={segmentName} />
      <p className="text-xs text-muted-foreground">
        Choose from segments loaded on this dashboard, or open another segment
        from the Segments menu.
      </p>
    </div>
  );
}
