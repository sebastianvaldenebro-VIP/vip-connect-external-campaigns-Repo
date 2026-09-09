import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

import type { BucketDefV2, PlanRunV2, PlanSummaryV2 } from '@/lib/api';

import { DayHeaderTimeline } from './DayHeaderTimeline';

// 2026-09-01T19:00:00Z = 14:00 COT (UTC-5) — inside the default 08:00-20:00 COT window.
const FIXED_NOW = new Date('2026-09-01T19:00:00.000Z');

function bucketDef(overrides: Partial<BucketDefV2>): BucketDefV2 {
  return {
    id: 'b1',
    name: 'B1',
    run_mode: 'status_based',
    cleanup: true,
    prestart_next: false,
    campaignConfig: {} as never,
    campaigns: [],
    ...overrides,
  };
}

function plan(buckets: BucketDefV2[], workingHours?: PlanSummaryV2['workingHours']): PlanSummaryV2 {
  return {
    planId: 'p1',
    name: 'Test Plan',
    trigger: { type: 'manual' },
    isTemplate: false,
    is_template: false,
    isDefault: false,
    buckets,
    createdAt: FIXED_NOW.toISOString(),
    workingHours,
  };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(FIXED_NOW);
});

afterEach(() => {
  vi.useRealTimers();
});

describe('<DayHeaderTimeline />', () => {
  it('falls back to the default 08:00-20:00 COT window and renders a legend entry per status plus "Now"', () => {
    const p = plan([
      bucketDef({ id: 'b1', name: 'B1' }),
      bucketDef({ id: 'b2', name: 'B2', run_mode: 'time_based', duration_minutes: 120 }),
      bucketDef({ id: 'b3', name: 'B3' }),
    ]);
    const run: PlanRunV2 = {
      planId: 'p1',
      runId: 'r1',
      status: 'running',
      currentBucketIndex: 1,
      startedAt: FIXED_NOW.toISOString(),
      bucketStates: [
        { bucketId: 'b1', name: 'B1', status: 'completed', campaignStates: [], startedAt: '2026-09-01T13:30:00.000Z', completedAt: '2026-09-01T14:30:00.000Z' },
        { bucketId: 'b2', name: 'B2', status: 'running', campaignStates: [], startedAt: '2026-09-01T14:30:00.000Z' },
        // No startedAt/completedAt and not running/warming — exercises the
        // `endedAt = startedAt` fallback for a bucket that hasn't started yet.
        { bucketId: 'b3', name: 'B3', status: 'queued', campaignStates: [] },
      ],
    };

    render(<DayHeaderTimeline plan={p} run={run} />);

    expect(
      screen.getByRole('img', { name: 'Bucket timeline from 08:00 to 20:00, now at 14:00' }),
    ).toBeInTheDocument();
    expect(screen.getByText('Queued')).toBeInTheDocument();
    expect(screen.getByText('Warming')).toBeInTheDocument();
    expect(screen.getByText('Running')).toBeInTheDocument();
    expect(screen.getByText('Completed')).toBeInTheDocument();
    expect(screen.getByText('Now')).toBeInTheDocument();
  });

  it('uses the plan-configured workingHours window instead of the default when present', () => {
    const p = plan(
      [bucketDef({ id: 'b1', name: 'B1' })],
      { days: ['mon', 'tue', 'wed', 'thu', 'fri'], startTime: '09:00', endTime: '17:00' },
    );
    const run: PlanRunV2 = {
      planId: 'p1',
      runId: 'r1',
      status: 'running',
      currentBucketIndex: 0,
      startedAt: FIXED_NOW.toISOString(),
      bucketStates: [
        { bucketId: 'b1', name: 'B1', status: 'warming', campaignStates: [], startedAt: '2026-09-01T18:00:00.000Z' },
      ],
    };

    render(<DayHeaderTimeline plan={p} run={run} />);

    expect(
      screen.getByRole('img', { name: 'Bucket timeline from 09:00 to 17:00, now at 14:00' }),
    ).toBeInTheDocument();
  });
});
