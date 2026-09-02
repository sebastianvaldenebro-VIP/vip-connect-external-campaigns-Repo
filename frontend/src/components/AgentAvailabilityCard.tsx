import type { ReactNode } from 'react';

import { StatusChip } from '@/components/ui/StatusChip';
import { STATUS_TONE_CLASSES } from '@/components/ui/status';
import { classifyStaffing, type AvailabilityCounts } from '@/lib/agentRoster';

/**
 * One group's (a routing profile's or a whole team's) availability,
 * compact enough to tile several per row. Shared by BrandedMonitor's Live
 * Monitor sidebar and Campaign Monitor's compact widget — both resolve
 * `label` and `minAvailable` themselves before calling this, so this
 * component has no team/profile-scoping or threshold logic of its own.
 */
export function AgentAvailabilityCard({
  label,
  counts,
  minAvailable,
  onClick,
}: {
  label: string;
  counts: AvailabilityCounts;
  minAvailable: number;
  onClick?: () => void;
}): ReactNode {
  const staffing = classifyStaffing(counts.available, minAvailable);
  const tone = STATUS_TONE_CLASSES[staffing.tone];
  const atRisk = staffing.risk !== 'healthy' && staffing.risk !== 'off-hours';

  return (
    <div
      onClick={onClick}
      onKeyDown={
        onClick
          ? (e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                onClick();
              }
            }
          : undefined
      }
      role={onClick ? 'button' : undefined}
      tabIndex={onClick ? 0 : undefined}
      className={`min-w-[200px] flex-1 basis-[220px] space-y-2 rounded-xl border p-3 transition-shadow ${
        onClick ? 'cursor-pointer hover:shadow-md' : ''
      } ${atRisk ? 'border-red-200 bg-red-50/60' : 'border-gray-200 bg-white'}`}
    >
      <div className="flex items-start justify-between gap-2">
        <span className="truncate text-sm font-semibold text-gray-800">{label}</span>
        <div className="shrink-0 text-right">
          <div className={`text-2xl font-bold tabular-nums ${tone.fg}`}>{counts.available}</div>
          <div className="-mt-1 text-[10px] uppercase text-gray-400">Available</div>
        </div>
      </div>
      <div className="flex gap-3 text-xs text-gray-500">
        <span>{counts.onCall} CALL</span>
        <span>{counts.acw} ACW</span>
        <span>{counts.offline + counts.unavailable} OFF</span>
      </div>
      {atRisk && (
        <div className="flex items-center justify-between">
          <StatusChip tone={staffing.tone} label={staffing.label} />
          <span className="text-[10px] text-gray-400">min {minAvailable} available</span>
        </div>
      )}
    </div>
  );
}
