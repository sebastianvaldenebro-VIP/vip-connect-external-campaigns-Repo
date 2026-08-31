import { config } from './config';
import { getIdToken } from './auth';

export type ApiError = {
  status: number;
  code?: string;
  message: string;
  details?: unknown;
};

export class ApiRequestError extends Error implements ApiError {
  status: number;
  code?: string;
  details?: unknown;

  constructor(error: ApiError) {
    super(error.message);
    this.name = 'ApiRequestError';
    this.status = error.status;
    this.code = error.code;
    this.details = error.details;
  }
}

type RequestOptions = {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  body?: unknown;
  query?: Record<string, string | number | undefined | null>;
  signal?: AbortSignal;
};

function buildUrl(path: string, query?: RequestOptions['query']): string {
  const base = config.api.baseUrl.replace(/\/$/, '');
  const url = new URL(`${base}${path.startsWith('/') ? path : `/${path}`}`);
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined && value !== null && value !== '') {
        url.searchParams.set(key, String(value));
      }
    }
  }
  return url.toString();
}

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  if (!config.api.baseUrl) {
    throw new ApiRequestError({ status: 0, message: 'VITE_API_BASE_URL is not configured' });
  }

  const token = await getIdToken();
  if (!token) {
    throw new ApiRequestError({ status: 401, message: 'Not authenticated' });
  }

  const res = await fetch(buildUrl(path, opts.query), {
    method: opts.method ?? 'GET',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    signal: opts.signal,
  });

  if (res.status === 204) {
    return undefined as T;
  }

  const text = await res.text();
  const payload = text ? (JSON.parse(text) as Record<string, unknown>) : {};

  if (!res.ok) {
    const err = typeof payload.error === 'object' && payload.error !== null
      ? (payload.error as Record<string, unknown>)
      : payload;
    const message =
      typeof err.message === 'string' && err.message
        ? err.message
        : `HTTP ${res.status}`;
    throw new ApiRequestError({
      status: res.status,
      code: typeof err.code === 'string' ? err.code : undefined,
      message,
      details: typeof err.details === 'object' ? (err.details as Record<string, unknown>) : undefined,
    });
  }

  return payload as T;
}

// ── Domain types ─────────────────────────────────────────────────────

export type SegmentSyncMode = 'live' | 'manual';

export type SegmentSummary = {
  name: string;
  displayName?: string;
  description?: string;
  segmentArn: string;
  createdAt?: string;
  tags?: Record<string, string>;
  /** Lineage: "nj-available-leads" regardless of current version. */
  family?: string;
  version?: number;
  syncMode: SegmentSyncMode;
};

export type SegmentDetail = SegmentSummary & {
  segmentGroups?: unknown;
};

export type VerifyCustomer = {
  customerId: string;
  phone?: string;
  name?: string;
  lastSeenRedis?: string;
  status: 'missing' | 'extra';
};

export type VerifyResult = {
  segmentName: string;
  family: string;
  version: number;
  redisCount: number;
  segmentCount: number;
  /** redis − cp: should be added */
  missingCustomerIds: string[];
  /** cp − redis: in segment but no longer matches Redis */
  extraCustomerIds: string[];
  verifiedAt: string;
  /** Combined preview for the detail table. */
  sample: VerifyCustomer[];
  /** Non-fatal informational messages — e.g., legacyFilter, extrasDetectionDisabled. */
  notes?: Record<string, string>;
};

export type ExtrasDetectionStatus =
  | 'IN_PROGRESS'
  | 'COMPLETED'
  | 'FAILED'
  | string;

export type ExtrasDetectionResult = {
  snapshotId: string;
  status: ExtrasDetectionStatus;
  destinationUri?: string;
  statusMessage?: string;
  /** Only set when status === "COMPLETED". */
  cpCount?: number;
  redisCount?: number;
  totalExtras?: number;
  extraCustomerIds?: string[];
  /** Proper Redis−CP diff. Verify alone over-counts (treats redis total as
   * "missing" placeholder); once the snapshot lands we can hand back the
   * real number. */
  totalMissing?: number;
  missingCustomerIds?: string[];
  computedAt?: string;
};

