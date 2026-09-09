import type { ReactNode } from 'react';
import { describe, expect, it } from 'vitest';

import { StatTile } from './StatTile';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const props = (node: ReactNode): any => (node as any).props;

describe('StatTile', () => {
  it('renders the label and value', () => {
    const el = StatTile({ label: 'Answer rate', value: '42%' });
    const [valueDiv, labelDiv] = props(el).children;
    expect(props(valueDiv).children).toBe('42%');
    expect(props(labelDiv).children).toBe('Answer rate');
  });

  it('accepts a ReactNode value (not just a string)', () => {
    const el = StatTile({ label: 'Trend', value: 42 });
    const [valueDiv] = props(el).children;
    expect(props(valueDiv).children).toBe(42);
  });

  it('applies a custom valueClassName to the value element', () => {
    const el = StatTile({ label: 'Answer rate', value: '42%', valueClassName: 'text-status-danger-fg' });
    const [valueDiv] = props(el).children;
    expect(props(valueDiv).className).toContain('text-status-danger-fg');
  });

  it('merges a custom className onto the outer container', () => {
    const el = StatTile({ label: 'Answer rate', value: '42%', className: 'col-span-2' });
    expect(props(el).className).toContain('col-span-2');
  });
});
