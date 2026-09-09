import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import type { AuditEntry } from '@/lib/api';

const listMock = vi.fn();
vi.mock('@/lib/api', () => ({
  api: {
    audit: {
      list: (query: unknown) => listMock(query),
    },
  },
}));

import { Audit } from './Audit';

function entry(overrides: Partial<AuditEntry>): AuditEntry {
  return {
    entityId: 'segment/seg-1',
    action: 'create',
    timestamp: '2026-09-01T12:00:00.000Z',
    ...overrides,
  };
}

function renderAudit(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <Audit />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  listMock.mockReset();
});

describe('<Audit /> — list states', () => {
  it('shows a loading spinner while the query is pending', () => {
    listMock.mockReturnValue(new Promise(() => {}));
    renderAudit();
    expect(document.querySelector('.animate-spin')).not.toBeNull();
  });

  it('shows the error message when the query fails', async () => {
    listMock.mockRejectedValue(new Error('DynamoDB throttled'));
    renderAudit();
    await waitFor(() => expect(screen.getByText('DynamoDB throttled')).toBeInTheDocument());
  });

  it('shows the empty state when no entries match', async () => {
    listMock.mockResolvedValue({ entries: [], count: 0 });
    renderAudit();
    await waitFor(() => expect(screen.getByText('No events match these filters.')).toBeInTheDocument());
  });

  it('renders a row per entry with actor email preferred over actorSub, and a fallback dash when neither is set', async () => {
    listMock.mockResolvedValue({
      entries: [
        entry({ entityId: 'segment/s1', action: 'create', actorEmail: 'agent.one@example.com', actorSub: 'sub-1', timestamp: '2026-09-01T12:00:00.000Z' }),
        entry({ entityId: 'campaign/c1', action: 'delete', actorSub: 'sub-2', timestamp: '2026-09-01T13:00:00.000Z' }),
        entry({ entityId: 'plan_run/p1/r1', action: 'window_closed', timestamp: '2026-09-01T14:00:00.000Z' }),
      ],
      count: 3,
    });
    renderAudit();

    await waitFor(() => expect(screen.getByText('agent.one@example.com')).toBeInTheDocument());
    expect(screen.getByText('sub-2')).toBeInTheDocument();
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
    // Action badges render as <span> — the filter <select> also has same-named <option>s.
    expect(screen.getByText('create', { selector: 'span' })).toBeInTheDocument();
    expect(screen.getByText('delete', { selector: 'span' })).toBeInTheDocument();
    expect(screen.getByText('window_closed', { selector: 'span' })).toBeInTheDocument();
  });
});

