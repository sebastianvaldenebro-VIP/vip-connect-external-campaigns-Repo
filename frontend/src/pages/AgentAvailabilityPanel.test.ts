import { describe, expect, it } from 'vitest';

import type { AgentRosterEntry } from '@/lib/api';

import { aggregateByRoutingProfile } from './AgentAvailabilityPanel';

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
      routingProfileName: 'Outbound',
      available: 1,
      onCall: 1,
      acw: 0,
      offline: 0,
      unavailable: 0,
      total: 2,
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

  it('returns an empty array for no agents', () => {
    expect(aggregateByRoutingProfile([])).toEqual([]);
  });

  it('sorts profiles by available count ascending (most understaffed first)', () => {
    const agents = [
      agent({ agentId: 'a1', routingProfileId: 'rp1', routingProfileName: 'Well staffed', effectiveStatus: 'Available' }),
      agent({ agentId: 'a2', routingProfileId: 'rp1', routingProfileName: 'Well staffed', effectiveStatus: 'Available' }),
      agent({ agentId: 'a3', routingProfileId: 'rp2', routingProfileName: 'Understaffed', effectiveStatus: 'On Call' }),
    ];
    const result = aggregateByRoutingProfile(agents);
    expect(result[0].routingProfileName).toBe('Understaffed');
    expect(result[1].routingProfileName).toBe('Well staffed');
  });
});
