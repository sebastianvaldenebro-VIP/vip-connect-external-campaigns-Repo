import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { BucketDefV2, CampaignDef, PrecallSmsConfig, SmsOriginationNumber } from '@/lib/api';
import { profileSmsPreview } from '@/lib/precallSms';

vi.mock('@/lib/api', () => ({ api: {
  leads: { distinctValues: async () => ({ values: ['New Lead / New Lead'] }) },
  segments: { list: async () => ({ segments: [] }) },
} }));
vi.mock('@/lib/stateLocationMap', () => ({
  useLocationMapping: () => ({ locationMap: [{ code: 'NY', state: 'New York', slug: 'NY', locations: [] }] }),
}));

import { CampaignCard } from './PlanNew';

const ORIGIN = 'arn:aws:sms-voice:us-east-1:123456789012:phone-number/example';
const MANUAL = { enabled: true, originationNumberArn: ORIGIN, messageTemplate: 'Hi {{FirstName}}! {{ClinicName}} here.', clinicName: 'Example Clinic' };
const PROFILE: PrecallSmsConfig = { enabled: true, mode: 'profile', catalogVersion: 'phase1-v1', originationNumberArn: ORIGIN };

function showCard(precallSms?: PrecallSmsConfig, dependsOn: string[] = []) {
  const initial: CampaignDef = {
    id: 'child', name: 'Child campaign', states: ['NY'], groups: ['New Lead / New Lead'],
    run_type: 'full', deliveryType: 'campaign', dependsOn,
    campaignConfig: {
      queueId: 'queue', contactFlowId: 'flow', sourcePhoneNumber: '', dialerType: 'progressive',
      bandwidthAllocation: 1, dialingCapacity: 1, amdEnabled: true, amdAwaitPrompt: true,
      ...(precallSms ? { precallSms } : {}),
    },
  };
  function Harness() {
    const [campaign, setCampaign] = useState(initial);
    const bucket: BucketDefV2 = {
      id: 'bucket', name: 'Bucket', run_mode: 'status_based', cleanup: false, prestart_next: true,
      campaignConfig: campaign.campaignConfig!,
      campaigns: [{ ...initial, id: 'parent', name: 'Parent campaign' }, campaign],
    };
    return <>
      <CampaignCard campaign={campaign} bucketIndex={0} allBuckets={[bucket]} isExpanded
        onToggle={() => {}} onChange={setCampaign} onRemove={() => {}} canRemove={false}
        queues={[]} contactFlows={[]}
        smsNumbers={[{ arn: ORIGIN, phoneNumber: '+12025550123', numberType: 'LONG_CODE', status: 'ACTIVE', messageType: 'TRANSACTIONAL', numberCapabilities: ['SMS'] } as SmsOriginationNumber]} />
      <output aria-label="Campaign configuration">{JSON.stringify(campaign)}</output>
    </>;
  }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><Harness /></QueryClientProvider>);
  return () => JSON.parse(screen.getByLabelText('Campaign configuration').textContent!) as CampaignDef;
}

