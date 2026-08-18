import { describe, expect, it } from 'vitest';

import { STATE_FLOW_PATTERNS, suggestCampaignFlow } from './EnableCampaignModal';
import type { ContactFlow } from '@/lib/api';

const flow = (name: string, id = name): ContactFlow => ({
  id,
  arn: `arn:aws:connect:us-east-1:165505826690:instance/x/contact-flow/${id}`,
  name,
  contactFlowType: 'CAMPAIGN',
});

describe('suggestCampaignFlow', () => {
  it('does not match campaign-CT for a state with no explicit pattern entry (regression: CAMPAIGN contains "PA")', () => {
    const flows = [flow('campaign-CT'), flow('campaign-NY'), flow('campaign-TX')];
    expect(suggestCampaignFlow(flows, ['PA'])).toBeUndefined();
  });

  it('still resolves an explicit-entry state correctly', () => {
    const flows = [flow('campaign-CT'), flow('campaign-NY'), flow('campaign-TX')];
    expect(suggestCampaignFlow(flows, ['NY'])?.name).toBe('campaign-NY');
  });

  it('auto-resolves a future state once its canonical flow exists, with no STATE_FLOW_PATTERNS entry needed', () => {
    const flows = [flow('campaign-CT'), flow('campaign-PA'), flow('campaign-NY')];
    expect(suggestCampaignFlow(flows, ['PA'])?.name).toBe('campaign-PA');
    expect(STATE_FLOW_PATTERNS.PA).toBeUndefined();
  });
});
