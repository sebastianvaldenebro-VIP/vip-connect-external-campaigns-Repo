/**
 * State → Location mapping consumed by the Segment create form.
 *
 * Static fallback (STATE_LOCATION_MAP) is kept for components that need a
 * synchronous reference (stateCodesFromSegmentName, reverse-lookup in
 * stateCodesFromSegmentGroups). All interactive creation flows should use
 * useLocationMapping() which fetches live data from VipLocationMapping DynamoDB.
 */
import { useQuery } from '@tanstack/react-query';
import { api } from './api';

export type StateGroup = {
  /** Label shown in the UI dropdown. */
  state: string;
  /** Short, human-readable slug used inside auto-generated segment names. */
  slug: string;
  /** Two or three letter code embedded in the auto-name (NJ, NY, SCA, etc.). */
  code: string;
  locations: readonly string[];
};

export const STATE_LOCATION_MAP: readonly StateGroup[] = [
  {
    state: 'South CA',
    slug: 'SouthCA',
    code: 'SCA',
    locations: [
      'CA - Arcadia',
      'CA - Encino',
      'CA - Huntington Beach',
      'CA - Irvine',
      'CA - Long Beach',
      'CA - National City',
      'CA - Newport Beach',
      'CA - Poway',
      'CA - San Diego',
      'CA - Temecula',
      'CA - Torrance',
      'California',
    ],
  },
  {
    state: 'North CA',
    slug: 'NorthCA',
    code: 'NCA',
    locations: ['CA - Palo Alto', 'CA - Sacramento', 'CA - San Jose'],
  },
  {
    state: 'Connecticut',
    slug: 'Connecticut',
    code: 'CT',
    locations: [
      'Connecticut',
      'CT - Farmington',
      'CT - Hamden',
      'CT - Stamford',
    ],
  },
  {
    state: 'Maryland',
    slug: 'Maryland',
    code: 'MD',
    locations: [
      'DC',
      'Maryland',
      'MD - Bethesda',
      'MD - Bowie',
      'MD - Maple Lawn Office',
    ],
  },
  {
    state: 'New Jersey',
    slug: 'NewJersey',
    code: 'NJ',
    locations: [
      'New Jersey',
      'NJ - Clifton',
      'NJ - Edgewater',
      'NJ - Harrison Office',
      'NJ - Hoboken',
      'NJ - Marlton',
      'NJ - Morris County Office',
      'NJ - Morristown',
      'NJ - Paramus',
      'NJ - Princeton',
      'NJ - Scotch Plains',
      'NJ - West Orange Office',
      'NJ - West Orange Office (NEW)',
      'NJ - Woodbridge',
      'NJ - Woodland Park Office',
    ],
  },
  {
    state: 'New York',
    slug: 'NewYork',
    code: 'NY',
    locations: [
      'New York',
      'NY - Brighton Beach',
      'NY - Bronx',
      'NY - Forest Hills',
      'NY - Hartsdale',
      'NY - Upper East Side',
      'NY - Yonkers',
      'NYC - Astoria',
      'NYC - Bronx',
      'NYC - Brooklyn - Williamsburg',
      'NYC - Downtown Brooklyn',
      'NYC - FiDi Manhattan',
      'NYC - Midtown Manhattan',
      'NYC - Staten Island',
      'NYC - Williamsburg',
    ],
  },
  {
    state: 'Long Island',
    slug: 'LongIsland',
    code: 'LI',
    locations: [
      'Long Island',
      'NY - LI Hampton Bays',
      'NY - LI Jericho',
      'NY - LI Port Jefferson',
      'NY - LI Rockville',
      'NY - LI West Islip',
    ],
  },
  {
    state: 'Texas',
    slug: 'Texas',
    code: 'TX',
    locations: [
      'Texas',
      'TX - Addison',
      'TX - Arlington',
      'TX - Cedar Park',
      'TX - Cibolo Creek',
      'TX - Cinco Ranch',
      'TX - Dallas - Addison',
      'TX - East Dallas',
      'TX - Flower Mound',
      'TX - Fort Worth',
      'TX - Kyle',
      'TX - Medical Center',
      'TX - Spring Branch',
      'TX - Sugar Land',
    ],
  },
];

