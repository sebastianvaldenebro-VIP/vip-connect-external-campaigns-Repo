import { describe, expect, it } from 'vitest';

import { buildChainMap, bucketColumnCount, chainBorderColor } from './chainMap';

// ── Helpers ────────────────────────────────────────────────────────────────────

const camp = (id: string, dependsOn: string[] = [], name?: string) => ({
  id,
  name: name ?? id,
  dependsOn,
});

const bucket = (...campaigns: ReturnType<typeof camp>[]) => ({ campaigns });

const plan = (...buckets: ReturnType<typeof bucket>[]) => ({ buckets });

// ── Normal cases ───────────────────────────────────────────────────────────────

describe('buildChainMap — normal cross-bucket chains', () => {
  it('assigns distinct chains to root campaigns in bucket 0', () => {
    const p = plan(bucket(camp('A'), camp('B')));
    const m = buildChainMap(p);

    expect(m.get('A')?.chainIndex).toBe(0);
    expect(m.get('B')?.chainIndex).toBe(1);
    expect(m.get('A')?.isMerge).toBe(false);
    expect(m.get('B')?.isMerge).toBe(false);
  });

  it('child inherits parent chain index across buckets', () => {
    // A(0) → C, B(1) → D — classic 2-column cross-bucket chain
    const p = plan(
      bucket(camp('A'), camp('B')),
      bucket(camp('C', ['A']), camp('D', ['B'])),
    );
    const m = buildChainMap(p);

    expect(m.get('C')?.chainIndex).toBe(m.get('A')?.chainIndex); // same as A
    expect(m.get('D')?.chainIndex).toBe(m.get('B')?.chainIndex); // same as B
    expect(m.get('C')?.isMerge).toBe(false);
    expect(m.get('D')?.isMerge).toBe(false);
  });

  it('three-level chain preserves column across all buckets', () => {
    const p = plan(
      bucket(camp('A'), camp('B')),
      bucket(camp('C', ['A']), camp('D', ['B'])),
      bucket(camp('E', ['C']), camp('F', ['D'])),
    );
    const m = buildChainMap(p);

    const chainA = m.get('A')!.chainIndex;
    const chainB = m.get('B')!.chainIndex;
    expect(m.get('C')?.chainIndex).toBe(chainA);
    expect(m.get('E')?.chainIndex).toBe(chainA);
    expect(m.get('D')?.chainIndex).toBe(chainB);
    expect(m.get('F')?.chainIndex).toBe(chainB);
  });

  it('single campaign in a bucket gets chain 0', () => {
    const p = plan(bucket(camp('A')));
    const m = buildChainMap(p);
    expect(m.get('A')?.chainIndex).toBe(0);
  });

  it('resolves parentNames from campaign names', () => {
    const p = plan(
      bucket(camp('a1', [], 'NY-NL')),
      bucket(camp('b1', ['a1'], 'CT-NL')),
    );
    const m = buildChainMap(p);
    expect(m.get('b1')?.parentNames).toEqual(['NY-NL']);
  });
});

// ── Case 1 — Intra-bucket dependency ──────────────────────────────────────────

describe('buildChainMap — Case 1: intra-bucket dependency', () => {
  it('child listed after parent in same bucket inherits parent chain', () => {
    // A and B in same bucket, B depends on A, A appears first
    const p = plan(bucket(camp('A'), camp('B', ['A'])));
    const m = buildChainMap(p);

    expect(m.get('A')?.chainIndex).toBe(0);
    // B's parent A is already processed → inherits chain 0
    expect(m.get('B')?.chainIndex).toBe(0);
    expect(m.get('B')?.isMerge).toBe(false);
  });

  it('child listed BEFORE parent in same bucket gets a new chain (graceful fallback)', () => {
    // B appears before A in campaigns array, but B depends on A
    // A is not yet in the map when B is processed → fallback to new chain
    const p = plan(bucket(camp('B', ['A']), camp('A')));
    const m = buildChainMap(p);

    // A gets chain 0 (first root encountered is... actually B gets a new chain first)
    // B: parent A not in map yet → new chain (0)
    // A: no deps → new chain (1)
    expect(m.get('B')?.chainIndex).not.toBe(m.get('A')?.chainIndex); // different chains
    expect(m.get('B')?.isMerge).toBe(false);
  });

  it('bucketColumnCount returns 1 when all campaigns share a chain', () => {
    const p = plan(bucket(camp('A'), camp('B', ['A'])));
    const m = buildChainMap(p);
    const cols = bucketColumnCount(['A', 'B'], m);
    // Both A and B are chain 0 (A processed first, B inherits)
    expect(cols).toBe(1);
  });
});

// ── Case 2 — Merge (multiple parents from different chains) ───────────────────

