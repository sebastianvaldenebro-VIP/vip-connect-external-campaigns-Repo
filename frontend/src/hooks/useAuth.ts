import { useEffect, useState } from 'react';
import { Hub } from 'aws-amplify/utils';

import { currentUser, getUserGroups } from '@/lib/auth';

type AuthUser = Awaited<ReturnType<typeof currentUser>>;

export type AuthState = {
  user: AuthUser;
  /** Cognito groups from the ID token, e.g. ['Admin'] or ['Agent'] — empty
   * (not yet loaded) while `loading` is true. UI gating only; the real
   * authorization boundary is server-side. */
  groups: string[];
  loading: boolean;
};

export function useAuth(): AuthState {
  const [state, setState] = useState<AuthState>({ user: null, groups: [], loading: true });

  useEffect(() => {
    let cancelled = false;

    const refresh = async () => {
      const [user, groups] = await Promise.all([currentUser(), getUserGroups()]);
      if (!cancelled) setState({ user, groups, loading: false });
    };

    void refresh();

    const unsubscribe = Hub.listen('auth', ({ payload }) => {
      if (
        payload.event === 'signedIn' ||
        payload.event === 'signedOut' ||
        payload.event === 'tokenRefresh' ||
        payload.event === 'signInWithRedirect'
      ) {
        void refresh();
      }
    });

    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, []);

  return state;
}
