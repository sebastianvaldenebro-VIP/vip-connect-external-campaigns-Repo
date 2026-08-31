import { describe, expect, it } from 'vitest';

import { initialsFromName } from './Avatar';

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
