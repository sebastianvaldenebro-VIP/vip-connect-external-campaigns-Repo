import { type ReactNode, useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import { StatTile } from '@/components/ui/StatTile';
import { StatusChip } from '@/components/ui/StatusChip';
import { api, type AgentRosterEntry } from '@/lib/api';
import {
  aggregateByRoutingProfile,
  agentAlert,
  classifyStaffing,
  DEFAULT_ALERT_THRESHOLDS,
  minAvailableFor,
  STAFFING_RISK_ORDER,
  type RoutingProfileAvailability,
} from '@/lib/agentRoster';
import { elapsedMinutes } from '@/lib/utils';
import { BRANDED_MONITOR_TEAMS, TEAM_LABELS, teamForProfile } from '@/lib/routingProfileTeams';

/** Ticks every second so elapsed-time displays (M:SS timers, alert thresholds)
 * update live without waiting for the next data refetch. */
function useNowTick(): number {
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  return nowMs;
}

export function AgentRoster(): ReactNode {
  const nowMs = useNowTick();

  const query = useQuery({
    queryKey: ['agent-roster', 'all'],
    queryFn: () => api.brandedMonitor.getAgentRoster(),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  const allAgents: AgentRosterEntry[] = query.data?.agents ?? [];
  const agents = allAgents.filter(
    (a) => (BRANDED_MONITOR_TEAMS as readonly string[]).includes(teamForProfile(a.routingProfileName) ?? ''),
  );
  const lastUpdated = query.data?.lastUpdated;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold text-gray-900">Agent roster</h1>
        <p className="text-xs text-gray-500 mt-0.5">
          Live status by team and routing profile — routing profile determines which calls an agent answers.
        </p>
      </div>

      {query.isError && (
        <p className="text-sm text-red-500">Failed to load agent roster.</p>
      )}

      {!query.isError && (
        <>
          <WorkforceSummary agents={agents} nowMs={nowMs} />
          <CapacityTable agents={agents} />
        </>
      )}

      {lastUpdated && !query.isLoading && (
        <div className="text-[11px] text-gray-400">
          updated {elapsedMinutes(lastUpdated, nowMs) === 0 ? 'just now' : `${elapsedMinutes(lastUpdated, nowMs)}m ago`}
        </div>
      )}
    </div>
  );
}

function WorkforceSummary({ agents, nowMs }: { agents: AgentRosterEntry[]; nowMs: number }): ReactNode {
  const total       = agents.length;
  const offline     = agents.filter((a) => a.effectiveStatus === 'Offline').length;
  const unavailable = agents.filter((a) => a.effectiveStatus === 'Unavailable').length;
  const online      = total - offline;
  const available   = agents.filter((a) => a.effectiveStatus === 'Available').length;
  const onCall      = agents.filter((a) => a.effectiveStatus === 'On Call').length;
  const acw         = agents.filter((a) => a.effectiveStatus === 'ACW').length;
  const flagged     = agents.filter((a) => agentAlert(a, DEFAULT_ALERT_THRESHOLDS, nowMs) !== null).length;

  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
      <StatTile label="Agents online" value={online} valueClassName="text-gray-900" />
      <StatTile
        label="Available"
        value={available}
        valueClassName={available === 0 ? 'text-status-danger-fg' : 'text-status-success-fg'}
      />
      <StatTile label="On call" value={onCall} valueClassName="text-status-info-fg" />
      <StatTile label="After-call work" value={acw} valueClassName="text-status-acw-fg" />
      <StatTile label="Away / offline" value={offline + unavailable} valueClassName="text-gray-900" />
      <StatTile
        label="Needs attention"
        value={flagged}
        valueClassName={flagged > 0 ? 'text-status-warning-fg' : 'text-status-success-fg'}
      />
    </div>
  );
}

function CapacityTable({ agents }: { agents: AgentRosterEntry[] }): ReactNode {
  const rows: RoutingProfileAvailability[] = aggregateByRoutingProfile(agents);
  const withStaffing = rows
    .map((row) => ({ row, staffing: classifyStaffing(row.available, minAvailableFor(row.routingProfileName)) }))
    .sort((a, b) => STAFFING_RISK_ORDER[a.staffing.risk] - STAFFING_RISK_ORDER[b.staffing.risk]);

  if (withStaffing.length === 0) {
    return null;
  }

  return (
    <div className="rounded-xl border border-gray-200 bg-white overflow-hidden">
      <div className="px-4 py-2.5 border-b border-gray-100">
        <h2 className="text-sm font-semibold text-gray-800">Teams &amp; routing profiles</h2>
      </div>
      <div className="divide-y divide-gray-100">
        {withStaffing.map(({ row, staffing }) => {
          const min = minAvailableFor(row.routingProfileName);
          const team = teamForProfile(row.routingProfileName);
          return (
            <div
              key={row.routingProfileId}
              className={`px-4 py-3 flex items-center gap-4 ${
                staffing.risk === 'no-coverage' || staffing.risk === 'understaffed' ? 'bg-red-50/40' : ''
              }`}
            >
              <div className="min-w-[200px] flex-1">
                <div className="text-sm font-medium text-gray-800">{row.routingProfileName}</div>
                {team && <div className="text-[11px] text-gray-400">{TEAM_LABELS[team] ?? team}</div>}
              </div>
              <div className="w-14 text-center text-sm tabular-nums text-gray-600">{row.total}</div>
              <div className="flex-1 min-w-[140px]">
                <StaffingBar row={row} />
                <div className="text-[10px] text-gray-400 mt-0.5">min {min} available</div>
              </div>
              <div className="w-12 text-center text-sm tabular-nums text-status-success-fg">{row.available}</div>
              <div className="w-12 text-center text-sm tabular-nums text-status-info-fg">{row.onCall}</div>
              <div className="w-12 text-center text-sm tabular-nums text-status-acw-fg">{row.acw}</div>
              <div className="w-12 text-center text-sm tabular-nums text-gray-500">{row.offline + row.unavailable}</div>
              <div className="w-[116px] flex justify-end">
                <StatusChip tone={staffing.tone} label={staffing.label} />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** 5-segment stacked bar (available/onCall/acw/unavailable/offline), proportional to `row.total`. */
function StaffingBar({ row }: { row: RoutingProfileAvailability }): ReactNode {
  const segments: { count: number; className: string }[] = [
    { count: row.available,   className: 'bg-status-success-bar' },
    { count: row.onCall,      className: 'bg-status-info-bar' },
    { count: row.acw,         className: 'bg-status-acw-bar' },
    { count: row.unavailable, className: 'bg-status-warning-bar' },
    { count: row.offline,     className: 'bg-status-neutral-bar' },
  ];
  return (
    <div className="h-2 w-full rounded-full bg-gray-100 overflow-hidden flex">
      {segments.map((seg, i) =>
        seg.count > 0 ? (
          <div
            key={i}
            className={seg.className}
            style={{ width: `${row.total > 0 ? (seg.count / row.total) * 100 : 0}%` }}
          />
        ) : null,
      )}
    </div>
  );
}
