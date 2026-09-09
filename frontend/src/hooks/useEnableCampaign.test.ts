import { beforeEach, describe, expect, it, vi } from 'vitest';

// useMutation is mocked as a plain function that just returns its config —
// this exercises useEnableCampaign()'s real mutationFn logic (the create+start
// orchestration and its error branch) without needing a React renderer, since
// the hook's own body performs no rendering work of its own.
vi.mock('@tanstack/react-query', () => ({
  useMutation: (config: unknown) => config,
}));

const campaignsCreate = vi.hoisted(() => vi.fn());
const campaignsStart = vi.hoisted(() => vi.fn());

vi.mock('@/lib/api', () => ({
  api: {
    campaigns: {
      create: campaignsCreate,
      start: campaignsStart,
    },
  },
}));

import { useEnableCampaign } from './useEnableCampaign';
import type { CreateCampaignBody } from '@/lib/api';

const body: CreateCampaignBody = {
  name: 'Test Campaign',
  queueId: 'q-1',
  contactFlowId: 'cf-1',
  sourcePhoneNumber: '+15550000000',
  dialer: { type: 'progressive' },
  schedule: { startTime: '2026-01-01T00:00:00Z', endTime: '2026-01-01T01:00:00Z' },
};

describe('useEnableCampaign', () => {
  beforeEach(() => {
    campaignsCreate.mockReset();
    campaignsStart.mockReset();
  });

  it('creates then starts the campaign, returning the started state on success', async () => {
    campaignsCreate.mockResolvedValueOnce({ id: 'cmp-1', arn: 'arn:cmp-1' });
    campaignsStart.mockResolvedValueOnce({ id: 'cmp-1', state: 'Running' });

    const config = useEnableCampaign() as {
      mutationFn: (b: CreateCampaignBody) => Promise<unknown>;
    };
    const result = await config.mutationFn(body);

    expect(campaignsCreate).toHaveBeenCalledWith(body);
    expect(campaignsStart).toHaveBeenCalledWith('cmp-1');
    expect(result).toEqual({ id: 'cmp-1', arn: 'arn:cmp-1', state: 'Running' });
  });

  it('keeps the created campaign and surfaces startError when start fails with an Error', async () => {
    campaignsCreate.mockResolvedValueOnce({ id: 'cmp-2', arn: 'arn:cmp-2' });
    campaignsStart.mockRejectedValueOnce(new Error('Connect throttled the start request'));

    const config = useEnableCampaign() as {
      mutationFn: (b: CreateCampaignBody) => Promise<unknown>;
    };
    const result = await config.mutationFn(body);

    expect(result).toEqual({
      id: 'cmp-2',
      arn: 'arn:cmp-2',
      startError: 'Connect throttled the start request',
    });
  });

  it('stringifies a non-Error thrown value for startError', async () => {
    campaignsCreate.mockResolvedValueOnce({ id: 'cmp-3', arn: 'arn:cmp-3' });
    // eslint-disable-next-line prefer-promise-reject-errors
    campaignsStart.mockRejectedValueOnce('raw string failure');

    const config = useEnableCampaign() as {
      mutationFn: (b: CreateCampaignBody) => Promise<unknown>;
    };
    const result = await config.mutationFn(body);

    expect(result).toEqual({
      id: 'cmp-3',
      arn: 'arn:cmp-3',
      startError: 'raw string failure',
    });
  });

  it('propagates a create() failure (never attempts start)', async () => {
    campaignsCreate.mockRejectedValueOnce(new Error('Segment ARN is invalid'));

    const config = useEnableCampaign() as {
      mutationFn: (b: CreateCampaignBody) => Promise<unknown>;
    };
    await expect(config.mutationFn(body)).rejects.toThrow('Segment ARN is invalid');
    expect(campaignsStart).not.toHaveBeenCalled();
  });
});
