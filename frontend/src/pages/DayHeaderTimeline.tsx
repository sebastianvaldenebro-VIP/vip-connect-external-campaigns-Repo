import type { ReactNode } from 'react';

import type { BucketStateV2, PlanRunV2, PlanSummaryV2 } from '@/lib/api';

export type TimelineSegment = {
  bucketIndex: number;
  startPct: number;
  endPct: number;
  status: BucketStateV2['status'];
};

function clampPct(minutesFromWindowStart: number, windowMinutes: number): number {
  return Math.min(100, Math.max(0, (minutesFromWindowStart / windowMinutes) * 100));
}

// Note: unlike BucketSection's `plannedStart` (PlanDetail.tsx), which chain-walks the plan
// to *project* a future start for buckets that haven't run yet, this component only needs
// the *actual* wall-clock position of each bucket within the window — no need to import
// computePlannedStart/campaignDurMin (or duplicate them) from './PlanDetail'. Deliberately
// not importing from './PlanDetail' at all (see Task 4's circular import lesson: Task 6
// will make PlanDetail.tsx import this file, so the reverse import must never exist).
//
// For a bucket's *end*, still-running time_based buckets project forward from their own
// plan-defined duration (a plain field read — plan.buckets[bucketIndex].duration_minutes,
// no helper needed) rather than from real-time `Date.now()`. Using wall-clock `now` here
// would make this pure function's output depend on when the test happens to run relative to
// the fixed window — status_based buckets (no fixed duration) still fall back to live `now`,
// since that's the only meaningful "still growing" position for them.
export function computeTimelineSegments(
  plan: PlanSummaryV2,
  run: PlanRunV2,
  windowStart: Date,
  windowEnd: Date,
): TimelineSegment[] {
  const windowMinutes = (windowEnd.getTime() - windowStart.getTime()) / 60_000;
  const now = Date.now();

  return run.bucketStates.map((bs, bucketIndex) => {
    const bucketDef = plan.buckets[bucketIndex];
    const startedAt = bs.startedAt ? new Date(bs.startedAt).getTime() : windowStart.getTime();

    let endedAt: number;
    if (bs.completedAt) {
      endedAt = new Date(bs.completedAt).getTime();
    } else if (bs.status === 'running' || bs.status === 'warming') {
      endedAt = bucketDef?.run_mode === 'time_based' && typeof bucketDef.duration_minutes === 'number'
        ? startedAt + bucketDef.duration_minutes * 60_000
        : now;
    } else {
      endedAt = startedAt;
    }

    const startMin = (startedAt - windowStart.getTime()) / 60_000;
    const endMin = (endedAt - windowStart.getTime()) / 60_000;

    return {
      bucketIndex,
      startPct: clampPct(startMin, windowMinutes),
      endPct: clampPct(endMin, windowMinutes),
      status: bs.status,
    };
  });
}

const SEGMENT_COLOR: Record<BucketStateV2['status'], string> = {
  queued: 'bg-gray-200',
  warming: 'bg-amber-300',
  running: 'bg-violet-500',
  completed: 'bg-green-500',
};

// Parses an "HH:MM" (COT) string and applies it to `base`'s local calendar date.
function applyHHMM(base: Date, hhmm: string): Date {
  const [h, m] = hhmm.split(':').map(Number);
  const d = new Date(base);
  d.setHours(h, m, 0, 0);
  return d;
}

export function DayHeaderTimeline({ plan, run }: { plan: PlanSummaryV2; run: PlanRunV2 }): ReactNode {
  const now = new Date();
  let windowStart: Date;
  let windowEnd: Date;
  if (plan.workingHours) {
    windowStart = applyHHMM(now, plan.workingHours.startTime);
    windowEnd = applyHHMM(now, plan.workingHours.endTime);
  } else {
    // Deliberate fallback: matches the reference design's default and this app's typical
    // operating hours when the plan has no configured workingHours.
    windowStart = new Date(now);
    windowStart.setHours(8, 0, 0, 0);
    windowEnd = new Date(now);
    windowEnd.setHours(20, 0, 0, 0);
  }

  const segments = computeTimelineSegments(plan, run, windowStart, windowEnd);
  const nowPct = Math.min(100, Math.max(0, ((now.getTime() - windowStart.getTime()) / (windowEnd.getTime() - windowStart.getTime())) * 100));

  return (
    <div className="relative h-6 w-full rounded-full bg-gray-100 overflow-hidden">
      {segments.map((seg) => (
        <div
          key={seg.bucketIndex}
          className={`absolute top-0 h-full ${SEGMENT_COLOR[seg.status]}`}
          style={{ left: `${seg.startPct}%`, width: `${Math.max(0.5, seg.endPct - seg.startPct)}%` }}
        />
      ))}
      <div className="absolute top-0 h-full w-0.5 bg-gray-900" style={{ left: `${nowPct}%` }} />
    </div>
  );
}
