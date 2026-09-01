import { describe, expect, it } from 'vitest';

import type { AgentRosterEntry } from './api';
import {
  aggregateByRoutingProfile,
  agentAlert,
  agentStatusTone,
  classifyStaffing,
  DEFAULT_ALERT_THRESHOLDS,
  isBusinessHours,
  minAvailableFor,
} from './agentRoster';

function agent(overrides: Partial<AgentRosterEntry>): AgentRosterEntry {
  return {
    agentId: 'a1',
    agentName: 'Test Agent',
    status: 'Available',
    statusType: 'ROUTABLE',
    effectiveStatus: 'Available',
    isIntentionalAbsence: false,
    activeContactState: '',
    statusStartTimestamp: new Date().toISOString(),
    routingProfileId: 'rp1',
    routingProfileName: 'Outbound Dialer Agent',
    contactsCount: 0,
    ...overrides,
  };
}

const T0 = new Date('2026-09-01T12:00:00.000Z').getTime();
const BUSINESS_HOURS_MS = new Date('2026-09-01T15:00:00.000Z').getTime(); // 10am COT — within 12:00-23:59 UTC
const OFF_HOURS_MS = new Date('2026-09-01T05:00:00.000Z').getTime(); // midnight COT — outside 12:00-23:59 UTC

describe('aggregateByRoutingProfile', () => {
  it('groups agents by routing profile and counts each effectiveStatus bucket', () => {
    const agents = [
      agent({ agentId: 'a1', routingProfileId: 'rp1', routingProfileName: 'Outbound', effectiveStatus: 'Available' }),
      agent({ agentId: 'a2', routingProfileId: 'rp1', routingProfileName: 'Outbound', effectiveStatus: 'On Call' }),
      agent({ agentId: 'a3', routingProfileId: 'rp2', routingProfileName: 'New Lead', effectiveStatus: 'Available' }),
    ];
    const result = aggregateByRoutingProfile(agents);
    expect(result).toHaveLength(2);
    const outbound = result.find((r) => r.routingProfileId === 'rp1')!;
    expect(outbound).toMatchObject({
      routingProfileName: 'Outbound', available: 1, onCall: 1, acw: 0, offline: 0, unavailable: 0, total: 2,
    });
  });

  it('counts every effectiveStatus value distinctly', () => {
    const agents = [
      agent({ agentId: 'a1', effectiveStatus: 'Available' }),
      agent({ agentId: 'a2', effectiveStatus: 'On Call' }),
      agent({ agentId: 'a3', effectiveStatus: 'ACW' }),
      agent({ agentId: 'a4', effectiveStatus: 'Offline' }),
      agent({ agentId: 'a5', effectiveStatus: 'Unavailable' }),
    ];
    const [result] = aggregateByRoutingProfile(agents);
    expect(result).toMatchObject({ available: 1, onCall: 1, acw: 1, offline: 1, unavailable: 1, total: 5 });
  });

  it('sorts profiles by available count ascending (most understaffed first)', () => {
    const agents = [
      agent({ agentId: 'a1', routingProfileId: 'rp1', routingProfileName: 'Well staffed', effectiveStatus: 'Available' }),
      agent({ agentId: 'a2', routingProfileId: 'rp1', routingProfileName: 'Well staffed', effectiveStatus: 'Available' }),
      agent({ agentId: 'a3', routingProfileId: 'rp2', routingProfileName: 'Understaffed', effectiveStatus: 'On Call' }),
    ];
    const result = aggregateByRoutingProfile(agents);
    expect(result[0]!.routingProfileName).toBe('Understaffed');
  });

  it('returns an empty array for no agents', () => {
    expect(aggregateByRoutingProfile([])).toEqual([]);
  });
});

