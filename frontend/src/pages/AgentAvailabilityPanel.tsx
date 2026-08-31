import type { ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';

import { StatTile } from '@/components/ui/StatTile';
import { api, type AgentRosterEntry } from '@/lib/api';

export type RoutingProfileAvailability = {
  routingProfileId: string;
  routingProfileName: string;
  available: number;
  onCall: number;
  acw: number;
  offline: number;
  unavailable: number;
  total: number;
};

export function aggregateByRoutingProfile(agents: AgentRosterEntry[]): RoutingProfileAvailability[] {
  const byProfile = new Map<string, RoutingProfileAvailability>();
  for (const agent of agents) {
    let row = byProfile.get(agent.routingProfileId);
    if (!row) {
      row = {
        routingProfileId: agent.routingProfileId,
        routingProfileName: agent.routingProfileName,
        available: 0,
        onCall: 0,
        acw: 0,
        offline: 0,
        unavailable: 0,
        total: 0,
      };
      byProfile.set(agent.routingProfileId, row);
    }
    row.total += 1;
    switch (agent.effectiveStatus) {
      case 'Available':
        row.available += 1;
        break;
      case 'On Call':
        row.onCall += 1;
        break;
      case 'ACW':
        row.acw += 1;
        break;
      case 'Offline':
        row.offline += 1;
        break;
      default:
        row.unavailable += 1;
    }
  }
  return [...byProfile.values()].sort((a, b) => a.available - b.available);
}

export function AgentAvailabilityPanel({ className }: { className?: string }): ReactNode {
  const query = useQuery({
    queryKey: ['agent-roster', 'all'],
    queryFn: () => api.brandedMonitor.getAgentRoster(),
    refetchInterval: 20_000,
  });

  const rows = aggregateByRoutingProfile(query.data?.agents ?? []);

  return (
    <div className={className}>
      <h3 className="text-sm font-semibold text-gray-700 mb-2">Agent availability</h3>
      {query.isPending ? (
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
