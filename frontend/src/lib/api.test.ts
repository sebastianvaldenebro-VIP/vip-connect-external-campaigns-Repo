import { beforeEach, describe, expect, it, vi } from 'vitest';

const mockConfig = vi.hoisted(() => ({
  previewMode: false,
  api: { baseUrl: 'https://api.example.test' },
}));

const getIdToken = vi.hoisted(() => vi.fn());

vi.mock('./config', () => ({ config: mockConfig }));
vi.mock('./auth', () => ({ getIdToken }));

import { ApiRequestError, api } from './api';
import type {
  CreateCampaignBody,
  PlanTrigger,
} from './api';

const jsonResponse = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });

const emptyOkResponse = () => new Response(null, { status: 204 });

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  mockConfig.previewMode = false;
  mockConfig.api.baseUrl = 'https://api.example.test';
  getIdToken.mockReset();
  getIdToken.mockResolvedValue('fake-id-token');
  fetchMock = vi.fn(async () => jsonResponse({ ok: true }));
  vi.stubGlobal('fetch', fetchMock);
});

// ── request()/buildUrl() internals, exercised through a representative endpoint ──

describe('request() — configuration guards', () => {
  it('throws ApiRequestError(status 0) when VITE_API_BASE_URL is not configured', async () => {
    mockConfig.api.baseUrl = '';
    await expect(api.segments.list()).rejects.toMatchObject({
      status: 0,
      message: 'VITE_API_BASE_URL is not configured',
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('throws ApiRequestError(status 401) when there is no id token', async () => {
    getIdToken.mockResolvedValue(null);
    await expect(api.segments.list()).rejects.toMatchObject({
      status: 401,
      message: 'Not authenticated',
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('errors are instances of both ApiRequestError and Error, with the right name', async () => {
    getIdToken.mockResolvedValue(null);
    try {
      await api.segments.list();
      throw new Error('should have thrown');
    } catch (e) {
      expect(e).toBeInstanceOf(ApiRequestError);
      expect(e).toBeInstanceOf(Error);
      expect((e as ApiRequestError).name).toBe('ApiRequestError');
    }
  });
});

describe('request() — response handling', () => {
  it('returns undefined for a 204 No Content response', async () => {
    fetchMock.mockResolvedValueOnce(emptyOkResponse());
    const result = await api.segments.remove('seg-1');
    expect(result).toBeUndefined();
  });

  it('treats an empty 200 body as {}', async () => {
    fetchMock.mockResolvedValueOnce(new Response('', { status: 200 }));
    const result = await api.segments.list();
    expect(result).toEqual({});
  });

  it('parses a non-empty JSON body on success', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ segments: [], nextToken: 'nt-1' }));
    const result = await api.segments.list();
    expect(result).toEqual({ segments: [], nextToken: 'nt-1' });
  });

  it('sends the Authorization bearer token and JSON content type', async () => {
    await api.segments.list();
    const [, init] = fetchMock.mock.calls[0];
    expect(init.headers.Authorization).toBe('Bearer fake-id-token');
    expect(init.headers['Content-Type']).toBe('application/json');
  });

  it('serializes the request body as JSON when present, omits it otherwise', async () => {
    await api.segments.list();
    expect(fetchMock.mock.calls[0][1].body).toBeUndefined();

    fetchMock.mockClear();
    await api.segments.create({
      name: 'seg-x',
      displayName: 'Seg X',
      segmentGroups: {},
      syncMode: 'manual',
    });
    expect(fetchMock.mock.calls[0][1].body).toBe(
      JSON.stringify({ name: 'seg-x', displayName: 'Seg X', segmentGroups: {}, syncMode: 'manual' }),
    );
  });

  it('defaults the HTTP method to GET when unspecified', async () => {
    await api.segments.list();
    expect(fetchMock.mock.calls[0][1].method).toBe('GET');
  });
});

describe('request() — error payload parsing', () => {
  it('uses the nested error.message/code/details when payload.error is an object', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        { error: { message: 'Segment not found', code: 'NOT_FOUND', details: { id: 'seg-1' } } },
        404,
      ),
    );
    await expect(api.segments.get('seg-1')).rejects.toMatchObject({
      status: 404,
      code: 'NOT_FOUND',
      message: 'Segment not found',
      details: { id: 'seg-1' },
    });
  });

  it('treats the whole payload as the error object when payload.error is absent', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ message: 'Boom, no wrapper' }, 500));
    await expect(api.segments.get('seg-1')).rejects.toMatchObject({
      status: 500,
      message: 'Boom, no wrapper',
      code: undefined,
    });
  });

  it('falls back to "HTTP <status>" when no message string is present', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({}, 503));
    await expect(api.segments.get('seg-1')).rejects.toMatchObject({
      status: 503,
      message: 'HTTP 503',
    });
  });

  it('leaves code undefined when err.code is not a string', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ message: 'x', code: 42 }, 400));
    await expect(api.segments.get('seg-1')).rejects.toMatchObject({ code: undefined });
  });

  it('leaves details undefined when err.details is not an object (e.g. a string)', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ message: 'x', details: 'not an object' }, 400));
    await expect(api.segments.get('seg-1')).rejects.toMatchObject({ details: undefined });
  });

  it('captures details when err.details is an object, even with payload.error unwrapped', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ error: { message: 'x', details: { field: 'name' } } }, 422),
    );
    await expect(api.segments.get('seg-1')).rejects.toMatchObject({ details: { field: 'name' } });
  });
});

