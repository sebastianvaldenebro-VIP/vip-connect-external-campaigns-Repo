import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

export function StatTile({
  label,
  value,
  valueClassName,
  className,
}: {
  label: string;
  value: ReactNode;
  valueClassName?: string;
  className?: string;
}): ReactNode {
  return (
    <div className={cn('rounded-lg border border-border bg-muted/40 p-3', className)}>
      <div className={cn('font-mono text-lg font-semibold tabular-nums text-foreground', valueClassName)}>
        {value}
      </div>
      <div className="mt-0.5 text-[10.5px] font-medium uppercase tracking-wide text-muted-foreground">{label}</div>
    </div>
  );
}