describe('buildChainMap — Case 2: merge campaign', () => {
  it('flags campaign as isMerge when parents are from different chains', () => {
    const p = plan(
      bucket(camp('A'), camp('B')),
      bucket(camp('C', ['A', 'B'])),
    );
    const m = buildChainMap(p);

    expect(m.get('C')?.isMerge).toBe(true);
  });

  it('merge campaign gets the minimum parent chain index', () => {
    const p = plan(
      bucket(camp('A'), camp('B')),
      bucket(camp('C', ['A', 'B'])),
    );
    const m = buildChainMap(p);

    const chainA = m.get('A')!.chainIndex;
    const chainB = m.get('B')!.chainIndex;
    expect(m.get('C')?.chainIndex).toBe(Math.min(chainA, chainB));
  });

  it('merge campaign includes all parent names', () => {
    const p = plan(
      bucket(camp('a1', [], 'NY-NL'), camp('b1', [], 'LI-NL')),
      bucket(camp('c1', ['a1', 'b1'], 'Merged')),
    );
    const m = buildChainMap(p);

    expect(m.get('c1')?.parentNames).toContain('NY-NL');
    expect(m.get('c1')?.parentNames).toContain('LI-NL');
    expect(m.get('c1')?.isMerge).toBe(true);
  });

  it('same-chain multiple parents are NOT flagged as merge', () => {
    // A and B both have chain 0, C depends on both → not a merge
    const p = plan(
      bucket(camp('A')),
      bucket(camp('B', ['A'])),
      bucket(camp('C', ['A', 'B'])),
    );
    const m = buildChainMap(p);

    expect(m.get('A')?.chainIndex).toBe(0);
    expect(m.get('B')?.chainIndex).toBe(0);
    expect(m.get('C')?.chainIndex).toBe(0);
    expect(m.get('C')?.isMerge).toBe(false);
  });
});

// ── Case 3 — N columns ────────────────────────────────────────────────────────

describe('buildChainMap — Case 3: N chains', () => {
  it('three independent root campaigns produce three distinct chain indices', () => {
    const p = plan(bucket(camp('A'), camp('B'), camp('C')));
    const m = buildChainMap(p);

    const chains = new Set([m.get('A')!.chainIndex, m.get('B')!.chainIndex, m.get('C')!.chainIndex]);
    expect(chains.size).toBe(3);
  });

  it('bucketColumnCount returns 3 for a bucket with 3 distinct chains', () => {
    const p = plan(bucket(camp('A'), camp('B'), camp('C')));
    const m = buildChainMap(p);
    expect(bucketColumnCount(['A', 'B', 'C'], m)).toBe(3);
  });

  it('bucketColumnCount returns 2 when 3 campaigns share 2 chains', () => {
    const p = plan(
      bucket(camp('A'), camp('B')),
      bucket(camp('C', ['A']), camp('D', ['B']), camp('E', ['A'])),
    );
    const m = buildChainMap(p);
    expect(bucketColumnCount(['C', 'D', 'E'], m)).toBe(2);
  });
});

// ── Empty / degenerate plans ──────────────────────────────────────────────────

describe('buildChainMap — edge cases', () => {
  it('empty plan returns empty map', () => {
    expect(buildChainMap({}).size).toBe(0);
    expect(buildChainMap({ buckets: [] }).size).toBe(0);
  });

  it('single bucket single campaign', () => {
    const m = buildChainMap(plan(bucket(camp('A'))));
    expect(m.get('A')?.chainIndex).toBe(0);
    expect(m.get('A')?.isMerge).toBe(false);
    expect(m.get('A')?.parentNames).toEqual([]);
  });

  it('dependsOn referencing unknown id falls back gracefully', () => {
    const p = plan(bucket(camp('A', ['nonexistent'])));
    const m = buildChainMap(p);
    // Parent not in map → new chain, no crash
    expect(m.has('A')).toBe(true);
    expect(m.get('A')?.isMerge).toBe(false);
  });

  it('chain indices are always non-negative integers', () => {
    const p = plan(
      bucket(camp('A'), camp('B'), camp('C')),
      bucket(camp('D', ['A']), camp('E', ['B', 'C'])),
    );
    const m = buildChainMap(p);
    for (const [, info] of m) {
      expect(info.chainIndex).toBeGreaterThanOrEqual(0);
      expect(Number.isInteger(info.chainIndex)).toBe(true);
    }
  });
});

// ── chainBorderColor ──────────────────────────────────────────────────────────

describe('chainBorderColor', () => {
  it('returns a Tailwind class string', () => {
    expect(chainBorderColor(0)).toMatch(/^border-l-/);
  });

  it('cycles back after 5 chains', () => {
    expect(chainBorderColor(0)).toBe(chainBorderColor(5));
    expect(chainBorderColor(1)).toBe(chainBorderColor(6));
  });

  it('never throws for large indices', () => {
    expect(() => chainBorderColor(999)).not.toThrow();
  });
});
