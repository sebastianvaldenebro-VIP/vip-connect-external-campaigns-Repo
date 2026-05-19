import type {
  AuditEntry,
  CampaignDetail,
  CampaignSummary,
  ContactFlow,
  PhoneNumber,
  Profile,
  Queue,
  SegmentDetail,
  SegmentSummary,
  VerifyCustomer,
  VerifyResult,
} from './api';

const now = Date.now();
const minutesAgo = (m: number) => new Date(now - m * 60_000).toISOString();
const hoursAgo = (h: number) => new Date(now - h * 3_600_000).toISOString();
const daysAgo = (d: number) => new Date(now - d * 86_400_000).toISOString();

const segmentArn = (name: string) =>
  `arn:aws:profile:us-east-1:165505826690:domains/connect-domain/segment-definitions/${name}`;

export const mockSegments: SegmentSummary[] = [
  {
    name: 'nj-available-leads-v3',
    family: 'nj-available-leads',
    version: 3,
    displayName: 'NJ Available Leads',
    description: 'Leads located in NJ markets and marked available=true.',
    segmentArn: segmentArn('nj-available-leads-v3'),
    createdAt: daysAgo(12),
    syncMode: 'manual',
  },
  {
    name: 'fl-returning-no-contact-7d',
    family: 'fl-returning-no-contact-7d',
    version: 1,
    displayName: 'FL Returning — No contact 7d',
    description: 'Florida leads who were attempted but not reached in the last 7 days.',
    segmentArn: segmentArn('fl-returning-no-contact-7d'),
    createdAt: daysAgo(6),
    syncMode: 'live',
  },
  {
    name: 'tx-high-intent-v2',
    family: 'tx-high-intent',
    version: 2,
    displayName: 'TX High-intent',
    description: 'Texas leads flagged as high-intent by the scoring model.',
    segmentArn: segmentArn('tx-high-intent-v2'),
    createdAt: daysAgo(2),
    syncMode: 'manual',
  },
];

export const mockSegmentDetail: SegmentDetail = {
  ...mockSegments[0],
  segmentGroups: {
    include: 'ALL',
    groups: [
      {
        type: 'ALL',
        dimensions: [
          {
            profileAttributes: {
              attributes: {
                location: { dimensionType: 'BEGINS_WITH', values: ['NJ -'] },
                available: { dimensionType: 'EQUAL', values: ['true'] },
              },
            },
          },
        ],
      },
    ],
  },
};

export const mockCampaigns: CampaignSummary[] = [
  {
    id: 'cmp-01HXY9',
    name: 'NJ morning dial — 2026-04-23',
    status: 'Running',
    channelSubtypes: ['TELEPHONY'],
    schedule: { startTime: hoursAgo(2), endTime: hoursAgo(-6) },
    source: { customerProfilesSegmentArn: mockSegments[0].segmentArn },
  },
  {
    id: 'cmp-01HXY7',
    name: 'FL re-engagement',
    status: 'Paused',
    channelSubtypes: ['TELEPHONY'],
    schedule: { startTime: hoursAgo(26), endTime: hoursAgo(2) },
    source: { customerProfilesSegmentArn: mockSegments[1].segmentArn },
  },
  {
    id: 'cmp-01HWQ2',
    name: 'TX high-intent pilot',
    status: 'Stopped',
    channelSubtypes: ['TELEPHONY'],
    schedule: { startTime: daysAgo(3), endTime: daysAgo(2) },
    source: { customerProfilesSegmentArn: mockSegments[2].segmentArn },
  },
  {
    id: 'cmp-01HVR9',
    name: 'Voicemail follow-up wave 2',
    status: 'Initialized',
    channelSubtypes: ['TELEPHONY'],
    schedule: { startTime: hoursAgo(-4), endTime: hoursAgo(-10) },
    source: { customerProfilesSegmentArn: mockSegments[0].segmentArn },
  },
];

export const mockCampaignDetail = (id: string): CampaignDetail => ({
  campaign: {
    id,
    name: mockCampaigns.find((c) => c.id === id)?.name ?? id,
    connectInstanceId: '0c123456-1234-1234-1234-000000000000',
    channelSubtypeConfig: {
      telephony: {
        capacity: 0.7,
        connectQueueId: 'qid-0001',
        outboundMode: { progressive: { bandwidthAllocation: 0.8 } },
        defaultOutboundConfig: {
          connectContactFlowId: 'cfid-0001',
          connectSourcePhoneNumber: '+15551234567',
          answerMachineDetectionConfig: {
            enableAnswerMachineDetection: true,
            awaitAnswerMachinePrompt: true,
          },
        },
      },
    },
    source: { customerProfilesSegmentArn: mockSegments[0].segmentArn },
    schedule: { startTime: hoursAgo(2), endTime: hoursAgo(-6) },
    communicationTimeConfig: {
      localTimeZoneConfig: { defaultTimeZone: 'America/New_York' },
    },
  },
  state: mockCampaigns.find((c) => c.id === id)?.status ?? 'Running',
});

