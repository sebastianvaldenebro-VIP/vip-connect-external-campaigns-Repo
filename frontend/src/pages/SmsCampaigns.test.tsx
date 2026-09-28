import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import type { BucketCampaignConfig, CampaignDef, PlanRunV2, PlanSummaryV2 } from '@/lib/api';

const mocks = vi.hoisted(() => ({ listV2: vi.fn(), triggerRunV2: vi.fn(), listNumbers: vi.fn() }));
vi.mock('@/lib/api', () => ({ api: { plans: mocks, sms: { listNumbers: mocks.listNumbers } } }));

import { SmsCampaigns } from './SmsCampaigns';

const CONFIG: BucketCampaignConfig = {
  smsTemplateVersion: 'campaign-v1', phiAcknowledged: true,
  smsMessageTemplate: 'Hi {{FirstName}}!',
  smsOriginationNumberArn: 'arn:aws:sms-voice:us-east-1:123456789012:phone-number/synthetic',
};
const PROMOTIONAL = { arn: CONFIG.smsOriginationNumberArn, phoneNumber: '+12025550123', status: 'ACTIVE',
  messageType: 'PROMOTIONAL', numberCapabilities: ['SMS'] };

function campaign(id: string, deliveryType: CampaignDef['deliveryType'] = 'sms'): CampaignDef {
  return {
    id, name: `${id} audience`, states: [], groups: [],
    run_type: 'full', dependsOn: [], deliveryType, campaignConfig: { ...CONFIG },
    pinnedSegmentArn: 'arn:aws:profile:us-east-1:123456789012:domains/example/segment-definitions/audience',
  };
}

function plan(overrides: Partial<PlanSummaryV2> = {}): PlanSummaryV2 {
  return {
    planId: 'sms-one', name: 'Synthetic SMS campaign', trigger: { type: 'manual' },
    isTemplate: false, is_template: false, isDefault: false,
    createdAt: '2026-09-14T12:00:00Z',
    buckets: [{
      id: 'bucket-one', name: 'SMS bucket', run_mode: 'status_based', cleanup: false,
      prestart_next: false, campaignConfig: {}, campaigns: [campaign('message-one')],
    }],
    ...overrides,
  };
}

function run(status: PlanRunV2['status'] = 'running'): PlanRunV2 {
  return {
    planId: 'sms-one', runId: 'synthetic-run', status, currentBucketIndex: 0,
    bucketStates: [], startedAt: '2026-09-14T12:15:00Z',
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

const clients: QueryClient[] = [];
function LocationProbe() {
  return <div aria-label="Current route">{useLocation().pathname}</div>;
}

function showList() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/sms']}>
        <Routes>
          <Route path="/sms" element={<SmsCampaigns />} />
          <Route path="/sms/new" element={<h1>New SMS draft</h1>} />
          <Route path="/sms/:id/edit" element={<h1>Edit SMS draft</h1>} />
          <Route path="/plans/:id" element={<h1>Campaign activity</h1>} />
        </Routes>
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mocks.listV2.mockReset();
  mocks.triggerRunV2.mockReset();
  mocks.listNumbers.mockReset();
  mocks.listV2.mockResolvedValue({ plans: [plan()] });
  mocks.triggerRunV2.mockResolvedValue(run());
  mocks.listNumbers.mockResolvedValue({ originationNumbers: [PROMOTIONAL] });
});

afterEach(() => {
  clients.splice(0).forEach((client) => client.clear());
});

