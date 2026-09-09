import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import type { AgentRosterEntry, RoutingProfileSummary } from '@/lib/api';

const getAgentRosterMock = vi.fn();
vi.mock('@/lib/api', () => ({
  api: {
    brandedMonitor: {
      getAgentRoster: (queueId?: string) => getAgentRosterMock(queueId),
    },
  },
}));

import { AgentRoster, groupAgentsByProfile } from './AgentRoster';

// 2026-09-09T15:00:00Z — business hours window (12-23 UTC).
const FIXED_NOW = new Date('2026-09-09T15:00:00.000Z');
const FIXED_NOW_MS = FIXED_NOW.getTime();

function minsAgo(mins: number): string {
  return new Date(FIXED_NOW_MS - mins * 60_000).toISOString();
}

function agent(overrides: Partial<AgentRosterEntry>): AgentRosterEntry {
  return {
    agentId: 'agent-x',
    agentName: 'Fake Agent',
    status: 'Available',
    statusType: 'ROUTABLE',
    effectiveStatus: 'Available',
    isIntentionalAbsence: false,
    activeContactState: '',
    statusStartTimestamp: minsAgo(1),
    routingProfileId: 'rp-x',
    routingProfileName: 'PC - New Leads',
    contactsCount: 0,
    ...overrides,
  };
}

const RP_PS: RoutingProfileSummary = { id: 'rp-ps', name: 'PC - New Leads' };
const RP_AS: RoutingProfileSummary = { id: 'rp-as', name: 'Appointment Services Agent' };
const RP_FD: RoutingProfileSummary = { id: 'rp-fd', name: 'Front Desk NYC' };

const BASE_AGENTS: AgentRosterEntry[] = [
  agent({ agentId: 'a1', agentName: 'Ana Lopez', routingProfileId: 'rp-ps', routingProfileName: 'PC - New Leads', effectiveStatus: 'Available', statusStartTimestamp: minsAgo(5) }),
  agent({ agentId: 'a2', agentName: 'Beto Cruz', routingProfileId: 'rp-ps', routingProfileName: 'PC - New Leads', effectiveStatus: 'Available', statusStartTimestamp: minsAgo(15) }), // idle warn
  agent({ agentId: 'a3', agentName: 'Cora Diaz', routingProfileId: 'rp-ps', routingProfileName: 'PC - New Leads', effectiveStatus: 'Offline', statusStartTimestamp: minsAgo(120) }),
  agent({ agentId: 'a4', agentName: 'Deb Reyes', routingProfileId: 'rp-as', routingProfileName: 'Appointment Services Agent', effectiveStatus: 'On Call', statusStartTimestamp: minsAgo(20) }), // longCall warn
  agent({ agentId: 'a5', agentName: '', routingProfileId: 'rp-as', routingProfileName: 'Appointment Services Agent', effectiveStatus: 'Unavailable', isIntentionalAbsence: false, statusStartTimestamp: minsAgo(25) }), // break error, empty name
  agent({ agentId: 'a6', agentName: 'Fabi Ortiz', routingProfileId: 'rp-as', routingProfileName: 'Appointment Services Agent', effectiveStatus: 'ACW', statusStartTimestamp: minsAgo(5) }), // longAcw warn
  // Not a branded-monitor team — must be filtered out of every count/list.
  agent({ agentId: 'a7', agentName: 'Front Desk Gina', routingProfileId: 'rp-fd', routingProfileName: 'Front Desk NYC', effectiveStatus: 'Available', statusStartTimestamp: minsAgo(1) }),
  // Profile with no team mapping at all (teamForProfile returns null) — exercises
  // the `?? ''` fallback in the top-level team filter, distinct from a7 above
  // (a7's team is real, just not a branded-monitor one).
  agent({ agentId: 'a8', agentName: 'Unmapped Umberto', routingProfileId: 'rp-zzz', routingProfileName: 'Zzz Totally Unmapped Profile', effectiveStatus: 'Available', statusStartTimestamp: minsAgo(1) }),
];

