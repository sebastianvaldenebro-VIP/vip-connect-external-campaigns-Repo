import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

export function formatDateTime(iso: string | undefined | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString();
}

// ── Time helpers (Colombia timezone, UTC-5, no DST) ──────────────────────────

export const COL_OFFSET_MS = -5 * 60 * 60 * 1000;

/** Formats a UTC timestamp as "HH:MM" in Colombia time (UTC-5, no DST). */
export function fmtTime(d: Date | string | null | undefined): string {
  if (!d) return '—';
  const dt = typeof d === 'string' ? new Date(d) : d;
  if (isNaN(dt.getTime())) return '—';
  const col = new Date(dt.getTime() + COL_OFFSET_MS);
  return `${col.getUTCHours().toString().padStart(2, '0')}:${col.getUTCMinutes().toString().padStart(2, '0')}`;
}

/**
 * Return the current instant as an ISO 8601 UTC string.
 */
export function nowIso(): string {
  return new Date().toISOString();
}

// Connect Campaigns V2 requires start time >= 5 minutes from now.
export function startTimeIso(): string {
  return new Date(Date.now() + 6 * 60 * 1000).toISOString();
}

/** Seconds elapsed since `iso`, relative to `nowMs` (defaults to the real clock). */
export function elapsedSeconds(iso: string, nowMs: number = Date.now()): number {
  return Math.max(0, Math.floor((nowMs - new Date(iso).getTime()) / 1000));
}

/** Whole minutes elapsed since `iso`, relative to `nowMs` (defaults to the real clock). */
export function elapsedMinutes(iso: string, nowMs: number = Date.now()): number {
  return Math.floor(elapsedSeconds(iso, nowMs) / 60);
}

/** `seconds` as "M:SS", or "H:MM:SS" once past an hour. */
export function formatRuntime(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  return `${m}:${String(s).padStart(2, '0')}`;
}

/** `minutes` as "Nm", or "Hh" / "Hh Mm" once past an hour. */
export function formatElapsed(minutes: number): string {
  if (minutes < 60) return `${minutes}m`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m === 0 ? `${h}h` : `${h}h ${m}m`;
}

/**
 * Return today at 21:00 America/New_York as an ISO 8601 UTC string.
 *
 * DST-aware: in EDT (March–November) returns *T01:00:00.000Z (next day);
 * in EST (November–March) returns *T02:00:00.000Z. The default end-time for
 * Enable Campaign — operator can override.
 */
export function today9pmNYIso(): string {
  const now = new Date();
  // Get NY's calendar date right now (so we don't roll over at UTC midnight).
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/New_York',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(now);
  const y = parts.find((p) => p.type === 'year')!.value;
  const m = parts.find((p) => p.type === 'month')!.value;
  const d = parts.find((p) => p.type === 'day')!.value;
  // Treat "YYYY-MM-DDT21:00:00Z" as if it were UTC, then subtract NY's offset
  // at this instant to recover the real UTC instant of 21:00 NY local time.
  const fakeUtc = new Date(`${y}-${m}-${d}T21:00:00Z`);
  return new Date(fakeUtc.getTime() - nyOffsetMinutes(now) * 60_000).toISOString();
}

function nyOffsetMinutes(at: Date): number {
  const fmt = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    timeZoneName: 'shortOffset',
  });
  const tz = fmt.formatToParts(at).find((p) => p.type === 'timeZoneName')?.value;
  // tz is e.g. "GMT-4" in EDT or "GMT-5" in EST.
  const m = tz?.match(/GMT([+-]\d+)/);
  return m ? parseInt(m[1], 10) * 60 : 0;
}
