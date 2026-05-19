/**
 * Chain assignment for plan campaign dependency visualization.
 *
 * A "chain" is a sequence of campaigns linked by dependsOn across buckets.
 * Campaigns in the same chain always render in the same grid column so
 * operators can trace the flow vertically without opening plan settings.
 */

export type ChainInfo = {
  /** 0-based index; campaigns with the same index belong to the same chain. */
  chainIndex: number;
  /**
   * True when this campaign depends on parents from MORE than one distinct chain.
   * These campaigns render full-width (col-span-all) with "↳ after A + B" label.
   */
  isMerge: boolean;
  /** Resolved parent campaign names for the dependency label. */
  parentNames: string[];
};

type CampaignDef = {
  id: string;
  name: string;
  dependsOn: string[];
};

type BucketDef = {
  campaigns: CampaignDef[];
};

type Plan = {
  buckets?: BucketDef[];
};

/**
 * Returns a map of campaignId → ChainInfo for every campaign in the plan.
 *
 * Rules:
 *  - No dependsOn              → new chain (root)
 *  - One parent chain           → inherit that chain
 *  - Multiple parents, same chain → inherit (not a merge)
 *  - Multiple parents, different chains → isMerge=true, chainIndex = min parent chain
 *  - Parent not yet seen (intra-bucket dep where parent listed after child) → new chain
 */
export function buildChainMap(plan: Plan): Map<string, ChainInfo> {
  const result = new Map<string, ChainInfo>();
  const nameById = new Map<string, string>();
  let nextChain = 0;

  for (const bucket of plan.buckets ?? []) {
    for (const campaign of bucket.campaigns) {
      nameById.set(campaign.id, campaign.name);
    }
  }

  for (const bucket of plan.buckets ?? []) {
    // Track which chain indices are already used in this bucket.
    // When two independent non-merge campaigns would inherit the same chain index
    // (e.g. after a merge node collapses two chains into one), the second one gets
    // a fresh index so it renders in its own column instead of stacking.
    const takenInBucket = new Set<number>();

    for (const campaign of bucket.campaigns) {
      if (campaign.dependsOn.length === 0) {
        const idx = nextChain++;
        takenInBucket.add(idx);
        result.set(campaign.id, { chainIndex: idx, isMerge: false, parentNames: [] });
        continue;
      }

      const parentChains = campaign.dependsOn
        .map((id) => result.get(id)?.chainIndex)
        .filter((c): c is number => c !== undefined);

      const parentNames = campaign.dependsOn
        .map((id) => nameById.get(id) ?? id.slice(0, 8))
        .filter(Boolean);

      const uniqueChains = [...new Set(parentChains)];

      let chainIndex: number;
      let isMerge: boolean;

      if (uniqueChains.length === 0) {
        // Intra-bucket dep: parent not yet processed (appears later in same bucket).
        chainIndex = nextChain++;
        isMerge = false;
      } else if (uniqueChains.length === 1) {
        chainIndex = uniqueChains[0];
        isMerge = false;
      } else {
        // Merge: parents from multiple chains — renders col-span-full.
        chainIndex = Math.min(...uniqueChains);
        isMerge = true;
      }

      // Non-merge: if this chain index was already claimed in this bucket by another
      // campaign, assign a new one so each independent parallel campaign gets its own column.
      if (!isMerge && takenInBucket.has(chainIndex)) {
        chainIndex = nextChain++;
      }

      takenInBucket.add(chainIndex);
      result.set(campaign.id, { chainIndex, isMerge, parentNames });
    }
  }

  return result;
}

/**
 * Given the chain map and a list of campaign IDs in one bucket,
 * returns the number of distinct chain columns that bucket needs.
 */
export function bucketColumnCount(ids: string[], chainMap: Map<string, ChainInfo>): number {
  const chains = new Set(ids.map((id) => chainMap.get(id)?.chainIndex ?? 0));
  return Math.max(1, chains.size);
}

/** Tailwind left-border color class for a given chain index (cycles through 5 colors). */
export const CHAIN_BORDER_COLORS = [
  'border-l-blue-400',
  'border-l-violet-400',
  'border-l-emerald-400',
  'border-l-amber-400',
  'border-l-rose-400',
] as const;

export function chainBorderColor(chainIndex: number): string {
  return CHAIN_BORDER_COLORS[chainIndex % CHAIN_BORDER_COLORS.length];
}
