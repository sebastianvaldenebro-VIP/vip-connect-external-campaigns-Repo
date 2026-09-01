import { describe, expect, it } from 'vitest';

import { aggregateByRoutingProfile } from '@/lib/agentRoster';

// Full behavior coverage for aggregateByRoutingProfile now lives in
// src/lib/agentRoster.test.ts (it moved there in the same commit this
// component started importing it from lib/agentRoster instead of defining
// it locally). This file keeps one sanity check that the import path
// AgentAvailabilityPanel.tsx actually uses is wired correctly.
describe('AgentAvailabilityPanel — aggregateByRoutingProfile import', () => {
  it('is importable from @/lib/agentRoster and behaves correctly', () => {
    expect(aggregateByRoutingProfile([])).toEqual([]);
  });
});
