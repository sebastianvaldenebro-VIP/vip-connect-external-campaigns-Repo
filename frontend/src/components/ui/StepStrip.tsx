import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

import { STATUS_TONE_CLASSES, type StatusTone } from './status';

export type StepStripStep = {
  tone: StatusTone;
  pulse?: boolean;
};

export function StepStrip({
  steps,
  className,
}: {
  steps: StepStripStep[];
  className?: string;
}): ReactNode {
  return (
    <div className={cn('flex items-center gap-1', className)} role="list">
      {steps.map((step, i) => (
        <span
          key={i}
          role="listitem"
          className={cn('h-2 flex-1 rounded-full', STATUS_TONE_CLASSES[step.tone].bar, step.pulse && 'animate-pulse')}
        />
      ))}
    </div>
  );
}
