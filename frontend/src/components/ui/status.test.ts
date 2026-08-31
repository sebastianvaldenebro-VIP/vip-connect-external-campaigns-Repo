import { describe, expect, it } from 'vitest';

import { clampPercent, formatProgress } from './status';

describe('clampPercent', () => {
  it('computes a normal percentage', () => {
    expect(clampPercent(112, 240)).toBeCloseTo(46.666, 2);
  });

  it('returns 0 when max is 0', () => {
    expect(clampPercent(5, 0)).toBe(0);
  });

  it('caps at 100 when value exceeds max', () => {
    expect(clampPercent(300, 240)).toBe(100);
  });

  it('returns 0 for a value of 0', () => {
    expect(clampPercent(0, 240)).toBe(0);
  });
});

describe('formatProgress', () => {
  it('formats a normal ratio with rounded percent', () => {
    expect(formatProgress(1240, 2980)).toBe('1,240 / 2,980 · 42%');
  });

  it('rounds up at the .5 boundary', () => {
    expect(formatProgress(112, 240)).toBe('112 / 240 · 47%');
  });

  it('marks over-100% (redials) explicitly instead of capping silently', () => {
    expect(formatProgress(255, 240)).toBe('255 / 240 · 100%+');
  });

  it('handles a zero denominator without throwing', () => {
    expect(formatProgress(0, 0)).toBe('0 / 0 · 0%');
  });
});
