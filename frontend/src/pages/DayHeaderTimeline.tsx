import type { ReactNode } from 'react';

import { clampPercent } from '@/components/ui/status';
import type { BucketStateV2, PlanRunV2, PlanSummaryV2 } from '@/lib/api';
import { COL_OFFSET_MS, fmtTime } from '@/lib/utils';

export type TimelineSegment = {
  bucketIndex: number;
  startPct: number;
  endPct: number;
  status: BucketStateV2['status'];
};

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
      startPct: clampPercent(startMin, windowMinutes),
      endPct: clampPercent(endMin, windowMinutes),
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

const RENDER_PRIORITY: Record<BucketStateV2['status'], number> = {
  queued: 0,
  warming: 1,
  running: 2,
  completed: 3,
};

/** Sorts segments so higher-priority statuses (completed last) paint on top —
 * absolutely-positioned overlapping segments otherwise stack by array order,
 * which can visually bury a completed (green) segment under a later-indexed
 * queued/running one. Stable sort: same-status segments keep their relative order. */
export function sortSegmentsForRender(segments: TimelineSegment[]): TimelineSegment[] {
  return [...segments].sort((a, b) => RENDER_PRIORITY[a.status] - RENDER_PRIORITY[b.status]);
}

const LEGEND: { status: BucketStateV2['status']; label: string }[] = [
  { status: 'queued', label: 'Queued' },
  { status: 'warming', label: 'Warming' },
  { status: 'running', label: 'Running' },
  { status: 'completed', label: 'Completed' },
];

// Parses an "HH:MM" (COT) string and applies it to `base`'s COT calendar date.
// Operates entirely via UTC-getters/Date.UTC (the same trick `fmtTime` in lib/utils
// uses) rather than Date.prototype.setHours, which would apply the browser's local
// timezone instead of COT.
function applyHHMM(base: Date, hhmm: string): Date {
  const [h, m] = hhmm.split(':').map(Number);
  const colBase = new Date(base.getTime() + COL_OFFSET_MS);
  const colInstantMs = Date.UTC(colBase.getUTCFullYear(), colBase.getUTCMonth(), colBase.getUTCDate(), h, m, 0, 0);
  return new Date(colInstantMs - COL_OFFSET_MS);
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
    windowStart = applyHHMM(now, '08:00');
    windowEnd = applyHHMM(now, '20:00');
  }

  const segments = computeTimelineSegments(plan, run, windowStart, windowEnd);
  const nowPct = Math.min(100, Math.max(0, ((now.getTime() - windowStart.getTime()) / (windowEnd.getTime() - windowStart.getTime())) * 100));

  return (
    <div>
      <div
        className="relative h-6 w-full rounded-full bg-gray-100 overflow-hidden"
        role="img"
        aria-label={`Bucket timeline from ${fmtTime(windowStart)} to ${fmtTime(windowEnd)}, now at ${fmtTime(now)}`}
      >
        {sortSegmentsForRender(segments).map((seg) => (
          <div
            key={seg.bucketIndex}
            className={`absolute top-0 h-full ${SEGMENT_COLOR[seg.status]}`}
            style={{ left: `${seg.startPct}%`, width: `${Math.max(0.5, seg.endPct - seg.startPct)}%` }}
          />
        ))}
        <div className="absolute top-0 h-full w-0.5 bg-gray-900" style={{ left: `${nowPct}%` }} />
      </div>
      <div className="mt-1.5 flex items-center gap-3 text-[11px] text-gray-500">
        {LEGEND.map(({ status, label }) => (
          <span key={status} className="inline-flex items-center gap-1">
            <span className={`inline-block h-2 w-2 rounded-full ${SEGMENT_COLOR[status]}`} />
            {label}
          </span>
        ))}
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-2 w-0.5 bg-gray-900" />
          Now
        </span>
      </div>
    </div>
  );
}