export type DiagnoseEntry = {
  customerId: string;
  cpLastUpdatedAt: string | null;
  cpAttributesMatchFilter: boolean;
  cpAttributes: Record<string, string>;
  isSegmentMember: false;
};

export type DiagnoseResult = {
  diagnosedAt: string;
  segmentName: string;
  message: string;
  sampledFromRedis: number;
  nonMembersInSample: number;
  confirmedStaleCount: number;
  cpNoMatchCount: number;
  confirmedStale: DiagnoseEntry[];
  cpNoMatch: DiagnoseEntry[];
};

export type ReconcileResult = {
  /** New segment that replaced the previous one. */
  newSegmentName: string;
  newSegmentArn: string;
  newVersion: number;
  targetCount: number;
  added: number;
  removed: number;
  campaignsUpdated: string[];
  oldSegmentDeleted: boolean;
  completedAt: string;
};

export type EstimateStatus = 'IN_PROGRESS' | 'SUCCEEDED' | 'FAILED' | string;

export type SegmentEstimate = {
  estimateId: string;
  status: EstimateStatus;
  estimate?: { totalCount: number | null };
  statusCode?: string;
  message?: string;
};

export type SnapshotStatus = 'IN_PROGRESS' | 'COMPLETED' | 'FAILED' | string;

export type SegmentSnapshot = {
  snapshotId: string;
  destinationUri?: string;
  dataFormat?: string;
  status: SnapshotStatus;
  statusMessage?: string;
  /** Presigned S3 URLs for download. Only present when status === "COMPLETED". */
  downloadUrls?: string[];
};

export type CampaignSummary = {
  id: string;
  arn?: string;
  name: string;
  status?: string;
  schedule?: { startTime?: string; endTime?: string };
  source?: Record<string, unknown>;
  channelSubtypes?: string[];
};

export type CampaignDetail = {
  campaign: Record<string, unknown>;
  state?: string;
};

export type Queue = { id: string; arn: string; name: string; queueType?: string };
export type ContactFlow = { id: string; arn: string; name: string; contactFlowType?: string };
export type PhoneNumber = { arn: string; number: string; type?: string; country?: string };

export type AuditEntry = {
  entityId: string;
  entityType?: string;
  resourceId?: string;
  action: string;
  actorSub?: string;
  actorEmail?: string;
  timestamp: string;
  before?: unknown;
  after?: unknown;
  ipAddress?: string;
  userAgent?: string;
  extra?: unknown;
};

export type Profile = {
  profileId: string;
  firstName?: string | null;
  lastName?: string | null;
  email?: string | null;
  phoneNumber?: string | null;
  attributes?: Record<string, string>;
  createdAt?: string;
  lastUpdatedAt?: string;
};


export type CreateCampaignBody = {
  name: string;
  segmentArn?: string;
  queueId: string;
  contactFlowId: string;
  /** Optional in V2 — only set for campaign-flow driven dialing. Plain agent
   * dialing campaigns (the default for VIP) leave this blank. */
  campaignFlowArn?: string;
  sourcePhoneNumber: string;
  dialer: {
    type: 'predictive' | 'progressive' | 'agentless';
    bandwidthAllocation?: number;
    dialingCapacity?: number;
  };
  answerMachineDetection?: { enabled: boolean; awaitPrompt: boolean };
  schedule: { startTime: string; endTime: string };
  communicationTime?: { timezone: string };
  communicationLimits?: { perDay?: number; perWeek?: number; perMonth?: number };
  tags?: Record<string, string>;
};

export type UpdateCampaignBody = Partial<
  Pick<CreateCampaignBody, 'name' | 'segmentArn' | 'schedule'>
>;

export type BucketSegmentFilters = {
  state: string[];
  groups: string[];
  attempts: string[];
  available: string;
};

export type BucketCampaignConfig = {
  queueId: string;
  contactFlowId: string;
  sourcePhoneNumber: string;
  dialerType: string;
  bandwidthAllocation: number;
  dialingCapacity: number;
  amdEnabled: boolean;
  amdAwaitPrompt: boolean;
  campaignFlowArn?: string;
  /** Full routing queue ARN — required for deliveryType='branded' */
  queueArn?: string;
  /** EUM SMS origination number ARN — required for deliveryType='sms' */
  smsOriginationNumberArn?: string;
  /** SMS message template (≤160 chars, no PHI) — required for deliveryType='sms' */
  smsMessageTemplate?: string;
  /** Staff acknowledgment that template contains no PHI — required for deliveryType='sms' */
  phiAcknowledged?: boolean;
};

