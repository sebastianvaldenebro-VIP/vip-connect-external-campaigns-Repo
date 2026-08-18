/**
 * Canonical outbound numbers per state/region for Vein specialty campaigns.
 * Keys match the region codes used in segment location filters (stateLocationMap.ts).
 * Values are E.164 format.
 */
export const STATE_DEFAULT_PHONES: Record<string, string> = {
  NY:         '+19174105649',
  LI:         '+16314497507',  // Long Island
  NJ:         '+19734949660',
  MD:         '+13018594566',
  CT:         '+14753656590',
  TX:         '+15126508970',
  SCA:        '+18588686651',  // South CA
  NCA:        '+16694674988',  // North CA
  PA:         '+12154009167',  // PA - Vein Leads (Philadelphia, area code 215)
};

/** Normalize any phone format to digits-only for comparison (strips +, spaces, dashes). */
function digitsOnly(n: string): string {
  return n.replace(/\D/g, '');
}

/**
 * Pick the outbound phone for a campaign.
 *
 * Priority:
 *   1. Direct assignment from STATE_DEFAULT_PHONES for the first matching state.
 *      The number must be present in Connect's provisioned phone list.
 *   2. Area-code fallback: first phone whose area code belongs to any of the states.
 *   3. First available phone.
 */
export function pickPhoneForStates<T extends { number: string }>(
  phones: readonly T[],
  states: readonly string[],
): T | null {
  if (phones.length === 0) return null;

  // 1. Direct match against canonical defaults
  for (const state of states) {
    const defaultNum = STATE_DEFAULT_PHONES[state];
    if (!defaultNum) continue;
    const defaultDigits = digitsOnly(defaultNum);
    const found = phones.find((p) => digitsOnly(p.number) === defaultDigits);
    if (found) return found;
  }

  // 2. Area-code fallback (keeps working when new states are added before
  //    the canonical map is updated, or for non-Vein specialties in future)
  const STATE_AREA_CODES: Record<string, string[]> = {
    NY:  ['212', '646', '332', '718', '347', '929', '516', '631', '914', '845', '585', '716', '315', '680', '607', '518', '838'],
    NCA: ['415', '628', '510', '341', '925', '669', '408', '650', '209', '559', '916', '279', '530', '707', '369'],
    SCA: ['213', '323', '747', '818', '310', '424', '562', '626', '661', '714', '657', '949', '805', '619', '858', '760', '442', '951', '909'],
    NJ:  ['201', '551', '732', '848', '609', '640', '856', '862', '973', '908'],
    TX:  ['214', '469', '972', '945', '832', '713', '281', '346', '210', '726', '512', '737', '254', '325', '361', '409', '430', '432', '806', '830', '903', '915', '936', '940', '956', '979'],
    CT:  ['203', '475', '860', '959'],
    MD:  ['240', '301', '410', '443', '667'],
    LI:  ['516', '631'],
    PA:  ['215'],
  };
  const targetCodes = new Set<string>();
  for (const state of states) {
    for (const code of STATE_AREA_CODES[state] ?? []) targetCodes.add(code);
  }
  if (targetCodes.size > 0) {
    const byAreaCode = phones.find((p) => {
      const m = p.number.match(/^\+1(\d{3})/);
      return m != null && targetCodes.has(m[1]);
    });
    if (byAreaCode) return byAreaCode;
  }

  // 3. Last resort
  return phones[0];
}
