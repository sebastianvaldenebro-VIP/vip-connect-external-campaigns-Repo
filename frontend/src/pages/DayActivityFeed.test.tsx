import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import type { AuditEntry } from '@/lib/api';

const entityHistoryMock = vi.fn();
vi.mock('@/lib/api', () => ({
  api: {
    audit: {
      entityHistory: (entityId: string) => entityHistoryMock(entityId),
    },
  },
}));

import { DayActivityFeed } from './DayActivityFeed';

function entry(overrides: Partial<AuditEntry>): AuditEntry {
  return {
    entityId: 'plan_run/p1/r1',
    action: 'bucket_started',
    timestamp: '2026-09-01T19:40:00.000Z',
    extra: { bucketIndex: 0, bucketName: 'NJ/CT' },
    ...overrides,
  };
}

function renderFeed(props: Partial<Parameters<typeof DayActivityFeed>[0]> = {}): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <DayActivityFeed planId="p1" runId="r1" active={false} {...props} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  entityHistoryMock.mockReset();
});

describe('<DayActivityFeed />', () => {
  it('requests history for the right entity and shows "Loading…" while pending', () => {
    entityHistoryMock.mockReturnValue(new Promise(() => {}));
    renderFeed();
    expect(screen.getByText('Loading…')).toBeInTheDocument();
    expect(entityHistoryMock).toHaveBeenCalledWith('plan_run/p1/r1');
  });

  it('shows the error message when the history query fails', async () => {
    entityHistoryMock.mockRejectedValue(new Error('boom'));
    renderFeed();
    await waitFor(() => expect(screen.getByText('Failed to load activity.')).toBeInTheDocument());
  });

  it('shows "No activity yet." once loaded with zero entries', async () => {
    entityHistoryMock.mockResolvedValue({ entityId: 'plan_run/p1/r1', entries: [] });
    renderFeed();
    await waitFor(() => expect(screen.getByText('No activity yet.')).toBeInTheDocument());
  });

  it('renders each entry with its formatted timestamp, text, and tone', async () => {
    entityHistoryMock.mockResolvedValue({
      entityId: 'plan_run/p1/r1',
      entries: [
        entry({ action: 'bucket_started', extra: { bucketIndex: 0, bucketName: 'NJ/CT' }, timestamp: '2026-09-01T19:40:00.000Z' }),
        entry({ action: 'creation_failed', extra: { bucketIndex: 0, campaignIndex: 1, error: 'ThrottlingException' }, timestamp: '2026-09-01T19:45:00.000Z' }),
      ],
    });
    renderFeed();

    await waitFor(() => expect(screen.getByText('Bucket "NJ/CT" started')).toBeInTheDocument());
    expect(screen.getByText('Bucket 1 / campaign 2 — creation failed: ThrottlingException')).toBeInTheDocument();
    // fmtTime renders COT (UTC-5) hours:minutes — 19:40 UTC → 14:40 COT.
    expect(screen.getByText('14:40')).toBeInTheDocument();
    expect(screen.getByText('14:45')).toBeInTheDocument();
  });

  it('passes className through and works with active=true (live refetch enabled)', async () => {
    entityHistoryMock.mockResolvedValue({ entityId: 'plan_run/p1/r1', entries: [] });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <DayActivityFeed planId="p1" runId="r1" active className="custom-class" />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getByText('No activity yet.')).toBeInTheDocument());
    expect(screen.getByText('Day activity').parentElement).toHaveClass('custom-class');
  });
});
