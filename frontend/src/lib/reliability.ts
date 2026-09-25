import type { AuditEntry } from './api';

export type Observation = {
  segment: string;
  checkedAt: string;
  redisCount: number | null;
  segmentCount: number | null;
};
const count = (value: unknown): number | null =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
    ? value
    : null;

/** Audit verify events contain counts, not an intersection of segment membership.
 * Equal counts must never be presented as 100% matching members. */
export function observationsFromAudit(entries: AuditEntry[]): Observation[] {
  const latest = new Map<string, Observation>();
  for (const entry of entries) {
    if (entry.action !== 'verify' || entry.entityType !== 'segment') continue;
    const segment =
      entry.resourceId ??
      (entry.entityId.startsWith('segment/') ? entry.entityId.slice(8) : '');
    if (!segment || !Number.isFinite(Date.parse(entry.timestamp))) continue;
    const extra =
      entry.extra && typeof entry.extra === 'object'
        ? (entry.extra as Record<string, unknown>)
        : {};
    const observation = {
      segment,
      checkedAt: entry.timestamp,
      redisCount: count(extra.redisCount),
      segmentCount: count(extra.segmentCount),
    };
    const previous = latest.get(segment);
    if (
      !previous ||
      Date.parse(previous.checkedAt) < Date.parse(observation.checkedAt)
    )
      latest.set(segment, observation);
  }
  return [...latest.values()].sort(
    (a, b) => Date.parse(b.checkedAt) - Date.parse(a.checkedAt),
  );
}

export function observationStatus(
  observation: Observation,
  now = Date.now(),
  freshnessMinutes = 15,
): string {
  const age = now - Date.parse(observation.checkedAt);
  if (!Number.isFinite(age) || age < -60_000) return 'Invalid observation time';
  if (age >= freshnessMinutes * 60_000) return 'Stale';
  if (observation.redisCount === null || observation.segmentCount === null)
    return 'Incomplete counts';
  return observation.redisCount === observation.segmentCount
    ? 'Counts equal · membership unverified'
    : 'Count difference · review needed';
}

export function campaignSegmentName(
  campaign: Record<string, unknown>,
): string | undefined {
  const source = campaign.source;
  if (!source || typeof source !== 'object' || Array.isArray(source))
    return undefined;
  const arn = (source as Record<string, unknown>).customerProfilesSegmentArn;
  if (typeof arn !== 'string') return undefined;
  const match = arn.match(
    /^arn:[^:]+:profile:[^:]+:\d{12}:domains\/[^/]+\/segment-definitions\/(.+)$/,
  );
  return match?.[1];
}
