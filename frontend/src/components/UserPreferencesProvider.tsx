import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from 'react';
import { useAuth } from '@/hooks/useAuth';
import {
  DEFAULT_PREFERENCES,
  parsePreferences,
  preferencesKey,
  type Preferences,
} from '@/lib/preferences';
const Context = createContext({
  preferences: DEFAULT_PREFERENCES,
  save: (_value: Preferences): boolean => false,
});
export const usePreferences = () => useContext(Context);
function read(userId: string): Preferences {
  if (!userId) return { ...DEFAULT_PREFERENCES };
  try {
    return parsePreferences(
      JSON.parse(localStorage.getItem(preferencesKey(userId)) ?? 'null'),
    );
  } catch {
    return { ...DEFAULT_PREFERENCES };
  }
}
function ScopedPreferences({
  userId,
  children,
}: {
  userId: string;
  children: ReactNode;
}) {
  const [preferences, setPreferences] = useState(() => read(userId));
  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key === preferencesKey(userId) || event.key === null)
        setPreferences(read(userId));
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, [userId]);
  const save = (value: Preferences) => {
    if (!userId) return false;
    const parsed = parsePreferences(value);
    try {
      localStorage.setItem(preferencesKey(userId), JSON.stringify(parsed));
    } catch {
      return false;
    }
    setPreferences(parsed);
    return true;
  };
  return (
    <Context.Provider value={{ preferences, save }}>
      {children}
    </Context.Provider>
  );
}
export function UserPreferencesProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  return (
    <ScopedPreferences key={user?.userId ?? ''} userId={user?.userId ?? ''}>
      {children}
    </ScopedPreferences>
  );
}