describe('SMS campaigns list and explicit start', () => {
  it('lists only nonempty SMS-only plans, excluding voice, mixed plans and both template forms', async () => {
    const mixed = plan({ planId: 'mixed', name: 'Mixed voice and SMS' });
    mixed.buckets[0].campaigns.push(campaign('voice-child', 'campaign'));
    const voice = plan({ planId: 'voice', name: 'Voice only' });
    voice.buckets[0].campaigns = [campaign('voice', 'campaign')];
    const omittedType = plan({ planId: 'legacy-voice', name: 'Legacy voice default' });
    delete omittedType.buckets[0].campaigns[0].deliveryType;
    const emptyBucket = plan({ planId: 'empty-bucket', name: 'Empty bucket' });
    emptyBucket.buckets[0].campaigns = [];
    const mixedEmptyBucket = plan({ planId: 'legacy-bucket', name: 'SMS plus legacy empty bucket' });
    mixedEmptyBucket.buckets.push({ ...emptyBucket.buckets[0], id: 'legacy' });
    mocks.listV2.mockResolvedValue({ plans: [
      plan(), mixed, voice, omittedType, emptyBucket, mixedEmptyBucket,
      plan({ planId: 'empty', name: 'No buckets', buckets: [] }),
      plan({ planId: 'template-one', name: 'Camel template', isTemplate: true }),
      plan({ planId: 'template-two', name: 'Snake template', is_template: true }),
    ] });

    showList();

    expect(await screen.findByText('Synthetic SMS campaign')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'SMS campaigns' })).toBeInTheDocument();
    for (const name of ['Mixed voice and SMS', 'Voice only', 'Legacy voice default', 'Empty bucket', 'SMS plus legacy empty bucket', 'No buckets', 'Camel template', 'Snake template']) {
      expect(screen.queryByText(name)).not.toBeInTheDocument();
    }
    expect(mocks.triggerRunV2).not.toHaveBeenCalled();
  });

  it('opens a new draft without starting any campaign', async () => {
    const user = userEvent.setup();
    showList();
    await screen.findByText('Synthetic SMS campaign');

    await user.click(screen.getByRole('link', { name: 'New SMS campaign' }));

    expect(screen.getByLabelText('Current route')).toHaveTextContent('/sms/new');
    expect(mocks.triggerRunV2).not.toHaveBeenCalled();
  });

  it('opens existing activity without sending a start request', async () => {
    const user = userEvent.setup();
    showList();
    await screen.findByText('Synthetic SMS campaign');

    await user.click(screen.getByRole('link', { name: 'View activity' }));

    expect(screen.getByLabelText('Current route')).toHaveTextContent('/plans/sms-one');
    expect(mocks.triggerRunV2).not.toHaveBeenCalled();
  });

  it('starts only the selected campaign after an explicit click and confirms the active run', async () => {
    const user = userEvent.setup();
    mocks.listV2.mockResolvedValueOnce({ plans: [plan()] })
      .mockResolvedValue({ plans: [plan({ latestRun: run() })] });
    showList();
    const start = await screen.findByRole('button', { name: 'Start' });
    expect(mocks.triggerRunV2).not.toHaveBeenCalled();

    await user.click(start);

    await waitFor(() => expect(mocks.triggerRunV2).toHaveBeenCalledExactlyOnceWith('sms-one'));
    expect(await screen.findByRole('status')).toHaveTextContent('Synthetic SMS campaign started');
    expect(screen.getByLabelText('Current route').textContent).toBe('/sms');
    await waitFor(() => expect(screen.getByRole('button', { name: 'Start' })).toBeDisabled());
  });

  it('disables the pending start and does not issue duplicate requests on a double click', async () => {
    const user = userEvent.setup();
    const pending = deferred<PlanRunV2>();
    mocks.triggerRunV2.mockReturnValue(pending.promise);
    mocks.listV2.mockResolvedValueOnce({ plans: [plan()] })
      .mockResolvedValue({ plans: [plan({ latestRun: run() })] });
    showList();
    const start = await screen.findByRole('button', { name: 'Start' });

    await user.dblClick(start);

    expect(mocks.triggerRunV2).toHaveBeenCalledExactlyOnceWith('sms-one');
    expect(start).toBeDisabled();
    expect(screen.getByLabelText('Current route').textContent).toBe('/sms');
    await user.click(start);
    expect(mocks.triggerRunV2).toHaveBeenCalledTimes(1);
    await act(async () => pending.resolve(run()));
    expect(await screen.findByRole('status')).toHaveTextContent('Synthetic SMS campaign started');
    expect(screen.getByLabelText('Current route').textContent).toBe('/sms');
  });

  it('keeps an accepted start blocked across stale refetches until that same run is terminal', async () => {
    const user = userEvent.setup();
    const stale = deferred<{ plans: PlanSummaryV2[] }>();
    mocks.listV2.mockResolvedValueOnce({ plans: [plan()] })
      .mockReturnValueOnce(stale.promise)
      .mockResolvedValueOnce({ plans: [plan({ latestRun: { ...run('completed'), runId: 'older-run' } })] })
      .mockResolvedValue({ plans: [plan({ latestRun: run('completed') })] });
    showList();
    const client = clients.at(-1)!;
    await user.click(await screen.findByRole('button', { name: 'Start' }));
    await waitFor(() => expect(mocks.listV2).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole('status')).toHaveTextContent('Synthetic SMS campaign started');

    await act(async () => stale.resolve({ plans: [plan()] }));
    await waitFor(() => expect(client.getQueryData<{ plans: PlanSummaryV2[] }>(['plans'])?.plans[0].latestRun).toBeUndefined());
    expect(screen.getByRole('button', { name: 'Start' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: 'Start' }));
    expect(mocks.triggerRunV2).toHaveBeenCalledTimes(1);

    await act(async () => { await client.invalidateQueries({ queryKey: ['plans'] }); });
    await waitFor(() => expect(client.getQueryData<{ plans: PlanSummaryV2[] }>(['plans'])?.plans[0].latestRun?.runId).toBe('older-run'));
    expect(screen.getByRole('button', { name: 'Start' })).toBeDisabled();

    await act(async () => { await client.invalidateQueries({ queryKey: ['plans'] }); });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Start' })).toBeEnabled());
    expect(mocks.triggerRunV2).toHaveBeenCalledTimes(1);
  });

  it('opens editing for a single manual SMS campaign without starting it', async () => {
    const user = userEvent.setup();
    showList();
    await screen.findByText('Synthetic SMS campaign');

    await user.click(screen.getByRole('link', { name: 'Edit' }));

    expect(screen.getByLabelText('Current route').textContent).toBe('/sms/sms-one/edit');
    expect(mocks.triggerRunV2).not.toHaveBeenCalled();
  });

  it.each(['running', 'multiple campaigns', 'multiple buckets', 'scheduled', 'legacy template', 'custom duration', 'time-based bucket'] as const)(
    'keeps %s SMS plans out of the simple editor while preserving activity access', async (kind) => {
      const existing = plan();
      if (kind === 'running') existing.latestRun = run();
      if (kind === 'multiple campaigns') existing.buckets[0].campaigns.push(campaign('message-two'));
      if (kind === 'multiple buckets') existing.buckets.push({ ...existing.buckets[0], id: 'second-bucket', campaigns: [campaign('message-two')] });
      if (kind === 'scheduled') existing.trigger = { type: 'time', time: '12:00' };
      if (kind === 'legacy template') delete existing.buckets[0].campaigns[0].campaignConfig!.smsTemplateVersion;
      if (kind === 'custom duration') {
        existing.buckets[0].campaigns[0].run_type = 'custom';
        existing.buckets[0].campaigns[0].run_duration_minutes = 45;
      }
      if (kind === 'time-based bucket') {
        existing.buckets[0].run_mode = 'time_based';
        existing.buckets[0].duration_minutes = 45;
      }
      mocks.listV2.mockResolvedValue({ plans: [existing] });
      showList();

      await screen.findByText('Synthetic SMS campaign');

      expect(screen.queryByRole('link', { name: 'Edit' })).not.toBeInTheDocument();
      expect(screen.getByRole('link', { name: 'View activity' })).toBeInTheDocument();
      expect(mocks.triggerRunV2).not.toHaveBeenCalled();
    },
  );

  it('prevents another start when the latest run is already active', async () => {
    const user = userEvent.setup();
    mocks.listV2.mockResolvedValue({ plans: [plan({ latestRun: run() })] });
    showList();
    await screen.findByText('Synthetic SMS campaign');

    expect(screen.getAllByText(/^running$/i).length).toBeGreaterThan(0);
    const start = screen.queryByRole('button', { name: 'Start' });
    if (start) {
      expect(start).toBeDisabled();
      await user.click(start);
    }
    expect(mocks.triggerRunV2).not.toHaveBeenCalled();
    expect(screen.getByRole('link', { name: 'View activity' })).toBeInTheDocument();
  });

  it('allows a new explicit start after a completed run', async () => {
    const user = userEvent.setup();
    mocks.listV2.mockResolvedValue({ plans: [plan({ latestRun: run('completed') })] });
    showList();
    const start = await screen.findByRole('button', { name: 'Start' });
    expect(start).toBeEnabled();
    expect(mocks.triggerRunV2).not.toHaveBeenCalled();

    await user.click(start);

    await waitFor(() => expect(mocks.triggerRunV2).toHaveBeenCalledExactlyOnceWith('sms-one'));
  });

  it('keeps the loading state noninteractive until the list resolves', async () => {
    const pending = deferred<{ plans: PlanSummaryV2[] }>();
    mocks.listV2.mockReturnValue(pending.promise);
    showList();

    expect(screen.getByText(/loading/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Start' })).not.toBeInTheDocument();
    expect(mocks.triggerRunV2).not.toHaveBeenCalled();
    await act(async () => pending.resolve({ plans: [plan()] }));
    expect(await screen.findByRole('button', { name: 'Start' })).toBeEnabled();
  });

  it('reports a failed initial list load without enabling Start', async () => {
    mocks.listV2.mockRejectedValue(new Error('Synthetic list failure'));
    showList();

    expect(await screen.findByText(/failed to load|could not load|unable to load|synthetic list failure/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Start' })).not.toBeInTheDocument();
    expect(mocks.triggerRunV2).not.toHaveBeenCalled();
  });

  it('shows an empty state and keeps the new-campaign link when no SMS campaigns exist', async () => {
    mocks.listV2.mockResolvedValue({ plans: [] });
    showList();

    expect(await screen.findByText(/no sms campaigns/i)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'New SMS campaign' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Start' })).not.toBeInTheDocument();
    expect(mocks.triggerRunV2).not.toHaveBeenCalled();
  });

  it('shows a start error without navigating or automatically retrying', async () => {
    const user = userEvent.setup();
    mocks.triggerRunV2.mockRejectedValue(new Error('Synthetic start failure'));
    showList();
    await user.click(await screen.findByRole('button', { name: 'Start' }));

    expect(await screen.findByText(/failed to start|could not start|unable to start|synthetic start failure/i)).toBeInTheDocument();
    expect(screen.getByLabelText('Current route').textContent).toBe('/sms');
    expect(mocks.triggerRunV2).toHaveBeenCalledExactlyOnceWith('sms-one');
    expect(screen.getByRole('button', { name: 'Start' })).toBeEnabled();
  });

  it.each([
    ['transactional', { messageType: 'TRANSACTIONAL' }],
    ['inactive', { status: 'PENDING' }],
    ['no SMS capability', { numberCapabilities: ['VOICE'] }],
    ['missing metadata', { messageType: undefined, numberCapabilities: undefined }],
    ['another ARN', { arn: 'other' }],
  ])('prevents Start when the selected origin is %s', async (_kind, fields) => {
    mocks.listNumbers.mockResolvedValue({ originationNumbers: [{ ...PROMOTIONAL, ...fields }] });
    const user = userEvent.setup();
    showList();
    const button = await screen.findByRole('button', { name: 'Start' });
    expect(button).toBeDisabled();
    await user.click(button);
    expect(mocks.triggerRunV2).not.toHaveBeenCalled();
    expect(screen.getByText(/Select an active promotional SMS number/)).toBeInTheDocument();
  });

  it('rechecks origins before triggering a run and blocks newly incompatible metadata', async () => {
    mocks.listNumbers.mockResolvedValueOnce({ originationNumbers: [PROMOTIONAL] })
      .mockResolvedValue({ originationNumbers: [{ ...PROMOTIONAL, messageType: 'TRANSACTIONAL' }] });
    const user = userEvent.setup();
    showList();
    await user.click(await screen.findByRole('button', { name: 'Start' }));
    expect(await screen.findByText(/This campaign needs an active promotional origination/)).toBeInTheDocument();
    expect(mocks.triggerRunV2).not.toHaveBeenCalled();
    expect(mocks.listNumbers).toHaveBeenCalledTimes(2);
  });

  it('blocks Start while inventory is loading or fails to load', async () => {
    const pending = deferred<{ originationNumbers: never[] }>();
    mocks.listNumbers.mockReturnValue(pending.promise);
    const user = userEvent.setup();
    showList();
    const button = await screen.findByRole('button', { name: 'Start' });
    expect(button).toBeDisabled();
    await act(async () => pending.reject(new Error('Inventory unavailable')));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not verify promotional sending numbers.');
    await user.click(button);
    expect(mocks.triggerRunV2).not.toHaveBeenCalled();
  });

  it('rejects unsupported bucket-level campaign-v1 settings even with a compatible promotional number', async () => {
    const inherited = plan();
    inherited.buckets[0].campaignConfig = { ...CONFIG };
    inherited.buckets[0].campaigns[0].campaignConfig = {};
    mocks.listV2.mockResolvedValue({ plans: [inherited] });
    mocks.listNumbers.mockResolvedValue({ originationNumbers: [PROMOTIONAL] });
    const user = userEvent.setup();
    showList();
    const start = await screen.findByRole('button', { name: 'Start' });
    expect(start).toBeDisabled();
    expect(screen.getByText(/Configure the SMS template version on each campaign/)).toBeInTheDocument();
    await user.click(start);
    expect(mocks.triggerRunV2).not.toHaveBeenCalled();
  });

  it('preserves legacy SMS starts without applying the promotional filter', async () => {
    const legacy = plan();
    delete legacy.buckets[0].campaigns[0].campaignConfig!.smsTemplateVersion;
    mocks.listV2.mockResolvedValue({ plans: [legacy] });
    mocks.listNumbers.mockResolvedValue({ originationNumbers: [{ ...PROMOTIONAL, messageType: 'TRANSACTIONAL' }] });
    const user = userEvent.setup();
    showList();
    const button = await screen.findByRole('button', { name: 'Start' });
    expect(button).toBeEnabled();
    await user.click(button);
    await waitFor(() => expect(mocks.triggerRunV2).toHaveBeenCalledExactlyOnceWith('sms-one'));
    expect(mocks.listNumbers).toHaveBeenCalledTimes(1);
  });
});