/** Flat lookup: location name → state slug (for summaries / reverse lookup).
 * Derived from the static fallback map; dynamic callers should build their own
 * reverse map from the useLocationMapping() hook result. */
export const LOCATION_TO_STATE: Record<string, string> = Object.fromEntries(
  STATE_LOCATION_MAP.flatMap((group) =>
    group.locations.map((loc) => [loc, group.state]),
  ),
);

/** Build a live reverse-lookup from a dynamically fetched StateGroup array. */
export function buildLocationToState(map: readonly StateGroup[]): Record<string, string> {
  return Object.fromEntries(map.flatMap((g) => g.locations.map((loc) => [loc, g.state])));
}

export function locationsForStates(
  states: string[],
  map: readonly StateGroup[] = STATE_LOCATION_MAP,
): string[] {
  const selected = new Set(states);
  return map.filter((g) => selected.has(g.state)).flatMap((g) => g.locations);
}

export function slugsForStates(
  states: string[],
  map: readonly StateGroup[] = STATE_LOCATION_MAP,
): string[] {
  const selected = new Set(states);
  return map.filter((g) => selected.has(g.state)).map((g) => g.slug);
}

export function codesForStates(
  states: string[],
  map: readonly StateGroup[] = STATE_LOCATION_MAP,
): string[] {
  const selected = new Set(states);
  return map.filter((g) => selected.has(g.state)).map((g) => g.code);
}

/**
 * Walk a Customer Profiles `SegmentGroups` JSON and extract the unique state
 * codes implied by its location filters. Used by EnableCampaignModal to pick
 * a phone number whose area code matches the segment's geography.
 *
 * Returns an empty array when the segment has no `location` filter or when
 * none of its location values are recognized in `STATE_LOCATION_MAP`.
 */
export function stateCodesFromSegmentGroups(segmentGroups: unknown): string[] {
  const found = new Set<string>();
  const groups = (segmentGroups as { Groups?: unknown[] } | undefined)?.Groups;
  if (!Array.isArray(groups)) return [];
  for (const group of groups) {
    const dimensions = (group as { Dimensions?: unknown[] }).Dimensions;
    if (!Array.isArray(dimensions)) continue;
    for (const dim of dimensions) {
      const attrs = (dim as { ProfileAttributes?: { Attributes?: Record<string, { Values?: string[] }> } })
        .ProfileAttributes?.Attributes;
      const locationAttr = attrs?.location;
      if (!locationAttr || !Array.isArray(locationAttr.Values)) continue;
      for (const value of locationAttr.Values) {
        const stateName = LOCATION_TO_STATE[value];
        if (!stateName) continue;
        const code = STATE_LOCATION_MAP.find((g) => g.state === stateName)?.code;
        if (code) found.add(code);
      }
    }
  }
  return Array.from(found);
}

export const KNOWN_STATE_CODES = new Set(STATE_LOCATION_MAP.map((g) => g.code));

/**
 * Extract a state code from a segment name like "29-4-26-TX-3NL-1202-v4".
 * The naming convention is <date-parts>-<STATE>-<description>-<id>-<version>.
 * Returns the first token that matches a known state code, or [] if none found.
 *
 * Reconciled segments have SegmentGroups as a static ID list (no location filter),
 * so this is the reliable fallback for auto-selecting campaign flow and phone.
 */
export function stateCodesFromSegmentName(name: string): string[] {
  for (const token of name.toUpperCase().split('-')) {
    if (KNOWN_STATE_CODES.has(token)) return [token];
  }
  return [];
}

/**
 * Fetch the location mapping from VipLocationMapping DynamoDB via the API.
 * Returns the static fallback while loading so callers always have data.
 * Cache is shared app-wide via react-query (staleTime 10 min).
 */
export function useLocationMapping(): {
  locationMap: readonly StateGroup[];
  isLoading: boolean;
} {
  const { data, isLoading } = useQuery({
    queryKey: ['location-mapping'],
    queryFn: async () => {
      const resp = await api.plans.getLocationMapping();
      return resp.groups;
    },
    staleTime: 10 * 60 * 1000, // 10 minutes — backend cache is 1h
    retry: 2,
  });

  return {
    locationMap: data ?? STATE_LOCATION_MAP,
    isLoading: isLoading && !data,
  };
}