export type SmsOriginationNumber = {
  arn: string;
  phoneNumber: string;
  numberType: 'LONG_CODE' | 'SHORT_CODE' | 'TEN_DLC' | 'TOLL_FREE';
  countryCode: string;
  mps: string;
  twoWayEnabled: boolean;
  optOutListName: string;
  status: string;
};

export type SmsCampaignRunRecord = {
  planId: string;
  sk: string;
  smsCampaignId: string;
  planName: string;
  segmentName: string;
  segmentArn: string;
  messageTemplate: string;
  originationNumberArn: string;
  originationNumber: string;
  totalEnqueued: number;
  totalSent: number;
  totalFailed: number;
  totalOptedOut: number;
  status: 'RUNNING' | 'COMPLETED' | 'ABORTED';
  startedAt: string;
  completedAt?: string;
  exitReason?: string;
  pipelineVersion: string;
  createdAt: string;
  updatedAt: string;
};

export type BucketDef = {
  bucketId: string;
  name: string;
  type: 'time-based' | 'status-based';
  durationMinutes?: number;
  segmentFilters: BucketSegmentFilters;
  campaignConfig: BucketCampaignConfig;
  deleteAfter: boolean;
  /** Max reconcile attempts before applying onReconcileExhausted (spec §9.7) */
  reconcileRetryLimit?: number;
  /** What to do when reconcile retries are exhausted (spec §9.7) */
  onReconcileExhausted?: 'continue' | 'fail';
};

export type PlanSchedule = {
  enabled: boolean;
  hour: number;
  minute: number;
  timezone: string;
  days: string[];
};

export type PlanSummary = {
  planId: string;
  name: string;
  buckets: BucketDef[];
  isTemplate?: boolean;
  isDefault?: boolean;
  schedule?: PlanSchedule;
  createdAt: string;
  updatedAt?: string;
  latestRun?: PlanRun;
};

export type BucketExitReason =
  | 'completed'
  | 'stopped'
  | 'expired'
  | 'error'
  | 'skipped_empty'
  | 'reconcile_failed'
  | 'creation_failed'
  | 'cancelled'
  | 'aborted';

export type BucketState = {
  bucketId: string;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'aborted' | 'cancelled';
  exitReason?: BucketExitReason;
  errorFlag?: boolean;
  errorDetail?: string;
  segmentName?: string;
  segmentArn?: string;
  campaignId?: string;
  campaignName?: string;
  expiryTime?: string;
  startedAt?: string;
  completedAt?: string;
};

export type PlanRun = {
  planId: string;
  runId: string;
  status: 'running' | 'completed' | 'failed' | 'aborted';
  currentBucketIndex: number;
  scheduleName?: string;
  bucketStates: BucketState[];
  startedAt: string;
  completedAt?: string;
  error?: string;
};

// ── V2 types (DAG campaigns per bucket) ──────────────────────────────────────

export type CampaignRunType = 'full' | 'custom';

export type CampaignDef = {
  id: string;
  name: string;
  states: string[];
  /** Segment group strings fetched from leads distinct values, e.g. "New Lead / 3rd Attempt" */
  groups: string[];
  run_type: CampaignRunType;
  /** Minutes to run when run_type === 'custom'. If 0 or absent, falls back to daily cutoff. */
  run_duration_minutes?: number;
  dependsOn: string[];
  /** Per-campaign Connect config — overrides bucket-level campaignConfig when present. */
  campaignConfig?: BucketCampaignConfig;
  /**
   * When set, skip Redis lookup and segment auto-creation entirely.
   * The executor uses this CP segment ARN as-is — useful for testing with a hand-crafted segment.
   */
  pinnedSegmentArn?: string;
  /** Controls the Connect campaign type. Defaults to "campaign" (MANAGED). */
  deliveryType?: 'campaign' | 'journey' | 'branded' | 'sms';
  /**
   * Only include leads whose Redis `createdAt` is within this many minutes of
   * now (10-35, step 5). Undefined/null = no age filter. Evaluated directly
   * against Redis by the executor — does not apply to pinned or standalone
   * (Customer Profiles-native) segments.
   */
  maxLeadAgeMinutes?: number;
};