export const mockQueues: Queue[] = [
  { id: 'qid-0001', arn: 'arn:aws:connect:us-east-1:165505826690:instance/x/queue/qid-0001', name: 'Outbound-NJ', queueType: 'STANDARD' },
  { id: 'qid-0002', arn: 'arn:aws:connect:us-east-1:165505826690:instance/x/queue/qid-0002', name: 'Outbound-FL', queueType: 'STANDARD' },
  { id: 'qid-0003', arn: 'arn:aws:connect:us-east-1:165505826690:instance/x/queue/qid-0003', name: 'Outbound-TX', queueType: 'STANDARD' },
];

export const mockContactFlows: ContactFlow[] = [
  { id: 'cfid-0001', arn: 'arn:aws:connect:us-east-1:165505826690:instance/x/contact-flow/cfid-0001', name: 'Outbound-Default', contactFlowType: 'CONTACT_FLOW' },
  { id: 'cfid-0002', arn: 'arn:aws:connect:us-east-1:165505826690:instance/x/contact-flow/cfid-0002', name: 'Outbound-VM-Drop', contactFlowType: 'CONTACT_FLOW' },
  { id: 'cfid-0100', arn: 'arn:aws:connect:us-east-1:165505826690:instance/x/contact-flow/cfid-0100', name: 'Campaign-Welcome', contactFlowType: 'CAMPAIGN' },
  { id: 'cfid-0101', arn: 'arn:aws:connect:us-east-1:165505826690:instance/x/contact-flow/cfid-0101', name: 'Campaign-Followup', contactFlowType: 'CAMPAIGN' },
];

export const mockPhoneNumbers: PhoneNumber[] = [
  { arn: 'arn:aws:connect:us-east-1:165505826690:phone-number/abc1', number: '+15551234567', type: 'DID', country: 'US' },
  { arn: 'arn:aws:connect:us-east-1:165505826690:phone-number/abc2', number: '+18009876543', type: 'TOLL_FREE', country: 'US' },
];

export const mockProfiles: Profile[] = [
  {
    profileId: '3bb7f1c0-1111-4bf0-a9a9-0123456789ab',
    firstName: 'Patrina',
    lastName: 'Gomez',
    email: 'patrina.gomez@example.com',
    phoneNumber: '+12017801027',
    attributes: {
      location: 'NJ - Newark',
      available: 'true',
      groups: 'returning',
      campaign: 'nj-morning',
      attempt: '2',
    },
    createdAt: daysAgo(45),
    lastUpdatedAt: minutesAgo(90),
  },
  {
    profileId: '3bb7f1c0-2222-4bf0-a9a9-0123456789ab',
    firstName: 'Miguel',
    lastName: 'Santana',
    email: 'msantana@example.com',
    phoneNumber: '+17878086669',
    attributes: {
      location: 'FL - Miami',
      available: 'true',
      groups: 'high-intent',
      attempt: '0',
    },
    createdAt: daysAgo(20),
    lastUpdatedAt: hoursAgo(3),
  },
];

export const mockAudit: AuditEntry[] = [
  {
    entityId: 'campaign/cmp-01HXY9',
    entityType: 'campaign',
    action: 'start',
    actorEmail: 'ops@vipmedical.com',
    timestamp: minutesAgo(5),
    after: { state: 'Running' },
    ipAddress: '10.0.1.42',
  },
  {
    entityId: 'segment/nj-available-leads',
    entityType: 'segment',
    action: 'estimate',
    actorEmail: 'ops@vipmedical.com',
    timestamp: minutesAgo(12),
    extra: { estimateId: 'est-0001', totalCount: 4213 },
  },
  {
    entityId: 'campaign/cmp-01HXY7',
    entityType: 'campaign',
    action: 'pause',
    actorEmail: 'ops@vipmedical.com',
    timestamp: hoursAgo(1),
    after: { state: 'Paused' },
  },
  {
    entityId: 'segment/tx-high-intent',
    entityType: 'segment',
    action: 'create',
    actorEmail: 'admin@vipmedical.com',
    timestamp: daysAgo(2),
    after: {
      name: 'tx-high-intent',
      displayName: 'TX High-intent',
      segmentGroups: { include: 'ALL', groups: [] },
    },
  },
  {
    entityId: 'campaign/cmp-01HWQ2',
    entityType: 'campaign',
    action: 'stop',
    actorEmail: 'admin@vipmedical.com',
    timestamp: daysAgo(2),
    after: { state: 'Stopped' },
  },
  {
    entityId: 'campaign/cmp-01HWQ2',
    entityType: 'campaign',
    action: 'delete',
    actorEmail: 'admin@vipmedical.com',
    timestamp: daysAgo(2),
    before: { state: 'Stopped' },
  },
];


/**
 * Per-family verification state. Keyed by `family` (stable across reconciles
 * that bump version) so the UX shows drift relative to the latest segment.
 */
type VerifyScenario = {
  redisCount: number;
  currentMembers: number;
  missing: number;
  extras: number;
};

