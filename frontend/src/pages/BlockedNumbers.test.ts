import { describe, expect, it } from 'vitest';

import { normalizePhoneInput } from './BlockedNumbers';

describe('normalizePhoneInput', () => {
  it('normalizes a 10-digit US number to E.164', () => {
    expect(normalizePhoneInput('(914) 555-1234')).toBe('+19145551234');
  });

  it('normalizes an 11-digit number with leading 1', () => {
    expect(normalizePhoneInput('19145551234')).toBe('+19145551234');
  });

  it('passes through an already-E.164 number', () => {
    expect(normalizePhoneInput('+19145551234')).toBe('+19145551234');
  });

  it('rejects too few digits', () => {
    expect(normalizePhoneInput('12345')).toBeNull();
  });

  it('rejects empty input', () => {
    expect(normalizePhoneInput('')).toBeNull();
  });

  it('rejects an 11-digit number not starting with 1', () => {
    expect(normalizePhoneInput('29145551234')).toBeNull();
  });
});
