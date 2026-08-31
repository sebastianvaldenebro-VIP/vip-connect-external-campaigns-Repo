import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

import { STATUS_TONE_CLASSES, type StatusTone } from './status';

/** "Diego Santos" → "DS"; single word → 1 letter; empty → "?". */
export function initialsFromName(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return '?';
  if (parts.length === 1) return parts[0]!.slice(0, 1).toUpperCase();
  return `${parts[0]!.slice(0, 1)}${parts[parts.length - 1]!.slice(0, 1)}`.toUpperCase();
}

export function Avatar({
  name,
  tone = 'neutral',
  className,
}: {
  name: string;
  tone?: StatusTone;
  className?: string;
}): ReactNode {
  const toneClasses = STATUS_TONE_CLASSES[tone];
  return (
    <span
      aria-hidden
      className={cn(
        'inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-semibold',
        toneClasses.bg,
        toneClasses.fg,
        className,
      )}
    >
      {initialsFromName(name)}
    </span>
  );
}
