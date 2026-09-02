import { describe, expect, it } from 'vitest';
import { breadcrumbLabelForPath } from './navConfig';

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
