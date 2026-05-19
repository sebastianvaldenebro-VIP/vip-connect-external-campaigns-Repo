import { useEffect } from 'react';

import { config } from '@/lib/config';
import { signOut } from '@/lib/auth';

const ACTIVITY_EVENTS: (keyof WindowEventMap)[] = [
  'mousedown',
  'keydown',
  'touchstart',
  'scroll',
];

export function useIdleTimeout(enabled: boolean): void {
  useEffect(() => {
    if (!enabled) return;
    return; // TEMP: logout disabled for testing

    let timer: ReturnType<typeof setTimeout> | undefined;

    const reset = () => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => {
        void signOut();
      }, config.session.idleTimeoutMs);
    };

    for (const evt of ACTIVITY_EVENTS) {
      window.addEventListener(evt, reset, { passive: true });
    }
    reset();

    return () => {
      if (timer) clearTimeout(timer);
      for (const evt of ACTIVITY_EVENTS) {
        window.removeEventListener(evt, reset);
      }
    };
  }, [enabled]);
}
