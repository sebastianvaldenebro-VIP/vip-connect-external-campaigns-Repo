import { describe, expect, it } from 'vitest';

import type { AuditEntry } from '@/lib/api';

import { formatActivityEntry } from './DayActivityFeed';

function entry(action: string, extra: unknown): AuditEntry {
  return {
    entityId: 'plan_run/p1/r1',
    action,
    timestamp: '2026-08-31T19:40:00.000Z',
    extra: extra as AuditEntry['extra'],
  };
}

describe('formatActivityEntry', () => {
  it('formats bucket_started', () => {
    expect(formatActivityEntry(entry('bucket_started', { bucketIndex: 2, bucketName: 'NJ/CT' })))
      .toBe('Bucket "NJ/CT" started');
  });

  it('formats bucket_started with no bucketName', () => {
    expect(formatActivityEntry(entry('bucket_started', { bucketIndex: 2, bucketName: null })))
      .toBe('Bucket 3 started');
  });

  it('formats bucket_completed', () => {
    expect(formatActivityEntry(entry('bucket_completed', { bucketIndex: 1, bucketName: 'NJ/CT', reason: 'all_campaigns_done' })))
      .toBe('Bucket "NJ/CT" completed — all_campaigns_done');
  });

  it('formats window_closed', () => {
    expect(formatActivityEntry(entry('window_closed', { reason: 'working_hours_cutoff' })))
      .toBe('Operating window closed — working_hours_cutoff');
  });

  it('formats reconcile_retry', () => {
    expect(formatActivityEntry(entry('reconcile_retry', { bucketIndex: 0, campaignIndex: 2, retry: 1, retryLimit: 5 })))
      .toBe('Bucket 1 / campaign 3 — reconcile retry 1 of 5');
  });

  it('formats creation_failed', () => {
    expect(formatActivityEntry(entry('creation_failed', { bucketIndex: 0, campaignIndex: 1, error: 'ThrottlingException' })))
      .toBe('Bucket 1 / campaign 2 — creation failed: ThrottlingException');
  });

  it('falls back to the raw action for an unrecognized event type', () => {
    expect(formatActivityEntry(entry('some_future_action', { anything: true }))).toBe('some_future_action');
  });
});
