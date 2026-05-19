import { describe, expect, it } from 'vitest';

import { buildSegmentGroups, type Rule } from './segmentGroups';

const rule = (field: string, operator: Rule['operator'], values: string[]): Rule => ({
  field,
  operator,
  values,
});

describe('buildSegmentGroups', () => {
  it('translates a single ALL rule into a Customer Profiles dimension', () => {
    const result = buildSegmentGroups([rule('location', 'BEGINS_WITH', ['NJ -'])], 'ALL');

    expect(result.Include).toBe('ALL');
    expect(result.Groups).toHaveLength(1);
    expect(result.Groups[0].Type).toBe('ALL');
    expect(result.Groups[0].Dimensions).toEqual([
      {
        ProfileAttributes: {
          Attributes: {
            location: { DimensionType: 'BEGINS_WITH', Values: ['NJ -'] },
          },
        },
      },
    ]);
  });

  it('emits one dimension per rule so group-level ALL/ANY combines them', () => {
    const result = buildSegmentGroups(
      [
        rule('location', 'BEGINS_WITH', ['NJ -']),
        rule('available', 'INCLUSIVE', ['1']),
      ],
      'ANY',
    );

    expect(result.Groups[0].Type).toBe('ANY');
    expect(result.Groups[0].Dimensions).toHaveLength(2);
    expect(
      result.Groups[0].Dimensions[0].ProfileAttributes.Attributes.location,
    ).toBeDefined();
    expect(
      result.Groups[0].Dimensions[1].ProfileAttributes.Attributes.available,
    ).toBeDefined();
  });

  it('drops rules missing a field or values so empty rows never reach the API', () => {
    const result = buildSegmentGroups(
      [
        rule('', 'INCLUSIVE', ['anything']),
        rule('available', 'INCLUSIVE', []),
        rule('location', 'BEGINS_WITH', ['NJ -']),
      ],
      'ALL',
    );

    expect(result.Groups[0].Dimensions).toHaveLength(1);
    expect(
      result.Groups[0].Dimensions[0].ProfileAttributes.Attributes.location,
    ).toBeDefined();
  });

  it('preserves the INCLUSIVE operator with multi-value lists', () => {
    const result = buildSegmentGroups(
      [rule('customerid', 'INCLUSIVE', ['a', 'b', 'c'])],
      'ALL',
    );

    expect(
      result.Groups[0].Dimensions[0].ProfileAttributes.Attributes.customerid,
    ).toEqual({ DimensionType: 'INCLUSIVE', Values: ['a', 'b', 'c'] });
  });
});
