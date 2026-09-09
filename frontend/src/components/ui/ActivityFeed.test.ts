import type { ReactNode } from 'react';
import { describe, expect, it } from 'vitest';

import { ActivityFeed, type ActivityFeedItem } from './ActivityFeed';
import { STATUS_TONE_CLASSES } from './status';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const props = (node: ReactNode): any => (node as any).props;

const item = (over: Partial<ActivityFeedItem> = {}): ActivityFeedItem => ({
  id: 'evt-1',
  timestampLabel: '07:34',
  text: 'Campaign started',
  tone: 'success',
  ...over,
});

describe('ActivityFeed', () => {
  it('renders the default empty-state label when items is empty', () => {
    const el = ActivityFeed({ items: [] });
    expect(props(el).children).toBe('No activity yet.');
  });

  it('renders a custom empty-state label', () => {
    const el = ActivityFeed({ items: [], emptyLabel: 'Nothing to show.' });
    expect(props(el).children).toBe('Nothing to show.');
  });

  it('applies a custom className to the empty-state paragraph', () => {
    const el = ActivityFeed({ items: [], className: 'extra-empty-class' });
    expect(props(el).className).toContain('extra-empty-class');
  });

  it('renders one <li> per item with timestamp, tone dot, and text', () => {
    const items = [item({ id: 'a', timestampLabel: '07:00', text: 'First', tone: 'info' })];
    const el = ActivityFeed({ items });
    const list = props(el).children as ReactNode[];
    expect(list).toHaveLength(1);
    const li = list[0];
    expect((li as { key?: string }).key).toBe('a');
    const [timestampSpan, dotSpan, textSpan] = props(li).children;
    expect(props(timestampSpan).children).toBe('07:00');
    expect(props(dotSpan).className).toContain(STATUS_TONE_CLASSES.info.bar);
    expect(props(textSpan).children).toBe('First');
  });

  it('renders multiple items in order and applies a custom className to the list', () => {
    const items = [item({ id: 'a' }), item({ id: 'b', text: 'Second' })];
    const el = ActivityFeed({ items, className: 'max-h-72' });
    expect(props(el).className).toContain('max-h-72');
    const list = props(el).children as ReactNode[];
    expect(list).toHaveLength(2);
    expect((list[1] as { key?: string }).key).toBe('b');
  });
});
