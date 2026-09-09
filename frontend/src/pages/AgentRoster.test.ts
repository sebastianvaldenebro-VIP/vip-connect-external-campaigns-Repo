import { describe, expect, it } from 'vitest';

import type { AgentRosterEntry } from '@/lib/api';

import { groupAgentsByProfile, sortAgentsForDisplay } from './AgentRoster';

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

describe('sortAgentsForDisplay', () => {
  it('puts flagged agents before unflagged ones', () => {
    const flagged   = agent({ agentId: 'flagged',   effectiveStatus: 'ACW',     statusStartTimestamp: new Date(T0 - 5 * 60_000).toISOString() });
    const unflagged = agent({ agentId: 'unflagged', effectiveStatus: 'Offline', statusStartTimestamp: new Date(T0 - 60 * 60_000).toISOString() });
    const result = sortAgentsForDisplay([unflagged, flagged], T0);
    expect(result.map((a) => a.agentId)).toEqual(['flagged', 'unflagged']);
  });

  it('within the same flagged state, sorts by longest time in status first', () => {
    const longer  = agent({ agentId: 'longer',  effectiveStatus: 'Offline', statusStartTimestamp: new Date(T0 - 30 * 60_000).toISOString() });
    const shorter = agent({ agentId: 'shorter', effectiveStatus: 'Offline', statusStartTimestamp: new Date(T0 - 5 * 60_000).toISOString() });
    const result = sortAgentsForDisplay([shorter, longer], T0);
    expect(result.map((a) => a.agentId)).toEqual(['longer', 'shorter']);
  });
});

describe('groupAgentsByProfile', () => {
  it('groups by routingProfileId and counts flagged agents per group', () => {
    const groups = groupAgentsByProfile(
      [
        agent({ agentId: 'a1', routingProfileId: 'rp1', routingProfileName: 'RP One', effectiveStatus: 'Available', statusStartTimestamp: new Date(T0 - 15 * 60_000).toISOString() }),
        agent({ agentId: 'a2', routingProfileId: 'rp1', routingProfileName: 'RP One', effectiveStatus: 'Available', statusStartTimestamp: new Date(T0 - 1 * 60_000).toISOString() }),
        agent({ agentId: 'a3', routingProfileId: 'rp2', routingProfileName: 'RP Two', effectiveStatus: 'Offline' }),
      ],
      T0,
    );
    expect(groups).toHaveLength(2);
    const rpOne = groups.find((g) => g.routingProfileId === 'rp1')!;
    expect(rpOne.agents).toHaveLength(2);
    expect(rpOne.flaggedCount).toBe(1);
  });

  it('sorts groups with any flagged agent before groups with none', () => {
    const groups = groupAgentsByProfile(
      [
        agent({ agentId: 'a1', routingProfileId: 'calm', routingProfileName: 'Calm RP', effectiveStatus: 'Available', statusStartTimestamp: new Date(T0 - 1 * 60_000).toISOString() }),
        agent({ agentId: 'a2', routingProfileId: 'busy', routingProfileName: 'Busy RP', effectiveStatus: 'Available', statusStartTimestamp: new Date(T0 - 15 * 60_000).toISOString() }),
      ],
      T0,
    );
    expect(groups[0]!.routingProfileId).toBe('busy');
  });

  it('returns an empty array for no agents', () => {
    expect(groupAgentsByProfile([], T0)).toEqual([]);
  });

  it('breaks a tie in flagged-state and staffing risk by putting the larger group first', () => {
    const recent = new Date(T0 - 1 * 60_000).toISOString();
    const groups = groupAgentsByProfile(
      [
        // Both groups: 0 flagged, staffing 'healthy' (available > default min of 1) — tied on both.
        agent({ agentId: 's1', routingProfileId: 'small', routingProfileName: 'Small RP', effectiveStatus: 'Available', statusStartTimestamp: recent }),
        agent({ agentId: 's2', routingProfileId: 'small', routingProfileName: 'Small RP', effectiveStatus: 'Available', statusStartTimestamp: recent }),
        agent({ agentId: 'l1', routingProfileId: 'large', routingProfileName: 'Large RP', effectiveStatus: 'Available', statusStartTimestamp: recent }),
        agent({ agentId: 'l2', routingProfileId: 'large', routingProfileName: 'Large RP', effectiveStatus: 'Available', statusStartTimestamp: recent }),
        agent({ agentId: 'l3', routingProfileId: 'large', routingProfileName: 'Large RP', effectiveStatus: 'Available', statusStartTimestamp: recent }),
      ],
      T0,
    );
    expect(groups[0]!.routingProfileId).toBe('large');
    expect(groups[1]!.routingProfileId).toBe('small');
  });
});
