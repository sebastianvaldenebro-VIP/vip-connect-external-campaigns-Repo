import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import type { ReactNode } from 'react';

import type { ContactFlow, PhoneNumber, Queue } from '@/lib/api';

const mockNavigate = vi.fn();
vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>();
  return { ...actual, useNavigate: () => mockNavigate };
});

const queuesMock = vi.fn();
const contactFlowsMock = vi.fn();
const phoneNumbersMock = vi.fn();
const createMock = vi.fn();
const startMock = vi.fn();
const resolveCampaignFlowMock = vi.fn();

vi.mock('@/lib/api', () => ({
  api: {
    campaigns: {
      queues: () => queuesMock(),
      contactFlows: () => contactFlowsMock(),
      phoneNumbers: () => phoneNumbersMock(),
      create: (body: unknown) => createMock(body),
      start: (id: string) => startMock(id),
    },
    plans: {
      resolveCampaignFlow: (states: string[]) => resolveCampaignFlowMock(states),
    },
  },
}));

import { EnableCampaignModal } from './EnableCampaignModal';

const QUEUE_DEFAULT: Queue = { id: 'q1', arn: 'arn:aws:connect:us-east-1:165505826690:instance/x/queue/q1', name: 'agents outbound' };
const QUEUE_HIGH: Queue = { id: 'q2', arn: 'arn:aws:connect:us-east-1:165505826690:instance/x/queue/q2', name: 'high priority agents outbounds' };
const FLOW_DEFAULT: ContactFlow = { id: 'f1', arn: 'arn:aws:connect:us-east-1:165505826690:instance/x/contact-flow/f1', name: '*Agent-staffed Campaign AMD', contactFlowType: 'CONTACT_FLOW' };
const CAMPAIGN_FLOW_TX: ContactFlow = { id: 'f2', arn: 'arn:aws:connect:us-east-1:165505826690:instance/x/contact-flow/f2', name: 'campaign-TX', contactFlowType: 'CAMPAIGN' };
// Canonical TX number per STATE_DEFAULT_PHONES.
const PHONE_TX_CANONICAL: PhoneNumber = { arn: 'arn:aws:connect:us-east-1:165505826690:phone/p1', number: '+15126508970' };
const PHONE_NON_CANONICAL: PhoneNumber = { arn: 'arn:aws:connect:us-east-1:165505826690:phone/p2', number: '+15125550100' };

function renderModal(
  props: Partial<Parameters<typeof EnableCampaignModal>[0]> = {},
): { onClose: ReturnType<typeof vi.fn>; onSuccess: ReturnType<typeof vi.fn> } {
  const onClose = vi.fn();
  const onSuccess = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <EnableCampaignModal
          open
          onClose={onClose}
          segmentName="TX-Vein-New-Leads-1200"
          segmentArn="arn:aws:connect:us-east-1:165505826690:instance/x/segment/s1"
          segmentStates={['TX']}
          onSuccess={onSuccess}
          {...props}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { onClose, onSuccess };
}

