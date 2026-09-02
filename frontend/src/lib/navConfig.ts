export type NavItem = { to: string; label: string };
export type NavGroup = { label: string; items: NavItem[] };

export const NAV_GROUPS: NavGroup[] = [
  {
    label: 'Contact center',
    items: [
      { to: '/dashboard', label: 'Monitor' },
      { to: '/plans/history', label: 'History' },
      { to: '/plans', label: 'Plans' },
      { to: '/plans/templates', label: 'Templates' },
      { to: '/segments', label: 'Segments' },
    ],
  },
  {
    label: 'Admin',
    items: [
      { to: '/campaigns', label: 'Campaigns' },
      { to: '/profiles', label: 'Profiles' },
      { to: '/audit', label: 'Audit' },
      { to: '/contact-artifacts', label: 'Artifacts' },
    ],
  },
];

/**
 * The active top-level nav item's label for a given pathname — used by the
 * TopBar breadcrumb. Matches the item whose `to` is the longest prefix of
 * `pathname` (so `/plans/history` picks "History" over "Plans", but
 * `/plans/p1` — no specific item matches beyond `/plans` — picks "Plans").
 * Falls back to "Monitor" (the app's default landing item) if nothing matches.
 */
export function breadcrumbLabelForPath(pathname: string): string {
  const allItems = NAV_GROUPS.flatMap((g) => g.items);
  let best: NavItem | null = null;
  for (const item of allItems) {
    if (pathname === item.to || pathname.startsWith(`${item.to}/`)) {
      if (!best || item.to.length > best.to.length) best = item;
    }
  }
  return best?.label ?? 'Monitor';
}

/**
 * The active top-level nav item's enclosing group label for a given
 * pathname — used by the TopBar breadcrumb's first segment. Same
 * longest-prefix-match logic as breadcrumbLabelForPath, but returns the
 * NavGroup's own label instead of the matched item's label, so routes in
 * the "Admin" group don't incorrectly read "Contact center".
 */
export function breadcrumbGroupForPath(pathname: string): string {
  let best: NavItem | null = null;
  let bestGroup = 'Contact center';
  for (const group of NAV_GROUPS) {
    for (const item of group.items) {
      if (pathname === item.to || pathname.startsWith(`${item.to}/`)) {
        if (!best || item.to.length > best.to.length) {
          best = item;
          bestGroup = group.label;
        }
      }
    }
  }
  return bestGroup;
}
