export type StatusTone = 'success' | 'info' | 'warning' | 'danger' | 'neutral' | 'acw';

type ToneClasses = { bg: string; fg: string; bar: string; dot: string };

export const STATUS_TONE_CLASSES: Record<StatusTone, ToneClasses> = {
  success: {
    bg: 'bg-status-success-bg',
    fg: 'text-status-success-fg',
    bar: 'bg-status-success-bar',
    dot: 'bg-status-success-bar',
  },
  info: {
    bg: 'bg-status-info-bg',
    fg: 'text-status-info-fg',
    bar: 'bg-status-info-bar',
    dot: 'bg-status-info-bar',
  },
  warning: {
    bg: 'bg-status-warning-bg',
    fg: 'text-status-warning-fg',
    bar: 'bg-status-warning-bar',
    dot: 'bg-status-warning-bar',
  },
  danger: {
    bg: 'bg-status-danger-bg',
    fg: 'text-status-danger-fg',
    bar: 'bg-status-danger-bar',
    dot: 'bg-status-danger-bar',
  },
  neutral: {
    bg: 'bg-status-neutral-bg',
    fg: 'text-status-neutral-fg',
    bar: 'bg-status-neutral-bar',
    dot: 'bg-status-neutral-bar',
  },
  acw: {
    bg: 'bg-status-acw-bg',
    fg: 'text-status-acw-fg',
    bar: 'bg-status-acw-bar',
    dot: 'bg-status-acw-bar',
  },
};

/** Percentage of `value` out of `max`, clamped to [0, 100]. `max <= 0` returns 0. */
export function clampPercent(value: number, max: number): number {
  if (max <= 0) return 0;
  return Math.min(100, Math.max(0, (value / max) * 100));
}

/**
 * "value / max · pct%" — the reference design requires the numerator/denominator
 * text beside every progress bar, never a bare percentage. When `value` exceeds
 * `max` (redials past the segment size), shows "100%+" instead of silently
 * capping the displayed number, since that's a materially different situation
 * from "exactly done".
 */
export function formatProgress(value: number, max: number): string {
  const suffix = value > max && max > 0 ? '100%+' : `${Math.round(clampPercent(value, max))}%`;
  return `${value.toLocaleString()} / ${max.toLocaleString()} · ${suffix}`;
}
