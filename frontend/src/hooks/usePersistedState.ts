import { useState } from 'react';

/**
 * A useState that also persists to localStorage under `key`. Reads/writes are
 * wrapped in try/catch — private browsing or a full storage quota must never
 * break the app, just silently fall back to in-memory-only behavior for that
 * session.
 */
export function usePersistedState<T>(key: string, defaultValue: T): [T, (value: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = window.localStorage.getItem(key);
      return raw !== null ? (JSON.parse(raw) as T) : defaultValue;
    } catch {
      return defaultValue;
    }
  });

  const setPersisted = (next: T): void => {
    setValue(next);
    try {
      window.localStorage.setItem(key, JSON.stringify(next));
    } catch {
      // Storage unavailable — in-memory state for this session still works.
    }
  };

  return [value, setPersisted];
}
