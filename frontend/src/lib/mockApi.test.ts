import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { mockApi } from './mockApi';
import { mockAudit, mockCampaigns, mockProfiles, mockSegments } from './mockData';

/**
 * mockApi.* methods use a real setTimeout-based delay() to simulate network
 * latency in preview mode. Fake timers let these tests run instantly while
 * still exercising the real code path (including the awaited delay).
 */
async function flush<T>(promise: Promise<T>): Promise<T> {
  await vi.advanceTimersByTimeAsync(3000);
  return promise;
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('mockApi.segments', () => {
  it('list returns copies of mockSegments (mutation-safe)', async () => {
    const { segments } = await flush(mockApi.segments.list());
    expect(segments.length).toBe(mockSegments.length);
    expect(segments[0]).toEqual(mockSegments[0]);
    expect(segments[0]).not.toBe(mockSegments[0]);
  });

  it('get returns the matching segment merged with the fixture segmentGroups', async () => {
    const known = mockSegments[0];
    const result = await flush(mockApi.segments.get(known.name));
    expect(result.name).toBe(known.name);
    expect(result.segmentGroups).toBeDefined();
  });

  it('get falls back to the first segment when the id is unknown', async () => {
    const result = await flush(mockApi.segments.get('does-not-exist'));
    expect(result.name).toBe(mockSegments[0].name);
  });

  it('create pushes and returns a new segment record', async () => {
    const before = mockSegments.length;
    const created = await flush(
      mockApi.segments.create({
        name: 'preview-seg-1',
        displayName: 'Preview Seg 1',
        segmentGroups: {},
        syncMode: 'manual',
      }),
    );
    expect(created.name).toBe('preview-seg-1');
    expect(created.family).toBe('preview-seg-1');
    expect(created.version).toBe(1);
    expect(mockSegments.length).toBe(before + 1);
  });

  it('remove deletes an existing segment', async () => {
    mockSegments.push({
      name: 'to-be-removed',
      family: 'to-be-removed',
      version: 1,
      segmentArn: 'arn:aws:profile:us-east-1:165505826690:segment/to-be-removed',
      syncMode: 'manual',
    });
    const before = mockSegments.length;
    await flush(mockApi.segments.remove('to-be-removed'));
    expect(mockSegments.length).toBe(before - 1);
    expect(mockSegments.find((s) => s.name === 'to-be-removed')).toBeUndefined();
  });

  it('remove is a no-op for an unknown id', async () => {
    const before = mockSegments.length;
    await flush(mockApi.segments.remove('does-not-exist'));
    expect(mockSegments.length).toBe(before);
  });

  it('createEstimate returns a fixed in-progress estimate', async () => {
    const result = await flush(mockApi.segments.createEstimate('seg-1'));
    expect(result).toEqual({ estimateId: 'est-mock-0001', status: 'IN_PROGRESS' });
  });

  it('getEstimate echoes the estimateId with a plausible random count', async () => {
    const result = await flush(mockApi.segments.getEstimate('seg-1', 'est-abc'));
    expect(result.estimateId).toBe('est-abc');
    expect(result.status).toBe('SUCCEEDED');
    expect(result.estimate?.totalCount).toBeGreaterThanOrEqual(4213);
    expect(result.estimate?.totalCount).toBeLessThan(4413);
  });

  it('createSnapshot returns a fixed in-progress snapshot', async () => {
    const result = await flush(mockApi.segments.createSnapshot('seg-1'));
    expect(result.status).toBe('IN_PROGRESS');
    expect(result.snapshotId).toBe('snap-mock-0001');
  });

  it('getSnapshot echoes the snapshotId as completed CSV', async () => {
    const result = await flush(mockApi.segments.getSnapshot('seg-1', 'snap-xyz'));
    expect(result.snapshotId).toBe('snap-xyz');
    expect(result.status).toBe('SUCCEEDED');
    expect(result.dataFormat).toBe('CSV');
  });

  it('updateSyncMode mutates and returns the segment when found', async () => {
    const known = mockSegments[0];
    const result = await flush(mockApi.segments.updateSyncMode(known.name, 'live'));
    expect(result.syncMode).toBe('live');
    expect(mockSegments.find((s) => s.name === known.name)?.syncMode).toBe('live');
  });

  it('updateSyncMode throws for an unknown segment', async () => {
    // Attach the rejection handler before advancing fake timers, so the
    // in-flight delay()'s eventual throw is never briefly "unhandled".
    const promise = mockApi.segments.updateSyncMode('does-not-exist', 'live');
    const expectation = expect(promise).rejects.toThrow(/Unknown segment/);
    await vi.advanceTimersByTimeAsync(3000);
    await expectation;
  });

  it('verify delegates to the real runVerify fixture logic', async () => {
    const known = mockSegments.find((s) => s.name.startsWith('tx-high-intent'))!;
    const result = await flush(mockApi.segments.verify(known.name));
    expect(result.family).toBe('tx-high-intent');
  });

  it('startExtrasDetection returns an in-progress snapshot with a random id', async () => {
    const result = await flush(mockApi.segments.startExtrasDetection('seg-1'));
    expect(result.status).toBe('IN_PROGRESS');
    expect(result.snapshotId).toMatch(/^snap-preview-/);
  });

  it('getExtrasDetection echoes the snapshotId as completed', async () => {
    const result = await flush(mockApi.segments.getExtrasDetection('seg-1', 'snap-9'));
    expect(result.snapshotId).toBe('snap-9');
    expect(result.status).toBe('COMPLETED');
    expect(result.extraCustomerIds).toHaveLength(5);
  });

  it('reconcile delegates to the real applyReconcile fixture logic (after verify)', async () => {
    const known = mockSegments.find((s) => s.name.startsWith('fl-returning'))!;
    await flush(mockApi.segments.verify(known.name));
    const result = await flush(mockApi.segments.reconcile(known.name));
    expect(result.oldSegmentDeleted).toBe(true);
  });

  it('diagnose returns a fixed stale-membership report echoing the segmentName', async () => {
    const result = await flush(mockApi.segments.diagnose('seg-42'));
    expect(result.segmentName).toBe('seg-42');
    expect(result.confirmedStale).toHaveLength(3);
    expect(result.cpNoMatch).toHaveLength(0);
  });
});

describe('mockApi.campaigns', () => {
  it('list returns the mockCampaigns fixture', async () => {
    const { campaigns } = await flush(mockApi.campaigns.list());
    expect(campaigns).toBe(mockCampaigns);
  });

  it('get delegates to mockCampaignDetail', async () => {
    const known = mockCampaigns[0];
    const result = await flush(mockApi.campaigns.get(known.id));
    expect(result.campaign.id).toBe(known.id);
  });

  it('create returns a random preview id and arn', async () => {
    const result = await flush(
      mockApi.campaigns.create({
        name: 'Preview Campaign',
        queueId: 'q-1',
        contactFlowId: 'cf-1',
        sourcePhoneNumber: '+15550000000',
        dialer: { type: 'progressive' },
        schedule: { startTime: '2026-01-01T00:00:00Z', endTime: '2026-01-01T01:00:00Z' },
      }),
    );
    expect(result.id).toMatch(/^cmp-preview-/);
    expect(result.arn).toContain('campaign/preview');
  });

  it('update echoes the id and body', async () => {
    const result = await flush(mockApi.campaigns.update('c-1', { name: 'Renamed' }));
    expect(result).toEqual({ id: 'c-1', updated: { name: 'Renamed' } });
  });

  it('remove deletes an existing campaign', async () => {
    mockCampaigns.push({ id: 'cmp-removable', name: 'Removable', channelSubtypes: ['TELEPHONY'] });
    const before = mockCampaigns.length;
    await flush(mockApi.campaigns.remove('cmp-removable'));
    expect(mockCampaigns.length).toBe(before - 1);
  });

  it('remove is a no-op for an unknown campaign id', async () => {
    const before = mockCampaigns.length;
    await flush(mockApi.campaigns.remove('does-not-exist'));
    expect(mockCampaigns.length).toBe(before);
  });

  it('start sets status to Running when the campaign exists', async () => {
    const known = mockCampaigns[0];
    const result = await flush(mockApi.campaigns.start(known.id));
    expect(result).toEqual({ id: known.id, state: 'Running' });
    expect(known.status).toBe('Running');
  });

  it('start is a no-op mutation but still returns Running for an unknown id', async () => {
    const result = await flush(mockApi.campaigns.start('does-not-exist'));
    expect(result).toEqual({ id: 'does-not-exist', state: 'Running' });
  });

  it('stop sets status to Stopped when the campaign exists', async () => {
    const known = mockCampaigns[0];
    const result = await flush(mockApi.campaigns.stop(known.id));
    expect(result).toEqual({ id: known.id, state: 'Stopped' });
    expect(known.status).toBe('Stopped');
  });

  it('stop still returns Stopped for an unknown id', async () => {
    const result = await flush(mockApi.campaigns.stop('does-not-exist'));
    expect(result).toEqual({ id: 'does-not-exist', state: 'Stopped' });
  });

  it('pause sets status to Paused when the campaign exists', async () => {
    const known = mockCampaigns[0];
    const result = await flush(mockApi.campaigns.pause(known.id));
    expect(result).toEqual({ id: known.id, state: 'Paused' });
    expect(known.status).toBe('Paused');
  });

  it('pause still returns Paused for an unknown id', async () => {
    const result = await flush(mockApi.campaigns.pause('does-not-exist'));
    expect(result).toEqual({ id: 'does-not-exist', state: 'Paused' });
  });

  it('resume sets status to Running when the campaign exists', async () => {
    const known = mockCampaigns[0];
    const result = await flush(mockApi.campaigns.resume(known.id));
    expect(result).toEqual({ id: known.id, state: 'Running' });
    expect(known.status).toBe('Running');
  });

  it('resume still returns Running for an unknown id', async () => {
    const result = await flush(mockApi.campaigns.resume('does-not-exist'));
    expect(result).toEqual({ id: 'does-not-exist', state: 'Running' });
  });

  it('queues/contactFlows/phoneNumbers return their fixture arrays', async () => {
    const queues = await flush(mockApi.campaigns.queues());
    const flows = await flush(mockApi.campaigns.contactFlows());
    const phones = await flush(mockApi.campaigns.phoneNumbers());
    expect(queues.queues.length).toBeGreaterThan(0);
    expect(flows.contactFlows.length).toBeGreaterThan(0);
    expect(phones.phoneNumbers.length).toBeGreaterThan(0);
  });
});

describe('mockApi.profiles', () => {
  it('search delegates to searchProfilesMock', async () => {
    const known = mockProfiles[0];
    const result = await flush(mockApi.profiles.search({ key: 'name', value: known.firstName! }));
    expect(result.count).toBeGreaterThan(0);
  });

  it('batchGet returns only the requested profile ids', async () => {
    const known = mockProfiles[0];
    const result = await flush(mockApi.profiles.batchGet({ profileIds: [known.profileId] }));
    expect(result.profiles).toHaveLength(1);
    expect(result.errors).toEqual([]);
  });

  it('get returns the matching profile when found', async () => {
    const known = mockProfiles[0];
    const result = await flush(mockApi.profiles.get(known.profileId));
    expect(result.profile.profileId).toBe(known.profileId);
  });

  it('get falls back to the first profile when the id is unknown', async () => {
    const result = await flush(mockApi.profiles.get('does-not-exist'));
    expect(result.profile.profileId).toBe(mockProfiles[0].profileId);
  });

  it('listObjects returns a fixed leads-data-mapping shape echoing the id', async () => {
    const result = await flush(mockApi.profiles.listObjects('p-1'));
    expect(result.profileId).toBe('p-1');
    expect(result.objects).toHaveLength(2);
  });

  it('listCalculatedAttributes returns a fixed shape echoing the id', async () => {
    const result = await flush(mockApi.profiles.listCalculatedAttributes('p-1'));
    expect(result.profileId).toBe('p-1');
    expect(result.calculatedAttributes.length).toBeGreaterThan(0);
  });
});

describe('mockApi.leads.distinctValues', () => {
  it('returns attempt values for field="attempt"', async () => {
    const result = await flush(mockApi.leads.distinctValues('attempt'));
    expect(result.values).toEqual(['0', '1', '2', '3', '4']);
    expect(result.truncated).toBe(false);
  });

  it('returns group values for field="groups"', async () => {
    const result = await flush(mockApi.leads.distinctValues('groups'));
    expect(result.values.length).toBeGreaterThan(10);
  });

  it('returns an empty list for any other field', async () => {
    const result = await flush(mockApi.leads.distinctValues('unknown-field'));
    expect(result.values).toEqual([]);
    expect(result.truncated).toBe(false);
  });
});

describe('mockApi.previewCount', () => {
  it('returns a fixed preview count', async () => {
    const result = await flush(mockApi.previewCount({ segmentGroups: {} }));
    expect(result).toEqual({ redisCount: 1298, segmentCount: 1250 });
  });
});

describe('mockApi.sms', () => {
  it('listNumbers returns an empty list', async () => {
    expect(await mockApi.sms.listNumbers()).toEqual({ originationNumbers: [] });
  });

  it('getSmsRuns returns an empty run list', async () => {
    expect(await mockApi.sms.getSmsRuns('p-1')).toEqual({ runs: [] });
  });
});

describe('mockApi.brandedMonitor', () => {
  it('getTodaySummary returns a zeroed-out summary for today', async () => {
    const result = await mockApi.brandedMonitor.getTodaySummary();
    expect(result.total).toBe(0);
    expect(result.date).toBe(new Date().toISOString().slice(0, 10));
  });

  it('getCampaignMetrics returns an empty metrics array', async () => {
    expect(await mockApi.brandedMonitor.getCampaignMetrics('c-1')).toEqual({ campaignId: '', metrics: [] });
  });

  it('getAgentRoster returns an empty roster shape', async () => {
    const result = await mockApi.brandedMonitor.getAgentRoster();
    expect(result.agents).toEqual([]);
    expect(result.routingProfiles).toEqual([]);
  });

  it('getHistory returns an empty history shape', async () => {
    expect(await mockApi.brandedMonitor.getHistory('p-1')).toEqual({ planId: '', days: 30, history: [] });
  });
});

describe('mockApi.audit', () => {
  it('list returns all entries when no filters are given', async () => {
    const result = await flush(mockApi.audit.list());
    expect(result.count).toBe(mockAudit.length);
  });

  it('list filters by action', async () => {
    const result = await flush(mockApi.audit.list({ action: 'start' }));
    expect(result.entries.every((e) => e.action === 'start')).toBe(true);
    expect(result.entries.length).toBeGreaterThan(0);
  });

  it('list filters by entityType', async () => {
    const result = await flush(mockApi.audit.list({ entityType: 'segment' }));
    expect(result.entries.every((e) => e.entityType === 'segment')).toBe(true);
    expect(result.entries.length).toBeGreaterThan(0);
  });

  it('list filters by actor matching actorEmail', async () => {
    const result = await flush(mockApi.audit.list({ actor: 'ops@vipmedical.com' }));
    expect(result.entries.length).toBeGreaterThan(0);
    expect(result.entries.every((e) => e.actorEmail?.includes('ops@vipmedical.com'))).toBe(true);
  });

  it('list filters by actor matching actorSub when actorEmail is absent', async () => {
    mockAudit.push({
      entityId: 'test/synthetic-entry',
      action: 'synthetic',
      actorSub: 'sub-synthetic-123',
      timestamp: new Date().toISOString(),
    });
    const result = await flush(mockApi.audit.list({ actor: 'sub-synthetic-123' }));
    expect(result.entries.some((e) => e.entityId === 'test/synthetic-entry')).toBe(true);
  });

  it('entityHistory returns only entries for the given entityId', async () => {
    const known = mockAudit[0];
    const result = await flush(mockApi.audit.entityHistory(known.entityId));
    expect(result.entries.every((e) => e.entityId === known.entityId)).toBe(true);
    expect(result.entries.length).toBeGreaterThan(0);
  });
});

describe('mockApi.plans — implemented subset', () => {
  it('list returns an empty plan list', async () => {
    expect(await mockApi.plans.list()).toEqual({ plans: [] });
  });

  it('create returns a new plan echoing name/buckets with a generated id', async () => {
    const result = await mockApi.plans.create({ name: 'Plan A', buckets: [] });
    expect(result.name).toBe('Plan A');
    expect(result.planId).toBeTruthy();
  });

  it('update echoes id/name/buckets with fallbacks for missing fields', async () => {
    const withBody = await mockApi.plans.update('p-1', { name: 'Plan B', buckets: [] });
    expect(withBody).toMatchObject({ planId: 'p-1', name: 'Plan B', buckets: [] });

    const withoutBody = await mockApi.plans.update('p-2', {});
    expect(withoutBody).toMatchObject({ planId: 'p-2', name: '', buckets: [] });
  });

  it('duplicate returns a new plan with the given name and empty buckets', async () => {
    const result = await mockApi.plans.duplicate('p-1', 'Plan A copy');
    expect(result.name).toBe('Plan A copy');
    expect(result.buckets).toEqual([]);
  });

  it('delete resolves to undefined', async () => {
    expect(await mockApi.plans.delete('p-1')).toBeUndefined();
  });

  it('listRuns/listRunsV2/getBrandedHistoryV2/listTemplates/listV2 return empty collections', async () => {
    expect(await mockApi.plans.listRuns('p-1')).toEqual({ runs: [] });
    expect(await mockApi.plans.listRunsV2('p-1')).toEqual({ runs: [] });
    expect(await mockApi.plans.getBrandedHistoryV2('p-1')).toEqual({ history: [] });
    expect(await mockApi.plans.listTemplates()).toEqual({ plans: [] });
    expect(await mockApi.plans.listV2()).toEqual({ plans: [] });
  });

  it('getLocationMapping returns an empty groups array', async () => {
    expect(await mockApi.plans.getLocationMapping()).toEqual({ groups: [] });
  });

  it('resolveCampaignFlow always defers to the client-side heuristic (arn: null)', async () => {
    expect(await mockApi.plans.resolveCampaignFlow(['NY', 'NJ'])).toEqual({ arn: null });
  });
});

describe('mockApi.plans — not-implemented subset (preview mode has no backend for these)', () => {
  const notImplemented: Array<[string, () => Promise<unknown>]> = [
    ['get', () => mockApi.plans.get('p-1')],
    ['triggerRun', () => mockApi.plans.triggerRun('p-1')],
    ['getRun', () => mockApi.plans.getRun('p-1', 'r-1')],
    ['abortRun', () => mockApi.plans.abortRun('p-1', 'r-1')],
    ['getV2', () => mockApi.plans.getV2('p-1')],
    ['createV2', () => mockApi.plans.createV2({ name: 'x', trigger: { type: 'manual' }, buckets: [] })],
    ['updateV2', () => mockApi.plans.updateV2('p-1', {})],
    ['triggerRunV2', () => mockApi.plans.triggerRunV2('p-1')],
    ['getRunV2', () => mockApi.plans.getRunV2('p-1', 'r-1')],
    ['abortRunV2', () => mockApi.plans.abortRunV2('p-1', 'r-1')],
    ['forceFinishRunV2', () => mockApi.plans.forceFinishRunV2('p-1', 'r-1')],
    ['forceStartBucketV2', () => mockApi.plans.forceStartBucketV2('p-1', 'r-1', 0)],
    ['forceStopBucketV2', () => mockApi.plans.forceStopBucketV2('p-1', 'r-1', 0)],
    ['forceStartCampaignV2', () => mockApi.plans.forceStartCampaignV2('p-1', 'r-1', 0, 0)],
    ['forceStopCampaignV2', () => mockApi.plans.forceStopCampaignV2('p-1', 'r-1', 0, 0)],
    ['skipCampaignV2', () => mockApi.plans.skipCampaignV2('p-1', 'r-1', 0, 0)],
    ['applySnapshotV2', () => mockApi.plans.applySnapshotV2('p-1', 'r-1')],
    ['getBrandedProgressV2', () => mockApi.plans.getBrandedProgressV2('p-1', 'r-1')],
    ['getBrandedQueueV2', () => mockApi.plans.getBrandedQueueV2('p-1', 'r-1')],
    ['cloneTemplate', () => mockApi.plans.cloneTemplate('t-1')],
  ];

  for (const [name, run] of notImplemented) {
    it(`${name} rejects with "Not implemented in mock"`, async () => {
      await expect(run()).rejects.toThrow(/Not implemented in mock/);
    });
  }
});

describe('mockApi.contacts', () => {
  it('getArtifacts rejects with "Not implemented in mock"', async () => {
    await expect(mockApi.contacts.getArtifacts('ct-1')).rejects.toThrow(/Not implemented in mock/);
  });
});
