import { type ReactNode, useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useLocation } from 'react-router-dom';

import { signOut } from '@/lib/auth';
import { api } from '@/lib/api';
import { totalActiveAlerts } from '@/lib/agentRoster';
import { breadcrumbGroupForPath, breadcrumbLabelForPath } from '@/lib/navConfig';
import { fmtTime } from '@/lib/utils';
import { useAuth } from '@/hooks/useAuth';

/** First-letter-of-first-two-segments initials from a username/email, e.g.
 * "sebastian.valdenebro@medwork.io" → "SV", "preview@local" → "PL". */
function initialsFor(username: string): string {
  const local = username.split('@')[0] ?? username;
  const parts = local.split(/[.\s_-]+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0]![0] + parts[1]![0]).toUpperCase();
  return local.slice(0, 2).toUpperCase();
}

export function TopBar(): ReactNode {
  const location = useLocation();
  const { user } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 30_000);
    return () => clearInterval(id);
  }, []);

  // Same query key AgentAvailabilityPanel.tsx already uses — React Query
  // dedupes the network call when both are mounted on the same page.
  const agentQuery = useQuery({
    queryKey: ['agent-roster', 'all'],
    queryFn: () => api.brandedMonitor.getAgentRoster(),
    refetchInterval: 20_000,
  });
  const alertCount = totalActiveAlerts(agentQuery.data?.agents ?? []);

  return (
    <header className="flex h-14 items-center justify-between border-b border-border bg-card px-6">
      <div className="text-sm text-muted-foreground">
        {breadcrumbGroupForPath(location.pathname)} <span className="mx-1.5 text-border">/</span>
        <span className="font-medium text-foreground">{breadcrumbLabelForPath(location.pathname)}</span>
      </div>
      <div className="flex items-center gap-4">
        <span className="rounded-md bg-muted px-2.5 py-1 text-xs font-medium tabular-nums text-muted-foreground">
          {fmtTime(now)}
        </span>
        <div className="relative flex items-center gap-1 text-muted-foreground" title={`${alertCount} active alert${alertCount === 1 ? '' : 's'}`}>
          <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-5 w-5">
            <path d="M10 3a5 5 0 00-5 5v2.5L3.5 13h13L15 10.5V8a5 5 0 00-5-5z" strokeLinecap="round" strokeLinejoin="round" />
            <path d="M8 15.5a2 2 0 004 0" strokeLinecap="round" />
          </svg>
          {alertCount > 0 && (
            <span className="absolute -right-1.5 -top-1.5 flex h-4 min-w-[16px] items-center justify-center rounded-full bg-status-danger-bar px-1 text-[10px] font-bold leading-none text-white">
              {alertCount}
            </span>
          )}
        </div>
        <div className="relative">
          <button
            type="button"
            onClick={() => setMenuOpen((v) => !v)}
            className="flex h-8 w-8 items-center justify-center rounded-full bg-primary text-xs font-bold text-primary-foreground"
          >
            {initialsFor(user?.username ?? '?')}
          </button>
          {menuOpen && (
            <div className="absolute right-0 top-10 z-10 w-44 rounded-md border border-border bg-card py-1 shadow-md">
              <div className="truncate border-b border-border px-3 py-2 text-xs text-muted-foreground">{user?.username}</div>
              <button
                type="button"
                onClick={() => void signOut()}
                className="w-full px-3 py-2 text-left text-sm text-muted-foreground hover:bg-muted hover:text-foreground"
              >
                Sign out
              </button>
            </div>
          )}
        </div>
      </div>
    </header>
  );
}