describe('Plan campaign pre-call configuration', () => {
  it('continues offering transactional SMS numbers for profile and manual precall', async () => {
    const user = userEvent.setup();
    const saved = showCard(PROFILE);
    const origin = screen.getByLabelText('Pre-call SMS origination number');
    expect(origin).toHaveValue(ORIGIN);
    expect(screen.getByRole('option', { name: /\+12025550123/ })).toBeEnabled();
    await user.selectOptions(screen.getByLabelText('Pre-call SMS message source'), 'manual');
    await user.selectOptions(screen.getByLabelText('Pre-call SMS origination number'), ORIGIN);
    expect(saved().campaignConfig?.precallSms?.originationNumberArn).toBe(ORIGIN);
    expect(screen.getByRole('option', { name: /\+12025550123/ })).toBeEnabled();
  });

  it('opts a new dependent campaign into profile mode without manual text fields', async () => {
    const user = userEvent.setup();
    const saved = showCard(undefined, ['parent']);
    expect(saved().campaignConfig).not.toHaveProperty('precallSms');
    expect(screen.getByLabelText('Pre-call SMS message source')).toHaveValue('profile');
    await user.click(screen.getByRole('checkbox', { name: 'Pre-Call SMS' }));
    await user.selectOptions(screen.getByLabelText('Pre-call SMS origination number'), ORIGIN);
    expect(saved().campaignConfig?.precallSms).toEqual(PROFILE);
    expect(saved().dependsOn).toEqual(['parent']);
    expect(screen.queryByPlaceholderText('e.g. VIP Medical Group')).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText('Hi {{FirstName}}! {{ClinicName}} here…')).not.toBeInTheDocument();
    expect(screen.getByText(/using Alex as the patient name and your clinic name/)).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Clinic name (optional)' })).toHaveValue('');
    expect(screen.getByText('Leave blank to omit the clinic mention.')).toBeInTheDocument();
    const preview = screen.getByRole('textbox', { name: 'Vein message preview' });
    expect(preview).toHaveAttribute('readonly');
    await user.type(preview, 'edited text');
    expect(preview).toHaveValue(profileSmsPreview('vein'));
    expect(screen.getByRole('textbox', { name: 'Pain management message preview' })).toHaveValue(profileSmsPreview('pain'));
    expect(JSON.stringify(saved().campaignConfig?.precallSms)).not.toContain('Alex');
  });

  it('keeps legacy manual text and omitted mode unchanged when disabling and re-enabling', async () => {
    const user = userEvent.setup();
    const saved = showCard(MANUAL);
    expect(screen.getByLabelText('Pre-call SMS message source')).toHaveValue('manual');
    expect(screen.getByPlaceholderText('e.g. VIP Medical Group')).toHaveValue(MANUAL.clinicName);
    await user.click(screen.getByRole('checkbox', { name: 'Pre-Call SMS' }));
    expect(saved().campaignConfig?.precallSms).toEqual({ ...MANUAL, enabled: false });
    await user.click(screen.getByRole('checkbox', { name: 'Pre-Call SMS' }));
    expect(saved().campaignConfig?.precallSms).toEqual(MANUAL);
    expect(saved().campaignConfig?.precallSms).not.toHaveProperty('mode');
  });

  it('preserves profile configuration when a dependency is added', async () => {
    const user = userEvent.setup();
    const saved = showCard(PROFILE);
    await user.click(screen.getByRole('checkbox', { name: 'Bucket › Parent campaign' }));
    expect(saved().dependsOn).toEqual(['parent']);
    expect(saved().campaignConfig?.precallSms).toEqual(PROFILE);
    expect(screen.getByRole('checkbox', { name: 'Pre-Call SMS' })).toBeEnabled();
  });

  it('keeps manual dependency behavior but offers automatic mode without removing the dependency', async () => {
    const user = userEvent.setup();
    const saved = showCard(MANUAL);
    await user.click(screen.getByRole('checkbox', { name: 'Bucket › Parent campaign' }));
    expect(saved().dependsOn).toEqual(['parent']);
    expect(saved().campaignConfig).not.toHaveProperty('precallSms');
    await user.click(screen.getByRole('checkbox', { name: 'Pre-Call SMS' }));
    expect(saved().campaignConfig?.precallSms?.mode).toBe('profile');
    expect(saved().dependsOn).toEqual(['parent']);
  });

  it('keeps the chosen clinic and drops the manual template across source changes', async () => {
    const user = userEvent.setup();
    const saved = showCard(MANUAL);
    await user.selectOptions(screen.getByLabelText('Pre-call SMS message source'), 'profile');
    expect(saved().campaignConfig?.precallSms).toEqual({ ...PROFILE, clinicName: MANUAL.clinicName });
    expect(saved().campaignConfig?.precallSms).not.toHaveProperty('messageTemplate');
    expect(screen.getByRole('textbox', { name: 'Clinic name (optional)' })).toHaveValue(MANUAL.clinicName);
    expect(screen.queryByPlaceholderText('e.g. VIP Medical Group')).not.toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Vein message preview' })).toHaveAttribute('readonly');
    expect(screen.getByRole('textbox', { name: 'Vein message preview' })).toHaveValue(profileSmsPreview('vein', MANUAL.clinicName));
    await user.selectOptions(screen.getByLabelText('Pre-call SMS message source'), 'manual');
    expect(screen.getByPlaceholderText('e.g. VIP Medical Group')).toHaveValue(MANUAL.clinicName);
    expect(screen.getByPlaceholderText('Hi {{FirstName}}! {{ClinicName}} here…')).toHaveValue('');
  });

  it('updates both previews as the clinic is typed and removes the entire phrase when cleared', async () => {
    const user = userEvent.setup();
    const saved = showCard(PROFILE);
    const clinic = screen.getByRole('textbox', { name: 'Clinic name (optional)' });
    await user.type(clinic, '  North Clinic  ');
    expect(saved().campaignConfig?.precallSms?.clinicName).toBe('  North Clinic  ');
    expect(screen.getByRole('textbox', { name: 'Vein message preview' })).toHaveValue('Hi Alex! This is North Clinic. We’re about to give you a quick call regarding your vein consultation request. Look out for a call!');
    expect(screen.getByRole('textbox', { name: 'Pain management message preview' })).toHaveValue('Hi Alex! North Clinic here. We’re calling you in just a moment to discuss your pain management request. Talk soon!');
    await user.clear(clinic);
    expect(saved().campaignConfig?.precallSms?.clinicName).toBe('');
    expect(screen.getByRole('textbox', { name: 'Vein message preview' })).toHaveValue('Hi Alex! We’re about to give you a quick call regarding your vein consultation request. Look out for a call!');
    expect(screen.getByRole('textbox', { name: 'Pain management message preview' })).toHaveValue('Hi Alex! We’re calling you in just a moment to discuss your pain management request. Talk soon!');
    expect(saved().campaignConfig?.precallSms).not.toHaveProperty('messageTemplate');
  });

  it('restores the campaign clinic from saved JSON and preserves it through disabling', async () => {
    const user = userEvent.setup();
    const saved = showCard(PROFILE);
    await user.type(screen.getByRole('textbox', { name: 'Clinic name (optional)' }), 'Clínica Norte');
    const persisted = saved().campaignConfig!.precallSms!;
    cleanup();
    const reloaded = showCard(persisted);
    expect(screen.getByRole('textbox', { name: 'Clinic name (optional)' })).toHaveValue('Clínica Norte');
    expect(screen.getByRole('textbox', { name: 'Vein message preview' })).toHaveValue(profileSmsPreview('vein', 'Clínica Norte'));
    await user.click(screen.getByRole('checkbox', { name: 'Pre-Call SMS' }));
    expect(reloaded().campaignConfig?.precallSms).toEqual({ ...persisted, enabled: false });
    await user.click(screen.getByRole('checkbox', { name: 'Pre-Call SMS' }));
    expect(reloaded().campaignConfig?.precallSms).toEqual(persisted);
    expect(screen.getByRole('textbox', { name: 'Clinic name (optional)' })).toHaveValue('Clínica Norte');
  });

  it('does not replace an unknown catalog version or present its copy as supported', async () => {
    const user = userEvent.setup();
    const saved = showCard({ ...PROFILE, catalogVersion: 'future' } as unknown as PrecallSmsConfig);
    expect(screen.getByRole('alert')).toHaveTextContent(/catalog version is unsupported/);
    expect(screen.queryByRole('textbox', { name: 'Vein message preview' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('checkbox', { name: 'Pre-Call SMS' }));
    await waitFor(() => expect(saved().campaignConfig?.precallSms).toEqual({ ...PROFILE, catalogVersion: 'future', enabled: false }));
  });
});
