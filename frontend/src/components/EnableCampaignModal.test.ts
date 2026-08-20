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

describe('resolveCampaignFlowArn (backend-first, client-heuristic fallback)', () => {
  it('prefers the backend-resolved ARN when available', async () => {
    const { resolveCampaignFlowArn } = await import('./EnableCampaignModal');
    const flows = [flow('campaign-CT')];
    const backendArn = 'arn:aws:connect:us-east-1:165505826690:instance/x/contact-flow/backend-resolved';
    const result = await resolveCampaignFlowArn(
      flows,
      ['PA'],
      async () => ({ arn: backendArn }),
    );
    expect(result).toBe(backendArn);
  });

  it('falls back to the client-side heuristic when the backend call throws', async () => {
    const { resolveCampaignFlowArn } = await import('./EnableCampaignModal');
    const flows = [flow('campaign-NY')];
    const result = await resolveCampaignFlowArn(
      flows,
      ['NY'],
      async () => {
        throw new Error('network error');
      },
    );
    expect(result).toBe(flows[0].arn);
  });

  it('falls back to the client-side heuristic when the backend returns null', async () => {
    const { resolveCampaignFlowArn } = await import('./EnableCampaignModal');
    const flows = [flow('campaign-NY')];
    const result = await resolveCampaignFlowArn(
      flows,
      ['NY'],
      async () => ({ arn: null }),
    );
    expect(result).toBe(flows[0].arn);
  });
});
