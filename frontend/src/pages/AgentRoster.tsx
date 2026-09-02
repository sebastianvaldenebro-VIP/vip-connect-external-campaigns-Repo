import { type ReactNode, useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import { Avatar } from '@/components/ui/Avatar';
import { StatTile } from '@/components/ui/StatTile';
import { StatusChip } from '@/components/ui/StatusChip';
import { api, type AgentRosterEntry, type RoutingProfileSummary } from '@/lib/api';
import {
  aggregateByRoutingProfile,
  agentAlert,
  agentStatusTone,
  classifyStaffing,
  DEFAULT_ALERT_THRESHOLDS,
  minAvailableFor,
  STAFFING_RISK_ORDER,
  type AgentAlertKey,
  type RoutingProfileAvailability,
} from '@/lib/agentRoster';
import { elapsedMinutes, elapsedSeconds, formatElapsed, formatRuntime } from '@/lib/utils';
import { BRANDED_MONITOR_TEAMS, TEAM_LABELS, teamForProfile } from '@/lib/routingProfileTeams';

type AlertFilter = 'all' | 'any' | AgentAlertKey;

export function sortAgentsForDisplay(agents: AgentRosterEntry[], nowMs: number = Date.now()): AgentRosterEntry[] {
  return [...agents].sort((a, b) => {
    const aFlagged = agentAlert(a, DEFAULT_ALERT_THRESHOLDS, nowMs) !== null;
    const bFlagged = agentAlert(b, DEFAULT_ALERT_THRESHOLDS, nowMs) !== null;
    if (aFlagged !== bFlagged) return aFlagged ? -1 : 1;
    return elapsedMinutes(b.statusStartTimestamp, nowMs) - elapsedMinutes(a.statusStartTimestamp, nowMs);
  });
}

export type AgentRosterGroup = {
  routingProfileId: string;
  routingProfileName: string;
  agents: AgentRosterEntry[];
  flaggedCount: number;
  staffing: ReturnType<typeof classifyStaffing>;
};

export function groupAgentsByProfile(agents: AgentRosterEntry[], nowMs: number = Date.now()): AgentRosterGroup[] {
  const byProfile = new Map<string, AgentRosterEntry[]>();
  for (const agent of agents) {
    const list = byProfile.get(agent.routingProfileId) ?? [];
    list.push(agent);
    byProfile.set(agent.routingProfileId, list);
  }
  const groups: AgentRosterGroup[] = [...byProfile.entries()].map(([routingProfileId, list]) => {
    const available = list.filter((a) => a.effectiveStatus === 'Available').length;
    const flaggedCount = list.filter((a) => agentAlert(a, DEFAULT_ALERT_THRESHOLDS, nowMs) !== null).length;
    return {
      routingProfileId,
      routingProfileName: list[0]!.routingProfileName,
      agents: sortAgentsForDisplay(list, nowMs),
      flaggedCount,
      staffing: classifyStaffing(available, minAvailableFor(list[0]!.routingProfileName), nowMs),
    };
  });
  return groups.sort((a, b) => {
    if ((a.flaggedCount > 0) !== (b.flaggedCount > 0)) return a.flaggedCount > 0 ? -1 : 1;
    const riskDiff = STAFFING_RISK_ORDER[a.staffing.risk] - STAFFING_RISK_ORDER[b.staffing.risk];
    if (riskDiff !== 0) return riskDiff;
    return b.agents.length - a.agents.length;
  });
}

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

export function AgentRoster({
  initialTeamFilter,
  initialProfileFilter,
}: {
  initialTeamFilter?: string | null;
  initialProfileFilter?: string | null;
} = {}): ReactNode {
  const nowMs = useNowTick();

  const query = useQuery({
    queryKey: ['agent-roster', 'all'],
    queryFn: () => api.brandedMonitor.getAgentRoster(),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  const [search, setSearch] = useState('');
  const [statusFilters, setStatusFilters] = useState<Set<AgentRosterEntry['effectiveStatus']>>(new Set());
  const [teamFilter, setTeamFilter] = useState<string | null>(initialTeamFilter ?? null);
  const [rpFilters, setRpFilters] = useState<Set<string>>(
    () => (initialProfileFilter ? new Set([initialProfileFilter]) : new Set()),
  );
  const [alertFilter, setAlertFilter] = useState<AlertFilter>('all');
  const [groupByProfile, setGroupByProfile] = useState(true);

  const allAgents: AgentRosterEntry[] = query.data?.agents ?? [];
  const agents = allAgents.filter(
    (a) => (BRANDED_MONITOR_TEAMS as readonly string[]).includes(teamForProfile(a.routingProfileName) ?? ''),
  );
  const lastUpdated = query.data?.lastUpdated;
  const routingProfiles: RoutingProfileSummary[] = query.data?.routingProfiles ?? [];

  function selectStatus(status: AgentRosterEntry['effectiveStatus'] | null) {
    setStatusFilters(status ? new Set([status]) : new Set());
  }

  function selectTeam(team: string | null) {
    setTeamFilter(team);
    setRpFilters(new Set());
  }

  function selectProfile(id: string | null) {
    setRpFilters(id ? new Set([id]) : new Set());
  }

  function clearFilters() {
    setSearch('');
    setStatusFilters(new Set());
    setTeamFilter(null);
    setRpFilters(new Set());
    setAlertFilter('all');
  }

  const searchLower = search.trim().toLowerCase();
  const filteredAgents = agents.filter((a) => {
    if (statusFilters.size > 0 && !statusFilters.has(a.effectiveStatus)) return false;
    const team = teamForProfile(a.routingProfileName);
    if (teamFilter !== null && team !== teamFilter) return false;
    if (rpFilters.size > 0 && !rpFilters.has(a.routingProfileId)) return false;
    if (alertFilter !== 'all') {
      const alert = agentAlert(a, DEFAULT_ALERT_THRESHOLDS, nowMs);
      if (alertFilter === 'any' && alert === null) return false;
      if (alertFilter !== 'any' && alert?.key !== alertFilter) return false;
    }
    if (searchLower) {
      const teamLabel = team ? TEAM_LABELS[team] ?? '' : '';
      const haystack = `${a.agentName} ${a.routingProfileName} ${teamLabel}`.toLowerCase();
      if (!haystack.includes(searchLower)) return false;
    }
    return true;
  });

  const hasActiveFilters = search !== '' || statusFilters.size > 0 || teamFilter !== null || rpFilters.size > 0 || alertFilter !== 'all';
  const flaggedInScope = agents.filter((a) => agentAlert(a, DEFAULT_ALERT_THRESHOLDS, nowMs) !== null);

  const activeTeams = (BRANDED_MONITOR_TEAMS as readonly string[]).filter((team) =>
    agents.some((a) => teamForProfile(a.routingProfileName) === team),
  );
  const visibleRps = teamFilter === null
    ? routingProfiles.filter((rp) => {
        const t = teamForProfile(rp.name);
        return t !== null && (BRANDED_MONITOR_TEAMS as readonly string[]).includes(t);
      })
    : routingProfiles.filter((rp) => teamForProfile(rp.name) === teamFilter);
  const teamCounts = activeTeams.reduce<Record<string, number>>((acc, team) => {
    acc[team] = agents.filter((a) => teamForProfile(a.routingProfileName) === team).length;
    return acc;
  }, {});
  const rpCounts = visibleRps.reduce<Record<string, number>>((acc, rp) => {
    acc[rp.id] = agents.filter((a) => a.routingProfileId === rp.id).length;
    return acc;
  }, {});

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
          <CapacityTable agents={agents} nowMs={nowMs} />
          <ControlBar
            search={search}
            onSearch={setSearch}
            statusFilters={statusFilters}
            onSelectStatus={selectStatus}
            statusCounts={agents.reduce<Record<string, number>>((acc, a) => {
              acc[a.effectiveStatus] = (acc[a.effectiveStatus] ?? 0) + 1;
              return acc;
            }, {})}
            alertFilter={alertFilter}
            onAlertFilter={setAlertFilter}
            flaggedCount={flaggedInScope.length}
            groupByProfile={groupByProfile}
            onToggleGroup={() => setGroupByProfile((v) => !v)}
            filteredCount={filteredAgents.length}
            totalCount={agents.length}
            hasActiveFilters={hasActiveFilters}
            onClearFilters={clearFilters}
            activeTeams={activeTeams}
            teamFilter={teamFilter}
            onSelectTeam={selectTeam}
            teamCounts={teamCounts}
            visibleRps={visibleRps}
            rpFilters={rpFilters}
            onSelectProfile={selectProfile}
            rpCounts={rpCounts}
          />
          <NeedsAttentionPanel agents={flaggedInScope} nowMs={nowMs} onAlertKeyClick={setAlertFilter} />
          <AgentList agents={filteredAgents} groupByProfile={groupByProfile} nowMs={nowMs} isLoading={query.isLoading} />
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

function CapacityTable({ agents, nowMs }: { agents: AgentRosterEntry[]; nowMs: number }): ReactNode {
  const rows: RoutingProfileAvailability[] = aggregateByRoutingProfile(agents);
  const withStaffing = rows
    .map((row) => ({ row, staffing: classifyStaffing(row.available, minAvailableFor(row.routingProfileName), nowMs) }))
    .sort((a, b) => STAFFING_RISK_ORDER[a.staffing.risk] - STAFFING_RISK_ORDER[b.staffing.risk]);

  if (withStaffing.length === 0) {
    return null;
  }

  return (
    <div className="rounded-xl border border-gray-200 bg-white overflow-hidden">
      <div className="px-4 py-2.5 border-b border-gray-100 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-800">Teams &amp; routing profiles</h2>
        <div className="flex items-center gap-3 text-[11px] text-gray-500">
          <span className="inline-flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full bg-status-success-bar" />Available</span>
          <span className="inline-flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full bg-status-info-bar" />On call</span>
          <span className="inline-flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full bg-status-acw-bar" />ACW</span>
          <span className="inline-flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full bg-status-warning-bar" />Away</span>
          <span className="inline-flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full bg-status-neutral-bar" />Offline</span>
        </div>
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

const EFFECTIVE_STATUSES: AgentRosterEntry['effectiveStatus'][] = ['Available', 'On Call', 'ACW', 'Unavailable', 'Offline'];
const STATUS_LABELS: Record<AgentRosterEntry['effectiveStatus'], string> = {
  Available: 'Available',
  'On Call': 'On Call',
  ACW: 'ACW',
  Unavailable: 'Away',
  Offline: 'Offline',
};

function FilterBtn({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }): ReactNode {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`px-3 py-1 rounded-lg text-xs font-medium transition-colors ${
        active ? 'bg-amber-100 text-amber-800' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
      }`}
    >
      {children}
    </button>
  );
}

function ControlBar({
  search, onSearch,
  statusFilters, onSelectStatus, statusCounts,
  alertFilter, onAlertFilter, flaggedCount,
  groupByProfile, onToggleGroup,
  filteredCount, totalCount, hasActiveFilters, onClearFilters,
  activeTeams, teamFilter, onSelectTeam, teamCounts,
  visibleRps, rpFilters, onSelectProfile, rpCounts,
}: {
  search: string; onSearch: (v: string) => void;
  statusFilters: Set<AgentRosterEntry['effectiveStatus']>; onSelectStatus: (s: AgentRosterEntry['effectiveStatus'] | null) => void;
  statusCounts: Record<string, number>;
  alertFilter: AlertFilter; onAlertFilter: (f: AlertFilter) => void; flaggedCount: number;
  groupByProfile: boolean; onToggleGroup: () => void;
  filteredCount: number; totalCount: number; hasActiveFilters: boolean; onClearFilters: () => void;
  activeTeams: string[]; teamFilter: string | null; onSelectTeam: (team: string | null) => void; teamCounts: Record<string, number>;
  visibleRps: RoutingProfileSummary[]; rpFilters: Set<string>; onSelectProfile: (id: string | null) => void; rpCounts: Record<string, number>;
}): ReactNode {
  return (
    <div className="bg-white rounded-xl border border-gray-200 p-4 space-y-3">
      <div className="flex items-center gap-3 flex-wrap">
        <input
          type="text"
          value={search}
          onChange={(e) => onSearch(e.target.value)}
          placeholder="Search name, profile, team..."
          className="flex-1 min-w-[220px] rounded-lg border border-gray-200 px-3 py-1.5 text-sm placeholder:text-gray-400"
        />
        <FilterBtn active={groupByProfile} onClick={onToggleGroup}>Group by profile</FilterBtn>
      </div>
      <div className="flex items-center gap-3 flex-wrap">
        <label className="flex items-center gap-1.5 text-xs text-gray-600">
          Status
          <select
            value={statusFilters.size === 1 ? [...statusFilters][0] : ''}
            onChange={(e) => onSelectStatus(e.target.value ? (e.target.value as AgentRosterEntry['effectiveStatus']) : null)}
            className="rounded-lg border border-gray-200 px-2 py-1 text-xs"
          >
            <option value="">All ({totalCount})</option>
            {EFFECTIVE_STATUSES.map((s) => (
              <option key={s} value={s}>{STATUS_LABELS[s]} ({statusCounts[s] ?? 0})</option>
            ))}
          </select>
        </label>
        {activeTeams.length > 0 && (
          <label className="flex items-center gap-1.5 text-xs text-gray-600">
            Team
            <select
              value={teamFilter ?? ''}
              onChange={(e) => onSelectTeam(e.target.value || null)}
              className="rounded-lg border border-gray-200 px-2 py-1 text-xs"
            >
              <option value="">All</option>
              {activeTeams.map((team) => (
                <option key={team} value={team}>{TEAM_LABELS[team] ?? team} ({teamCounts[team] ?? 0})</option>
              ))}
            </select>
          </label>
        )}
        {visibleRps.length > 0 && (
          <label className="flex items-center gap-1.5 text-xs text-gray-600">
            Profile
            <select
              value={rpFilters.size === 1 ? [...rpFilters][0] : ''}
              onChange={(e) => onSelectProfile(e.target.value || null)}
              className="rounded-lg border border-gray-200 px-2 py-1 text-xs max-w-[220px]"
            >
              <option value="">All profiles</option>
              {visibleRps.map((rp) => (
                <option key={rp.id} value={rp.id}>{rp.name} ({rpCounts[rp.id] ?? 0})</option>
              ))}
            </select>
          </label>
        )}
        <label className="flex items-center gap-1.5 text-xs text-gray-600">
          Alerts
          <select
            value={alertFilter}
            onChange={(e) => onAlertFilter(e.target.value as AlertFilter)}
            className="rounded-lg border border-gray-200 px-2 py-1 text-xs"
          >
            <option value="all">All agents</option>
            <option value="any">Needs attention ({flaggedCount})</option>
          </select>
        </label>
      </div>
      {hasActiveFilters && (
        <div className="flex items-center gap-3 pt-1 border-t border-gray-100">
          <span className="text-xs text-gray-500">Showing {filteredCount} of {totalCount} agents</span>
          <button type="button" onClick={onClearFilters} className="text-xs text-amber-600 hover:text-amber-700 font-medium">
            Clear all
          </button>
        </div>
      )}
    </div>
  );
}

function NeedsAttentionPanel({
  agents, nowMs, onAlertKeyClick,
}: {
  agents: AgentRosterEntry[]; nowMs: number; onAlertKeyClick: (key: AlertFilter) => void;
}): ReactNode {
  if (agents.length === 0) return null;

  const withAlerts = agents
    .map((a) => ({ agent: a, alert: agentAlert(a, DEFAULT_ALERT_THRESHOLDS, nowMs) }))
    .filter((x): x is { agent: AgentRosterEntry; alert: NonNullable<ReturnType<typeof agentAlert>> } => x.alert !== null)
    .sort((a, b) => {
      if (a.alert.sev !== b.alert.sev) return a.alert.sev === 'error' ? -1 : 1;
      return elapsedMinutes(b.agent.statusStartTimestamp, nowMs) - elapsedMinutes(a.agent.statusStartTimestamp, nowMs);
    });

  return (
    <div className="rounded-xl border border-amber-200 bg-white overflow-hidden">
      <div className="px-4 py-2.5 bg-amber-50 border-b border-amber-100">
        <h2 className="text-sm font-semibold text-amber-800">Needs attention</h2>
      </div>
      <div className="p-3 grid gap-2" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(310px, 1fr))' }}>
        {withAlerts.map(({ agent, alert }) => (
          <div key={agent.agentId} className="flex items-center gap-2.5 rounded-lg border border-gray-200 p-2.5">
            <Avatar name={agent.agentName || agent.agentId} tone={agentStatusTone(agent.effectiveStatus)} />
            <div className="flex-1 min-w-0">
              <div className="text-xs font-medium text-gray-800 truncate">{agent.agentName || agent.agentId}</div>
              <div className="text-[11px] text-gray-400 truncate">{agent.routingProfileName}</div>
            </div>
            <button
              type="button"
              onClick={() => onAlertKeyClick(alert.key)}
              className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium shrink-0 ${
                alert.sev === 'error' ? 'bg-red-100 text-red-700' : 'bg-amber-100 text-amber-700'
              }`}
            >
              {alert.label}
            </button>
            <span className="text-[11px] text-gray-400 tabular-nums shrink-0">
              {formatElapsed(elapsedMinutes(agent.statusStartTimestamp, nowMs))}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function AgentRow({ agent, nowMs }: { agent: AgentRosterEntry; nowMs: number }): ReactNode {
  const alert = agentAlert(agent, DEFAULT_ALERT_THRESHOLDS, nowMs);
  const rowTint = alert?.sev === 'error' ? 'bg-red-50/50' : alert?.sev === 'warn' ? 'bg-amber-50/50' : '';
  return (
    <div className={`flex items-center gap-3 px-4 py-2.5 ${rowTint}`}>
      <Avatar name={agent.agentName || agent.agentId} tone={agentStatusTone(agent.effectiveStatus)} />
      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium text-gray-800 truncate">{agent.agentName || agent.agentId}</div>
        <div className="text-[11px] text-gray-400 truncate">
          {alert ? `${alert.why} · ${agent.routingProfileName}` : agent.routingProfileName}
        </div>
      </div>
      <StatusChip tone={agentStatusTone(agent.effectiveStatus)} label={agent.effectiveStatus === 'Unavailable' ? 'Away' : agent.effectiveStatus} />
      <span className="w-14 text-right font-mono text-sm tabular-nums text-gray-600 shrink-0">
        {formatRuntime(elapsedSeconds(agent.statusStartTimestamp, nowMs))}
      </span>
    </div>
  );
}

function AgentList({
  agents, groupByProfile, nowMs, isLoading,
}: {
  agents: AgentRosterEntry[]; groupByProfile: boolean; nowMs: number; isLoading: boolean;
}): ReactNode {
  return (
    <div className="rounded-xl border border-gray-200 bg-white overflow-hidden">
      <div className="px-4 py-2.5 border-b border-gray-100 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-800">
          Agents <span className="text-gray-400 font-normal">({agents.length})</span>
        </h2>
        <span className="text-[11px] text-gray-400">Time in current status · flagged agents first</span>
      </div>

      {isLoading && <div className="px-4 py-10 text-center text-sm text-gray-400">Loading agents…</div>}

      {!isLoading && agents.length === 0 && (
        <div className="px-4 py-10 text-center">
          <div className="text-sm text-gray-500">No agents match</div>
          <div className="text-xs text-gray-400 mt-1">Try clearing a filter or the search.</div>
        </div>
      )}

      {!isLoading && agents.length > 0 && !groupByProfile && (
        <div className="divide-y divide-gray-100">
          {sortAgentsForDisplay(agents, nowMs).map((a) => <AgentRow key={a.agentId} agent={a} nowMs={nowMs} />)}
        </div>
      )}

      {!isLoading && agents.length > 0 && groupByProfile && (
        <div className="divide-y divide-gray-100">
          {groupAgentsByProfile(agents, nowMs).map((group) => (
            <div key={group.routingProfileId}>
              <div className="px-4 py-2 bg-gray-50 flex items-center gap-2 text-xs">
                <span className="font-medium text-gray-700">{group.routingProfileName}</span>
                <span className="text-gray-400">
                  {group.agents.length} · {group.agents.filter((a) => a.effectiveStatus === 'Available').length} available
                </span>
                {group.flaggedCount > 0 && (
                  <span className="text-amber-600">{group.flaggedCount} flagged</span>
                )}
              </div>
              <div className="divide-y divide-gray-100">
                {group.agents.map((a) => <AgentRow key={a.agentId} agent={a} nowMs={nowMs} />)}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
