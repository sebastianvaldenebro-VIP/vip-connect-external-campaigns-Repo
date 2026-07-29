import type { ReactNode } from 'react';
import { NavLink, Outlet, useNavigate } from 'react-router-dom';

import { Button } from '@/components/ui';
import { cn } from '@/lib/utils';

const GROUPS = [
  {
    label: 'Operations',
    items: [
      {
        to: '/plans/today',
        label: "Today's plan",
        icon: (
          <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-4 w-4 shrink-0">
            <rect x="3" y="4" width="14" height="14" rx="2" />
            <path d="M7 2v4M13 2v4M3 9h14" strokeLinecap="round" />
          </svg>
        ),
      },
      {
        to: '/plans/monitor',
        label: 'Live monitor',
        icon: (
          <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-4 w-4 shrink-0">
            <path d="M2 10h2l2-5 3 9 3-7 2 3h4" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        ),
      },
      {
        to: '/plans/history',
        label: 'History',
        icon: (
          <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-4 w-4 shrink-0">
            <circle cx="10" cy="10" r="8" />
            <path d="M10 6v4l3 2" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        ),
      },
    ],
  },
  {
    label: 'Configuration',
    items: [
      {
        to: '/plans/templates',
        label: 'Templates',
        icon: (
          <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-4 w-4 shrink-0">
            <rect x="2" y="13" width="16" height="4" rx="1" />
            <rect x="2" y="7" width="16" height="4" rx="1" />
            <rect x="2" y="1" width="16" height="4" rx="1" />
          </svg>
        ),
      },
      {
        to: '/plans/scheduler',
        label: 'Scheduler',
        icon: (
          <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-4 w-4 shrink-0">
            <circle cx="10" cy="10" r="8" />
            <path d="M10 6v4l2.5 2.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        ),
      },
      {
        to: '/plans/guide',
        label: 'How to use',
        icon: (
          <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-4 w-4 shrink-0">
            <circle cx="10" cy="10" r="8" />
            <path d="M10 14v-1" strokeLinecap="round" />
            <path d="M10 10.5c0-1.5 2-1.5 2-3a2 2 0 10-4 0" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        ),
      },
      {
        to: '/plans/branded-monitor',
        label: 'Branded Monitor',
        icon: (
          <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-4 w-4 shrink-0">
            <rect x="2" y="4" width="16" height="12" rx="2" />
            <path d="M6 14V10M10 14V8M14 14V6" strokeLinecap="round" />
          </svg>
        ),
      },
    ],
  },
] as const;

export function PlansLayout(): ReactNode {
  const navigate = useNavigate();

  return (
    <div className="flex gap-8">
      {/* ── Sidebar ─────────────────────────────────────────────────── */}
      <aside className="w-52 shrink-0">
        <div className="flex flex-col gap-5">
          {GROUPS.map((group) => (
            <div key={group.label} className="flex flex-col gap-1">
              <p className="px-3 text-[11px] font-semibold uppercase tracking-widest text-muted-foreground">
                {group.label}
              </p>
              {group.items.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  className={({ isActive }) =>
                    cn(
                      'flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors',
                      isActive
                        ? 'bg-primary/10 text-primary'
                        : 'text-muted-foreground hover:bg-muted hover:text-foreground',
                    )
                  }
                >
                  {item.icon}
                  {item.label}
                </NavLink>
              ))}
            </div>
          ))}

          <div className="mt-2 px-3">
            <Button size="sm" className="w-full" onClick={() => navigate('/plans/new')}>
              New plan
            </Button>
          </div>
        </div>
      </aside>

      {/* ── Content ─────────────────────────────────────────────────── */}
      <div className="flex-1 min-w-0">
        <Outlet />
      </div>
    </div>
  );
}
