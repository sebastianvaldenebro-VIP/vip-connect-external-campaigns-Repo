import { useEffect, useState } from 'react';
import { Hub } from 'aws-amplify/utils';

import { currentUser } from '@/lib/auth';

type AuthUser = Awaited<ReturnType<typeof currentUser>>;

export type AuthState = {
  user: AuthUser;
  loading: boolean;
};

export function useAuth(): AuthState {
  const [state, setState] = useState<AuthState>({ user: null, loading: true });

  useEffect(() => {
    let cancelled = false;

    const refresh = async () => {
      const user = await currentUser();
      if (!cancelled) setState({ user, loading: false });
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
