import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ReliabilityPanel, ReliabilityOverview } from './ReliabilityPanel';
const state = vi.hoisted(() => ({
  auth: {
    user: { userId: 'alice' } as { userId: string } | null,
    groups: ['Admin'],
    loading: false,
  },
  list: vi.fn(),
  history: vi.fn(),
}));
vi.mock('@/hooks/useAuth', () => ({ useAuth: () => state.auth }));
vi.mock('@/lib/api', () => ({
  api: { audit: { list: state.list, entityHistory: state.history } },
}));
function show(props = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <ReliabilityPanel {...props} />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return client;
}
beforeEach(() => {
  state.auth = { user: { userId: 'alice' }, groups: ['Admin'], loading: false };
  state.list.mockReset();
  state.history.mockReset();
});
describe('read-only reliability panel', () => {
  it.each(['anonymous', 'Agent', 'loading'])(
    'does not call audit for %s',
    async (mode) => {
      if (mode === 'anonymous') state.auth.user = null;
      if (mode === 'Agent') state.auth.groups = ['Agent'];
      if (mode === 'loading') state.auth.loading = true;
      show({ segmentName: 'demo' });
      await Promise.resolve();
      expect(state.list).not.toHaveBeenCalled();
      expect(state.history).not.toHaveBeenCalled();
      expect(screen.queryByRole('table')).not.toBeInTheDocument();
    },
  );
  it('surfaces failure rather than suggesting healthy or empty data', async () => {
    state.history.mockRejectedValue(new Error('private backend details'));
    show({ segmentName: 'demo' });
    expect(await screen.findByRole('alert')).toHaveTextContent('unavailable');
    expect(screen.queryByText(/private backend/)).not.toBeInTheDocument();
  });
  it('queries the exact linked segment and only caches minimized observations', async () => {
    state.history.mockResolvedValue({
      entries: [
        {
          entityId: 'segment/demo',
          entityType: 'segment',
          action: 'verify',
          timestamp: new Date().toISOString(),
          actorEmail: 'private-marker',
          extra: { redisCount: 4, segmentCount: 4 },
        },
      ],
    });
    const client = show({ segmentName: 'demo', campaign: true });
    expect(
      await screen.findByText('Counts equal · membership unverified'),
    ).toBeInTheDocument();
    expect(state.history).toHaveBeenCalledWith('segment/demo');
    expect(state.list).not.toHaveBeenCalled();
    expect(
      JSON.stringify(
        client.getQueryData([
          'reliability-observations',
          'alice',
          'segment',
          'demo',
        ]),
      ),
    ).not.toContain('private-marker');
  });
  it('does not substitute all segments for a campaign without a segment link', async () => {
    show({ campaign: true });
    await waitFor(() =>
      expect(
        screen.getByText(/No Customer Profiles segment/),
      ).toBeInTheDocument(),
    );
    expect(state.list).not.toHaveBeenCalled();
    expect(state.history).not.toHaveBeenCalled();
  });
});

it('only reads newest-first entity history after selecting a dashboard segment', async () => {
  state.history.mockResolvedValue({ entries: [] });
  const client = new QueryClient();
  render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <ReliabilityOverview segmentNames={['overview', 'demo']} />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  expect(state.history).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText('Reliability segment'), {
    target: { value: 'overview' },
  });
  await waitFor(() =>
    expect(state.history).toHaveBeenCalledWith('segment/overview'),
  );
  fireEvent.change(screen.getByLabelText('Reliability segment'), {
    target: { value: 'demo' },
  });
  await waitFor(() =>
    expect(state.history).toHaveBeenCalledWith('segment/demo'),
  );
  expect(state.list).not.toHaveBeenCalled();
});
