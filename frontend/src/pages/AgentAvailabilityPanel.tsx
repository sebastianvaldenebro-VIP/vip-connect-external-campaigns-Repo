import type { ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';

import { AgentAvailabilityCard } from '@/components/AgentAvailabilityCard';
import { api } from '@/lib/api';
import { aggregateByRoutingProfile, classifyStaffing, minAvailableFor, STAFFING_RISK_ORDER } from '@/lib/agentRoster';
import { teamForProfile } from '@/lib/routingProfileTeams';

// Deliberately narrower than BRANDED_MONITOR_TEAMS: this compact panel is scoped to
// Patient Access only by design, and its click-through must always land on this same
// team — kept in one constant so the two can't drift apart.
const PANEL_TEAM = 'patient-success';

export function AgentAvailabilityPanel({
  active,
  className,
}: {
  active: boolean;
  className?: string;
}): ReactNode {
  const navigate = useNavigate();
  const query = useQuery({
    queryKey: ['agent-roster', 'all'],
    queryFn: () => api.brandedMonitor.getAgentRoster(),
    refetchInterval: active ? 20_000 : false,
  });

  const patientAccessAgents = (query.data?.agents ?? []).filter(
    (a) => teamForProfile(a.routingProfileName) === PANEL_TEAM,
  );
  const rows = aggregateByRoutingProfile(patientAccessAgents)
    .sort((a, b) =>
      STAFFING_RISK_ORDER[classifyStaffing(a.available, minAvailableFor(a.routingProfileName)).risk]
      - STAFFING_RISK_ORDER[classifyStaffing(b.available, minAvailableFor(b.routingProfileName)).risk],
    );
  const shownIds = new Set(rows.map((r) => r.routingProfileId));
  const zeroAgentCount = (query.data?.allRoutingProfiles ?? []).filter(
    (p) => teamForProfile(p.name) === PANEL_TEAM && !shownIds.has(p.id),
  ).length;

  return (
    <div className={className}>
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-700">Agent availability — Patient Access</h3>
        <button
          type="button"
          onClick={() => navigate(`/plans/branded-monitor?tab=agents&team=${PANEL_TEAM}`)}
          className="text-[10px] font-medium text-amber-600 hover:text-amber-700"
        >
          All PA profiles →
        </button>
      </div>
      {query.isError ? (
        <p className="text-xs text-red-500">Failed to load agent roster.</p>
      ) : query.isPending ? (
        <p className="text-xs text-gray-400">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="text-xs text-gray-400">No Patient Access agents online.</p>
      ) : (
        <div className="flex flex-wrap gap-2">
          {rows.map((row) => (
            <AgentAvailabilityCard
              key={row.routingProfileId}
              row={row}
              onClick={() => navigate(`/plans/branded-monitor?tab=agents&team=${PANEL_TEAM}&profile=${encodeURIComponent(row.routingProfileId)}`)}
            />
          ))}
        </div>
      )}
      {zeroAgentCount > 0 && (
        <div className="mt-2 text-center text-xs text-gray-400">+{zeroAgentCount} profile{zeroAgentCount > 1 ? 's' : ''}</div>
      )}
    </div>
  );
}
