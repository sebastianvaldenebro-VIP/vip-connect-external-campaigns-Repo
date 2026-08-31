import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

import { STATUS_TONE_CLASSES, type StatusTone } from './status';

export function StatusChip({
  tone,
  label,
  pulse = false,
  mono = false,
  className,
}: {
  tone: StatusTone;
  label: string;
  pulse?: boolean;
  mono?: boolean;
  className?: string;
}): ReactNode {
  const toneClasses = STATUS_TONE_CLASSES[tone];
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium',
        toneClasses.bg,
        toneClasses.fg,
        mono && 'font-mono',
        className,
      )}
    >
      {pulse ? (
        <span aria-hidden className={cn('h-1.5 w-1.5 rounded-full animate-pulse', toneClasses.dot)} />
      ) : null}
      {label}
    </span>
  );
}
