import type { ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';

import { ActivityFeed, type ActivityFeedItem } from '@/components/ui/ActivityFeed';
import type { StatusTone } from '@/components/ui/status';
import { api, type AuditEntry } from '@/lib/api';
import { fmtTime } from '@/lib/utils';

import { actionTone } from './Audit';

export function formatActivityEntry(entry: AuditEntry): string {
  const extra = (entry.extra ?? {}) as Record<string, unknown>;
  switch (entry.action) {
    case 'bucket_started': {
      const name = extra.bucketName as string | null | undefined;
      return name ? `Bucket "${name}" started` : `Bucket ${(extra.bucketIndex as number) + 1} started`;
    }
    case 'bucket_completed': {
      const name = extra.bucketName as string | null | undefined;
      const label = name ? `Bucket "${name}"` : `Bucket ${(extra.bucketIndex as number) + 1}`;
      return `${label} completed — ${extra.reason as string}`;
    }
    case 'window_closed':
      return `Operating window closed — ${extra.reason as string}`;
    case 'reconcile_retry':
      return `Bucket ${(extra.bucketIndex as number) + 1} / campaign ${(extra.campaignIndex as number) + 1} — reconcile retry ${extra.retry as number} of ${extra.retryLimit as number}`;
    case 'creation_failed':
      return `Bucket ${(extra.bucketIndex as number) + 1} / campaign ${(extra.campaignIndex as number) + 1} — creation failed: ${extra.error as string}`;
    default:
      return entry.action;
  }
}

// `actionTone` (from ./Audit) returns the Badge-tone vocabulary, which includes
// 'default' — a value `StatusTone` (consumed by ActivityFeed) doesn't have. Map
// it to 'neutral' rather than redefining the underlying action→tone logic.
const BADGE_TONE_TO_STATUS_TONE: Record<ReturnType<typeof actionTone>, StatusTone> = {
  default: 'neutral',
  success: 'success',
  warning: 'warning',
  danger: 'danger',
};

export function DayActivityFeed({
  planId,
  runId,
  className,
}: {
  planId: string;
  runId: string;
  className?: string;
}): ReactNode {
  const query = useQuery({
    queryKey: ['day-activity', planId, runId],
    queryFn: () => api.audit.entityHistory(`plan_run/${planId}/${runId}`),
    refetchInterval: 15_000,
  });

  const items: ActivityFeedItem[] = (query.data?.entries ?? []).map((entry) => ({
    id: `${entry.timestamp}-${entry.action}`,
    timestampLabel: fmtTime(entry.timestamp),
    text: formatActivityEntry(entry),
    tone: BADGE_TONE_TO_STATUS_TONE[actionTone(entry.action)],
  }));

  return (
    <div className={className}>
      <h3 className="text-sm font-semibold text-gray-700 mb-2">Day activity</h3>
      <ActivityFeed items={items} emptyLabel={query.isPending ? 'Loading…' : 'No activity yet.'} />
    </div>
  );
}
