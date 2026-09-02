import type { ReactNode } from 'react';
import { NavLink, Outlet } from 'react-router-dom';

import { signOut } from '@/lib/auth';
import { useAuth } from '@/hooks/useAuth';
import { useIdleTimeout } from '@/hooks/useIdleTimeout';
import { cn } from '@/lib/utils';

const NAV = [
  { to: '/dashboard', label: 'Dashboard' },
  { to: '/segments', label: 'Segments' },
  { to: '/campaigns', label: 'Campaigns' },
  { to: '/plans', label: 'Plans' },
  { to: '/profiles', label: 'Profiles' },
  { to: '/audit', label: 'Audit' },
  { to: '/contact-artifacts', label: 'Artifacts' },
] as const;

export function Layout(): ReactNode {
  const { user } = useAuth();
  useIdleTimeout(Boolean(user));

  return (
    <div className="flex min-h-screen flex-col bg-background text-foreground">
      <header className="border-b border-border bg-card">
        <div className="flex h-14 items-center justify-between px-6">
          <div className="flex items-center gap-6">
            <span className="text-sm font-semibold tracking-tight">VIP Connect Admin</span>
            <nav className="flex items-center gap-1">
              {NAV.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  className={({ isActive }) =>
                    cn(
                      'rounded-md px-3 py-1.5 text-sm transition-colors',
                      isActive
                        ? 'bg-muted font-medium text-foreground'
                        : 'text-muted-foreground hover:text-foreground',
                    )
                  }
                >
                  {item.label}
                </NavLink>
              ))}
            </nav>
          </div>
          <div className="flex items-center gap-3 text-sm">
            <span className="text-muted-foreground">{user?.username}</span>
            <button
              onClick={() => void signOut()}
              className="rounded-md px-3 py-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground"
              type="button"
            >
              Sign out
            </button>
          </div>
        </div>
      </header>
      <main className="w-full flex-1 px-6 py-8">
        <Outlet />
      </main>
    </div>
  );
}
