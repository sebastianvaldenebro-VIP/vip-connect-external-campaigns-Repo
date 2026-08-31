import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

import { STATUS_TONE_CLASSES, type StatusTone } from './status';

export type ActivityFeedItem = {
  id: string;
  timestampLabel: string;
  text: string;
  tone: StatusTone;
};

export function ActivityFeed({
  items,
  className,
  emptyLabel = 'No activity yet.',
}: {
  items: ActivityFeedItem[];
  className?: string;
  emptyLabel?: string;
}): ReactNode {
  if (items.length === 0) {
    return <p className={cn('text-sm text-muted-foreground', className)}>{emptyLabel}</p>;
  }
  return (
    <ol className={cn('flex max-h-[360px] flex-col gap-2 overflow-y-auto', className)}>
      {items.map((item) => (
        <li key={item.id} className="flex items-start gap-2 text-xs">
          <span className="w-[52px] shrink-0 pt-0.5 text-right font-mono text-muted-foreground">
            {item.timestampLabel}
          </span>
          <span
            aria-hidden
            className={cn('mt-1 h-1.5 w-1.5 shrink-0 rounded-full', STATUS_TONE_CLASSES[item.tone].bar)}
          />
          <span className="text-foreground/80">{item.text}</span>
        </li>
      ))}
    </ol>
  );
}