function resolveHappyPath(): void {
  queuesMock.mockResolvedValue({ queues: [QUEUE_DEFAULT, QUEUE_HIGH] });
  contactFlowsMock.mockResolvedValue({ contactFlows: [FLOW_DEFAULT, CAMPAIGN_FLOW_TX] });
  phoneNumbersMock.mockResolvedValue({ phoneNumbers: [PHONE_TX_CANONICAL] });
  resolveCampaignFlowMock.mockResolvedValue({ arn: null });
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('<EnableCampaignModal /> — loading & closed', () => {
  it('renders nothing when open=false', () => {
    queuesMock.mockReturnValue(new Promise(() => {}));
    contactFlowsMock.mockReturnValue(new Promise(() => {}));
    phoneNumbersMock.mockReturnValue(new Promise(() => {}));
    const client = new QueryClient();
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <EnableCampaignModal
            open={false}
            onClose={vi.fn()}
            segmentName="Seg"
            segmentArn="arn:seg"
            segmentStates={['TX']}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('hides the dialog cleanly when open flips from true to false after defaults have resolved', async () => {
    resolveHappyPath();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const onClose = vi.fn();
    const { rerender } = render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <EnableCampaignModal
            open
            onClose={onClose}
            segmentName="TX-Vein-New-Leads-1200"
            segmentArn="arn:seg"
            segmentStates={['TX']}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getByText('agents outbound')).toBeInTheDocument());

    rerender(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <EnableCampaignModal
            open={false}
            onClose={onClose}
            segmentName="TX-Vein-New-Leads-1200"
            segmentArn="arn:seg"
            segmentStates={['TX']}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('shows the loading spinner while resources resolve', () => {
    queuesMock.mockReturnValue(new Promise(() => {}));
    contactFlowsMock.mockReturnValue(new Promise(() => {}));
    phoneNumbersMock.mockReturnValue(new Promise(() => {}));
    renderModal();
    expect(screen.getByText(/Resolving defaults from your Connect instance/i)).toBeInTheDocument();
  });
});

describe('<EnableCampaignModal /> — resolved defaults', () => {
  it('shows no errors and enables "Create and start" when everything resolves', async () => {
    resolveHappyPath();
    renderModal();

    await waitFor(() => expect(screen.getByText('agents outbound')).toBeInTheDocument());
    expect(screen.queryByText(/Cannot create with defaults/i)).not.toBeInTheDocument();
    expect(screen.getByText('campaign-TX')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /create and start/i })).not.toBeDisabled();
  });

  it('flags a phone number that does not match the canonical number for the state', async () => {
    queuesMock.mockResolvedValue({ queues: [QUEUE_DEFAULT, QUEUE_HIGH] });
    contactFlowsMock.mockResolvedValue({ contactFlows: [FLOW_DEFAULT, CAMPAIGN_FLOW_TX] });
    phoneNumbersMock.mockResolvedValue({ phoneNumbers: [PHONE_NON_CANONICAL] });
    resolveCampaignFlowMock.mockResolvedValue({ arn: null });
    renderModal();

    await waitFor(() => expect(screen.getByText('+15125550100')).toBeInTheDocument());
    expect(screen.getByText('fallback')).toBeInTheDocument();
    expect(screen.getByText(/is not the canonical number for TX/i)).toBeInTheDocument();
  });

  it('treats a malformed segmentGroups shape (Dimensions not an array) as not high-priority', async () => {
    resolveHappyPath();
    renderModal({
      segmentGroups: { Groups: [{ Dimensions: 'not-an-array' }] },
    });

    await waitFor(() => expect(screen.getByText('agents outbound')).toBeInTheDocument());
    expect(screen.queryByText('high priority agents outbounds')).not.toBeInTheDocument();
  });

  it('reports the high-priority queue name in the error when no queue at all is provisioned for a high-priority segment', async () => {
    queuesMock.mockResolvedValue({ queues: [] });
    contactFlowsMock.mockResolvedValue({ contactFlows: [FLOW_DEFAULT, CAMPAIGN_FLOW_TX] });
    phoneNumbersMock.mockResolvedValue({ phoneNumbers: [PHONE_TX_CANONICAL] });
    resolveCampaignFlowMock.mockResolvedValue({ arn: null });
    renderModal({
      segmentGroups: {
        Groups: [
          {
            Dimensions: [
              { ProfileAttributes: { Attributes: { groups: { Values: ['new lead / new lead'] } } } },
            ],
          },
        ],
      },
    });

    await waitFor(() =>
      expect(screen.getByText(/Queue "high priority agents outbounds" not found\./)).toBeInTheDocument(),
    );
  });

  it('reports multiple simultaneous issues in the plural when every default is missing', async () => {
    queuesMock.mockResolvedValue({ queues: [] });
    contactFlowsMock.mockResolvedValue({ contactFlows: [] });
    phoneNumbersMock.mockResolvedValue({ phoneNumbers: [] });
    resolveCampaignFlowMock.mockResolvedValue({ arn: null });
    renderModal();

    await waitFor(() => expect(screen.getByText(/4 issues:/)).toBeInTheDocument());
    expect(screen.queryByText(/1 issue:/)).not.toBeInTheDocument();
  });

  it('still flags the fallback warning for a state with no canonical default number (never matches)', async () => {
    queuesMock.mockResolvedValue({ queues: [QUEUE_DEFAULT] });
    contactFlowsMock.mockResolvedValue({ contactFlows: [FLOW_DEFAULT] });
    // 'ZZ' is not a real state code and has no entry in STATE_DEFAULT_PHONES —
    // exercises the `canonical ? ... : false` fallback inside phoneMatchesState.
    phoneNumbersMock.mockResolvedValue({ phoneNumbers: [PHONE_NON_CANONICAL] });
    resolveCampaignFlowMock.mockResolvedValue({ arn: null });
    renderModal({ segmentStates: ['ZZ'] });

    await waitFor(() => expect(screen.getByText('+15125550100')).toBeInTheDocument());
    expect(screen.getByText('fallback')).toBeInTheDocument();
  });

  it('routes to the high-priority queue for a "new lead / new lead" segment group', async () => {
    resolveHappyPath();
    renderModal({
      segmentGroups: {
        Groups: [
          {
            Dimensions: [
              { ProfileAttributes: { Attributes: { groups: { Values: ['new lead / new lead'] } } } },
            ],
          },
        ],
      },
    });

    await waitFor(() => expect(screen.getByText('high priority agents outbounds')).toBeInTheDocument());
  });

  it('falls back to the default queue for a high-priority segment when the high-priority queue itself is missing', async () => {
    queuesMock.mockResolvedValue({ queues: [QUEUE_DEFAULT] }); // no high-priority queue provisioned
    contactFlowsMock.mockResolvedValue({ contactFlows: [FLOW_DEFAULT, CAMPAIGN_FLOW_TX] });
    phoneNumbersMock.mockResolvedValue({ phoneNumbers: [PHONE_TX_CANONICAL] });
    resolveCampaignFlowMock.mockResolvedValue({ arn: null });
    renderModal({
      segmentGroups: {
        Groups: [
          {
            Dimensions: [
              { ProfileAttributes: { Attributes: { groups: { Values: ['new lead / new lead'] } } } },
            ],
          },
        ],
      },
    });

    await waitFor(() => expect(screen.getByText('agents outbound')).toBeInTheDocument());
    expect(screen.queryByText(/Queue ".*" not found/)).not.toBeInTheDocument();
  });

  it('shows an error banner and disables actions when the default queue is missing', async () => {
    queuesMock.mockResolvedValue({ queues: [] });
    contactFlowsMock.mockResolvedValue({ contactFlows: [FLOW_DEFAULT, CAMPAIGN_FLOW_TX] });
    phoneNumbersMock.mockResolvedValue({ phoneNumbers: [PHONE_TX_CANONICAL] });
    resolveCampaignFlowMock.mockResolvedValue({ arn: null });
    renderModal();

    await waitFor(() => expect(screen.getByText(/Cannot create with defaults/i)).toBeInTheDocument());
    expect(screen.getByText(/Queue "agents outbound" not found\./)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /create and start/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /edit before/i })).toBeDisabled();
  });

  it('shows an error when no campaign flow can be resolved for the state at all', async () => {
    queuesMock.mockResolvedValue({ queues: [QUEUE_DEFAULT] });
    contactFlowsMock.mockResolvedValue({ contactFlows: [FLOW_DEFAULT] }); // no CAMPAIGN-type flow
    phoneNumbersMock.mockResolvedValue({ phoneNumbers: [PHONE_TX_CANONICAL] });
    resolveCampaignFlowMock.mockResolvedValue({ arn: null });
    renderModal();

    await waitFor(() =>
      expect(screen.getByText(/No campaign flow found for this state/i)).toBeInTheDocument(),
    );
  });

  it('shows the contact flow as missing ("—" / bad tone) when it is not provisioned', async () => {
    queuesMock.mockResolvedValue({ queues: [QUEUE_DEFAULT] });
    contactFlowsMock.mockResolvedValue({ contactFlows: [CAMPAIGN_FLOW_TX] }); // no DEFAULT_FLOW_NAME
    phoneNumbersMock.mockResolvedValue({ phoneNumbers: [PHONE_TX_CANONICAL] });
    resolveCampaignFlowMock.mockResolvedValue({ arn: null });
    renderModal();

    await waitFor(() =>
      expect(screen.getByText(/Contact flow "\*Agent-staffed Campaign AMD" not found\./)).toBeInTheDocument(),
    );
    expect(screen.getByText('missing')).toBeInTheDocument();
  });

  it('shows exactly one issue in the singular when only a single default is missing', async () => {
    queuesMock.mockResolvedValue({ queues: [QUEUE_DEFAULT] });
    contactFlowsMock.mockResolvedValue({ contactFlows: [FLOW_DEFAULT, CAMPAIGN_FLOW_TX] });
    phoneNumbersMock.mockResolvedValue({ phoneNumbers: [] }); // the only missing default
    resolveCampaignFlowMock.mockResolvedValue({ arn: null });
    renderModal();

    await waitFor(() => expect(screen.getByText(/1 issue:/)).toBeInTheDocument());
    expect(screen.queryByText(/issues:/)).not.toBeInTheDocument();
    expect(screen.getByText('No phone numbers available.')).toBeInTheDocument();
    expect(screen.getByText('—', { selector: 'span.font-mono' })).toBeInTheDocument();
  });

  it('skips backend campaign-flow resolution and still resolves the phone when segmentStates is empty', async () => {
    queuesMock.mockResolvedValue({ queues: [QUEUE_DEFAULT] });
    contactFlowsMock.mockResolvedValue({ contactFlows: [FLOW_DEFAULT] });
    phoneNumbersMock.mockResolvedValue({ phoneNumbers: [PHONE_NON_CANONICAL] });
    renderModal({ segmentStates: [] });

    await waitFor(() => expect(screen.getByText('+15125550100')).toBeInTheDocument());
    // No states means resolveCampaignFlowArn's backend call is never invoked.
    expect(resolveCampaignFlowMock).not.toHaveBeenCalled();
    // Phone tone falls back to 'ok' (no fallback badge) because there's no state to compare against.
    expect(screen.queryByText('fallback')).not.toBeInTheDocument();
    expect(screen.getByText(/No campaign flow found for this state/i)).toBeInTheDocument();
  });

  it('prefers the backend-resolved campaign flow over the client heuristic', async () => {
    queuesMock.mockResolvedValue({ queues: [QUEUE_DEFAULT] });
    contactFlowsMock.mockResolvedValue({ contactFlows: [FLOW_DEFAULT, CAMPAIGN_FLOW_TX] });
    phoneNumbersMock.mockResolvedValue({ phoneNumbers: [PHONE_TX_CANONICAL] });
    resolveCampaignFlowMock.mockResolvedValue({
      arn: 'arn:aws:connect:us-east-1:165505826690:instance/x/contact-flow/backend-resolved',
    });
    renderModal();

    await waitFor(() => expect(screen.getByText('Resolved via backend')).toBeInTheDocument());
  });

  it('falls back to the client-side campaign flow suggestion when the backend call throws', async () => {
    queuesMock.mockResolvedValue({ queues: [QUEUE_DEFAULT] });
    contactFlowsMock.mockResolvedValue({ contactFlows: [FLOW_DEFAULT, CAMPAIGN_FLOW_TX] });
    phoneNumbersMock.mockResolvedValue({ phoneNumbers: [PHONE_TX_CANONICAL] });
    resolveCampaignFlowMock.mockRejectedValue(new Error('network down'));
    renderModal();

    await waitFor(() => expect(screen.getByText('campaign-TX')).toBeInTheDocument());
    expect(screen.queryByText('Resolved via backend')).not.toBeInTheDocument();
  });
});

