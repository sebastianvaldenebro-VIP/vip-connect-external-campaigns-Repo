import type { api as RealApi } from './api';
import {
  applyReconcile,
  mockAudit,
  mockCampaignDetail,
  mockCampaigns,
  mockContactFlows,
  mockPhoneNumbers,
  mockProfiles,
  mockQueues,
  mockSegmentDetail,
  mockSegments,
  runVerify,
  searchProfilesMock,
} from './mockData';

const delay = (ms: number) => new Promise((r) => setTimeout(r, ms));

export const mockApi: typeof RealApi = {
  segments: {
    list: async () => {
      await delay(250);
      return { segments: mockSegments.map((s) => ({ ...s })) };
    },
    get: async (id) => {
      await delay(200);
      const base = mockSegments.find((s) => s.name === id) ?? mockSegments[0];
      return { ...base, segmentGroups: mockSegmentDetail.segmentGroups };
    },
    create: async (body) => {
      await delay(400);
      const item = {
        name: body.name,
        family: body.name,
        version: 1,
        displayName: body.displayName,
        description: body.description,
        segmentArn: `arn:aws:profile:us-east-1:165505826690:segment/${body.name}`,
        createdAt: new Date().toISOString(),
        syncMode: body.syncMode,
      };
      mockSegments.push(item);
      return item;
    },
    remove: async (id) => {
      await delay(200);
      const idx = mockSegments.findIndex((s) => s.name === id);
      if (idx >= 0) mockSegments.splice(idx, 1);
    },
    createEstimate: async () => {
      await delay(150);
      return { estimateId: 'est-mock-0001', status: 'IN_PROGRESS' };
    },
    getEstimate: async (_id, estimateId) => {
      await delay(800);
      return {
        estimateId,
        status: 'SUCCEEDED',
        estimate: { totalCount: 4213 + Math.floor(Math.random() * 200) },
      };
    },
    createSnapshot: async () => {
      await delay(200);
      return {
        snapshotId: 'snap-mock-0001',
        destinationUri: 's3://preview-bucket/segment/20260423T120000Z/',
        status: 'IN_PROGRESS',
      };
    },
    getSnapshot: async (_id, snapshotId) => {
      await delay(900);
      return {
        snapshotId,
        status: 'SUCCEEDED',
        destinationUri: 's3://preview-bucket/segment/20260423T120000Z/',
        dataFormat: 'CSV',
      };
    },
    updateSyncMode: async (id, syncMode) => {
      await delay(250);
      const seg = mockSegments.find((s) => s.name === id);
      if (!seg) throw new Error(`Unknown segment ${id}`);
      seg.syncMode = syncMode;
      return { ...seg };
    },
    verify: async (id) => {
      await delay(1_500);
      return runVerify(id);
    },
    startExtrasDetection: async () => {
      await delay(300);
      return {
        snapshotId: 'snap-preview-' + Math.random().toString(36).slice(2, 8),
        status: 'IN_PROGRESS',
      };
    },
    getExtrasDetection: async (_id, snapshotId) => {
      await delay(600);
      return {
        snapshotId,
        status: 'COMPLETED',
        cpCount: 120,
        redisCount: 115,
        totalExtras: 5,
        extraCustomerIds: ['cust-ext-1', 'cust-ext-2', 'cust-ext-3', 'cust-ext-4', 'cust-ext-5'],
        computedAt: new Date().toISOString(),
      };
    },
    reconcile: async (id) => {
      await delay(2_200);
      return applyReconcile(id);
    },
    diagnose: async (id) => {
      await delay(1_500);
      return {
        diagnosedAt: new Date().toISOString(),
        segmentName: id,
        message: '3 of 10 sampled profiles have matching CP attributes but are NOT segment members — segment membership is stale.',
        sampledFromRedis: 10,
        nonMembersInSample: 3,
        confirmedStaleCount: 3,
        cpNoMatchCount: 0,
        confirmedStale: [
          { customerId: 'preview-001', cpLastUpdatedAt: new Date(Date.now() - 45 * 60000).toISOString(), cpAttributesMatchFilter: true, cpAttributes: { available: 'True', attempt: '1', location: 'NY - Albertson' }, isSegmentMember: false as const },
          { customerId: 'preview-002', cpLastUpdatedAt: new Date(Date.now() - 30 * 60000).toISOString(), cpAttributesMatchFilter: true, cpAttributes: { available: 'True', attempt: '1', location: 'NY - Armonk' }, isSegmentMember: false as const },
          { customerId: 'preview-003', cpLastUpdatedAt: new Date(Date.now() - 15 * 60000).toISOString(), cpAttributesMatchFilter: true, cpAttributes: { available: 'True', attempt: '1', location: 'NY - Bayside' }, isSegmentMember: false as const },
        ],
        cpNoMatch: [],
      };
    },
  },
  campaigns: {
    list: async () => {
      await delay(250);
      return { campaigns: mockCampaigns };
    },
    get: async (id) => {
      await delay(200);
      return mockCampaignDetail(id);
    },
    create: async () => {
      await delay(400);
      return {
        id: 'cmp-preview-' + Math.random().toString(36).slice(2, 8),
        arn: 'arn:aws:connect-campaigns:us-east-1:165505826690:campaign/preview',
      };
    },
    update: async (id, body) => {
      await delay(200);
      return { id, updated: body };
    },
    remove: async (id) => {
      await delay(200);
      const idx = mockCampaigns.findIndex((c) => c.id === id);
      if (idx >= 0) mockCampaigns.splice(idx, 1);
    },
    start: async (id) => {
      await delay(300);
      const c = mockCampaigns.find((x) => x.id === id);
      if (c) c.status = 'Running';
      return { id, state: 'Running' };
    },
    stop: async (id) => {
      await delay(300);
      const c = mockCampaigns.find((x) => x.id === id);
      if (c) c.status = 'Stopped';
      return { id, state: 'Stopped' };
    },
    pause: async (id) => {
      await delay(300);
      const c = mockCampaigns.find((x) => x.id === id);
      if (c) c.status = 'Paused';
      return { id, state: 'Paused' };
    },
    resume: async (id) => {
      await delay(300);
      const c = mockCampaigns.find((x) => x.id === id);
      if (c) c.status = 'Running';
      return { id, state: 'Running' };
    },
    queues: async () => {
      await delay(150);
      return { queues: mockQueues };
    },
    contactFlows: async () => {
      await delay(150);
      return { contactFlows: mockContactFlows };
    },
    phoneNumbers: async () => {
      await delay(150);
      return { phoneNumbers: mockPhoneNumbers };
    },
  },
  profiles: {
    search: async (query) => {
      await delay(300);
      const profiles = searchProfilesMock(query.key, query.value);
      return { profiles, count: profiles.length };
    },
    batchGet: async (body) => {
      await delay(250);
      const profiles = mockProfiles.filter((p) => body.profileIds.includes(p.profileId));
      return { profiles, errors: [] };
    },
    get: async (id) => {
      await delay(200);
      const profile = mockProfiles.find((p) => p.profileId === id) ?? mockProfiles[0];
      return { profile };
    },
    listObjects: async (id) => {
      await delay(250);
      return {
        profileId: id,
        objectType: 'leads-data-mapping',
        objects: [
          { ObjectId: 'obj-1', Timestamp: new Date().toISOString(), Attributes: { source: 'redis' } },
          { ObjectId: 'obj-2', Timestamp: new Date().toISOString(), Attributes: { source: 'redis' } },
        ],
      };
    },
    listCalculatedAttributes: async (id) => {
      await delay(250);
      return {
        profileId: id,
        calculatedAttributes: [
          { CalculatedAttributeName: 'total_attempts', Value: '7' },
          { CalculatedAttributeName: 'last_contact_days', Value: '2' },
        ],
      };
    },
  },
  leads: {
    distinctValues: async (field) => {
      await delay(200);
      if (field === 'attempt') {
        return { field, values: ['0', '1', '2', '3', '4'], truncated: false };
      }
      if (field === 'groups') {
        return {
          field,
          values: [
            'New Lead / 1st Attempt', 'New Lead / 2nd Attempt', 'New Lead / 3rd Attempt',
            'New Lead / 4th Attempt', 'New Lead / 5th Attempt', 'New Lead / 6th Attempt',
            'New Lead / 7th Attempt', 'New Lead / 8th Attempt', 'New Lead / 9th Attempt',
            'New Lead / 10th Attempt', 'New Lead / 11th Attempt', 'New Lead / 12th Attempt',
            'New Lead / 13th Attempt', 'New Lead / New Lead',
            'Cancellation / 1st attempt', 'Cancellation / 2nd attempt (send text)',
            'Cancellation / 3rd attempt', 'Cancellation / 4th attempt (send text)',
            'Cancellation / 5th attempt', 'Cancellation / 6th attempt (send text)',
            'Cancellation / Cancellation',
            'No Show / 1st attempt', 'No Show / 2nd attempt (send text)',
            'No Show / 3rd attempt', 'No Show / 4th attempt (send text)',
            'No Show / 5th attempt', 'No Show / 6th attempt (send text)', 'No Show / No Show',
            'Follow Up / 1st Attempt', 'Follow Up / 2nd Attempt', 'Follow Up / 3rd Attempt',
            'Follow Up / Follow Up',
            'Reschedule / 1st Attempt', 'Reschedule / 2nd Attempt', 'Reschedule / Reschedule',
          ],
          truncated: false,
        };
      }
      return { field, values: [], truncated: false };
    },
  },
  previewCount: async () => {
    await delay(600);
    return { redisCount: 1298, segmentCount: 1250 };
  },
  sms: {
    listNumbers: async () => ({ originationNumbers: [] }),
    getSmsRuns: async (_planId: string) => ({ runs: [] }),
  },
  brandedMonitor: {
    getTodaySummary: async () => ({
      date: new Date().toISOString().slice(0, 10),
      total: 0,
      active: 0,
      completed: 0,
      contactsDialed: 0,
      campaigns: [],
    }),
    getCampaignMetrics: async () => ({ campaignId: '', metrics: [] }),
    getAgentRoster: async () => ({ agents: [], queueId: '', lastUpdated: new Date().toISOString(), routingProfiles: [] }),
    getHistory: async () => ({ planId: '', days: 30, history: [] }),
  },
  audit: {
    list: async (query) => {
      await delay(300);
      let entries = [...mockAudit];
      if (query?.action) entries = entries.filter((e) => e.action === query.action);
      if (query?.entityType) entries = entries.filter((e) => e.entityType === query.entityType);
      if (query?.actor)
        entries = entries.filter(
          (e) => e.actorEmail?.includes(query.actor!) || e.actorSub?.includes(query.actor!),
        );
      return { entries, count: entries.length };
    },
    entityHistory: async (entityId) => {
      await delay(300);
      return { entityId, entries: mockAudit.filter((e) => e.entityId === entityId) };
    },
  },
  plans: {
    list: async () => ({ plans: [] }),
    get: async () => {
      throw new Error('Not implemented in mock');
    },
    create: async (body) => ({
      planId: crypto.randomUUID(),
      name: body.name,
      buckets: body.buckets,
      createdAt: new Date().toISOString(),
    }),
    update: async (id, body) => ({
      planId: id,
      name: body.name ?? '',
      buckets: body.buckets ?? [],
      createdAt: new Date().toISOString(),
    }),
    duplicate: async (_id, name) => ({
      planId: crypto.randomUUID(),
      name,
      buckets: [],
      createdAt: new Date().toISOString(),
    }),
    delete: async () => undefined,
    triggerRun: async () => {
      throw new Error('Not implemented in mock');
    },
    listRuns: async () => ({ runs: [] }),
    getRun: async () => {
      throw new Error('Not implemented in mock');
    },
    abortRun: async () => {
      throw new Error('Not implemented in mock');
    },
    listV2: async () => ({ plans: [] }),
    getV2: async () => {
      throw new Error('Not implemented in mock');
    },
    createV2: async () => {
      throw new Error('Not implemented in mock');
    },
    updateV2: async () => {
      throw new Error('Not implemented in mock');
    },
    triggerRunV2: async () => {
      throw new Error('Not implemented in mock');
    },
    listRunsV2: async () => ({ runs: [] }),
    getRunV2: async () => {
      throw new Error('Not implemented in mock');
    },
    abortRunV2: async () => {
      throw new Error('Not implemented in mock');
    },
    forceFinishRunV2: async () => {
      throw new Error('Not implemented in mock');
    },
    forceStartBucketV2: async () => {
      throw new Error('Not implemented in mock');
    },
    forceStopBucketV2: async () => {
      throw new Error('Not implemented in mock');
    },
    forceStartCampaignV2: async () => {
      throw new Error('Not implemented in mock');
    },
    forceStopCampaignV2: async () => {
      throw new Error('Not implemented in mock');
    },
    skipCampaignV2: async () => {
      throw new Error('Not implemented in mock');
    },
    applySnapshotV2: async () => {
      throw new Error('Not implemented in mock');
    },
    getBrandedProgressV2: async () => {
      throw new Error('Not implemented in mock');
    },
    getBrandedQueueV2: async () => {
      throw new Error('Not implemented in mock');
    },
    getBrandedHistoryV2: async () => ({ history: [] }),
    listTemplates: async () => ({ plans: [] }),
    cloneTemplate: async () => {
      throw new Error('Not implemented in mock');
    },
    getLocationMapping: async () => ({ groups: [] }),
    // No backend to call in preview mode — always defer to the client-side
    // suggestCampaignFlow heuristic (the intended fallback behavior).
    resolveCampaignFlow: async () => ({ arn: null }),
  },
  contacts: {
    getArtifacts: async () => {
      throw new Error('Not implemented in mock');
    },
  },
};
