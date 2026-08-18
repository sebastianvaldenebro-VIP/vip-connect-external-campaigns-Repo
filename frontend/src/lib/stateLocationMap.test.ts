import { describe, expect, it } from 'vitest';

import {
  buildLocationToState,
  codesFromMap,
  resolveLocationMap,
  STATE_LOCATION_MAP,
  stateCodesFromSegmentGroups,
  stateCodesFromSegmentName,
  type StateGroup,
} from './stateLocationMap';

const testMap: readonly StateGroup[] = [
  { state: 'Texas', slug: 'Texas', code: 'TX', locations: ['Texas', 'TX - Addison'] },
  { state: 'Pennsylvania', slug: 'Pennsylvania', code: 'PA', locations: ['PA - Center City'] },
];

describe('resolveLocationMap', () => {
  it('returns the live map when it is non-empty', () => {
    expect(resolveLocationMap(testMap)).toBe(testMap);
  });

  it('falls back to STATE_LOCATION_MAP when data is undefined (query still loading)', () => {
    expect(resolveLocationMap(undefined)).toBe(STATE_LOCATION_MAP);
  });

  it('falls back to STATE_LOCATION_MAP when data is an empty array (regression: preview-mode fixture returns groups: [])', () => {
    expect(resolveLocationMap([])).toBe(STATE_LOCATION_MAP);
  });
});

describe('codesFromMap', () => {
  it('returns the set of state codes present in the map', () => {
    expect(codesFromMap(testMap)).toEqual(new Set(['TX', 'PA']));
  });

  it('returns an empty set for an empty map', () => {
    expect(codesFromMap([])).toEqual(new Set());
  });
});

describe('buildLocationToState with a live map (not the static fallback)', () => {
  it('resolves a location only present in the live map, not in STATE_LOCATION_MAP', () => {
    const reverse = buildLocationToState(testMap);
    expect(reverse['PA - Center City']).toBe('Pennsylvania');
  });
});

describe('stateCodesFromSegmentGroups with an explicit map parameter', () => {
  const segmentGroups = {
    Groups: [
      {
        Dimensions: [
          {
            ProfileAttributes: {
              Attributes: { location: { Values: ['PA - Center City'] } },
            },
          },
        ],
      },
    ],
  };

  it('resolves Pennsylvania when given a map that includes it', () => {
    expect(stateCodesFromSegmentGroups(segmentGroups, testMap)).toEqual(['PA']);
  });

  it('returns empty when given a map that does not include it (documents current static-map gap)', () => {
    expect(stateCodesFromSegmentGroups(segmentGroups, [])).toEqual([]);
  });
});

describe('stateCodesFromSegmentName with an explicit map parameter', () => {
  it('resolves a PA token when given a map that includes the PA code', () => {
    expect(stateCodesFromSegmentName('29-4-26-PA-3NL-1202-v4', testMap)).toEqual(['PA']);
  });

  it('returns empty for a code not present in the given map', () => {
    expect(stateCodesFromSegmentName('29-4-26-PA-3NL-1202-v4', [])).toEqual([]);
  });
});
