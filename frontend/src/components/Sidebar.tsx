import type { ReactNode } from 'react';
import { NavLink } from 'react-router-dom';

import { usePersistedState } from '@/hooks/usePersistedState';
import { NAV_GROUPS } from '@/lib/navConfig';
import { cn } from '@/lib/utils';

// Keyed by route (`to`), not by label — routes are guaranteed unique across
// NAV_GROUPS, labels are not enforced to be. navConfig.ts stays plain data
// (no JSX) so every other file in lib/ keeps its no-JSX convention; icons
// live here instead, next to the only place that renders them.
const NAV_ICONS: Record<string, ReactNode> = {
  '/dashboard': (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-4 w-4 shrink-0">
      <path d="M2 10h2l2-5 3 9 3-7 2 3h4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ),
  '/plans/history': (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-4 w-4 shrink-0">
      <circle cx="10" cy="10" r="8" />
      <path d="M10 6v4l3 2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ),
  '/plans': (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-4 w-4 shrink-0">
      <rect x="2" y="4" width="16" height="12" rx="2" />
      <path d="M6 14V10M10 14V8M14 14V6" strokeLinecap="round" />
    </svg>
  ),
  '/plans/templates': (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-4 w-4 shrink-0">
      <rect x="2" y="13" width="16" height="4" rx="1" />
      <rect x="2" y="7" width="16" height="4" rx="1" />
      <rect x="2" y="1" width="16" height="4" rx="1" />
    </svg>
  ),
  '/segments': (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-4 w-4 shrink-0">
      <circle cx="7" cy="7" r="3" />
      <circle cx="14" cy="14" r="3" />
      <path d="M9.5 9.5l3 3" strokeLinecap="round" />
    </svg>
  ),
  '/campaigns': (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-4 w-4 shrink-0">
      <path d="M3 8l14-4v12L3 12V8z" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ),
  '/profiles': (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-4 w-4 shrink-0">
      <circle cx="10" cy="7" r="3" />
      <path d="M4 17c0-3 2.5-5 6-5s6 2 6 5" strokeLinecap="round" />
    </svg>
  ),
  '/audit': (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-4 w-4 shrink-0">
      <rect x="4" y="2" width="12" height="16" rx="1" />
      <path d="M7 7h6M7 10h6M7 13h4" strokeLinecap="round" />
    </svg>
  ),
  '/contact-artifacts': (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-4 w-4 shrink-0">
      <path d="M4 4h8l4 4v10H4V4z" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M12 4v4h4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ),
};

export function Sidebar(): ReactNode {
  const [collapsed, setCollapsed] = usePersistedState('sidebar-collapsed', false);

  return (
    <aside className={cn('flex flex-col border-r border-border bg-card transition-all', collapsed ? 'w-16' : 'w-56')}>
      <div className="flex h-14 items-center gap-2 border-b border-border px-4">
        <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded bg-primary text-xs font-bold text-primary-foreground">
          +
        </span>
        {!collapsed && <span className="truncate text-sm font-semibold tracking-tight">VIP Connect Admin</span>}
      </div>

      <nav className="flex-1 space-y-5 overflow-y-auto px-2 py-4">
        {NAV_GROUPS.map((group) => (
          <div key={group.label} className="flex flex-col gap-1">
            {!collapsed && (
              <div className="px-2 pb-1 text-[11px] font-semibold uppercase tracking-widest text-muted-foreground">
                {group.label}
              </div>
            )}
            {group.items.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === '/plans'}
                title={collapsed ? item.label : undefined}
                className={({ isActive }) =>
                  cn(
                    'flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors',
                    isActive ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:bg-muted hover:text-foreground',
                  )
                }
              >
                {NAV_ICONS[item.to]}
                {!collapsed && <span className="truncate">{item.label}</span>}
              </NavLink>
            ))}
          </div>
        ))}
      </nav>

      <button
        type="button"
        onClick={() => setCollapsed(!collapsed)}
        className="flex items-center justify-center gap-2 border-t border-border px-3 py-3 text-xs font-medium text-muted-foreground hover:bg-muted hover:text-foreground"
        title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
      >
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" className={cn('h-4 w-4 shrink-0 transition-transform', collapsed && 'rotate-180')}>
          <path d="M12 4l-6 6 6 6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        {!collapsed && <span>Collapse</span>}
      </button>

      {!collapsed && (
        <div className="border-t border-border px-4 py-3">
          <div className="text-[10px] font-semibold uppercase tracking-widest text-muted-foreground">Context</div>
          <div className="text-sm font-medium text-foreground">Outbound scheduling</div>
        </div>
      )}
    </aside>
  );
}
