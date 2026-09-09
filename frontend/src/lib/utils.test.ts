import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  cn,
  elapsedMinutes,
  elapsedSeconds,
  fmtTime,
  formatDateTime,
  formatElapsed,
  formatRuntime,
  nowIso,
  startTimeIso,
  today9pmNYIso,
} from './utils';

describe('cn', () => {
  it('joins simple class names', () => {
    expect(cn('a', 'b')).toBe('a b');
  });

  it('drops falsy values', () => {
    expect(cn('a', false, undefined, null, '', 'b')).toBe('a b');
  });

  it('resolves conflicting tailwind classes, keeping the last one (twMerge)', () => {
    expect(cn('p-2', 'p-4')).toBe('p-4');
  });
});

describe('formatDateTime', () => {
  it('returns an em dash for undefined', () => {
    expect(formatDateTime(undefined)).toBe('—');
  });

  it('returns an em dash for null', () => {
    expect(formatDateTime(null)).toBe('—');
  });

  it('returns an em dash for an empty string', () => {
    expect(formatDateTime('')).toBe('—');
  });

  it('returns an em dash for an unparseable date string', () => {
    expect(formatDateTime('not-a-date')).toBe('—');
  });

  it('formats a valid ISO date using toLocaleString', () => {
    const iso = '2026-03-15T18:30:00Z';
    expect(formatDateTime(iso)).toBe(new Date(iso).toLocaleString());
  });
});

describe('fmtTime (Colombia time, UTC-5, no DST)', () => {
  it('returns an em dash for null', () => {
    expect(fmtTime(null)).toBe('—');
  });

  it('returns an em dash for undefined', () => {
    expect(fmtTime(undefined)).toBe('—');
  });

  it('returns an em dash for an unparseable string', () => {
    expect(fmtTime('not-a-date')).toBe('—');
  });

  it('converts a UTC ISO string to HH:MM Colombia time', () => {
    // 12:34 UTC - 5h = 07:34 COT
    expect(fmtTime('2026-01-15T12:34:00Z')).toBe('07:34');
  });

  it('accepts a Date instance directly', () => {
    expect(fmtTime(new Date('2026-01-15T12:34:00Z'))).toBe('07:34');
  });

  it('pads single-digit hours and minutes', () => {
    expect(fmtTime('2026-01-15T05:03:00Z')).toBe('00:03');
  });

  it('wraps around midnight correctly', () => {
    // 02:00 UTC - 5h = 21:00 the previous day in COT, but only HH:MM is shown
    expect(fmtTime('2026-01-15T02:00:00Z')).toBe('21:00');
  });
});

describe('nowIso', () => {
  it('returns a valid ISO 8601 string close to the current time', () => {
    const before = Date.now();
    const iso = nowIso();
    const after = Date.now();
    const t = new Date(iso).getTime();
    expect(t).toBeGreaterThanOrEqual(before);
    expect(t).toBeLessThanOrEqual(after);
  });
});

describe('startTimeIso', () => {
  it('returns a timestamp roughly 6 minutes in the future', () => {
    const t = new Date(startTimeIso()).getTime();
    const diffMinutes = (t - Date.now()) / 60_000;
    expect(diffMinutes).toBeGreaterThan(5.9);
    expect(diffMinutes).toBeLessThanOrEqual(6.1);
  });
});

describe('elapsedSeconds', () => {
  it('computes whole seconds elapsed relative to a fixed now', () => {
    const iso = '2026-01-15T12:00:00Z';
    const nowMs = new Date('2026-01-15T12:00:30Z').getTime();
    expect(elapsedSeconds(iso, nowMs)).toBe(30);
  });

  it('clamps to 0 when the timestamp is in the future relative to now', () => {
    const iso = '2026-01-15T12:00:30Z';
    const nowMs = new Date('2026-01-15T12:00:00Z').getTime();
    expect(elapsedSeconds(iso, nowMs)).toBe(0);
  });

  it('returns 0 for an unparseable iso string', () => {
    expect(elapsedSeconds('not-a-date', Date.now())).toBe(0);
  });

  it('defaults nowMs to the real clock when omitted', () => {
    const iso = new Date(Date.now() - 5000).toISOString();
    expect(elapsedSeconds(iso)).toBeGreaterThanOrEqual(4);
  });
});

describe('elapsedMinutes', () => {
  it('floors partial minutes', () => {
    const iso = '2026-01-15T12:00:00Z';
    const nowMs = new Date('2026-01-15T12:02:59Z').getTime();
    expect(elapsedMinutes(iso, nowMs)).toBe(2);
  });
});

describe('formatRuntime', () => {
  it('formats under an hour as M:SS', () => {
    expect(formatRuntime(65)).toBe('1:05');
  });

  it('formats zero seconds', () => {
    expect(formatRuntime(0)).toBe('0:00');
  });

  it('formats an hour or more as H:MM:SS', () => {
    expect(formatRuntime(3725)).toBe('1:02:05');
  });
});

describe('formatElapsed', () => {
  it('formats minutes under an hour as "Nm"', () => {
    expect(formatElapsed(45)).toBe('45m');
  });

  it('formats an exact hour with no remainder as "Hh"', () => {
    expect(formatElapsed(120)).toBe('2h');
  });

  it('formats hours with a remainder as "Hh Mm"', () => {
    expect(formatElapsed(125)).toBe('2h 5m');
  });
});

describe('today9pmNYIso', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('returns 01:00:00.000Z the next day during EDT (summer, UTC-4)', () => {
    vi.setSystemTime(new Date('2026-07-15T10:00:00Z'));
    expect(today9pmNYIso()).toBe('2026-07-16T01:00:00.000Z');
  });

  it('returns 02:00:00.000Z the next day during EST (winter, UTC-5)', () => {
    vi.setSystemTime(new Date('2026-01-15T10:00:00Z'));
    expect(today9pmNYIso()).toBe('2026-01-16T02:00:00.000Z');
  });

  it('uses NY\'s current calendar date, not UTC\'s (near UTC midnight rollover)', () => {
    // 2026-01-16T03:00:00Z is still 2026-01-15 22:00 EST — NY calendar date is the 15th.
    vi.setSystemTime(new Date('2026-01-16T03:00:00Z'));
    expect(today9pmNYIso()).toBe('2026-01-16T02:00:00.000Z');
  });

  it('falls back to a 0-minute offset when the ICU shortOffset name does not match "GMT±N" (defensive branch)', () => {
    vi.setSystemTime(new Date('2026-07-15T10:00:00Z'));
    const OriginalDTF = Intl.DateTimeFormat;
    vi.spyOn(Intl, 'DateTimeFormat').mockImplementation(function (
      this: unknown,
      locale?: unknown,
      options?: unknown,
    ) {
      const opts = options as { timeZoneName?: string } | undefined;
      if (opts?.timeZoneName === 'shortOffset') {
        return {
          formatToParts: () => [{ type: 'timeZoneName', value: 'Nowhere Time' }],
        } as unknown as Intl.DateTimeFormat;
      }
      return new OriginalDTF(locale as string, options as Intl.DateTimeFormatOptions);
    } as unknown as typeof Intl.DateTimeFormat);

    // With offset forced to 0, fakeUtc is returned unchanged (no NY-offset correction).
    expect(today9pmNYIso()).toBe('2026-07-15T21:00:00.000Z');
  });
});