describe('agentAlert', () => {
  it('flags an Available agent past the idle threshold as idle/warn', () => {
    const a = agent({ effectiveStatus: 'Available', statusStartTimestamp: new Date(T0 - 11 * 60_000).toISOString() });
    expect(agentAlert(a, DEFAULT_ALERT_THRESHOLDS, T0)).toEqual({ key: 'idle', label: 'Idle', why: 'No calls routed', sev: 'warn' });
  });

  it('does not flag an Available agent under the idle threshold', () => {
    const a = agent({ effectiveStatus: 'Available', statusStartTimestamp: new Date(T0 - 5 * 60_000).toISOString() });
    expect(agentAlert(a, DEFAULT_ALERT_THRESHOLDS, T0)).toBeNull();
  });

  it('flags an Unavailable (non-intentional) agent past the break threshold as break/error', () => {
    const a = agent({
      effectiveStatus: 'Unavailable', isIntentionalAbsence: false,
      statusStartTimestamp: new Date(T0 - 21 * 60_000).toISOString(),
    });
    expect(agentAlert(a, DEFAULT_ALERT_THRESHOLDS, T0)).toMatchObject({ key: 'break', sev: 'error' });
  });

  it('never flags an intentional absence, no matter how long', () => {
    const a = agent({
      effectiveStatus: 'Unavailable', isIntentionalAbsence: true,
      statusStartTimestamp: new Date(T0 - 999 * 60_000).toISOString(),
    });
    expect(agentAlert(a, DEFAULT_ALERT_THRESHOLDS, T0)).toBeNull();
  });

  it('flags an On Call agent past the long-call threshold as longCall/warn', () => {
    const a = agent({ effectiveStatus: 'On Call', statusStartTimestamp: new Date(T0 - 13 * 60_000).toISOString() });
    expect(agentAlert(a, DEFAULT_ALERT_THRESHOLDS, T0)).toMatchObject({ key: 'longCall', sev: 'warn' });
  });

  it('flags an ACW agent past the long-wrap-up threshold as longAcw/warn', () => {
    const a = agent({ effectiveStatus: 'ACW', statusStartTimestamp: new Date(T0 - 4 * 60_000).toISOString() });
    expect(agentAlert(a, DEFAULT_ALERT_THRESHOLDS, T0)).toMatchObject({ key: 'longAcw', sev: 'warn' });
  });

  it('never flags Offline agents', () => {
    const a = agent({ effectiveStatus: 'Offline', statusStartTimestamp: new Date(T0 - 999 * 60_000).toISOString() });
    expect(agentAlert(a, DEFAULT_ALERT_THRESHOLDS, T0)).toBeNull();
  });

  it('respects custom thresholds', () => {
    const a = agent({ effectiveStatus: 'Available', statusStartTimestamp: new Date(T0 - 3 * 60_000).toISOString() });
    expect(agentAlert(a, { ...DEFAULT_ALERT_THRESHOLDS, idleAlertMin: 2 }, T0)).toMatchObject({ key: 'idle' });
  });
});

describe('classifyStaffing', () => {
  it('is "no-coverage"/danger when 0 are available, regardless of min', () => {
    expect(classifyStaffing(0, 3, BUSINESS_HOURS_MS)).toEqual({ risk: 'no-coverage', label: 'No coverage', tone: 'danger' });
  });

  it('is "understaffed"/danger when available is below min', () => {
    expect(classifyStaffing(1, 3, BUSINESS_HOURS_MS)).toEqual({ risk: 'understaffed', label: 'Understaffed', tone: 'danger' });
  });

  it('is "at-minimum"/warning when available equals min exactly', () => {
    expect(classifyStaffing(2, 2, BUSINESS_HOURS_MS)).toEqual({ risk: 'at-minimum', label: 'At minimum', tone: 'warning' });
  });

  it('is "healthy"/success when available exceeds min', () => {
    expect(classifyStaffing(5, 2, BUSINESS_HOURS_MS)).toEqual({ risk: 'healthy', label: 'Healthy', tone: 'success' });
  });
});

describe('isBusinessHours', () => {
  it('is true within 7am-7pm COT (12:00-23:59 UTC)', () => {
    expect(isBusinessHours(new Date('2026-09-01T12:00:00.000Z').getTime())).toBe(true);
    expect(isBusinessHours(new Date('2026-09-01T23:59:00.000Z').getTime())).toBe(true);
  });

  it('is false outside 7am-7pm COT', () => {
    expect(isBusinessHours(new Date('2026-09-01T11:59:00.000Z').getTime())).toBe(false);
    expect(isBusinessHours(new Date('2026-09-01T00:00:00.000Z').getTime())).toBe(false);
  });
});

describe('classifyStaffing — off hours', () => {
  it('returns off-hours/neutral outside business hours, regardless of available/min', () => {
    expect(classifyStaffing(0, 3, OFF_HOURS_MS)).toEqual({ risk: 'off-hours', label: 'Off hours', tone: 'neutral' });
    expect(classifyStaffing(10, 3, OFF_HOURS_MS)).toEqual({ risk: 'off-hours', label: 'Off hours', tone: 'neutral' });
  });

  it('still classifies normally at the exact business-hours boundary', () => {
    expect(classifyStaffing(0, 3, new Date('2026-09-01T12:00:00.000Z').getTime())).toEqual({ risk: 'no-coverage', label: 'No coverage', tone: 'danger' });
  });
});

describe('minAvailableFor', () => {
  it('falls back to DEFAULT_MIN_AVAILABLE for any profile not in the map', () => {
    expect(minAvailableFor('Some Profile Nobody Configured')).toBe(1);
  });
});

describe('agentStatusTone', () => {
  it('maps every effectiveStatus value to a distinct, correct tone', () => {
    expect(agentStatusTone('Available')).toBe('success');
    expect(agentStatusTone('On Call')).toBe('info');
    expect(agentStatusTone('ACW')).toBe('acw');
    expect(agentStatusTone('Unavailable')).toBe('warning');
    expect(agentStatusTone('Offline')).toBe('neutral');
  });
});
