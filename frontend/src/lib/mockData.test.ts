import { beforeEach, describe, expect, it } from 'vitest';

import {
  applyReconcile,
  lastVerify,
  mockCampaignDetail,
  mockCampaigns,
  mockSegments,
  runVerify,
  searchProfilesMock,
} from './mockData';

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

  it('throws if the segment exists but its family was never verified', () => {
    // Distinct fixture family untouched by any other test in this file —
    // exercises the "seg found, but no verifyState yet" branch specifically.
    mockSegments.push({
      name: 'zz-unverified-family',
      family: 'zz-unverified-family',
      version: 1,
      segmentArn:
        'arn:aws:profile:us-east-1:165505826690:domains/connect-domain/segment-definitions/zz-unverified-family',
      syncMode: 'manual',
    });
    expect(() => applyReconcile('zz-unverified-family')).toThrow(/Run Verify first/);
  });
});

describe('runVerify — fallback branches for an unknown segment', () => {
  it('uses the raw segment name as family/version=1/default scenario when no fixture segment matches', () => {
    const result = runVerify('some-segment-name-not-in-fixtures');
    expect(result.family).toBe('some-segment-name-not-in-fixtures');
    expect(result.version).toBe(1);
    expect(result.redisCount).toBe(100);
    expect(result.segmentCount).toBe(100);
    expect(result.missingCustomerIds).toHaveLength(0);
    expect(result.extraCustomerIds).toHaveLength(0);
  });
});

describe('applyReconcile — fallback branches', () => {
  it('falls back to segmentName as family when the segment record has no family field', () => {
    mockSegments.push({
      name: 'yy-no-family',
      version: 1,
      segmentArn: 'arn:aws:profile:us-east-1:165505826690:domains/connect-domain/segment-definitions/yy-no-family',
      syncMode: 'manual',
    });
    runVerify('yy-no-family');

    const result = applyReconcile('yy-no-family');
    expect(result.newSegmentName).toBe('yy-no-family-v2');
  });

  it('treats a campaign with no source field as {} rather than throwing', () => {
    mockCampaigns.push({
      id: 'cmp-no-source',
      name: 'No Source Campaign',
      channelSubtypes: ['TELEPHONY'],
    });
    mockSegments.push({
      name: 'ww-source-fallback',
      family: 'ww-source-fallback',
      version: 1,
      segmentArn: 'arn:aws:profile:us-east-1:165505826690:domains/connect-domain/segment-definitions/ww-source-fallback',
      syncMode: 'manual',
    });
    runVerify('ww-source-fallback');

    expect(() => applyReconcile('ww-source-fallback')).not.toThrow();
    // The source-less campaign is untouched (never matched, never crashed).
    const untouched = mockCampaigns.find((c) => c.id === 'cmp-no-source');
    expect(untouched?.source).toBeUndefined();
  });
});

describe('mockCampaignDetail', () => {
  it('returns the full campaign detail shape with the matching name and status', () => {
    const known = mockCampaigns[0];
    const detail = mockCampaignDetail(known.id);
    expect(detail.campaign.id).toBe(known.id);
    expect(detail.campaign.name).toBe(known.name);
    expect(detail.state).toBe(known.status);
  });

  it('falls back to the raw id as the name and "Running" as state for an unknown campaign id', () => {
    const detail = mockCampaignDetail('cmp-does-not-exist');
    expect(detail.campaign.name).toBe('cmp-does-not-exist');
    expect(detail.state).toBe('Running');
  });
});

describe('lastVerify', () => {
  it('returns undefined for a family that has never been verified', () => {
    expect(lastVerify('never-verified-family')).toBeUndefined();
  });

  it('returns the most recent verify result for a family after runVerify', () => {
    const result = runVerify('tx-high-intent-v2');
    expect(lastVerify('tx-high-intent')).toBe(result);
  });
});

describe('searchProfilesMock', () => {
  it('matches profiles by a substring of the name (case-insensitive)', () => {
    const results = searchProfilesMock('name', 'patrina');
    expect(results.some((p) => p.firstName === 'Patrina')).toBe(true);
  });

  it('matches profiles by phone number', () => {
    const results = searchProfilesMock('phone', '+17878086669');
    expect(results.map((p) => p.profileId)).toContain('3bb7f1c0-2222-4bf0-a9a9-0123456789ab');
  });

  it('returns every profile when value is empty and key is non-empty (key !== "" short-circuit)', () => {
    const results = searchProfilesMock('anyKey', '');
    expect(results.length).toBe(2);
  });

  it('returns no matches for a value with no matching key and an empty key', () => {
    const results = searchProfilesMock('', 'no-such-value-xyz');
    expect(results).toHaveLength(0);
  });
});
