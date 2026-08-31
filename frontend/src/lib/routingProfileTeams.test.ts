import { describe, expect, it } from 'vitest';

import { teamForProfile } from './routingProfileTeams';

describe('teamForProfile — exact matches (existing behavior)', () => {
  it('matches a profile listed verbatim in TEAM_ROUTING_PROFILES', () => {
    expect(teamForProfile('Front Desk NYC')).toBe('front-desk');
    expect(teamForProfile('PC - Existing Patient')).toBe('patient-success');
  });

  it('returns null for a profile with no match at all', () => {
    expect(teamForProfile('Some Unrelated Profile')).toBeNull();
  });
});

describe('teamForProfile — PC-* prefix fallback (root fix, 2026-08-28)', () => {
  it('classifies a brand-new PC routing profile not yet in the static list', () => {
    expect(teamForProfile('PC - New Leads Weekend')).toBe('patient-success');
  });

  it('does not prefix-match a name that merely contains "PC" without the team dash', () => {
    expect(teamForProfile('PC Legacy Profile')).toBeNull();
    expect(teamForProfile('SPECIALTY')).toBeNull();
  });
});

describe('teamForProfile — PS-* is explicitly excluded (corrected 2026-08-28)', () => {
  it('never classifies a PS- routing profile as patient-success, listed or not', () => {
    expect(teamForProfile('PS - Management')).toBeNull();
    expect(teamForProfile('PS - Success Coordinator')).toBeNull();
    expect(teamForProfile('PS - Overnight Coordinator')).toBeNull();
  });
});
