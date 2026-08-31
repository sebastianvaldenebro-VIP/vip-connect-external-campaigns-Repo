import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

import { clampPercent, formatProgress, STATUS_TONE_CLASSES, type StatusTone } from './status';

export function ProgressBar({
  value,
  max,
  tone = 'info',
  label,
  className,
}: {
  value: number;
  max: number;
  tone?: StatusTone;
  label?: string;
  className?: string;
}): ReactNode {
  const pct = clampPercent(value, max);
  const toneClasses = STATUS_TONE_CLASSES[tone];
  return (
    <div className={cn('flex flex-col gap-1', className)}>
      <div className="flex items-center justify-between text-xs">
        {label ? <span className="text-muted-foreground">{label}</span> : <span />}
        <span className="font-mono text-foreground">{formatProgress(value, max)}</span>
      </div>
      <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
        <div
          className={cn('h-full rounded-full transition-[width] duration-500', toneClasses.bar)}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}
