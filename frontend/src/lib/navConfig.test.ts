import { describe, expect, it } from 'vitest';
import {
  breadcrumbGroupForPath,
  breadcrumbLabelForPath,
  isNavItemVisible,
  visibleNavGroups,
} from './navConfig';

describe('breadcrumbLabelForPath', () => {
  it('matches an exact top-level nav route', () => {
    expect(breadcrumbLabelForPath('/segments')).toBe('Segments');
  });

  it('matches a nested route by its longest matching prefix', () => {
    // /plans/history is its own nav item (History); /plans/anything-else
    // that isn't a specific nav item should still resolve to "Plans".
    expect(breadcrumbLabelForPath('/plans/history')).toBe('History');
    expect(breadcrumbLabelForPath('/plans/templates')).toBe('Templates');
    expect(breadcrumbLabelForPath('/plans/p1')).toBe('Plans');
  });

  it('falls back to Monitor for an unmatched path', () => {
    expect(breadcrumbLabelForPath('/some-unknown-route')).toBe('Monitor');
  });

  it('matches /dashboard to Monitor', () => {
    expect(breadcrumbLabelForPath('/dashboard')).toBe('Monitor');
  });
});

describe('breadcrumbGroupForPath', () => {
  it('returns the Contact center group for a Contact-center route', () => {
    expect(breadcrumbGroupForPath('/segments')).toBe('Contact center');
  });

  it('returns the Admin group for an Admin route', () => {
    expect(breadcrumbGroupForPath('/audit')).toBe('Admin');
    expect(breadcrumbGroupForPath('/profiles')).toBe('Admin');
  });

  it('resolves a nested route to its item\'s group, not a shorter sibling\'s', () => {
    // /plans/history is in the same group as /plans ("Contact center"), so
    // this alone wouldn't distinguish a broken implementation — paired with
    // the Admin-route case above, which WOULD fail under a "always return
    // the first group" bug, this confirms the longest-prefix match still
    // drives which group wins.
    expect(breadcrumbGroupForPath('/plans/history')).toBe('Contact center');
  });

  it('falls back to Contact center for an unmatched path', () => {
    expect(breadcrumbGroupForPath('/some-unknown-route')).toBe('Contact center');
  });
});

describe('isNavItemVisible', () => {
  it('shows every item to an Admin, extraRoles or not', () => {
    expect(isNavItemVisible({ to: '/campaigns', label: 'Campaigns' }, ['Admin'])).toBe(true);
    expect(
      isNavItemVisible(
        { to: '/blocked-numbers', label: 'Blocked numbers', extraRoles: ['Agent'] },
        ['Admin'],
      ),
    ).toBe(true);
  });

  it('shows an item to Agent only when extraRoles includes Agent', () => {
    expect(
      isNavItemVisible(
        { to: '/blocked-numbers', label: 'Blocked numbers', extraRoles: ['Agent'] },
        ['Agent'],
      ),
    ).toBe(true);
    expect(isNavItemVisible({ to: '/campaigns', label: 'Campaigns' }, ['Agent'])).toBe(false);
  });

  it('hides everything from a user with no matching group', () => {
    expect(isNavItemVisible({ to: '/campaigns', label: 'Campaigns' }, [])).toBe(false);
  });
});

describe('visibleNavGroups', () => {
  it('returns every group unfiltered for Admin', () => {
    const groups = visibleNavGroups(['Admin']);
    const totalItems = groups.flatMap((g) => g.items).length;
    expect(totalItems).toBeGreaterThan(1);
    expect(groups.some((g) => g.label === 'Contact center')).toBe(true);
  });

  it('drops the entire Contact center group for Agent, keeping only Blocked numbers', () => {
    const groups = visibleNavGroups(['Agent']);
    expect(groups.some((g) => g.label === 'Contact center')).toBe(false);
    const allItems = groups.flatMap((g) => g.items);
    expect(allItems).toHaveLength(1);
    expect(allItems[0]!.to).toBe('/blocked-numbers');
  });

  it('drops every group for a user with no matching role', () => {
    expect(visibleNavGroups([])).toHaveLength(0);
  });
});