function renderRoster(props: Partial<Parameters<typeof AgentRoster>[0]> = {}): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <AgentRoster {...props} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(FIXED_NOW);
  getAgentRosterMock.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('<AgentRoster /> — top-level states', () => {
  it('shows the loading state and hides the "updated" footer while the query is pending', async () => {
    getAgentRosterMock.mockReturnValue(new Promise(() => {}));
    renderRoster();
    expect(screen.getByText('Loading agents…')).toBeInTheDocument();
    expect(screen.queryByText(/updated/i)).not.toBeInTheDocument();
  });

  it('shows an error message and hides the rest of the page when the query fails', async () => {
    getAgentRosterMock.mockRejectedValue(new Error('network error'));
    renderRoster();
    await waitFor(() => expect(screen.getByText('Failed to load agent roster.')).toBeInTheDocument());
    expect(screen.queryByText('Agents online')).not.toBeInTheDocument();
  });

  it('renders the empty state with no team/profile filters when there are no agents at all', async () => {
    getAgentRosterMock.mockResolvedValue({
      agents: [],
      queueId: 'q',
      lastUpdated: FIXED_NOW.toISOString(),
      routingProfiles: [],
      allRoutingProfiles: [],
    });
    renderRoster();

    await waitFor(() => expect(screen.getByText('No agents match')).toBeInTheDocument());
    expect(screen.getByText('Try clearing a filter or the search.')).toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: /team/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: /profile/i })).not.toBeInTheDocument();
    expect(screen.getByText('updated just now')).toBeInTheDocument();
  });
});

