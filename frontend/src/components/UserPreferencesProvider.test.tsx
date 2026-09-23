import { beforeEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { UserPreferencesProvider } from './UserPreferencesProvider';
import { Preferences } from '@/pages/Preferences';
import { parsePreferences, preferencesKey } from '@/lib/preferences';
const state = vi.hoisted(() => ({ id: 'alice' }));
vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({ user: { userId: state.id } }),
}));
beforeEach(() => {
  state.id = 'alice';
  localStorage.clear();
});
it('validates persisted settings rather than trusting localStorage JSON', () => {
  expect(
    parsePreferences({ theme: 'injected', freshnessMinutes: Infinity }),
  ).toEqual({ freshnessMinutes: 15 });
});
it('persists per authenticated user and restores defaults on user switch', () => {
  const view = render(
    <UserPreferencesProvider>
      <Preferences />
    </UserPreferencesProvider>,
  );
  fireEvent.change(
    screen.getByLabelText('Mark observations stale after (minutes)'),
    { target: { value: '30' } },
  );
  fireEvent.click(screen.getByRole('button', { name: 'Save preferences' }));
  expect(screen.getByRole('status')).toHaveTextContent('saved in this browser');
  expect(JSON.parse(localStorage.getItem(preferencesKey('alice'))!)).toEqual({
    freshnessMinutes: 30,
  });
  state.id = 'bob';
  view.rerender(
    <UserPreferencesProvider>
      <Preferences />
    </UserPreferencesProvider>,
  );
  expect(
    screen.getByLabelText('Mark observations stale after (minutes)'),
  ).toHaveValue(15);
  expect(localStorage.getItem(preferencesKey('bob'))).toBeNull();
});
it('reports storage failure without claiming a successful save', () => {
  const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
    throw new Error('quota');
  });
  render(
    <UserPreferencesProvider>
      <Preferences />
    </UserPreferencesProvider>,
  );
  fireEvent.click(screen.getByRole('button', { name: 'Save preferences' }));
  expect(screen.getByRole('status')).toHaveTextContent('could not be saved');
  spy.mockRestore();
});
