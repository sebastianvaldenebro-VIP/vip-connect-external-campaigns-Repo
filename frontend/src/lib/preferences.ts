export type Preferences = {
  freshnessMinutes: number;
};
export const DEFAULT_PREFERENCES: Preferences = {
  freshnessMinutes: 15,
};
export function parsePreferences(value: unknown): Preferences {
  if (!value || typeof value !== 'object') return { ...DEFAULT_PREFERENCES };
  const candidate = value as Record<string, unknown>;
  return {
    freshnessMinutes:
      typeof candidate.freshnessMinutes === 'number' &&
      Number.isInteger(candidate.freshnessMinutes) &&
      candidate.freshnessMinutes >= 1 &&
      candidate.freshnessMinutes <= 1440
        ? candidate.freshnessMinutes
        : 15,
  };
}
export function preferencesKey(userId: string): string {
  return `vip-admin:preferences:v1:${encodeURIComponent(userId)}`;
}