describe('buildUrl() — query string construction (via campaigns.list/audit.list)', () => {
  it('omits the query string entirely when no query object is passed', async () => {
    await api.plans.list();
    const url = new URL(fetchMock.mock.calls[0][0] as string);
    expect(url.search).toBe('');
  });

  it('includes defined string/number params and skips undefined/null/empty-string ones', async () => {
    await api.audit.list({
      actor: '',
      action: 'start',
      entityType: undefined,
      limit: 5,
      nextToken: undefined,
    });
    const url = new URL(fetchMock.mock.calls[0][0] as string);
    expect(url.searchParams.get('actor')).toBeNull();
    expect(url.searchParams.get('entityType')).toBeNull();
    expect(url.searchParams.get('nextToken')).toBeNull();
    expect(url.searchParams.get('action')).toBe('start');
    expect(url.searchParams.get('limit')).toBe('5');
  });

  it('trims a trailing slash from the configured base URL', async () => {
    mockConfig.api.baseUrl = 'https://api.example.test/';
    await api.plans.list();
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toBe('https://api.example.test/plans');
  });
});

// ── Full sweep: every realApi method, verifying HTTP method + path wiring ──────

type Case = {
  name: string;
  run: () => Promise<unknown>;
  method: string;
  urlIncludes: string;
};

