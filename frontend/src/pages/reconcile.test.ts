import { describe, expect, it } from 'vitest';

import { formatReconcile, reconcileTone } from './reconcile';

describe('reconcileTone', () => {
  it('returns success when actual equals expected', () => {
    expect(reconcileTone({ expected: 50, actual: 50, retries: 0 })).toBe('success');
  });

  it('returns warning when actual is less than expected (truncation or exclusions)', () => {
    expect(reconcileTone({ expected: 50, actual: 47, retries: 0 })).toBe('warning');
  });

  it('returns warning when retries were needed even if counts match', () => {
    expect(reconcileTone({ expected: 50, actual: 50, retries: 2 })).toBe('warning');
  });

  it('returns neutral when reconcile data is absent', () => {
    expect(reconcileTone(undefined)).toBe('neutral');
  });
});

describe('formatReconcile', () => {
  it('formats a clean match with no retries', () => {
    expect(formatReconcile({ expected: 50, actual: 50, retries: 0 })).toBe('reconcile: 50 → 50 · clean');
  });

  it('formats a mismatch', () => {
    expect(formatReconcile({ expected: 50, actual: 47, retries: 0 })).toBe('reconcile: 50 → 47 · clean');
  });

  it('formats retries, singular', () => {
    expect(formatReconcile({ expected: 50, actual: 50, retries: 1 })).toBe('reconcile: 50 → 50 · 1 retry');
  });

  it('formats retries, plural', () => {
    expect(formatReconcile({ expected: 50, actual: 50, retries: 3 })).toBe('reconcile: 50 → 50 · 3 retries');
  });

  it('returns empty string when reconcile data is absent', () => {
    expect(formatReconcile(undefined)).toBe('');
  });
});