const INITIAL_SCENARIOS: Record<string, VerifyScenario> = {
  'nj-available-leads': {
    redisCount: 1820,
    currentMembers: 1420,
    missing: 515, // redis has 515 that CP segment is missing
    extras: 115, // CP has 115 that no longer match Redis filters
  },
  'fl-returning-no-contact-7d': {
    redisCount: 982,
    currentMembers: 982,
    missing: 0,
    extras: 0,
  },
  'tx-high-intent': {
    redisCount: 640,
    currentMembers: 618,
    missing: 30,
    extras: 8,
  },
};

const verifyState: Record<string, VerifyResult | undefined> = {};

function buildSample(
  missingIds: string[],
  extraIds: string[],
  familyPrefix: string,
): VerifyCustomer[] {
  const missingSlice = missingIds.slice(0, 15).map((id, i) => ({
    customerId: id,
    phone: `+1${(2010000000 + i).toString()}`,
    name: `${familyPrefix} Lead ${i + 1}`,
    lastSeenRedis: minutesAgo(Math.floor(Math.random() * 60)),
    status: 'missing' as const,
  }));
  const extraSlice = extraIds.slice(0, 10).map((id, i) => ({
    customerId: id,
    phone: `+1${(9010000000 + i).toString()}`,
    name: `${familyPrefix} (stale) ${i + 1}`,
    lastSeenRedis: daysAgo(2 + Math.floor(Math.random() * 5)),
    status: 'extra' as const,
  }));
  return [...missingSlice, ...extraSlice];
}

export function runVerify(segmentName: string): VerifyResult {
  const seg = mockSegments.find((s) => s.name === segmentName);
  const family = seg?.family ?? segmentName;
  const version = seg?.version ?? 1;
  const scenario =
    INITIAL_SCENARIOS[family] ?? {
      redisCount: 100,
      currentMembers: 100,
      missing: 0,
      extras: 0,
    };

  const missingIds = Array.from(
    { length: scenario.missing },
    (_, i) => `cust-${family}-m-${1000 + i}`,
  );
  const extraIds = Array.from(
    { length: scenario.extras },
    (_, i) => `cust-${family}-x-${9000 + i}`,
  );

  const result: VerifyResult = {
    segmentName,
    family,
    version,
    redisCount: scenario.redisCount,
    segmentCount: scenario.currentMembers,
    missingCustomerIds: missingIds,
    extraCustomerIds: extraIds,
    sample: buildSample(missingIds, extraIds, family.toUpperCase().slice(0, 3)),
    verifiedAt: new Date().toISOString(),
  };
  verifyState[family] = result;
  return result;
}

/**
 * Simulates the reconcile workflow:
 * 1. Create new segment with version+1 whose membership equals the Redis set.
 * 2. Retarget any campaign that referenced the old segment to the new ARN.
 * 3. Delete the old segment.
 * Returns metadata the UI can display.
 */
export function applyReconcile(segmentName: string) {
  const seg = mockSegments.find((s) => s.name === segmentName);
  if (!seg) throw new Error(`Unknown segment ${segmentName}`);
  const family = seg.family ?? segmentName;
  const state = verifyState[family];
  if (!state) throw new Error('Run Verify first');

  const newVersion = state.version + 1;
  const newName = `${family}-v${newVersion}`;
  const newArn = segmentArn(newName);

  const campaignsUpdated: string[] = [];
  for (const campaign of mockCampaigns) {
    const src = (campaign.source ?? {}) as Record<string, unknown>;
    if (src.customerProfilesSegmentArn === seg.segmentArn) {
      src.customerProfilesSegmentArn = newArn;
      campaign.source = src;
      campaignsUpdated.push(campaign.id);
    }
  }

  const idx = mockSegments.findIndex((s) => s.name === seg.name);
  if (idx >= 0) {
    mockSegments.splice(idx, 1, {
      ...seg,
      name: newName,
      version: newVersion,
      segmentArn: newArn,
      createdAt: new Date().toISOString(),
    });
  }

  const added = state.missingCustomerIds.length;
  const removed = state.extraCustomerIds.length;
  const targetCount = state.redisCount;

  // Reset drift — reconciled segment matches Redis exactly.
  state.segmentCount = targetCount;
  state.missingCustomerIds = [];
  state.extraCustomerIds = [];
  state.sample = [];
  state.version = newVersion;
  state.segmentName = newName;
  state.verifiedAt = new Date().toISOString();

  // Future verifies on this family should see "in sync" until drift accumulates
  // again — without this, runVerify would reread the initial drift numbers.
  INITIAL_SCENARIOS[family] = {
    redisCount: targetCount,
    currentMembers: targetCount,
    missing: 0,
    extras: 0,
  };

  return {
    newSegmentName: newName,
    newSegmentArn: newArn,
    newVersion,
    targetCount,
    added,
    removed,
    campaignsUpdated,
    oldSegmentDeleted: true,
    completedAt: new Date().toISOString(),
  };
}

export function lastVerify(family: string): VerifyResult | undefined {
  return verifyState[family];
}

export function searchProfilesMock(key: string, value: string): Profile[] {
  const needle = value.toLowerCase();
  return mockProfiles.filter((p) => {
    const haystack = [p.firstName, p.lastName, p.phoneNumber, p.email, p.profileId]
      .join(' ')
      .toLowerCase();
    return haystack.includes(needle) || key !== '';
  });
}