const cases: Case[] = [
  { name: 'segments.list', run: () => api.segments.list(), method: 'GET', urlIncludes: '/segments' },
  { name: 'segments.list(query)', run: () => api.segments.list({ maxResults: 10 }), method: 'GET', urlIncludes: '/segments?maxResults=10' },
  { name: 'segments.get', run: () => api.segments.get('seg-1'), method: 'GET', urlIncludes: '/segments/seg-1' },
  {
    name: 'segments.create',
    run: () => api.segments.create({ name: 'seg-x', displayName: 'Seg X', segmentGroups: {}, syncMode: 'manual' }),
    method: 'POST',
    urlIncludes: '/segments',
  },
  { name: 'segments.remove', run: () => api.segments.remove('seg-1'), method: 'DELETE', urlIncludes: '/segments/seg-1' },
  { name: 'segments.createEstimate', run: () => api.segments.createEstimate('seg-1'), method: 'POST', urlIncludes: '/segments/seg-1/estimate' },
  { name: 'segments.getEstimate', run: () => api.segments.getEstimate('seg-1', 'est-1'), method: 'GET', urlIncludes: '/segments/seg-1/estimate/est-1' },
  { name: 'segments.createSnapshot', run: () => api.segments.createSnapshot('seg-1'), method: 'POST', urlIncludes: '/segments/seg-1/snapshot' },
  { name: 'segments.createSnapshot(body)', run: () => api.segments.createSnapshot('seg-1', { dataFormat: 'CSV' }), method: 'POST', urlIncludes: '/segments/seg-1/snapshot' },
  { name: 'segments.getSnapshot', run: () => api.segments.getSnapshot('seg-1', 'snap-1'), method: 'GET', urlIncludes: '/segments/seg-1/snapshot/snap-1' },
  { name: 'segments.updateSyncMode', run: () => api.segments.updateSyncMode('seg-1', 'live'), method: 'PATCH', urlIncludes: '/segments/seg-1' },
  { name: 'segments.verify', run: () => api.segments.verify('seg-1'), method: 'POST', urlIncludes: '/segments/seg-1/verify' },
  { name: 'segments.startExtrasDetection', run: () => api.segments.startExtrasDetection('seg-1'), method: 'POST', urlIncludes: '/segments/seg-1/verify/extras' },
  { name: 'segments.getExtrasDetection', run: () => api.segments.getExtrasDetection('seg-1', 'snap-1'), method: 'GET', urlIncludes: '/segments/seg-1/verify/extras/snap-1' },
  { name: 'segments.reconcile', run: () => api.segments.reconcile('seg-1'), method: 'POST', urlIncludes: '/segments/seg-1/reconcile' },
  { name: 'segments.diagnose', run: () => api.segments.diagnose('seg-1'), method: 'POST', urlIncludes: '/segments/seg-1/diagnose' },

  { name: 'campaigns.list', run: () => api.campaigns.list(), method: 'GET', urlIncludes: '/campaigns' },
  { name: 'campaigns.get', run: () => api.campaigns.get('c-1'), method: 'GET', urlIncludes: '/campaigns/c-1' },
  {
    name: 'campaigns.create',
    run: () =>
      api.campaigns.create({
        name: 'Test Campaign',
        queueId: 'q-1',
        contactFlowId: 'cf-1',
        sourcePhoneNumber: '+15550000000',
        dialer: { type: 'progressive' },
        schedule: { startTime: '2026-01-01T00:00:00Z', endTime: '2026-01-01T01:00:00Z' },
      } satisfies CreateCampaignBody),
    method: 'POST',
    urlIncludes: '/campaigns',
  },
  { name: 'campaigns.update', run: () => api.campaigns.update('c-1', { name: 'Renamed' }), method: 'PATCH', urlIncludes: '/campaigns/c-1' },
  { name: 'campaigns.remove', run: () => api.campaigns.remove('c-1'), method: 'DELETE', urlIncludes: '/campaigns/c-1' },
  { name: 'campaigns.start', run: () => api.campaigns.start('c-1'), method: 'POST', urlIncludes: '/campaigns/c-1/start' },
  { name: 'campaigns.stop', run: () => api.campaigns.stop('c-1'), method: 'POST', urlIncludes: '/campaigns/c-1/stop' },
  { name: 'campaigns.pause', run: () => api.campaigns.pause('c-1'), method: 'POST', urlIncludes: '/campaigns/c-1/pause' },
  { name: 'campaigns.resume', run: () => api.campaigns.resume('c-1'), method: 'POST', urlIncludes: '/campaigns/c-1/resume' },
  { name: 'campaigns.queues', run: () => api.campaigns.queues(), method: 'GET', urlIncludes: '/campaigns/resources/queues' },
  { name: 'campaigns.contactFlows', run: () => api.campaigns.contactFlows(), method: 'GET', urlIncludes: '/campaigns/resources/contact-flows' },
  { name: 'campaigns.contactFlows(types)', run: () => api.campaigns.contactFlows(['CAMPAIGN']), method: 'GET', urlIncludes: 'types=CAMPAIGN' },
  { name: 'campaigns.phoneNumbers', run: () => api.campaigns.phoneNumbers(), method: 'GET', urlIncludes: '/campaigns/resources/phone-numbers' },

  { name: 'profiles.search', run: () => api.profiles.search({ key: 'phone', value: '+15550000000' }), method: 'GET', urlIncludes: '/profiles/search' },
  { name: 'profiles.batchGet', run: () => api.profiles.batchGet({ profileIds: ['p-1'] }), method: 'POST', urlIncludes: '/profiles/batch' },
  { name: 'profiles.get', run: () => api.profiles.get('p-1'), method: 'GET', urlIncludes: '/profiles/p-1' },
  { name: 'profiles.listObjects', run: () => api.profiles.listObjects('p-1'), method: 'GET', urlIncludes: '/profiles/p-1/objects' },
  { name: 'profiles.listObjects(query)', run: () => api.profiles.listObjects('p-1', { objectType: 'leads', max: 5 }), method: 'GET', urlIncludes: '/profiles/p-1/objects' },
  { name: 'profiles.listCalculatedAttributes', run: () => api.profiles.listCalculatedAttributes('p-1'), method: 'GET', urlIncludes: '/profiles/p-1/calculated-attributes' },

  { name: 'leads.distinctValues (default max)', run: () => api.leads.distinctValues('groups'), method: 'GET', urlIncludes: 'max=200' },
  { name: 'leads.distinctValues (explicit max)', run: () => api.leads.distinctValues('attempt', 50), method: 'GET', urlIncludes: 'max=50' },

  { name: 'plans.list', run: () => api.plans.list(), method: 'GET', urlIncludes: '/plans' },
  { name: 'plans.get', run: () => api.plans.get('p-1'), method: 'GET', urlIncludes: '/plans/p-1' },
  { name: 'plans.create', run: () => api.plans.create({ name: 'Plan 1', buckets: [] }), method: 'POST', urlIncludes: '/plans' },
  { name: 'plans.update', run: () => api.plans.update('p-1', { name: 'Plan 1b' }), method: 'PUT', urlIncludes: '/plans/p-1' },
  { name: 'plans.duplicate', run: () => api.plans.duplicate('p-1', 'Plan 1 copy'), method: 'POST', urlIncludes: '/plans' },
  { name: 'plans.delete', run: () => api.plans.delete('p-1'), method: 'DELETE', urlIncludes: '/plans/p-1' },
  { name: 'plans.triggerRun', run: () => api.plans.triggerRun('p-1'), method: 'POST', urlIncludes: '/plans/p-1/runs' },
  { name: 'plans.listRuns', run: () => api.plans.listRuns('p-1'), method: 'GET', urlIncludes: '/plans/p-1/runs' },
  { name: 'plans.getRun', run: () => api.plans.getRun('p-1', 'r-1'), method: 'GET', urlIncludes: '/plans/p-1/runs/r-1' },
  { name: 'plans.abortRun', run: () => api.plans.abortRun('p-1', 'r-1'), method: 'POST', urlIncludes: '/plans/p-1/runs/r-1/abort' },

  { name: 'plans.listV2', run: () => api.plans.listV2(), method: 'GET', urlIncludes: '/plans' },
  { name: 'plans.getV2', run: () => api.plans.getV2('p-1'), method: 'GET', urlIncludes: '/plans/p-1' },
  {
    name: 'plans.createV2',
    run: () =>
      api.plans.createV2({
        name: 'Plan V2',
        trigger: { type: 'manual' } satisfies PlanTrigger,
        buckets: [],
      }),
    method: 'POST',
    urlIncludes: '/plans',
  },
  { name: 'plans.updateV2', run: () => api.plans.updateV2('p-1', { name: 'Plan V2b' }), method: 'PUT', urlIncludes: '/plans/p-1' },
  { name: 'plans.triggerRunV2 (no bucket index)', run: () => api.plans.triggerRunV2('p-1'), method: 'POST', urlIncludes: '/plans/p-1/runs' },
  { name: 'plans.triggerRunV2 (with bucket index)', run: () => api.plans.triggerRunV2('p-1', 2), method: 'POST', urlIncludes: '/plans/p-1/runs' },
  { name: 'plans.listRunsV2', run: () => api.plans.listRunsV2('p-1'), method: 'GET', urlIncludes: '/plans/p-1/runs' },
  { name: 'plans.getRunV2', run: () => api.plans.getRunV2('p-1', 'r-1'), method: 'GET', urlIncludes: '/plans/p-1/runs/r-1' },
  { name: 'plans.abortRunV2', run: () => api.plans.abortRunV2('p-1', 'r-1'), method: 'POST', urlIncludes: '/plans/p-1/runs/r-1/abort' },
  { name: 'plans.forceFinishRunV2', run: () => api.plans.forceFinishRunV2('p-1', 'r-1'), method: 'POST', urlIncludes: '/plans/p-1/runs/r-1/force-finish' },
  { name: 'plans.forceStartBucketV2', run: () => api.plans.forceStartBucketV2('p-1', 'r-1', 0), method: 'POST', urlIncludes: '/plans/p-1/runs/r-1/buckets/0/force-start' },
  { name: 'plans.forceStopBucketV2', run: () => api.plans.forceStopBucketV2('p-1', 'r-1', 0), method: 'POST', urlIncludes: '/plans/p-1/runs/r-1/buckets/0/force-stop' },
  { name: 'plans.forceStartCampaignV2', run: () => api.plans.forceStartCampaignV2('p-1', 'r-1', 0, 1), method: 'POST', urlIncludes: '/plans/p-1/runs/r-1/buckets/0/campaigns/1/force-start' },
  { name: 'plans.forceStopCampaignV2', run: () => api.plans.forceStopCampaignV2('p-1', 'r-1', 0, 1), method: 'POST', urlIncludes: '/plans/p-1/runs/r-1/buckets/0/campaigns/1/force-stop' },
  { name: 'plans.skipCampaignV2', run: () => api.plans.skipCampaignV2('p-1', 'r-1', 0, 1), method: 'POST', urlIncludes: '/plans/p-1/runs/r-1/buckets/0/campaigns/1/skip' },
  { name: 'plans.applySnapshotV2', run: () => api.plans.applySnapshotV2('p-1', 'r-1'), method: 'POST', urlIncludes: '/plans/p-1/runs/r-1/apply-snapshot' },
  { name: 'plans.getBrandedProgressV2', run: () => api.plans.getBrandedProgressV2('p-1', 'r-1'), method: 'GET', urlIncludes: '/plans/p-1/runs/r-1/branded-progress' },
  { name: 'plans.getBrandedQueueV2', run: () => api.plans.getBrandedQueueV2('p-1', 'r-1'), method: 'GET', urlIncludes: '/plans/p-1/runs/r-1/branded-queue' },
  { name: 'plans.getBrandedHistoryV2', run: () => api.plans.getBrandedHistoryV2('p-1'), method: 'GET', urlIncludes: '/plans/p-1/branded-history' },
  { name: 'plans.listTemplates', run: () => api.plans.listTemplates(), method: 'GET', urlIncludes: '/templates' },
  { name: 'plans.cloneTemplate (no body)', run: () => api.plans.cloneTemplate('t-1'), method: 'POST', urlIncludes: '/plans/from-template/t-1' },
  { name: 'plans.cloneTemplate (with body)', run: () => api.plans.cloneTemplate('t-1', { name: 'Cloned' }), method: 'POST', urlIncludes: '/plans/from-template/t-1' },
  { name: 'plans.getLocationMapping', run: () => api.plans.getLocationMapping(), method: 'GET', urlIncludes: '/location-mapping' },
  { name: 'plans.resolveCampaignFlow', run: () => api.plans.resolveCampaignFlow(['NY']), method: 'POST', urlIncludes: '/plans/resolve-campaign-flow' },

  { name: 'previewCount', run: () => api.previewCount({ segmentGroups: {} }), method: 'POST', urlIncludes: '/segments/preview-count' },

  { name: 'brandedMonitor.getTodaySummary (no date)', run: () => api.brandedMonitor.getTodaySummary(), method: 'GET', urlIncludes: '/metrics/branded/today' },
  { name: 'brandedMonitor.getTodaySummary (with date)', run: () => api.brandedMonitor.getTodaySummary('2026-01-01'), method: 'GET', urlIncludes: 'date=2026-01-01' },
  { name: 'brandedMonitor.getCampaignMetrics (default limit)', run: () => api.brandedMonitor.getCampaignMetrics('c-1'), method: 'GET', urlIncludes: 'limit=24' },
  { name: 'brandedMonitor.getCampaignMetrics (explicit limit)', run: () => api.brandedMonitor.getCampaignMetrics('c-1', 5), method: 'GET', urlIncludes: 'limit=5' },
  { name: 'brandedMonitor.getAgentRoster (no queueId)', run: () => api.brandedMonitor.getAgentRoster(), method: 'GET', urlIncludes: '/metrics/branded/agents' },
  { name: 'brandedMonitor.getAgentRoster (with queueId)', run: () => api.brandedMonitor.getAgentRoster('q-1'), method: 'GET', urlIncludes: 'queueId=q-1' },
  { name: 'brandedMonitor.getHistory (default days)', run: () => api.brandedMonitor.getHistory('p-1'), method: 'GET', urlIncludes: 'days=30' },
  { name: 'brandedMonitor.getHistory (explicit days)', run: () => api.brandedMonitor.getHistory('p-1', 7), method: 'GET', urlIncludes: 'days=7' },

  { name: 'sms.listNumbers', run: () => api.sms.listNumbers(), method: 'GET', urlIncludes: '/sms/numbers' },
  { name: 'sms.getSmsRuns', run: () => api.sms.getSmsRuns('p-1'), method: 'GET', urlIncludes: '/plans/p-1/sms-runs' },

  { name: 'audit.list (no query)', run: () => api.audit.list(), method: 'GET', urlIncludes: '/audit' },
  { name: 'audit.entityHistory', run: () => api.audit.entityHistory('e-1'), method: 'GET', urlIncludes: '/audit/e-1' },

  { name: 'contacts.getArtifacts', run: () => api.contacts.getArtifacts('ct-1'), method: 'GET', urlIncludes: '/contacts/ct-1/artifacts' },
];

describe('realApi — full endpoint sweep (method + path wiring)', () => {
  for (const c of cases) {
    it(`${c.name} → ${c.method} ${c.urlIncludes}`, async () => {
      await c.run();
      expect(fetchMock).toHaveBeenCalledTimes(1);
      const [url, init] = fetchMock.mock.calls[0];
      expect(init.method ?? 'GET').toBe(c.method);
      expect(String(url)).toContain(c.urlIncludes);
    });
  }
});

// ── config.previewMode swap (module-level ternary) ─────────────────────────────

describe('api export — preview mode swap', () => {
  it('binds to mockApi when config.previewMode is true at module load', async () => {
    vi.resetModules();
    mockConfig.previewMode = true;
    const { api: swappedApi } = await import('./api');
    const { mockApi } = await import('./mockApi');
    expect(swappedApi).toBe(mockApi);
  });

  it('binds to realApi when config.previewMode is false at module load', async () => {
    vi.resetModules();
    mockConfig.previewMode = false;
    const { api: swappedApi } = await import('./api');
    const { mockApi } = await import('./mockApi');
    expect(swappedApi).not.toBe(mockApi);
  });
});
