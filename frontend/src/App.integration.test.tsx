import { beforeEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import App from './App';
const state = vi.hoisted(() => ({
  user: { userId: 'demo-user', username: 'demo.user' } as {
    userId: string;
    username: string;
  } | null,
  signIn: vi.fn().mockResolvedValue(undefined),
  signOut: vi.fn().mockResolvedValue(undefined),
}));
vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({ user: state.user, groups: ['Admin'], loading: false }),
}));
vi.mock('@/lib/auth', () => ({ signIn: state.signIn, signOut: state.signOut }));
vi.mock('@/lib/api', () => ({
  api: {
    brandedMonitor: {
      getAgentRoster: vi.fn().mockResolvedValue({ agents: [] }),
    },
  },
}));
function show() {
  render(
    <MemoryRouter initialEntries={['/preferences']}>
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <App />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}
beforeEach(() => {
  state.user = { userId: 'demo-user', username: 'demo.user' };
  state.signIn.mockClear();
  localStorage.clear();
});
it('keeps preferences behind the existing corporate login', async () => {
  state.user = null;
  show();
  expect(
    await screen.findByText('Redirecting to the corporate identity provider…'),
  ).toBeInTheDocument();
  expect(state.signIn).toHaveBeenCalledOnce();
  expect(
    screen.queryByRole('button', { name: 'Save preferences' }),
  ).not.toBeInTheDocument();
});
it('renders preferences inside the existing shell and preserves navigation and sign out', async () => {
  show();
  expect(
    screen.getByRole('heading', { name: 'Preferences' }),
  ).toBeInTheDocument();
  for (const name of [
    'Monitor',
    'History',
    'Plans',
    'Templates',
    'Segments',
    'Campaigns',
    'Profiles',
    'Audit',
    'Artifacts',
    'Blocked numbers',
  ]) {
    expect(screen.getByRole('link', { name })).toBeInTheDocument();
  }
  fireEvent.click(screen.getByRole('button', { name: 'DU' }));
  expect(screen.getByRole('link', { name: 'Preferences' })).toHaveAttribute(
    'href',
    '/preferences',
  );
  expect(screen.getByRole('button', { name: 'Sign out' })).toBeInTheDocument();
  fireEvent.click(screen.getByRole('link', { name: 'Preferences' }));
  expect(
    screen.queryByRole('button', { name: 'Sign out' }),
  ).not.toBeInTheDocument();
});
