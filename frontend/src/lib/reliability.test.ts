import { describe, expect, it } from 'vitest';
import {
  campaignSegmentName,
  observationsFromAudit,
  observationStatus,
} from './reliability';
import type { AuditEntry } from './api';
const time = '2026-09-14T12:00:00Z';
const event = (overrides: Partial<AuditEntry> = {}): AuditEntry => ({
  entityId: 'segment/demo',
  entityType: 'segment',
  action: 'verify',
  timestamp: time,
  extra: { redisCount: 10, segmentCount: 10 },
  ...overrides,
});
describe('reliability observations', () => {
  it('selects the newest event regardless of order and does not expose audit metadata', () => {
    const rows = observationsFromAudit([
      event({ timestamp: '2026-09-14T11:00:00Z' }),
      event({
        actorEmail: 'sensitive-marker',
        extra: { redisCount: 10, segmentCount: 9, sample: 'sensitive-marker' },
      }),
    ]);
    expect(rows).toEqual([
      { segment: 'demo', checkedAt: time, redisCount: 10, segmentCount: 9 },
    ]);
    expect(JSON.stringify(rows)).not.toContain('sensitive-marker');
  });
  it('equal counts never imply identical members or a 100% match', () => {
    expect(
      observationStatus(observationsFromAudit([event()])[0], Date.parse(time)),
    ).toBe('Counts equal · membership unverified');
  });
  it('preserves zero, rejects invalid counts and omits unrelated/invalid events', () => {
    const rows = observationsFromAudit([
      event({ extra: { redisCount: 0, segmentCount: -1 } }),
      event({ entityId: 'segment/other', action: 'create' }),
      event({ timestamp: 'invalid' }),
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0].redisCount).toBe(0);
    expect(rows[0].segmentCount).toBeNull();
    expect(observationStatus(rows[0], Date.parse(time))).toBe(
      'Incomplete counts',
    );
  });
  it('marks records stale at the freshness boundary and rejects future timestamps', () => {
    const row = observationsFromAudit([event()])[0];
    expect(observationStatus(row, Date.parse(time) + 15 * 60_000)).toBe(
      'Stale',
    );
    expect(observationStatus(row, Date.parse(time) - 120_000)).toBe(
      'Invalid observation time',
    );
  });
  it('uses the linked campaign segment, never the campaign name as a guess', () => {
    expect(campaignSegmentName({ name: 'demo' })).toBeUndefined();
    expect(
      campaignSegmentName({
        source: {
          customerProfilesSegmentArn:
            'arn:aws:profile:us-east-1:000000000000:domains/demo/segment-definitions/segment-a',
        },
      }),
    ).toBe('segment-a');
    expect(
      campaignSegmentName({
        source: { customerProfilesSegmentArn: 'https://untrusted/segment-a' },
      }),
    ).toBeUndefined();
  });
});