export type BucketDefV2 = {
  id: string;
  name: string;
  run_mode: 'time_based' | 'status_based';
  duration_minutes?: number;
  cleanup: boolean;
  prestart_next: boolean;
  /** When true, this bucket starts in parallel with the previous bucket (not after it). */
  parallel?: boolean;
  campaignConfig: BucketCampaignConfig;
  campaigns: CampaignDef[];
  reconcileRetryLimit?: number;
  onReconcileExhausted?: 'continue' | 'fail';
};

export type PlanTrigger =
  | { type: 'manual' }
  | { type: 'time'; time: string }
  | { type: 'on_plan_complete'; planId: string; repeat: boolean; afterBucket?: number; afterCampaign?: string };

export type CampaignStatus = 'queued' | 'warming' | 'running' | 'completed' | 'cancelled' | 'error' | 'expired';

export type BrandedCampaignCounts = {
  pending: number;
  dialed: number;
  total: number;
};

export type BrandedProgressResponse = {
  progress: Record<string, BrandedCampaignCounts>;
};

export type BrandedQueueItem = {
  phone_last4: string;
  status: 'PENDING' | 'DISPATCHING' | 'DIALED';
  seededAt: string;
};

export type BrandedQueueResponse = {
  items: Record<string, BrandedQueueItem[]>;
};

export type BrandedRunSummary = {
  runId: string;
  campaignId: string;
  exitReason: string;
  totalSeeded: number;
  totalDialed: number;
  startedAt: string;
  completedAt: string;
};

export type BrandedHistoryResponse = {
  history: BrandedRunSummary[];
};

export type BrandedCampaignRecord = {
  planId: string;
  sk: string;
  runId: string;
  campaignId: string;
  brandedCampaignId: string;
  planName: string;
  segmentName: string;
  segmentArn?: string;
  queueArn: string;
  sourcePhoneLast4?: string;
  status: 'RUNNING' | 'COMPLETED' | 'ABORTED' | 'ERROR';
  startedAt: string;
  completedAt?: string;
  exitReason?: string;
  segmentSize?: number;
  totalSeeded?: number;
  totalDialed?: number;
  durationSeconds?: number;
};

export type BrandedTodaySummary = {
  date: string;
  total: number;
  active: number;
  completed: number;
  contactsDialed: number;
  campaigns: BrandedCampaignRecord[];
};

export type ContactArtifacts = {
  contactId: string;
  voicemail: string | null;
  recording: string | null;
  transcript: string | null;
  expiresInSeconds: number;
};

export type BrandedMetricSnapshot = {
  brandedCampaignId: string;
  snapshotAt: string;
  planId?: string;
  contactsPlaced: number;
  contactsAnswered: number;
  contactsVoicemail: number;
  contactsBusy: number;
  contactsNoAnswer: number;
  contactsFailed?: number;
  answerRate: string;
  voicemailRate: string;
  agentsAvailable: number;
  agentsStaffed: number;
  contactsInQueue: number;
};

export type AgentRosterEntry = {
  agentId: string;
  agentName: string;
  status: string;
  statusType: string;
  effectiveStatus: 'Available' | 'On Call' | 'ACW' | 'Unavailable' | 'Offline';
  isIntentionalAbsence: boolean;
  activeContactState: string;
  statusStartTimestamp: string;
  routingProfileId: string;
  routingProfileName: string;
  contactsCount: number;
};

export type RoutingProfileSummary = { id: string; name: string };

export type CampaignState = {
  campaignId: string;
  name: string;
  status: CampaignStatus;
  connectCampaignId?: string;
  segmentName?: string;
  leadCount?: number;
  startedAt?: string;
  completedAt?: string;
  exitReason?: string;
  errorDetail?: string;
};