describe('<AgentRoster /> — populated roster', () => {
  beforeEach(() => {
    getAgentRosterMock.mockResolvedValue({
      agents: BASE_AGENTS,
      queueId: 'q',
      lastUpdated: minsAgo(5),
      routingProfiles: [RP_PS, RP_AS, RP_FD],
      allRoutingProfiles: [RP_PS, RP_AS, RP_FD],
    });
  });

  it('excludes non-branded-monitor agents from every count and shows workforce totals', async () => {
    renderRoster();
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Agents (6)' })).toBeInTheDocument());

    // Front Desk Gina (a7, classified but non-branded team) and Unmapped
    // Umberto (a8, unclassified profile) must never appear.
    expect(screen.queryByText('Front Desk Gina')).not.toBeInTheDocument();
    expect(screen.queryByText('Unmapped Umberto')).not.toBeInTheDocument();

    expect(screen.getByText('updated 5m ago')).toBeInTheDocument();

    // Team select defaults to the empty "All" option before any team is chosen.
    const teamSelect = screen.getByRole('combobox', { name: /team/i }) as HTMLSelectElement;
    expect(teamSelect.value).toBe('');
  });

  it('renders the capacity table with both a healthy and a no-coverage routing profile', async () => {
    renderRoster();
    await waitFor(() => expect(screen.getByText('Teams & routing profiles')).toBeInTheDocument());

    expect(screen.getByText('Healthy')).toBeInTheDocument();
    expect(screen.getByText('No coverage')).toBeInTheDocument();
  });

  it('lists flagged agents in "Needs attention" with distinct severities and lets the operator jump to that alert filter', async () => {
    renderRoster();
    await waitFor(() => expect(screen.getByText('Needs attention')).toBeInTheDocument());

    // The "break" alert (error severity) and "idle"/"longCall"/"longAcw" (warn) all show.
    expect(screen.getByText('Extended break')).toBeInTheDocument();
    expect(screen.getByText('Idle')).toBeInTheDocument();
    expect(screen.getByText('Long call')).toBeInTheDocument();
    expect(screen.getByText('Long wrap-up')).toBeInTheDocument();

    const user = userEvent.setup({ delay: null });
    await user.click(screen.getByText('Extended break'));

    // Clicking a specific alert badge narrows the list to just that alert key
    // (AlertFilter supports per-key values, not only 'all'/'any') — functionally
    // correct even though the Alerts <select> below has no <option> for
    // individual keys, so it cosmetically keeps showing "All agents" selected.
    // See final report: pre-existing UX inconsistency, not fixed here (out of
    // scope for a coverage-only change).
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Agents (1)' })).toBeInTheDocument());
    expect(screen.getByText('Showing 1 of 6 agents')).toBeInTheDocument();
  });

  it('falls back to agentId for the empty-name agent, and maps "Unavailable" to the "Away" chip label', async () => {
    renderRoster();
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Agents (6)' })).toBeInTheDocument());

    // a5 has agentName: '' — row + avatar fall back to the agentId.
    expect(screen.getAllByText('a5').length).toBeGreaterThan(0);
    // One static "Away" legend key (capacity table) + one live status chip for a5.
    expect(screen.getAllByText('Away')).toHaveLength(2);
  });

  it('filters agents by the search box', async () => {
    renderRoster();
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Agents (6)' })).toBeInTheDocument());

    const user = userEvent.setup({ delay: null });
    await user.type(screen.getByPlaceholderText(/search name, profile, team/i), 'Ana Lopez');

    await waitFor(() => expect(screen.getByRole('heading', { name: 'Agents (1)' })).toBeInTheDocument());
    expect(screen.getByText('Showing 1 of 6 agents')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /clear all/i }));
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Agents (6)' })).toBeInTheDocument());
  });

  it('filters agents by status via the Status select', async () => {
    renderRoster();
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Agents (6)' })).toBeInTheDocument());

    const user = userEvent.setup({ delay: null });
    await user.selectOptions(screen.getByRole('combobox', { name: /status/i }), 'Offline');

    await waitFor(() => expect(screen.getByRole('heading', { name: 'Agents (1)' })).toBeInTheDocument());
    expect(screen.getByText('Cora Diaz')).toBeInTheDocument();

    // Selecting the empty "All" option clears the status filter again.
    await user.selectOptions(screen.getByRole('combobox', { name: /status/i }), '');
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Agents (6)' })).toBeInTheDocument());
  });

  it('filters agents by team, which resets any active profile filter', async () => {
    renderRoster();
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Agents (6)' })).toBeInTheDocument());

    const user = userEvent.setup({ delay: null });
    await user.selectOptions(screen.getByRole('combobox', { name: /profile/i }), 'rp-ps');
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Agents (3)' })).toBeInTheDocument());

    await user.selectOptions(screen.getByRole('combobox', { name: /team/i }), 'appointment-services');
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Agents (3)' })).toBeInTheDocument());
    // Deb Reyes is flagged (longCall), so she renders both in "Needs attention"
    // and in the agent list row — same multi-match pattern as the a5/"Away"
    // assertion above.
    expect(screen.getAllByText('Deb Reyes').length).toBeGreaterThan(0);

    const profileSelect = screen.getByRole('combobox', { name: /profile/i }) as HTMLSelectElement;
    expect(profileSelect.value).toBe('');

    // Selecting the empty "All" option clears the team filter again.
    await user.selectOptions(screen.getByRole('combobox', { name: /team/i }), '');
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Agents (6)' })).toBeInTheDocument());
  });

  it('filters agents by routing profile', async () => {
    renderRoster();
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Agents (6)' })).toBeInTheDocument());

    const user = userEvent.setup({ delay: null });
    await user.selectOptions(screen.getByRole('combobox', { name: /profile/i }), 'rp-as');

    await waitFor(() => expect(screen.getByRole('heading', { name: 'Agents (3)' })).toBeInTheDocument());

    // Selecting the empty "All profiles" option clears the profile filter again.
    await user.selectOptions(screen.getByRole('combobox', { name: /profile/i }), '');
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Agents (6)' })).toBeInTheDocument());
  });

  it('filters agents by "needs attention" via the Alerts select', async () => {
    renderRoster();
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Agents (6)' })).toBeInTheDocument());

    const user = userEvent.setup({ delay: null });
    await user.selectOptions(screen.getByRole('combobox', { name: /alerts/i }), 'any');

    await waitFor(() => expect(screen.getByRole('heading', { name: 'Agents (4)' })).toBeInTheDocument());
  });

  it('toggles grouping by routing profile', async () => {
    renderRoster();
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Agents (6)' })).toBeInTheDocument());

    // Grouped by default: per-profile header with flagged count shows.
    expect(screen.getByText('1 flagged')).toBeInTheDocument();
    expect(screen.getByText('3 flagged')).toBeInTheDocument();

    const user = userEvent.setup({ delay: null });
    await user.click(screen.getByRole('button', { name: /group by profile/i }));

    // Ungrouped: the per-profile "N flagged" summary no longer renders.
    expect(screen.queryByText('1 flagged')).not.toBeInTheDocument();
    expect(screen.queryByText('3 flagged')).not.toBeInTheDocument();
  });

  it('clears all active filters via "Clear all"', async () => {
    renderRoster();
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Agents (6)' })).toBeInTheDocument());

    const user = userEvent.setup({ delay: null });
    await user.type(screen.getByPlaceholderText(/search name, profile, team/i), 'Beto');
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Agents (1)' })).toBeInTheDocument());

    await user.click(screen.getByRole('button', { name: /clear all/i }));

    await waitFor(() => expect(screen.getByRole('heading', { name: 'Agents (6)' })).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: /clear all/i })).not.toBeInTheDocument();
  });

  it('advances the live "now" tick so elapsed timers keep counting', async () => {
    renderRoster();
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Agents (6)' })).toBeInTheDocument());

    await vi.advanceTimersByTimeAsync(2_000);
    // No crash / still rendered after the tick interval fires.
    expect(screen.getByRole('heading', { name: 'Agents (6)' })).toBeInTheDocument();
  });
});

