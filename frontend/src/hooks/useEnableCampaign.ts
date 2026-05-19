import { useMutation } from '@tanstack/react-query';

import { api, type CreateCampaignBody } from '@/lib/api';

export type EnableCampaignResult = {
  id: string;
  arn: string;
  /** State after attempting Start. May be undefined if Start failed and we
   * left the campaign in the just-created (typically "Stopped") state. */
  state?: string;
  /** Set when create succeeded but start failed — UI surfaces this so the
   * operator knows to retry Start manually instead of assuming success. */
  startError?: string;
};

/**
 * Create + auto-start a campaign in two sequential calls. If `start` fails
 * after `create` succeeded we keep the created campaign and surface the
 * partial result — re-running create here would create a duplicate, and
 * deleting on best-effort would mask the real problem from the operator.
 */
export function useEnableCampaign() {
  return useMutation<EnableCampaignResult, Error, CreateCampaignBody>({
    mutationFn: async (body) => {
      const created = await api.campaigns.create(body);
      try {
        const started = await api.campaigns.start(created.id);
        return { id: created.id, arn: created.arn, state: started.state };
      } catch (e) {
        return {
          id: created.id,
          arn: created.arn,
          startError: e instanceof Error ? e.message : String(e),
        };
      }
    },
  });
}