export type BucketStateV2 = {
  bucketId: string;
  name: string;
  status: 'queued' | 'warming' | 'running' | 'completed';
  scheduleName?: string;
  campaignStates: CampaignState[];
  startedAt?: string;
  completedAt?: string;
};

export type PlanRunV2 = {
  planId: string;
  runId: string;
  status: 'running' | 'completed' | 'failed' | 'aborted';
  currentBucketIndex: number;
  bucketStates: BucketStateV2[];
  planSnapshot?: PlanSummaryV2;
  startedAt: string;
  completedAt?: string;
  triggeredBy?: string;
  error?: string;
};

export type PlanLoop = {
  startTime?: string; // HH:MM COT — start looping from this time (optional, defaults to 00:00)
  endTime: string;    // HH:MM COT — stop looping after this time each day
};

export type PlanSummaryV2 = {
  planId: string;
  name: string;
  description?: string;
  trigger: PlanTrigger;
  loop?: PlanLoop;
  workingHours?: { days: string[]; startTime: string; endTime: string };
  isTemplate: boolean;
  is_template: boolean;
  isDefault: boolean;
  buckets: BucketDefV2[];
  createdAt: string;
  updatedAt?: string;
  latestRun?: PlanRunV2;
};

// ── API client ───────────────────────────────────────────────────────