describe('<Audit /> — detail panel', () => {
  it('shows the placeholder hint before any row is selected', async () => {
    listMock.mockResolvedValue({
      entries: [entry({ entityId: 'segment/s1', action: 'create' })],
      count: 1,
    });
    renderAudit();
    await waitFor(() => expect(screen.getByText('create')).toBeInTheDocument());
    expect(screen.getByText('Click a row to see the full before/after diff.')).toBeInTheDocument();
  });

  it('shows entity/action/actor plus before, after and extra JSON once a full row is selected', async () => {
    listMock.mockResolvedValue({
      entries: [
        entry({
          entityId: 'segment/s1',
          action: 'update',
          actorEmail: 'agent.two@example.com',
          ipAddress: '203.0.113.5',
          before: { status: 'draft' },
          after: { status: 'active' },
          extra: { bucketIndex: 0, bucketName: 'NJ/CT' },
        }),
      ],
      count: 1,
    });
    renderAudit();
    await waitFor(() => expect(screen.getByText('update')).toBeInTheDocument());

    const user = userEvent.setup();
    await user.click(screen.getByText('segment/s1'));

    // The detail panel's actor line renders in a <p>; the table's Actor cell is a <td>.
    expect(screen.getByText('agent.two@example.com', { selector: 'p' })).toBeInTheDocument();
    expect(screen.getByText('from 203.0.113.5')).toBeInTheDocument();
    expect(screen.getByText(/"status": "draft"/)).toBeInTheDocument();
    expect(screen.getByText(/"status": "active"/)).toBeInTheDocument();
    expect(screen.getByText(/"bucketName": "NJ\/CT"/)).toBeInTheDocument();
  });

  it('omits the IP line and the before/after/extra blocks when none are present on the entry', async () => {
    listMock.mockResolvedValue({
      entries: [entry({ entityId: 'segment/s2', action: 'estimate' })],
      count: 1,
    });
    renderAudit();
    await waitFor(() => expect(screen.getByText('estimate')).toBeInTheDocument());

    const user = userEvent.setup();
    await user.click(screen.getByText('segment/s2'));

    expect(screen.queryByText(/^from /)).not.toBeInTheDocument();
    expect(screen.queryByText('Before')).not.toBeInTheDocument();
    expect(screen.queryByText('After')).not.toBeInTheDocument();
    expect(screen.queryByText('Extra')).not.toBeInTheDocument();
  });

  it('re-highlights the newly selected row and keeps only one row highlighted at a time', async () => {
    listMock.mockResolvedValue({
      entries: [
        entry({ entityId: 'segment/s1', action: 'create', timestamp: '2026-09-01T12:00:00.000Z' }),
        entry({ entityId: 'segment/s2', action: 'delete', timestamp: '2026-09-01T13:00:00.000Z' }),
      ],
      count: 2,
    });
    renderAudit();
    await waitFor(() => expect(screen.getByText('create')).toBeInTheDocument());

    const user = userEvent.setup();
    await user.click(screen.getByText('segment/s1'));
    const rowOne = screen.getByText('segment/s1').closest('tr')!;
    expect(rowOne.className).toContain('bg-blue-50');

    await user.click(screen.getByText('segment/s2'));
    const rowTwo = screen.getByText('segment/s2').closest('tr')!;
    expect(rowTwo.className).toContain('bg-blue-50');
    expect(rowOne.className).not.toContain('bg-blue-50');
  });
});

describe('<Audit /> — filters', () => {
  it('applies actor/action/entityType filters on submit and includes them in the query', async () => {
    listMock.mockResolvedValue({ entries: [], count: 0 });
    renderAudit();
    await waitFor(() =>
      expect(listMock).toHaveBeenCalledWith({ actor: undefined, action: undefined, entityType: undefined, limit: 100 }),
    );

    const user = userEvent.setup();
    await user.type(screen.getByPlaceholderText('uuid-or-email'), 'agent.one@example.com');
    const [actionSelect, entityTypeSelect] = screen.getAllByRole('combobox');
    await user.selectOptions(actionSelect!, 'delete');
    await user.selectOptions(entityTypeSelect!, 'campaign');
    await user.click(screen.getByRole('button', { name: /apply/i }));

    await waitFor(() =>
      expect(listMock).toHaveBeenLastCalledWith({
        actor: 'agent.one@example.com',
        action: 'delete',
        entityType: 'campaign',
        limit: 100,
      }),
    );
  });

  it('resets applied and pending filters back to defaults, re-querying with no filters', async () => {
    listMock.mockResolvedValue({ entries: [], count: 0 });
    renderAudit();
    await waitFor(() =>
      expect(listMock).toHaveBeenCalledWith({ actor: undefined, action: undefined, entityType: undefined, limit: 100 }),
    );

    const user = userEvent.setup();
    await user.type(screen.getByPlaceholderText('uuid-or-email'), 'someone');
    await user.click(screen.getByRole('button', { name: /apply/i }));
    await waitFor(() =>
      expect(listMock).toHaveBeenLastCalledWith({ actor: 'someone', action: undefined, entityType: undefined, limit: 100 }),
    );

    await user.click(screen.getByRole('button', { name: /reset/i }));

    expect((screen.getByPlaceholderText('uuid-or-email') as HTMLInputElement).value).toBe('');
    await waitFor(() =>
      expect(listMock).toHaveBeenLastCalledWith({ actor: undefined, action: undefined, entityType: undefined, limit: 100 }),
    );
  });
});
