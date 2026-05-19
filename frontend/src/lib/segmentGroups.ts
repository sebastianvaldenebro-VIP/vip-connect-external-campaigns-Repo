/**
 * Translate the simple UI filter model into Customer Profiles segmentGroups.
 *
 * UI model (per rule):
 *   {field: string, operator: string, values: string[]}
 * Plus a group-level ALL | ANY combinator.
 *
 * Output follows the exact PascalCase shape the Customer Profiles API expects
 * (see CreateSegmentDefinition → SegmentGroups). Each rule becomes its own
 * `Dimensions` entry (a single attribute dimension), so the group-level
 * `Type` (ALL|ANY) combines rules additively.
 */

/**
 * AWS Customer Profiles AttributeDimension.DimensionType values we support.
 *
 * Important: AWS `EQUAL` / `NOT_EQUAL` are *numeric-only* operators — sending
 * them with a string value (e.g. "NJ - Newark") fails with
 * "must be a valid number". For string equality we use `INCLUSIVE` with a
 * single value, and for string inequality `EXCLUSIVE`. The UI surfaces them
 * under the same labels ("equals" / "not equals") — the distinction only
 * matters on the wire.
 */
export type RuleOperator =
  | 'INCLUSIVE'
  | 'EXCLUSIVE'
  | 'BEGINS_WITH'
  | 'ENDS_WITH'
  | 'CONTAINS';

export type Rule = {
  field: string;
  operator: RuleOperator;
  values: string[];
};

export type Combinator = 'ALL' | 'ANY';

export type SegmentGroups = {
  Include: 'ALL' | 'ANY' | 'NONE';
  Groups: Array<{
    Type: Combinator;
    Dimensions: Array<{
      ProfileAttributes: {
        Attributes: Record<
          string,
          { DimensionType: RuleOperator; Values: string[] }
        >;
      };
    }>;
  }>;
};

export function buildSegmentGroups(rules: Rule[], combinator: Combinator): SegmentGroups {
  const dimensions = rules
    .filter((r) => r.field.trim() !== '' && r.values.length > 0)
    .map((r) => ({
      ProfileAttributes: {
        Attributes: {
          [r.field.trim()]: {
            DimensionType: r.operator,
            Values: r.values,
          },
        },
      },
    }));

  return {
    Include: 'ALL',
    Groups: [
      {
        Type: combinator,
        Dimensions: dimensions,
      },
    ],
  };
}
