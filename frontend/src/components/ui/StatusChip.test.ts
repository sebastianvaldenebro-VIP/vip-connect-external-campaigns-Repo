import type { ReactNode } from 'react';
import { describe, expect, it } from 'vitest';

import { StatusChip } from './StatusChip';
import { STATUS_TONE_CLASSES } from './status';

/** Pull `.props` off a React element without fighting TS's opaque ReactNode type. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const props = (node: ReactNode): any => (node as any).props;

describe('StatusChip', () => {
  it('renders the label as a child', () => {
    const el = StatusChip({ tone: 'success', label: 'Running' });
    expect(props(el).children).toEqual([null, 'Running']);
  });

  it('applies the tone background/foreground classes', () => {
    const el = StatusChip({ tone: 'danger', label: 'Failed' });
    expect(props(el).className).toContain(STATUS_TONE_CLASSES.danger.bg);
    expect(props(el).className).toContain(STATUS_TONE_CLASSES.danger.fg);
  });

  it('omits the mono font class by default', () => {
    const el = StatusChip({ tone: 'info', label: 'Info' });
    expect(props(el).className).not.toContain('font-mono');
  });

  it('adds the mono font class when mono=true', () => {
    const el = StatusChip({ tone: 'info', label: 'Info', mono: true });
    expect(props(el).className).toContain('font-mono');
  });

  it('omits the pulsing dot by default', () => {
    const el = StatusChip({ tone: 'warning', label: 'Warn' });
    expect(props(el).children[0]).toBeNull();
  });

  it('renders a pulsing dot with the tone dot color when pulse=true', () => {
    const el = StatusChip({ tone: 'warning', label: 'Warn', pulse: true });
    const dot = props(el).children[0];
    expect(props(dot).className).toContain('animate-pulse');
    expect(props(dot).className).toContain(STATUS_TONE_CLASSES.warning.dot);
  });

  it('merges a custom className', () => {
    const el = StatusChip({ tone: 'neutral', label: 'N', className: 'my-extra-class' });
    expect(props(el).className).toContain('my-extra-class');
  });
});
