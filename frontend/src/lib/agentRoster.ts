import type { AgentRosterEntry } from './api';
import { elapsedMinutes } from './utils';
import type { StatusTone } from '@/components/ui/status';

// ── Per-routing-profile aggregation ─────────────────────────────────────────

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

// ── Ungrouped availability tally (team-level cards) ──────────────────────────

export type AvailabilityCounts = {
  available: number;
  onCall: number;
  acw: number;
  offline: number;
  unavailable: number;
};

/**
 * Same per-agent status tally as aggregateByRoutingProfile, but with no
 * grouping key — the caller has already decided what these agents have in
 * common (e.g. "same team"). Used for team-level cards, where several
 * routing profiles collapse into one combined count.
 */
export function aggregateAvailability(agents: AgentRosterEntry[]): AvailabilityCounts {
  const counts: AvailabilityCounts = { available: 0, onCall: 0, acw: 0, offline: 0, unavailable: 0 };
  for (const agent of agents) {
    switch (agent.effectiveStatus) {
      case 'Available':
        counts.available += 1;
        break;
      case 'On Call':
        counts.onCall += 1;
        break;
      case 'ACW':
        counts.acw += 1;
        break;
      case 'Offline':
        counts.offline += 1;
        break;
      default:
        counts.unavailable += 1;
    }
  }
  return counts;
}

// ── Per-agent alerts ─────────────────────────────────────────────────────────

export type AlertThresholds = {
  idleAlertMin: number;
  breakAlertMin: number;
  longCallMin: number;
  acwAlertMin: number;
};

export const DEFAULT_ALERT_THRESHOLDS: AlertThresholds = {
  idleAlertMin: 10,
  breakAlertMin: 20,
  longCallMin: 12,
  acwAlertMin: 3,
};

export type AgentAlertKey = 'idle' | 'break' | 'longCall' | 'longAcw';

export type AgentAlert = {
  key: AgentAlertKey;
  label: string;
  why: string;
  sev: 'warn' | 'error';
};

/** Returns the single highest-priority alert for this agent's current status, or null. */
export function agentAlert(
  agent: AgentRosterEntry,
  thresholds: AlertThresholds = DEFAULT_ALERT_THRESHOLDS,
  nowMs: number = Date.now(),
): AgentAlert | null {
  const elapsed = elapsedMinutes(agent.statusStartTimestamp, nowMs);
  if (agent.effectiveStatus === 'Available' && elapsed > thresholds.idleAlertMin) {
    return { key: 'idle', label: 'Idle', why: 'No calls routed', sev: 'warn' };
  }
  if (agent.effectiveStatus === 'Unavailable' && !agent.isIntentionalAbsence && elapsed > thresholds.breakAlertMin) {
    return { key: 'break', label: 'Extended break', why: 'Extended break', sev: 'error' };
  }
  if (agent.effectiveStatus === 'On Call' && elapsed > thresholds.longCallMin) {
    return { key: 'longCall', label: 'Long call', why: 'Long call', sev: 'warn' };
  }
  if (agent.effectiveStatus === 'ACW' && elapsed > thresholds.acwAlertMin) {
    return { key: 'longAcw', label: 'Long wrap-up', why: 'Long wrap-up', sev: 'warn' };
  }
  return null;
}

// ── Staffing risk ────────────────────────────────────────────────────────────

export type StaffingRisk = 'off-hours' | 'no-coverage' | 'understaffed' | 'at-minimum' | 'healthy';

export type StaffingStatus = {
  risk: StaffingRisk;
  label: string;
  tone: StatusTone;
};

export const STAFFING_RISK_ORDER: Record<StaffingRisk, number> = {
  'no-coverage': 0,
  understaffed: 1,
  'at-minimum': 2,
  healthy: 3,
  'off-hours': 4,
};

/**
 * 7am-7pm Colombia time (COT, UTC-5, no DST) = 12:00-23:59 UTC. Matches
 * services/api-metrics/src/metrics_collector_handler.py's
 * _emit_business_hours_metric — the one other place this window is defined.
 * Kept in sync manually since the two live in different languages/services.
 */
export function isBusinessHours(nowMs: number = Date.now()): boolean {
  const utcHour = new Date(nowMs).getUTCHours();
  return utcHour >= 12 && utcHour <= 23;
}

/**
 * Outside business hours, low/zero availability is expected, not a risk —
 * without this gate, any min > 0 would report "No coverage" on every
 * profile every night and weekend.
 */
export function classifyStaffing(available: number, min: number, nowMs: number = Date.now()): StaffingStatus {
  if (!isBusinessHours(nowMs)) return { risk: 'off-hours', label: 'Off hours', tone: 'neutral' };
  if (available === 0) return { risk: 'no-coverage', label: 'No coverage', tone: 'danger' };
  if (available < min) return { risk: 'understaffed', label: 'Understaffed', tone: 'danger' };
  if (available === min) return { risk: 'at-minimum', label: 'At minimum', tone: 'warning' };
  return { risk: 'healthy', label: 'Healthy', tone: 'success' };
}

/**
 * Per-routing-profile minimum staffing level. Empty by default — no real
 * thresholds have been set for any profile yet. Every profile falls back to
 * DEFAULT_MIN_AVAILABLE until these are tuned against real staffing needs.
 */
export const MIN_AVAILABLE_BY_PROFILE: Record<string, number> = {};
export const DEFAULT_MIN_AVAILABLE = 1;

export function minAvailableFor(routingProfileName: string): number {
  return MIN_AVAILABLE_BY_PROFILE[routingProfileName] ?? DEFAULT_MIN_AVAILABLE;
}

/**
 * Per-team minimum staffing level, for cards that aggregate a whole team's
 * routing profiles into one number. Empty by default, same rationale as
 * MIN_AVAILABLE_BY_PROFILE — every team falls back to DEFAULT_MIN_AVAILABLE
 * until real thresholds are tuned.
 */
export const MIN_AVAILABLE_BY_TEAM: Record<string, number> = {};

export function minAvailableForTeam(team: string): number {
  return MIN_AVAILABLE_BY_TEAM[team] ?? DEFAULT_MIN_AVAILABLE;
}

// ── Status → tone ────────────────────────────────────────────────────────────

const AGENT_STATUS_TONE: Record<AgentRosterEntry['effectiveStatus'], StatusTone> = {
  Available: 'success',
  'On Call': 'info',
  ACW: 'acw',
  Unavailable: 'warning',
  Offline: 'neutral',
};

export function agentStatusTone(effectiveStatus: AgentRosterEntry['effectiveStatus']): StatusTone {
  return AGENT_STATUS_TONE[effectiveStatus];
}

// ── Aggregate alert count (TopBar badge) ─────────────────────────────────────

/**
 * Total active-alert count across the whole roster: every agent with a
 * per-agent alert (idle/break/longCall/longAcw), plus every routing profile
 * whose staffing risk isn't healthy/off-hours. Two different alert families,
 * summed into one number for a single global badge.
 */
export function totalActiveAlerts(agents: AgentRosterEntry[], nowMs: number = Date.now()): number {
  const agentAlertCount = agents.filter((a) => agentAlert(a, DEFAULT_ALERT_THRESHOLDS, nowMs) !== null).length;
  const profileRiskCount = aggregateByRoutingProfile(agents).filter((row) => {
    const risk = classifyStaffing(row.available, minAvailableFor(row.routingProfileName), nowMs).risk;
    return risk !== 'healthy' && risk !== 'off-hours';
  }).length;
  return agentAlertCount + profileRiskCount;
}