describe('<EnableCampaignModal /> — interactions', () => {
  it('lets the operator override the campaign name', async () => {
    resolveHappyPath();
    renderModal();
    await waitFor(() => expect(screen.getByText('agents outbound')).toBeInTheDocument());

    const input = screen.getByRole('textbox') as HTMLInputElement;
    const user = userEvent.setup();
    await user.clear(input);
    await user.type(input, 'My-Custom-Name');
    expect(input.value).toBe('My-Custom-Name');
  });

  it('Cancel closes the modal without navigating', async () => {
    resolveHappyPath();
    const { onClose } = renderModal();
    await waitFor(() => expect(screen.getByText('agents outbound')).toBeInTheDocument());

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /cancel/i }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it('"Edit before" closes the modal and navigates to /campaigns/new with the prefilled body', async () => {
    resolveHappyPath();
    const { onClose } = renderModal();
    await waitFor(() => expect(screen.getByText('agents outbound')).toBeInTheDocument());

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /edit before/i }));

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(mockNavigate).toHaveBeenCalledWith(
      '/campaigns/new',
      expect.objectContaining({
        state: expect.objectContaining({
          prefilledBody: expect.objectContaining({ queueId: 'q1', sourcePhoneNumber: '+15126508970' }),
        }),
      }),
    );
  });

  it('"Create and start" creates, starts, calls onSuccess, closes, and navigates to the new campaign', async () => {
    resolveHappyPath();
    createMock.mockResolvedValue({ id: 'camp-123', arn: 'arn:campaign/camp-123' });
    startMock.mockResolvedValue({ state: 'Running' });
    const { onClose, onSuccess } = renderModal();
    await waitFor(() => expect(screen.getByText('agents outbound')).toBeInTheDocument());

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /create and start/i }));

    await waitFor(() => expect(onSuccess).toHaveBeenCalledWith({ id: 'camp-123', state: 'Running' }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(mockNavigate).toHaveBeenCalledWith('/campaigns/camp-123');
  });

  it('shows the "creating…" spinner state on the button while the mutation is in flight', async () => {
    resolveHappyPath();
    let resolveCreate!: (v: { id: string; arn: string }) => void;
    createMock.mockReturnValue(new Promise((res) => { resolveCreate = res; }));
    renderModal();
    await waitFor(() => expect(screen.getByText('agents outbound')).toBeInTheDocument());

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /create and start/i }));

    expect(await screen.findByText(/creating…/i)).toBeInTheDocument();
    resolveCreate({ id: 'camp-789', arn: 'arn:campaign/camp-789' });
    startMock.mockResolvedValue({ state: 'Running' });
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith('/campaigns/camp-789'));
  });

  it('shows the mutation error message when create fails', async () => {
    resolveHappyPath();
    createMock.mockRejectedValue(new Error('ThrottlingException: rate exceeded'));
    renderModal();
    await waitFor(() => expect(screen.getByText('agents outbound')).toBeInTheDocument());

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /create and start/i }));

    await waitFor(() =>
      expect(screen.getByText(/ThrottlingException: rate exceeded/)).toBeInTheDocument(),
    );
  });

  it('shows the "created but not started" banner when start fails after create succeeds', async () => {
    resolveHappyPath();
    createMock.mockResolvedValue({ id: 'camp-456', arn: 'arn:campaign/camp-456' });
    startMock.mockRejectedValue(new Error('InvalidStateException'));
    renderModal();
    await waitFor(() => expect(screen.getByText('agents outbound')).toBeInTheDocument());

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /create and start/i }));

    await waitFor(() => expect(screen.getByText(/Created but not started:/)).toBeInTheDocument());
    expect(within(screen.getByText(/Created but not started:/).parentElement!).getByText(/InvalidStateException/)).toBeInTheDocument();
  });
});
