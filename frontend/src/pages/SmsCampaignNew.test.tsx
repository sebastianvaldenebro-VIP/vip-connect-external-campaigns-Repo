import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { DEFAULT_SMS_CAMPAIGN_TEMPLATE, SMS_CAMPAIGN_TEMPLATE_VERSION, previewSmsCampaign } from '@/lib/smsCampaign';

const mocks = vi.hoisted(() => ({
  segments: vi.fn(), createSegment: vi.fn(), numbers: vi.fn(), create: vi.fn(),
  update: vi.fn(), get: vi.fn(), start: vi.fn(),
}));
vi.mock('@/lib/api', () => ({ api: {
  segments: { list: mocks.segments, create: mocks.createSegment },
  sms: { listNumbers: mocks.numbers },
  plans: { createV2: mocks.create, updateV2: mocks.update, getV2: mocks.get, triggerRunV2: mocks.start },
  leads: { distinctValues: async () => ({ values: [] }) },
  previewCount: async () => ({ redisCount: 1, segmentCount: 1 }),
} }));
vi.mock('@/lib/stateLocationMap', () => ({
  useLocationMapping: () => ({ locationMap: [{ state: 'Test State', slug: 'test-state', code: 'TS', locations: [] }] }),
  locationsForStates: () => [], codesForStates: () => [],
}));
vi.mock('@/components/EnableCampaignModal', () => ({ EnableCampaignModal: () => <div>Voice activation panel</div> }));

import { SmsCampaignNew } from './SmsCampaignNew';

const ORIGIN = 'arn:aws:sms-voice:us-east-1:123456789012:phone-number/origin';
const PROMOTIONAL = { arn: ORIGIN, phoneNumber: '+12025550123', numberType: 'TEN_DLC', status: 'ACTIVE', messageType: 'PROMOTIONAL', numberCapabilities: ['SMS'] };
const SEGMENT = { name: 'audience-one', displayName: 'Audience one', segmentArn: 'arn:aws:profile:us-east-1:123456789012:domains/example/segment-definitions/audience-one', syncMode: 'manual' };
const CREATED = { ...SEGMENT, name: 'new-audience', displayName: 'New audience', segmentArn: SEGMENT.segmentArn.replace('audience-one', 'new-audience') };

beforeEach(() => {
  vi.clearAllMocks();
  mocks.segments.mockResolvedValue({ segments: [SEGMENT] });
  mocks.createSegment.mockResolvedValue(CREATED);
  mocks.numbers.mockResolvedValue({ originationNumbers: [PROMOTIONAL] });
  mocks.create.mockResolvedValue({ planId: 'saved' });
  mocks.update.mockResolvedValue({ planId: 'saved' });
});
afterEach(cleanup);

function show(path = '/sms/new') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><Routes>
    <Route path="/sms/new" element={<SmsCampaignNew />} />
    <Route path="/sms/:id/edit" element={<SmsCampaignNew />} />
    <Route path="/sms" element={<p>Campaign list</p>} />
  </Routes></MemoryRouter></QueryClientProvider>);
}

async function fill(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText('Campaign name'), 'Booking reminder');
  await user.click(screen.getByRole('checkbox', { name: 'TS' }));
  await user.selectOptions(screen.getByLabelText('Origination number'), ORIGIN);
}

