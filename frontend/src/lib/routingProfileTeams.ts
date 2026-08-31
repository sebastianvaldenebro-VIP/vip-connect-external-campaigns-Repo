/** Static mapping of team keys → routing profile display names.
 *
 * Matched at runtime against routingProfileName values from the Connect API.
 * Update this list when routing profiles are added or renamed in Connect.
 */
export const TEAM_ROUTING_PROFILES: Record<string, string[]> = {
  'front-desk': [
    'Front Desk NYC',
    'Front Desk NYC 2',
    'Front Desk NYC 3',
    'Front Desk NYC 4',
    'Front Desk NYC 5',
    'Front Desk NJ (name redacted)',
    'Front Desk NJ 2',
    'Front Desk NJ 3',
    'Front Desk NJ 4',
    'Front Desk NJ 5',
    'Front Desk South CA',
    'Front Desk South CA 2',
    'Front Desk South CA 3',
    'Front Desk MD',
    'Front Desk LI',
    'Front Desk LI 2',
    'Front Desk LI 3',
    'Front Desk North CA',
    'Front Desk CT',
    'Front Desk TX',
  ],
  'appointment-services': [
    'Appointment Services Agent',
    'Appointment Services Management',
    'Appointment Services Specialist',
  ],
  'rcm': [
    'RCM - Bill Scrub',
    'RCM - Collections',
    'RCM - Credentialing',
    'RCM - Patient Accounting',
    'RCM - Repricing (negotiations)',
    'RCM - Repricing (repricing)',
    'RCM - (name redacted)',
  ],
  // patient-success (displayed as "Patient Access") is PC-* only — PS-*
  // (Patient Success coordinators/management) are a different team and must
  // not be included here, even though the names look related.
  'patient-success': [
    'PC - Existing Patient',
    'PC - Existing Patient Spanish',
    'PC - Hybrid All',
    'PC - Hybrid PV',
    'PC - Hybrid Spanish Speakers',
    'PC - New Leads',
    'PC - New Leads OB',
    'PC - New Leads OB + Spanish',
    'PC - New Leads Spanish',
    'PC - New Leads Temporary Profile',
    'PC - TL',
  ],
  'insurance-verification': [
    'IV - Agent',
    'IV - Agent + Escalation',
  ],
  'referrals-medical': [
    'HMO Referral Agent',
    'Physician Referral Agent',
    'Referral Agent - All',
    'Physician Outreach',
  ],
  'specialty': [
    'Pain Outbound Routing Profile',
    'Vein Outbound Profile',
    'Vein and Pain Treatment Center',
  ],
  'other': [
    'VIP Medical Group',
    'VIP Test',
    'Basic Routing Profile',
    'SMS-Emergency-line',
    'Test MJ',
    'Medical Records',
    'Medical Staff',
    'Conservative Calls',
  ],
};

/** Prefix-matched teams — for naming conventions stable enough that any current
 * or future routing profile matching the prefix belongs to the team, without
 * needing this file edited every time Connect gets a new one added/renamed.
 * Checked after TEAM_ROUTING_PROFILES' exact matches in teamForProfile().
 */
export const TEAM_ROUTING_PROFILE_PREFIXES: Record<string, string[]> = {
  'patient-success': ['PC - '],
};

export const TEAM_LABELS: Record<string, string> = {
  'front-desk':            'Front Desk',
  'appointment-services':  'Appointment Svcs',
  'rcm':                   'RCM',
  'patient-success':       'Patient Access',
  'insurance-verification':'Insurance Verification',
  'referrals-medical':     'Referrals',
  'specialty':             'Specialty',
  'other':                 'Other',
};

/** Teams shown in the Branded Monitor agents view (the two teams relevant to branded dialer). */
export const BRANDED_MONITOR_TEAMS = ['patient-success', 'appointment-services'] as const;

/** Returns the team key for a given routing profile name, or null if unclassified. */
export function teamForProfile(profileName: string): string | null {
  for (const [team, names] of Object.entries(TEAM_ROUTING_PROFILES)) {
    if (names.includes(profileName)) return team;
  }
  for (const [team, prefixes] of Object.entries(TEAM_ROUTING_PROFILE_PREFIXES)) {
    if (prefixes.some((prefix) => profileName.startsWith(prefix))) return team;
  }
  return null;
}
