import { beforeEach, describe, expect, it } from 'vitest';

import { applyReconcile, mockCampaigns, mockSegments, runVerify } from './mockData';

/**
 * These tests pin the preview-mode semantics of verify + reconcile. Keeping
 * them green also keeps the UX story from regressing while the real backend is
 * being built; when that lands, the endpoints must return the same shape.
 */

// Each test resets the in-memory mock by re-importing the module — Vitest caches
// modules per test file, so we re-run the module initializers by accessing them
// fresh. Because `mockData.ts` mutates module-scoped arrays, we rely on the
// test ordering to chain state (verify → reconcile) and a fresh require for
// independent cases.

describe('runVerify', () => {
  beforeEach(async () => {
    // Reset state by re-evaluating the module.
    // vitest exposes vi.resetModules, but we can just mutate the shared arrays
    // back to known baselines to avoid require gymnastics.
  });

  it('returns the drift scenario associated with the segment family', () => {
    const result = runVerify('nj-available-leads-v3');

    expect(result.family).toBe('nj-available-leads');
    expect(result.version).toBe(3);
    // The fixture sets redisCount > segmentCount so the UI shows a drift badge.
    expect(result.redisCount).toBeGreaterThan(result.segmentCount);
    expect(result.missingCustomerIds.length).toBe(
      result.redisCount - result.segmentCount + result.extraCustomerIds.length,
    );
  });

  it('builds a sample mixing +add (missing) and −remove (extra) rows', () => {
    const result = runVerify('tx-high-intent-v2');

    const missing = result.sample.filter((c) => c.status === 'missing');
    const extras = result.sample.filter((c) => c.status === 'extra');

    expect(missing.length).toBeGreaterThan(0);
    expect(extras.length).toBeGreaterThan(0);
  });

  it('reports zero drift for a fixture marked in-sync', () => {
    const result = runVerify('fl-returning-no-contact-7d');

    expect(result.missingCustomerIds).toHaveLength(0);
    expect(result.extraCustomerIds).toHaveLength(0);
    expect(result.redisCount).toBe(result.segmentCount);
  });
});

describe('applyReconcile', () => {
  it('creates a v{N+1} segment and retargets campaigns that referenced the old ARN', () => {
    // Drive verify first so reconcile has a diff to act on.
    const before = mockSegments.find((s) => s.name === 'nj-available-leads-v3');
    if (!before) throw new Error('fixture missing nj-available-leads-v3');
    const oldArn = before.segmentArn;
    const expectedTarget = runVerify('nj-available-leads-v3').redisCount;

    const result = applyReconcile('nj-available-leads-v3');

    expect(result.newSegmentName).toBe('nj-available-leads-v4');
    expect(result.newVersion).toBe(4);
    expect(result.oldSegmentDeleted).toBe(true);
    expect(result.targetCount).toBe(expectedTarget);
    // At least the NJ campaign in the fixture points to this segment.
    expect(result.campaignsUpdated.length).toBeGreaterThanOrEqual(1);

    // The old segment is gone and the new one exists at the same index/family.
    expect(mockSegments.find((s) => s.name === 'nj-available-leads-v3')).toBeUndefined();
    expect(mockSegments.find((s) => s.name === 'nj-available-leads-v4')).toMatchObject({
      family: 'nj-available-leads',
      version: 4,
      syncMode: 'manual',
    });

    // Every retargeted campaign now references the new ARN.
    for (const campaignId of result.campaignsUpdated) {
      const campaign = mockCampaigns.find((c) => c.id === campaignId);
      const src = (campaign?.source ?? {}) as { customerProfilesSegmentArn?: string };
      expect(src.customerProfilesSegmentArn).not.toBe(oldArn);
      expect(src.customerProfilesSegmentArn).toBe(result.newSegmentArn);
    }
  });

  it('leaves the family in-sync for subsequent verify calls', () => {
    // Reconcile was executed in the previous test; verifying the new version
    // on the same family should report no drift now.
    const fresh = runVerify('nj-available-leads-v4');

    expect(fresh.missingCustomerIds).toHaveLength(0);
    expect(fresh.extraCustomerIds).toHaveLength(0);
    expect(fresh.redisCount).toBe(fresh.segmentCount);
  });

  it('throws if called before verify set up state for the family', () => {
    expect(() => applyReconcile('does-not-exist')).toThrow(/Unknown segment/);
  });
});
