import type { ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';

import { StatTile } from '@/components/ui/StatTile';
import { api } from '@/lib/api';
import { aggregateByRoutingProfile } from '@/lib/agentRoster';

export function AgentAvailabilityPanel({
  active,
  className,
}: {
  active: boolean;
  className?: string;
}): ReactNode {
  const query = useQuery({
    queryKey: ['agent-roster', 'all'],
    queryFn: () => api.brandedMonitor.getAgentRoster(),
    refetchInterval: active ? 20_000 : false,
  });

  const rows = aggregateByRoutingProfile(query.data?.agents ?? []);

  return (
    <div className={className}>
      <h3 className="text-sm font-semibold text-gray-700 mb-2">Agent availability</h3>
      {query.isError ? (
        <p className="text-xs text-red-500">Failed to load agent roster.</p>
      ) : query.isPending ? (
        <p className="text-xs text-gray-400">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="text-xs text-gray-400">No agents online.</p>
      ) : (
        <div className="space-y-2">
          {rows.map((row) => (
            <div key={row.routingProfileId} className="rounded-lg border border-gray-200 p-2.5">
              <div className="text-xs font-medium text-gray-700 truncate mb-1.5">{row.routingProfileName}</div>
              <div className="grid grid-cols-4 gap-1.5">
                <StatTile
                  label="Avail"
                  value={row.available}
                  valueClassName={row.available === 0 ? 'text-red-600' : undefined}
                />
                <StatTile label="Call" value={row.onCall} />
                <StatTile label="ACW" value={row.acw} />
                <StatTile label="Off" value={row.offline + row.unavailable} />
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
