import { describe, expect, it } from 'vitest';

import { pickPhoneForStates, STATE_DEFAULT_PHONES } from './areaCodeMap';

describe('STATE_DEFAULT_PHONES', () => {
  it('has the confirmed Pennsylvania canonical number', () => {
    expect(STATE_DEFAULT_PHONES.PA).toBe('+12154009167');
  });
});

describe('pickPhoneForStates for Pennsylvania', () => {
  it('picks the canonical PA number when it is in the provisioned phone list', () => {
    const phones = [
      { number: '+19734949660' }, // NJ
      { number: '+12154009167' }, // PA - Vein Leads
      { number: '+15126508970' }, // TX
    ];
    const picked = pickPhoneForStates(phones, ['PA']);
    expect(picked?.number).toBe('+12154009167');
  });

  it('falls back to the 215 area code when the exact canonical number is absent', () => {
    const phones = [
      { number: '+19734949660' }, // NJ
      { number: '+12154009168' }, // some other 215 number, not the canonical one
    ];
    const picked = pickPhoneForStates(phones, ['PA']);
    expect(picked?.number).toBe('+12154009168');
  });

  it('falls back to the first available phone when no canonical or area-code match exists', () => {
    const phones = [
      { number: '+19999999999' }, // no matching area code for any known state
      { number: '+18888888888' },
    ];
    const picked = pickPhoneForStates(phones, ['PA']);
    expect(picked?.number).toBe('+19999999999');
  });
});

describe('pickPhoneForStates — general edge cases', () => {
  it('returns null when the phone list is empty', () => {
    expect(pickPhoneForStates([], ['NY'])).toBeNull();
  });

  it('falls back to the first phone when states is empty', () => {
    const phones = [{ number: '+15551234567' }, { number: '+15559876543' }];
    expect(pickPhoneForStates(phones, [])?.number).toBe('+15551234567');
  });

  it('falls back to the first phone for an unrecognized state code', () => {
    const phones = [{ number: '+15551234567' }, { number: '+15559876543' }];
    expect(pickPhoneForStates(phones, ['ZZ'])?.number).toBe('+15551234567');
  });
});
