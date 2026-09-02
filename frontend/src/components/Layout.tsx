import type { ReactNode } from 'react';
import { Outlet } from 'react-router-dom';

import { useAuth } from '@/hooks/useAuth';
import { useIdleTimeout } from '@/hooks/useIdleTimeout';
import { Sidebar } from './Sidebar';
import { TopBar } from './TopBar';

export function Layout(): ReactNode {
  const { user } = useAuth();
  useIdleTimeout(Boolean(user));

  return (
    <div className="flex min-h-screen bg-background text-foreground">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar />
        <main className="min-w-0 flex-1 px-6 py-8">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
