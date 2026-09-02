import { describe, expect, it } from 'vitest';

import type { PlanRunV2, PlanSummaryV2 } from '@/lib/api';

import { computeTimelineSegments, sortSegmentsForRender } from './DayHeaderTimeline';

const windowStart = new Date('2026-08-31T13:00:00.000Z'); // 8:00 AM COT (UTC-5)
const windowEnd = new Date('2026-09-01T01:00:00.000Z');   // 8:00 PM COT

function plan(buckets: PlanSummaryV2['buckets']): PlanSummaryV2 {
  return {
    planId: 'p1', name: 'Test', trigger: { type: 'manual' }, isTemplate: false, is_template: false,
    isDefault: false, buckets, createdAt: windowStart.toISOString(),
  };
}

describe('computeTimelineSegments', () => {
  it('positions a single bucket at its actual start/end within the window', () => {
    const p = plan([{ id: 'b1', name: 'B1', run_mode: 'time_based', duration_minutes: 120, cleanup: true, prestart_next: false, campaignConfig: {} as never, campaigns: [] }]);
    const run: PlanRunV2 = {
      planId: 'p1', runId: 'r1', status: 'running', currentBucketIndex: 0,
      startedAt: windowStart.toISOString(),
      bucketStates: [{
        bucketId: 'b1', name: 'B1', status: 'completed', campaignStates: [],
        startedAt: new Date(windowStart.getTime() + 60 * 60_000).toISOString(),
        completedAt: new Date(windowStart.getTime() + 180 * 60_000).toISOString(),
      }],
    };
    const [seg] = computeTimelineSegments(p, run, windowStart, windowEnd);
    expect(seg.startPct).toBeCloseTo((60 / 720) * 100, 1);
    expect(seg.endPct).toBeCloseTo((180 / 720) * 100, 1);
    expect(seg.status).toBe('completed');
  });

  it('clamps a segment that starts before the window to 0%', () => {
    const p = plan([{ id: 'b1', name: 'B1', run_mode: 'time_based', duration_minutes: 60, cleanup: true, prestart_next: false, campaignConfig: {} as never, campaigns: [] }]);
    const run: PlanRunV2 = {
      planId: 'p1', runId: 'r1', status: 'running', currentBucketIndex: 0,
      startedAt: windowStart.toISOString(),
      bucketStates: [{
        bucketId: 'b1', name: 'B1', status: 'running', campaignStates: [],
        startedAt: new Date(windowStart.getTime() - 30 * 60_000).toISOString(),
      }],
    };
    const [seg] = computeTimelineSegments(p, run, windowStart, windowEnd);
    expect(seg.startPct).toBe(0);
  });

  it('clamps a still-running bucket projected past the window to 100%', () => {
    const p = plan([{ id: 'b1', name: 'B1', run_mode: 'time_based', duration_minutes: 10_000, cleanup: true, prestart_next: false, campaignConfig: {} as never, campaigns: [] }]);
    const run: PlanRunV2 = {
      planId: 'p1', runId: 'r1', status: 'running', currentBucketIndex: 0,
      startedAt: windowStart.toISOString(),
      bucketStates: [{ bucketId: 'b1', name: 'B1', status: 'running', campaignStates: [], startedAt: windowStart.toISOString() }],
    };
    const [seg] = computeTimelineSegments(p, run, windowStart, windowEnd);
    expect(seg.endPct).toBe(100);
  });

  it('returns one segment per bucket, in plan order', () => {
    const p = plan([
      { id: 'b1', name: 'B1', run_mode: 'status_based', cleanup: true, prestart_next: false, campaignConfig: {} as never, campaigns: [] },
      { id: 'b2', name: 'B2', run_mode: 'status_based', cleanup: true, prestart_next: false, campaignConfig: {} as never, campaigns: [] },
    ]);
    const run: PlanRunV2 = {
      planId: 'p1', runId: 'r1', status: 'running', currentBucketIndex: 1,
      startedAt: windowStart.toISOString(),
      bucketStates: [
        { bucketId: 'b1', name: 'B1', status: 'completed', campaignStates: [], startedAt: windowStart.toISOString(), completedAt: windowStart.toISOString() },
        { bucketId: 'b2', name: 'B2', status: 'running', campaignStates: [], startedAt: windowStart.toISOString() },
      ],
    };
    const segments = computeTimelineSegments(p, run, windowStart, windowEnd);
    expect(segments).toHaveLength(2);
    expect(segments[0].bucketIndex).toBe(0);
    expect(segments[1].bucketIndex).toBe(1);
  });
});

describe('sortSegmentsForRender', () => {
  it('renders completed segments last (on top), regardless of input order', () => {
    const queued    = { bucketIndex: 0, startPct: 0,  endPct: 10, status: 'queued' as const };
    const running   = { bucketIndex: 1, startPct: 10, endPct: 20, status: 'running' as const };
    const completed = { bucketIndex: 2, startPct: 20, endPct: 30, status: 'completed' as const };
    const warming   = { bucketIndex: 3, startPct: 30, endPct: 40, status: 'warming' as const };

    const result = sortSegmentsForRender([completed, queued, running, warming]);
    expect(result[result.length - 1]).toBe(completed);
  });

  it('sorts strictly by status priority: queued, warming, running, completed', () => {
    const queued    = { bucketIndex: 0, startPct: 0,  endPct: 10, status: 'queued' as const };
    const warming   = { bucketIndex: 1, startPct: 10, endPct: 20, status: 'warming' as const };
    const running   = { bucketIndex: 2, startPct: 20, endPct: 30, status: 'running' as const };
    const completed = { bucketIndex: 3, startPct: 30, endPct: 40, status: 'completed' as const };

    const result = sortSegmentsForRender([completed, running, warming, queued]);
    expect(result.map((s) => s.status)).toEqual(['queued', 'warming', 'running', 'completed']);
  });

  it('does not mutate the input array', () => {
    const input = [
      { bucketIndex: 0, startPct: 0, endPct: 10, status: 'completed' as const },
      { bucketIndex: 1, startPct: 10, endPct: 20, status: 'queued' as const },
    ];
    const inputCopy = [...input];
    sortSegmentsForRender(input);
    expect(input).toEqual(inputCopy);
  });

  it('preserves relative order between segments of the same status', () => {
    const a = { bucketIndex: 0, startPct: 0,  endPct: 10, status: 'queued' as const };
    const b = { bucketIndex: 1, startPct: 10, endPct: 20, status: 'queued' as const };
    const result = sortSegmentsForRender([a, b]);
    expect(result).toEqual([a, b]);
  });
});