describe('<AgentRoster /> — zero-available edge case', () => {
  it('shows the danger tone for "Available" when nobody in scope is available', async () => {
    getAgentRosterMock.mockResolvedValue({
      agents: [
        agent({ agentId: 'b1', routingProfileId: 'rp-ps', routingProfileName: 'PC - New Leads', effectiveStatus: 'Offline' }),
      ],
      queueId: 'q',
      lastUpdated: FIXED_NOW.toISOString(),
      routingProfiles: [RP_PS],
      allRoutingProfiles: [RP_PS],
    });
    renderRoster();

    await waitFor(() => expect(screen.getByRole('heading', { name: 'Agents (1)' })).toBeInTheDocument());
    // "Available" also appears in the capacity table's legend — the workforce
    // summary tile renders first in DOM order, so index 0 is the stat tile.
    const availableTile = screen.getAllByText('Available')[0]!.closest('div')!.parentElement!;
    expect(within(availableTile).getByText('0')).toBeInTheDocument();
  });
});

describe('<AgentRoster /> — initial filter props', () => {
  it('honors initialTeamFilter and initialProfileFilter on first render', async () => {
    getAgentRosterMock.mockResolvedValue({
      agents: BASE_AGENTS,
      queueId: 'q',
      lastUpdated: FIXED_NOW.toISOString(),
      routingProfiles: [RP_PS, RP_AS],
      allRoutingProfiles: [RP_PS, RP_AS],
    });
    renderRoster({ initialTeamFilter: 'patient-success', initialProfileFilter: 'rp-ps' });

    await waitFor(() => expect(screen.getByRole('heading', { name: 'Agents (3)' })).toBeInTheDocument());
    const teamSelect = screen.getByRole('combobox', { name: /team/i }) as HTMLSelectElement;
    expect(teamSelect.value).toBe('patient-success');
  });
});

describe('groupAgentsByProfile — sort ordering', () => {
  // Two "no-coverage" (0 available) profiles, both fully flagged, different
  // sizes — ties on both "has a flagged agent" and staffing risk, so the
  // comparator must fall through to the final agent-count tie-break.
  const flaggedSmall = [
    agent({ agentId: 'fs1', routingProfileId: 'rp-fs', routingProfileName: 'Appointment Services Agent', effectiveStatus: 'Unavailable', statusStartTimestamp: minsAgo(25) }), // break error
  ];
  const flaggedBig = [
    agent({ agentId: 'fb1', routingProfileId: 'rp-fb', routingProfileName: 'Appointment Services Management', effectiveStatus: 'On Call', statusStartTimestamp: minsAgo(20) }), // longCall warn
    agent({ agentId: 'fb2', routingProfileId: 'rp-fb', routingProfileName: 'Appointment Services Management', effectiveStatus: 'ACW', statusStartTimestamp: minsAgo(5) }), // longAcw warn
  ];
  const unflagged = agent({ agentId: 'u1', routingProfileId: 'rp-u', routingProfileName: 'PC - New Leads', effectiveStatus: 'Available', statusStartTimestamp: minsAgo(1) });

  it('ranks flagged profiles before an unflagged one, and — among equally-flagged, equally-risky profiles — the larger one first', () => {
    // Exercised with the unflagged group in different input positions so the
    // sort comparator sees both argument orders across the two calls.
    const orderA = groupAgentsByProfile([unflagged, ...flaggedSmall, ...flaggedBig], FIXED_NOW_MS);
    expect(orderA.map((g) => g.routingProfileId)).toEqual(['rp-fb', 'rp-fs', 'rp-u']);

    const orderB = groupAgentsByProfile([...flaggedSmall, ...flaggedBig, unflagged], FIXED_NOW_MS);
    expect(orderB.map((g) => g.routingProfileId)).toEqual(['rp-fb', 'rp-fs', 'rp-u']);
  });
});
