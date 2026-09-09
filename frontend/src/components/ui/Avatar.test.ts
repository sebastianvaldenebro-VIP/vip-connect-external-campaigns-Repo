import type { ReactNode } from 'react';
import { describe, expect, it } from 'vitest';

import { Avatar, initialsFromName } from './Avatar';
import { STATUS_TONE_CLASSES } from './status';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const props = (node: ReactNode): any => (node as any).props;

describe('initialsFromName', () => {
  it('takes first letter of first and last name', () => {
    expect(initialsFromName('Diego Santos')).toBe('DS');
  });

  it('handles a single name with one initial', () => {
    expect(initialsFromName('Ana')).toBe('A');
  });

  it('collapses extra internal whitespace', () => {
    expect(initialsFromName('  Tom   Fisher  ')).toBe('TF');
  });

  it('uses first and last of a three-plus-word name', () => {
    expect(initialsFromName('Maria De La Cruz')).toBe('MC');
  });

  it('falls back to "?" for an empty string', () => {
    expect(initialsFromName('')).toBe('?');
  });

  it('uppercases lowercase input', () => {
    expect(initialsFromName('jack ryan')).toBe('JR');
  });
});

describe('Avatar', () => {
  it('renders the initials as its child', () => {
    const el = Avatar({ name: 'Diego Santos' });
    expect(props(el).children).toBe('DS');
  });

  it('defaults to the neutral tone classes', () => {
    const el = Avatar({ name: 'Diego Santos' });
    expect(props(el).className).toContain(STATUS_TONE_CLASSES.neutral.bg);
    expect(props(el).className).toContain(STATUS_TONE_CLASSES.neutral.fg);
  });

  it('applies the requested tone classes', () => {
    const el = Avatar({ name: 'Diego Santos', tone: 'danger' });
    expect(props(el).className).toContain(STATUS_TONE_CLASSES.danger.bg);
    expect(props(el).className).toContain(STATUS_TONE_CLASSES.danger.fg);
  });

  it('merges a custom className', () => {
    const el = Avatar({ name: 'Diego Santos', className: 'ring-2' });
    expect(props(el).className).toContain('ring-2');
  });

  it('is aria-hidden (decorative avatar, name is shown elsewhere)', () => {
    const el = Avatar({ name: 'Diego Santos' });
    expect(props(el)['aria-hidden']).toBe(true);
  });
});