const realApi = {
  segments: {
    list: (query?: { maxResults?: number; nextToken?: string }) =>
      request<{ segments: SegmentSummary[]; nextToken?: string }>('/segments', { query }),
    get: (id: string) => request<SegmentDetail>(`/segments/${encodeURIComponent(id)}`),
    create: (body: {
      name: string;
      displayName: string;
      description?: string;
      segmentGroups: unknown;
      syncMode: SegmentSyncMode;
      tags?: Record<string, string>;
    }) => request<SegmentSummary>('/segments', { method: 'POST', body }),
    remove: (id: string) =>
      request<void>(`/segments/${encodeURIComponent(id)}`, { method: 'DELETE' }),
    createEstimate: (id: string) =>
      request<{ estimateId: string; status: EstimateStatus }>(
        `/segments/${encodeURIComponent(id)}/estimate`,
        { method: 'POST' },
      ),
    getEstimate: (id: string, estimateId: string) =>
      request<SegmentEstimate>(
        `/segments/${encodeURIComponent(id)}/estimate/${encodeURIComponent(estimateId)}`,
      ),
    createSnapshot: (id: string, body: { dataFormat?: 'CSV' | 'JSONL' } = {}) =>
      request<{ snapshotId: string; destinationUri?: string; status: SnapshotStatus }>(
        `/segments/${encodeURIComponent(id)}/snapshot`,
        { method: 'POST', body },
      ),
    getSnapshot: (id: string, snapshotId: string) =>
      request<SegmentSnapshot>(
        `/segments/${encodeURIComponent(id)}/snapshot/${encodeURIComponent(snapshotId)}`,
      ),
    updateSyncMode: (id: string, syncMode: SegmentSyncMode) =>
      request<SegmentSummary>(`/segments/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        body: { syncMode },
      }),
    verify: (id: string) =>
      request<VerifyResult>(`/segments/${encodeURIComponent(id)}/verify`, {
        method: 'POST',
      }),
    startExtrasDetection: (id: string) =>
      request<ExtrasDetectionResult>(
        `/segments/${encodeURIComponent(id)}/verify/extras`,
        { method: 'POST' },
      ),
    getExtrasDetection: (id: string, snapshotId: string) =>
      request<ExtrasDetectionResult>(
        `/segments/${encodeURIComponent(id)}/verify/extras/${encodeURIComponent(snapshotId)}`,
      ),
    reconcile: (id: string) =>
      request<ReconcileResult>(`/segments/${encodeURIComponent(id)}/reconcile`, {
        method: 'POST',
      }),
    diagnose: (id: string) =>
      request<DiagnoseResult>(`/segments/${encodeURIComponent(id)}/diagnose`, {
        method: 'POST',
      }),
  },
  campaigns: {
    list: (query?: { maxResults?: number; nextToken?: string }) =>
      request<{ campaigns: CampaignSummary[]; nextToken?: string }>('/campaigns', { query }),
    get: (id: string) => request<CampaignDetail>(`/campaigns/${encodeURIComponent(id)}`),
    create: (body: CreateCampaignBody) =>
      request<{ id: string; arn: string }>('/campaigns', { method: 'POST', body }),
    update: (id: string, body: UpdateCampaignBody) =>
      request<{ id: string; updated: UpdateCampaignBody }>(
        `/campaigns/${encodeURIComponent(id)}`,
        { method: 'PATCH', body },
      ),
    remove: (id: string) =>
      request<void>(`/campaigns/${encodeURIComponent(id)}`, { method: 'DELETE' }),
    start: (id: string) =>
      request<{ id: string; state: string }>(
        `/campaigns/${encodeURIComponent(id)}/start`,
        { method: 'POST' },
      ),
    stop: (id: string) =>
      request<{ id: string; state: string }>(
        `/campaigns/${encodeURIComponent(id)}/stop`,
        { method: 'POST' },
      ),
    pause: (id: string) =>
      request<{ id: string; state: string }>(
        `/campaigns/${encodeURIComponent(id)}/pause`,
        { method: 'POST' },
      ),
    resume: (id: string) =>
      request<{ id: string; state: string }>(
        `/campaigns/${encodeURIComponent(id)}/resume`,
        { method: 'POST' },
      ),
    queues: () =>
      request<{ queues: Queue[] }>('/campaigns/resources/queues'),
    contactFlows: (types?: string[]) =>
      request<{ contactFlows: ContactFlow[] }>('/campaigns/resources/contact-flows', {
        query: types ? { types: types.join(',') } : undefined,
      }),
    phoneNumbers: () =>
      request<{ phoneNumbers: PhoneNumber[] }>('/campaigns/resources/phone-numbers'),
  },
  profiles: {
    search: (query: { key: string; value: string; max?: number }) =>
      request<{ profiles: Profile[]; count: number }>('/profiles/search', { query }),
    batchGet: (body: { profileIds: string[] }) =>
      request<{ profiles: Profile[]; errors: unknown[] }>('/profiles/batch', {
        method: 'POST',
        body,
      }),
    get: (id: string) => request<{ profile: Profile }>(`/profiles/${encodeURIComponent(id)}`),
    listObjects: (id: string, query?: { objectType?: string; max?: number }) =>
      request<{
        profileId: string;
        objectType: string;
        objects: unknown[];
        nextToken?: string;
      }>(`/profiles/${encodeURIComponent(id)}/objects`, { query }),
    listCalculatedAttributes: (id: string) =>
      request<{ profileId: string; calculatedAttributes: unknown[] }>(
        `/profiles/${encodeURIComponent(id)}/calculated-attributes`,
      ),
  },
  leads: {
    distinctValues: (field: string, max = 200) =>
      request<{ field: string; values: string[]; truncated: boolean }>(
        '/leads/distinct-values',
        { query: { field, max } },
      ),
  },
  plans: {
    list: () => request<{ plans: PlanSummary[] }>('/plans'),
    get: (id: string) =>
      request<{ plan: PlanSummary; latestRun?: PlanRun }>(`/plans/${encodeURIComponent(id)}`),
    create: (body: { name: string; buckets: BucketDef[]; isTemplate?: boolean }) =>
      request<PlanSummary>('/plans', { method: 'POST', body }),
    update: (id: string, body: { name?: string; buckets?: BucketDef[]; isTemplate?: boolean; schedule?: PlanSchedule; workingHours?: { days: string[]; startTime: string; endTime: string } | null }) =>
      request<PlanSummary>(`/plans/${encodeURIComponent(id)}`, { method: 'PUT', body }),
    duplicate: (id: string, name: string) =>
      request<PlanSummary>('/plans', {
        method: 'POST',
        body: { _duplicateFromId: id, name },
      }),
    delete: (id: string) =>
      request<void>(`/plans/${encodeURIComponent(id)}`, { method: 'DELETE' }),
    triggerRun: (id: string) =>
      request<PlanRun>(`/plans/${encodeURIComponent(id)}/runs`, { method: 'POST', body: {} }),
    listRuns: (id: string) =>
      request<{ runs: PlanRun[] }>(`/plans/${encodeURIComponent(id)}/runs`),
    getRun: (id: string, runId: string) =>
      request<PlanRun>(`/plans/${encodeURIComponent(id)}/runs/${encodeURIComponent(runId)}`),
    abortRun: (id: string, runId: string) =>
      request<PlanRun>(
        `/plans/${encodeURIComponent(id)}/runs/${encodeURIComponent(runId)}/abort`,
        { method: 'POST', body: {} },
      ),
    // V2 methods
    listV2: () => request<{ plans: PlanSummaryV2[] }>('/plans'),
    getV2: (id: string) =>
      request<{ plan: PlanSummaryV2; latestRun?: PlanRunV2 }>(`/plans/${encodeURIComponent(id)}`),
    createV2: (body: { name: string; description?: string; trigger: PlanTrigger; loop?: PlanLoop | null; buckets: BucketDefV2[]; isTemplate?: boolean }) =>
      request<PlanSummaryV2>('/plans', { method: 'POST', body }),
    updateV2: (id: string, body: Partial<{ name: string; description: string; trigger: PlanTrigger; loop: PlanLoop | null; workingHours: { days: string[]; startTime: string; endTime: string } | null; buckets: BucketDefV2[]; isTemplate: boolean }>) =>
      request<PlanSummaryV2>(`/plans/${encodeURIComponent(id)}`, { method: 'PUT', body }),
    triggerRunV2: (id: string, startBucketIndex?: number) =>
      request<PlanRunV2>(`/plans/${encodeURIComponent(id)}/runs`, { method: 'POST', body: startBucketIndex != null ? { startBucketIndex } : {} }),
    listRunsV2: (id: string) =>
      request<{ runs: PlanRunV2[] }>(`/plans/${encodeURIComponent(id)}/runs`),
    getRunV2: (id: string, runId: string) =>
      request<PlanRunV2>(`/plans/${encodeURIComponent(id)}/runs/${encodeURIComponent(runId)}`),
    abortRunV2: (id: string, runId: string) =>
      request<PlanRunV2>(
        `/plans/${encodeURIComponent(id)}/runs/${encodeURIComponent(runId)}/abort`,
        { method: 'POST', body: {} },
      ),
    forceFinishRunV2: (id: string, runId: string) =>
      request<PlanRunV2>(
        `/plans/${encodeURIComponent(id)}/runs/${encodeURIComponent(runId)}/force-finish`,
        { method: 'POST', body: {} },
      ),
    forceStartBucketV2: (id: string, runId: string, bucketIndex: number) =>
      request<PlanRunV2>(
        `/plans/${encodeURIComponent(id)}/runs/${encodeURIComponent(runId)}/buckets/${bucketIndex}/force-start`,
        { method: 'POST', body: {} },
      ),
    forceStopBucketV2: (id: string, runId: string, bucketIndex: number) =>
      request<PlanRunV2>(
        `/plans/${encodeURIComponent(id)}/runs/${encodeURIComponent(runId)}/buckets/${bucketIndex}/force-stop`,
        { method: 'POST', body: {} },
      ),
    forceStartCampaignV2: (id: string, runId: string, bucketIndex: number, campaignIndex: number) =>
      request<PlanRunV2>(
        `/plans/${encodeURIComponent(id)}/runs/${encodeURIComponent(runId)}/buckets/${bucketIndex}/campaigns/${campaignIndex}/force-start`,
        { method: 'POST', body: {} },
      ),
    forceStopCampaignV2: (id: string, runId: string, bucketIndex: number, campaignIndex: number) =>
      request<PlanRunV2>(
        `/plans/${encodeURIComponent(id)}/runs/${encodeURIComponent(runId)}/buckets/${bucketIndex}/campaigns/${campaignIndex}/force-stop`,
        { method: 'POST', body: {} },
      ),
    skipCampaignV2: (id: string, runId: string, bucketIndex: number, campaignIndex: number) =>
      request<PlanRunV2>(
        `/plans/${encodeURIComponent(id)}/runs/${encodeURIComponent(runId)}/buckets/${bucketIndex}/campaigns/${campaignIndex}/skip`,
        { method: 'POST', body: {} },
      ),
    applySnapshotV2: (id: string, runId: string) =>
      request<PlanRunV2>(
        `/plans/${encodeURIComponent(id)}/runs/${encodeURIComponent(runId)}/apply-snapshot`,
        { method: 'POST', body: {} },
      ),
    getBrandedProgressV2: (planId: string, runId: string) =>
      request<BrandedProgressResponse>(
        `/plans/${encodeURIComponent(planId)}/runs/${encodeURIComponent(runId)}/branded-progress`,
      ),
    getBrandedQueueV2: (planId: string, runId: string) =>
      request<BrandedQueueResponse>(
        `/plans/${encodeURIComponent(planId)}/runs/${encodeURIComponent(runId)}/branded-queue`,
      ),
    getBrandedHistoryV2: (planId: string) =>
      request<BrandedHistoryResponse>(
        `/plans/${encodeURIComponent(planId)}/branded-history`,
      ),
    listTemplates: () => request<{ plans: PlanSummaryV2[] }>('/templates'),
    cloneTemplate: (tid: string, body?: { name?: string }) =>
      request<PlanSummaryV2>(`/plans/from-template/${encodeURIComponent(tid)}`, {
        method: 'POST',
        body: body || {},
      }),
    getLocationMapping: () =>
      request<{ groups: import('./stateLocationMap').StateGroup[] }>('/location-mapping'),
    resolveCampaignFlow: (states: string[]) =>
      request<{ arn: string | null }>('/plans/resolve-campaign-flow', {
        method: 'POST',
        body: { states },
      }),
  },
  previewCount: (body: { segmentGroups: unknown }) =>
    request<{ redisCount: number; segmentCount: number | null }>(
      '/segments/preview-count',
      { method: 'POST', body },
    ),
  brandedMonitor: {
    getTodaySummary: (date?: string) =>
      request<BrandedTodaySummary>(
        `/metrics/branded/today${date ? `?date=${encodeURIComponent(date)}` : ''}`,
      ),
    getCampaignMetrics: (campaignId: string, limit = 24) =>
      request<{ campaignId: string; metrics: BrandedMetricSnapshot[] }>(
        `/metrics/branded/campaigns/${encodeURIComponent(campaignId)}/metrics?limit=${limit}`,
      ),
    getAgentRoster: (queueId?: string) =>
      request<{ agents: AgentRosterEntry[]; queueId: string; lastUpdated: string; routingProfiles: RoutingProfileSummary[] }>(
        `/metrics/branded/agents${queueId ? `?queueId=${encodeURIComponent(queueId)}` : ''}`,
      ),
    getHistory: (planId: string, days = 30) =>
      request<{ planId: string; days: number; history: BrandedCampaignRecord[] }>(
        `/metrics/branded/history?planId=${encodeURIComponent(planId)}&days=${days}`,
      ),
  },
  sms: {
    listNumbers: () =>
      request<{ originationNumbers: SmsOriginationNumber[] }>('/sms/numbers'),
    getSmsRuns: (planId: string) =>
      request<{ runs: SmsCampaignRunRecord[] }>(
        `/plans/${encodeURIComponent(planId)}/sms-runs`,
      ),
  },
  audit: {
    list: (query?: {
      actor?: string;
      action?: string;
      entityType?: string;
      limit?: number;
      nextToken?: string;
    }) =>
      request<{ entries: AuditEntry[]; nextToken?: string; count: number }>('/audit', {
        query,
      }),
    entityHistory: (entityId: string) =>
      request<{ entityId: string; entries: AuditEntry[] }>(
        `/audit/${encodeURIComponent(entityId)}`,
      ),
  },
  contacts: {
    getArtifacts: (contactId: string) =>
      request<ContactArtifacts>(`/contacts/${encodeURIComponent(contactId)}/artifacts`),
  },
};

// Swap in mock fixtures when VITE_PREVIEW_MODE=true so the UI can be reviewed
// without a deployed backend. Vite's dead-code elimination drops the mock
// module from prod bundles where the constant is false at build time.
import { mockApi } from './mockApi';

export const api: typeof realApi = config.previewMode ? mockApi : realApi;