describe('SMS campaign editor', () => {
  it('saves the editable default with FirstName intact and never starts on save', async () => {
    const user = userEvent.setup();
    show();
    expect(screen.getByLabelText('Message')).toHaveValue(DEFAULT_SMS_CAMPAIGN_TEMPLATE);
    expect(screen.getByLabelText('Message preview')).toHaveValue(previewSmsCampaign(DEFAULT_SMS_CAMPAIGN_TEMPLATE));
    expect(screen.getByLabelText('Message preview')).toHaveAttribute('readonly');
    await fill(user);
    await user.click(screen.getByRole('checkbox', { name: /protected health information/ }));
    await user.click(screen.getAllByRole('button', { name: 'Save campaign' })[0]);
    await screen.findByText('Campaign list');
    expect(mocks.create).toHaveBeenCalledTimes(1);
    const body = mocks.create.mock.calls[0]![0];
    expect(body.trigger).toEqual({ type: 'manual' });
    expect(body.isTemplate).toBe(false);
    expect(body).not.toHaveProperty('workingHours');
    expect(body.buckets).toHaveLength(1);
    expect(body.buckets[0]).toMatchObject({ run_mode: 'status_based', cleanup: false, prestart_next: false, campaignConfig: {} });
    expect(body.buckets[0].campaigns).toHaveLength(1);
    expect(body.buckets[0].campaigns[0]).toMatchObject({
      deliveryType: 'sms', pinnedSegmentArn: undefined, states: ['TS'], groups: [], dependsOn: [],
      campaignConfig: { smsMessageTemplate: DEFAULT_SMS_CAMPAIGN_TEMPLATE, smsTemplateVersion: SMS_CAMPAIGN_TEMPLATE_VERSION, smsOriginationNumberArn: ORIGIN, phiAcknowledged: true },
    });
    expect(JSON.stringify(body)).toContain('{{FirstName}}');
    expect(JSON.stringify(body)).not.toContain('Alex');
    expect(mocks.start).not.toHaveBeenCalled();
  });

  it('updates preview and size for edits and requires acknowledgement of the changed message', async () => {
    const user = userEvent.setup();
    show();
    await fill(user);
    await user.click(screen.getByRole('checkbox', { name: /protected health information/ }));
    await user.clear(screen.getByLabelText('Message'));
    await user.click(screen.getByLabelText('Message'));
    await user.paste('Hello {{FirstName}}! Your booking link is ready.');
    expect(screen.getByLabelText('Message preview')).toHaveValue('Hello Alex! Your booking link is ready.');
    expect(screen.getByRole('checkbox', { name: /protected health information/ })).not.toBeChecked();
    expect(screen.getByText(/Estimated 1 SMS part/)).toBeInTheDocument();
    await user.click(screen.getAllByRole('button', { name: 'Save campaign' })[0]);
    expect(mocks.create).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent('Confirm the message');
    await user.click(screen.getByRole('checkbox', { name: /protected health information/ }));
    await user.click(screen.getAllByRole('button', { name: 'Save campaign' })[0]);
    await screen.findByText('Campaign list');
    expect(mocks.create.mock.calls[0]![0].buckets[0].campaigns[0].campaignConfig.smsMessageTemplate).toBe('Hello {{FirstName}}! Your booking link is ready.');
  });

  it('loads every segment page before offering the complete pinned-segment list', async () => {
    mocks.segments.mockResolvedValueOnce({ segments: [SEGMENT], nextToken: 'next-page' })
      .mockResolvedValueOnce({ segments: [CREATED] });
    const user = userEvent.setup();
    show();
    await screen.findByRole('option', { name: 'New audience' });
    expect(mocks.segments).toHaveBeenNthCalledWith(2, { maxResults: 100, nextToken: 'next-page' });
    await fill(user);
    await user.selectOptions(screen.getByLabelText('Pinned segment (optional)'), CREATED.segmentArn);
    await user.click(screen.getByRole('checkbox', { name: /protected health information/ }));
    await user.click(screen.getAllByRole('button', { name: 'Save campaign' })[0]);
    await screen.findByText('Campaign list');
    expect(mocks.create.mock.calls[0]![0].buckets[0].campaigns[0].pinnedSegmentArn).toBe(CREATED.segmentArn);
  });

  it('reports a later-page failure instead of presenting a partial pinned-segment list', async () => {
    mocks.segments.mockResolvedValueOnce({ segments: [SEGMENT], nextToken: 'next-page' })
      .mockRejectedValue(new Error('Second page unavailable'));
    show();
    expect(await screen.findByRole('alert')).toHaveTextContent('Second page unavailable');
    expect(screen.getByLabelText('Pinned segment (optional)')).toBeDisabled();
    expect(screen.queryByRole('option', { name: 'Audience one' })).not.toBeInTheDocument();
    expect(mocks.create).not.toHaveBeenCalled();
    expect(mocks.start).not.toHaveBeenCalled();
  });

  it('keeps a failed save on the form and does not retry or start automatically', async () => {
    mocks.create.mockRejectedValue(new Error('Save unavailable'));
    const user = userEvent.setup();
    show();
    await fill(user);
    await user.click(screen.getByRole('checkbox', { name: /protected health information/ }));
    await user.click(screen.getAllByRole('button', { name: 'Save campaign' })[0]);
    expect(await screen.findByRole('alert')).toHaveTextContent('Save unavailable');
    expect(mocks.create).toHaveBeenCalledTimes(1);
    expect(mocks.start).not.toHaveBeenCalled();
    expect(screen.getByLabelText('Campaign name')).toHaveValue('Booking reminder');
  });

  it('blocks missing identity, states/segment, origin and disallowed personalization', async () => {
    const user = userEvent.setup();
    show();
    await screen.findByRole('option', { name: 'Audience one' });
    await user.clear(screen.getByLabelText('Message'));
    await user.click(screen.getByLabelText('Message'));
    await user.paste('Hello {{Unknown}}');
    await user.click(screen.getAllByRole('button', { name: 'Save campaign' })[0]);
    expect(screen.getByRole('alert')).toHaveTextContent('Enter a campaign name');
    expect(screen.getByRole('alert')).toHaveTextContent('Select at least one state, or pin an existing segment.');
    expect(screen.getByRole('alert')).toHaveTextContent('Select an active promotional origination');
    expect(screen.getByRole('alert')).toHaveTextContent('only {{FirstName}}');
    expect(mocks.create).not.toHaveBeenCalled();
  });

  it('loads and updates a simple saved campaign without regenerating its IDs or starting', async () => {
    mocks.get.mockResolvedValue({ plan: {
      planId: 'saved', name: 'Saved reminder', trigger: { type: 'manual' },
      buckets: [{ id: 'bucket-existing', name: 'SMS', run_mode: 'status_based', cleanup: false, prestart_next: false, campaignConfig: {}, campaigns: [{
        id: 'campaign-existing', deliveryType: 'sms', pinnedSegmentArn: SEGMENT.segmentArn, run_type: 'full', states: [], groups: [], dependsOn: [],
        campaignConfig: { smsMessageTemplate: 'Hello {{FirstName}}!', smsTemplateVersion: SMS_CAMPAIGN_TEMPLATE_VERSION, smsOriginationNumberArn: ORIGIN, phiAcknowledged: true },
      }] }],
    } });
    const user = userEvent.setup();
    show('/sms/saved/edit');
    await screen.findByRole('option', { name: 'Audience one' });
    expect(screen.getByLabelText('Campaign name')).toHaveValue('Saved reminder');
    expect(screen.getByLabelText('Message')).toHaveValue('Hello {{FirstName}}!');
    await user.click(screen.getAllByRole('button', { name: 'Save changes' })[0]);
    await screen.findByText('Campaign list');
    expect(mocks.update.mock.calls[0]![0]).toBe('saved');
    expect(mocks.update.mock.calls[0]![1].buckets[0].id).toBe('bucket-existing');
    expect(mocks.update.mock.calls[0]![1].buckets[0].campaigns[0].id).toBe('campaign-existing');
    expect(mocks.create).not.toHaveBeenCalled();
    expect(mocks.start).not.toHaveBeenCalled();
  });

  it('refuses to flatten a recurring or multi-campaign plan into this editor', async () => {
    mocks.get.mockResolvedValue({ plan: {
      planId: 'saved', name: 'Automatic SMS', trigger: { type: 'time', time: '10:00' },
      buckets: [{ campaigns: [{ deliveryType: 'sms' }] }],
    } });
    show('/sms/saved/edit');
    await screen.findByText(/cannot be edited here/);
    expect(screen.queryByLabelText('Message')).not.toBeInTheDocument();
    expect(mocks.update).not.toHaveBeenCalled();
  });

  it.each([
    ['transactional', { messageType: 'TRANSACTIONAL' }],
    ['inactive', { status: 'PENDING' }],
    ['no SMS capability', { numberCapabilities: ['VOICE'] }],
    ['missing message type', { messageType: undefined }],
    ['missing capabilities', { numberCapabilities: undefined }],
  ])('does not offer a %s number and disables saving when no compatible number exists', async (_kind, fields) => {
    mocks.numbers.mockResolvedValue({ originationNumbers: [{ ...PROMOTIONAL, ...fields }] });
    const user = userEvent.setup();
    show();
    expect(await screen.findByRole('alert')).toHaveTextContent('No promotional SMS sending number is available.');
    expect(screen.queryByRole('option', { name: '+12025550123 (TEN_DLC)' })).not.toBeInTheDocument();
    const save = screen.getAllByRole('button', { name: 'Save campaign' })[0];
    expect(save).toBeDisabled();
    await user.click(save);
    expect(mocks.create).not.toHaveBeenCalled();
    expect(mocks.start).not.toHaveBeenCalled();
  });

  it('offers only compatible promotional numbers from mixed inventory', async () => {
    mocks.numbers.mockResolvedValue({ originationNumbers: [PROMOTIONAL,
      { ...PROMOTIONAL, arn: ORIGIN + '-tx', phoneNumber: '+12025550124', messageType: 'TRANSACTIONAL' },
      { ...PROMOTIONAL, arn: ORIGIN + '-voice', phoneNumber: '+12025550125', numberCapabilities: ['VOICE'] },
    ] });
    show();
    expect(await screen.findByRole('option', { name: '+12025550123 (TEN_DLC)' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /12025550124|12025550125/ })).not.toBeInTheDocument();
  });

  it('requires replacing an incompatible saved ARN instead of silently selecting a different number', async () => {
    const incompatibleArn = ORIGIN + '-transactional';
    mocks.numbers.mockResolvedValue({ originationNumbers: [PROMOTIONAL,
      { ...PROMOTIONAL, arn: incompatibleArn, messageType: 'TRANSACTIONAL' },
    ] });
    mocks.get.mockResolvedValue({ plan: {
      planId: 'saved', name: 'Saved campaign', trigger: { type: 'manual' },
      buckets: [{ id: 'bucket', run_mode: 'status_based', cleanup: false, prestart_next: false, campaignConfig: {}, campaigns: [{
        id: 'campaign', deliveryType: 'sms', pinnedSegmentArn: SEGMENT.segmentArn, run_type: 'full', states: [], groups: [], dependsOn: [],
        campaignConfig: { smsTemplateVersion: SMS_CAMPAIGN_TEMPLATE_VERSION, smsMessageTemplate: 'Hello {{FirstName}}!', smsOriginationNumberArn: incompatibleArn, phiAcknowledged: true },
      }] }],
    } });
    const user = userEvent.setup();
    show('/sms/saved/edit');
    expect(await screen.findByRole('alert')).toHaveTextContent('The saved origination number cannot send promotional SMS.');
    expect(screen.getByLabelText('Origination number')).toHaveValue(incompatibleArn);
    expect(screen.getAllByRole('button', { name: 'Save changes' })[0]).toBeDisabled();
    expect(mocks.update).not.toHaveBeenCalled();
    await user.selectOptions(screen.getByLabelText('Origination number'), ORIGIN);
    await user.click(screen.getAllByRole('button', { name: 'Save changes' })[0]);
    await screen.findByText('Campaign list');
    expect(mocks.update.mock.calls[0]![1].buckets[0].campaigns[0].campaignConfig.smsOriginationNumberArn).toBe(ORIGIN);
    expect(mocks.start).not.toHaveBeenCalled();
  });

  it('rechecks compatibility on save and prevents a write if the number changed since loading', async () => {
    mocks.numbers.mockResolvedValueOnce({ originationNumbers: [PROMOTIONAL] })
      .mockResolvedValue({ originationNumbers: [{ ...PROMOTIONAL, messageType: 'TRANSACTIONAL' }] });
    const user = userEvent.setup();
    show();
    await fill(user);
    await user.click(screen.getByRole('checkbox', { name: /protected health information/ }));
    await user.click(screen.getAllByRole('button', { name: 'Save campaign' })[0]);
    await waitFor(() => expect(screen.getAllByRole('alert').some((alert) => alert.textContent?.includes('before saving'))).toBe(true));
    expect(mocks.create).not.toHaveBeenCalled();
    expect(mocks.start).not.toHaveBeenCalled();
  });
});
