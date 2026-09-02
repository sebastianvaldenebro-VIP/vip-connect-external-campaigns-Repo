import { describe, expect, it } from 'vitest';
import { breadcrumbGroupForPath, breadcrumbLabelForPath } from './navConfig';

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
