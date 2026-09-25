import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import {
  PRECALL_ALLOWED_PLACEHOLDERS,
  MAX_SMS_CHARS,
  extractPlaceholders,
  renderedWorstCaseLength,
  validatePrecallSms,
  validateBulkSms,
  precallSmsAvailability,
  changePrecallSmsMode,
  PRECALL_CATALOG_VERSION,
  PRECALL_PROFILE_CATALOG,
  PRECALL_PROFILE_CATALOG_WITHOUT_CLINIC,
  profileSmsPreview,
} from './precallSms';

const CLINIC = 'VIP Medical Group';
const PAIN =
  "Hi {{FirstName}}! {{ClinicName}} here. We're calling you in just a moment " +
  'to discuss your pain management request. Talk soon!';
const VEIN =
  "Hi {{FirstName}}! This is {{ClinicName}}. We're about to give you a quick " +
  'call regarding your vein consultation request. Look out for our call!';
// The pre-2026-09-10 Vein draft, kept ONLY as the over-ceiling fixture below.
// It is 158 raw / 168 rendered — the exact string that forced OQ-9. Never ship it.
const VEIN_REJECTED_DRAFT =
  "Hi {{FirstName}}! This is {{ClinicName}}. We're about to give you a quick " +
  'call regarding your vein consultation request. Look out for a call from ' +
  'this number!';

describe('placeholder allowlist', () => {
  it('mirrors the backend allowlist exactly', () => {
    // Drift here means the UI accepts copy the API rejects, or vice versa.
    // Backend source of truth: vip_shared.domain.services.sms_template
    // (RECIPIENT_FIELDS | CAMPAIGN_FIELDS).
    expect([...PRECALL_ALLOWED_PLACEHOLDERS].sort()).toEqual([
      'ClinicName',
      'FirstName',
    ]);
  });

  it('rejects a non-allowlisted placeholder by name', () => {
    const errs = validatePrecallSms({
      enabled: true,
      messageTemplate: 'Hi {{FirstName}}, your {{Diagnosis}} is ready.',
      clinicName: CLINIC,
      originationNumberArn: 'arn:x',
    });
    expect(errs.some((e) => e.includes('Diagnosis'))).toBe(true);
  });

  it('rejects {{Specialty}} — not allowlisted, the copy bakes it into the text', () => {
    const errs = validatePrecallSms({
      enabled: true,
      messageTemplate: 'Hi {{FirstName}}, about your {{Specialty}} visit.',
      clinicName: CLINIC,
      originationNumberArn: 'arn:x',
    });
    expect(errs.some((e) => e.includes('Specialty'))).toBe(true);
  });

  it('is case sensitive, like the backend regex', () => {
    expect(extractPlaceholders('Hi {{firstName}}!')).toEqual(
      new Set(['firstName']),
    );
    const errs = validatePrecallSms({
      enabled: true,
      messageTemplate: 'Hi {{firstName}}!',
      clinicName: CLINIC,
      originationNumberArn: 'arn:x',
    });
    expect(errs.some((e) => e.includes('firstName'))).toBe(true);
  });
});

describe('rendered length, not raw length', () => {
  it('measures the 20-char worst-case name, matching max_rendered_length', () => {
    expect(PAIN.length).toBe(125);
    expect(renderedWorstCaseLength(PAIN, CLINIC)).toBe(135);
  });

  it('accepts both approved templates — neither is over the ceiling', () => {
    expect(renderedWorstCaseLength(VEIN, CLINIC)).toBe(153);
    for (const tmpl of [VEIN, PAIN]) {
      const errs = validatePrecallSms({
        enabled: true,
        messageTemplate: tmpl,
        clinicName: CLINIC,
        originationNumberArn: 'arn:x',
      });
      expect(errs).toEqual([]);
    }
  });

  it('catches the template that is under 160 raw but over 160 rendered', () => {
    // This is why maxLength={160} on the raw textarea would be a bug. The
    // rejected Vein draft is the real example: 158 raw, 168 rendered.
    expect(VEIN_REJECTED_DRAFT.length).toBeLessThanOrEqual(160);
    expect(renderedWorstCaseLength(VEIN_REJECTED_DRAFT, CLINIC)).toBe(168);
    const errs = validatePrecallSms({
      enabled: true,
      messageTemplate: VEIN_REJECTED_DRAFT,
      clinicName: CLINIC,
      originationNumberArn: 'arn:x',
    });
    expect(errs.some((e) => e.includes('160'))).toBe(true);
  });
});

describe('required fields', () => {
  it('reports nothing when disabled, however empty', () => {
    expect(validatePrecallSms({ enabled: false })).toEqual([]);
  });

  it.each(['messageTemplate', 'originationNumberArn', 'clinicName'])(
    'requires %s when enabled',
    (field) => {
      const cfg = {
        enabled: true,
        messageTemplate: PAIN,
        clinicName: CLINIC,
        originationNumberArn: 'arn:x',
      } as Record<string, unknown>;
      delete cfg[field];
      expect(validatePrecallSms(cfg).some((e) => e.includes(field))).toBe(true);
    },
  );

  it('accepts the approved Pain copy with every field set', () => {
    expect(
      validatePrecallSms({
        enabled: true,
        messageTemplate: PAIN,
        clinicName: CLINIC,
        originationNumberArn: 'arn:x',
      }),
    ).toEqual([]);
  });

  it('does not require clinicName when the template does not reference {{ClinicName}}', () => {
    // Mirrors the backend's conditional guard in _validate_precall_sms:
    // `"ClinicName" in extract_placeholders(tmpl) and not precall.get("clinicName")`.
    // A template that never uses {{ClinicName}} has nothing to interpolate,
    // so an operator config with no clinicName set must still be accepted —
    // the old unconditional check rejected this even though the server would
    // not have.
    const errs = validatePrecallSms({
      enabled: true,
      messageTemplate: 'Hi {{FirstName}}, quick reminder about your visit.',
      originationNumberArn: 'arn:x',
    });
    expect(errs.some((e) => e.includes('clinicName'))).toBe(false);
  });
});

describe('profile pre-call configuration', () => {
  const profile = {
    enabled: true, mode: 'profile', catalogVersion: 'phase1-v1', originationNumberArn: 'arn:x',
  };

  it('accepts automatic copy without a manual template or clinic', () => {
    expect(validatePrecallSms(profile)).toEqual([]);
  });

  it('allows dependencies for profile mode without changing the DAG', () => {
    expect(precallSmsAvailability({ deliveryType: 'campaign', dependsOn: ['parent'], mode: 'profile' }).available).toBe(true);
  });

  it.each(['phase1-v2', '', null, undefined, 1])('rejects catalog version %s', (catalogVersion) => {
    expect(validatePrecallSms({ ...profile, catalogVersion }).join(' ')).toMatch(/catalogVersion/);
  });

  it.each(['unknown', '', null, true, 1])('rejects enabled mode %s', (mode) => {
    expect(validatePrecallSms({ ...profile, mode }).join(' ')).toMatch(/mode/);
  });

  it.each([1, 'true', {}])('requires boolean true for enabled profile mode, rejecting %j', (enabled) => {
    expect(validatePrecallSms({ ...profile, enabled }).join(' ')).toContain('enabled');
  });

  it.each(['manual override', '   '])('rejects a profile template override %j', (messageTemplate) => {
    expect(validatePrecallSms({ ...profile, messageTemplate }).join(' ')).toContain('messageTemplate');
  });

  it.each([' ', '', null, undefined, 123, {}, false])('rejects invalid profile origination %j', (originationNumberArn) => {
    expect(validatePrecallSms({ ...profile, originationNumberArn }).join(' ')).toContain('originationNumberArn');
  });

  it.each([undefined, '', '   ', '  Clínica St. Mary’s (North) - A/B: 2 & Co.  ', 'A'.repeat(80)])(
    'accepts an optional campaign clinic %j', (clinicName) => {
      expect(validatePrecallSms({ ...profile, clinicName })).toEqual([]);
    },
  );

  it.each([null, 123, false, true, {}, []])('rejects non-string campaign clinic %j', (clinicName) => {
    expect(validatePrecallSms({ ...profile, clinicName }).join(' ')).toContain('clinicName must be a string');
  });

  it.each(['A'.repeat(81), '{{ClinicName}}', 'Clinic\nNorth', 'Clinic <North>', 'Clinic 😀', '123', '---']) (
    'rejects a clinic outside the length or character contract %j', (clinicName) => {
      expect(validatePrecallSms({ ...profile, clinicName }).join(' ')).toContain('clinicName');
    },
  );

  it('measures clinic length after NFC normalization and trimming, in Unicode code points', () => {
    expect(validatePrecallSms({ ...profile, clinicName: `  ${'e\u0301'.repeat(80)}  ` })).toEqual([]);
    expect(validatePrecallSms({ ...profile, clinicName: '𐐀'.repeat(80) })).toEqual([]);
    expect(validatePrecallSms({ ...profile, clinicName: '𐐀'.repeat(81) }).join(' ')).toContain('at most 80');
  });

  it('accepts empty optional profile fields while preserving strict versions', () => {
    expect(validatePrecallSms({ ...profile, messageTemplate: '', clinicName: '' })).toEqual([]);
  });

  it('keeps disabled configurations inert even with unknown fields', () => {
    expect(validatePrecallSms({ enabled: false, mode: 'unknown', catalogVersion: 'invalid' })).toEqual([]);
  });

  it('keeps explicit manual mode equivalent to omitted mode', () => {
    const manual = { enabled: true, messageTemplate: PAIN, clinicName: CLINIC, originationNumberArn: 'arn:x' };
    expect(validatePrecallSms({ ...manual, mode: 'manual' })).toEqual(validatePrecallSms(manual));
  });

  it('preserves the chosen clinic and removes the template when explicitly changing to profile', () => {
    const legacy = { enabled: true, messageTemplate: PAIN, clinicName: CLINIC, originationNumberArn: 'arn:x' };
    expect(changePrecallSmsMode(legacy, 'profile')).toEqual({ ...profile, clinicName: CLINIC });
    expect(legacy).not.toHaveProperty('mode');
    expect(legacy.messageTemplate).toBe(PAIN);
    expect(changePrecallSmsMode(profile as Parameters<typeof changePrecallSmsMode>[0], 'manual'))
      .toEqual({ enabled: true, mode: 'manual', originationNumberArn: 'arn:x', messageTemplate: '', clinicName: '' });
    expect(changePrecallSmsMode(changePrecallSmsMode(legacy, 'profile'), 'manual'))
      .toEqual({ enabled: true, mode: 'manual', originationNumberArn: 'arn:x', messageTemplate: '', clinicName: CLINIC });
  });
});

describe('profile catalog preview contract', () => {
  const pythonCatalog = readFileSync(resolve(process.cwd(), '../services/shared/python/vip_shared/domain/services/precall_sms.py'), 'utf8');

  it('uses the exact approved Python copies and version, including curly apostrophes', () => {
    expect(pythonCatalog).toContain(`CATALOG_VERSION = "${PRECALL_CATALOG_VERSION}"`);
    for (const [pythonName, catalog] of [
      ['CATALOG', PRECALL_PROFILE_CATALOG],
      ['CATALOG_WITHOUT_CLINIC', PRECALL_PROFILE_CATALOG_WITHOUT_CLINIC],
    ] as const) {
      const pythonCopies = pythonCatalog.match(new RegExp(`^${pythonName} = \\{[\\s\\S]*?^\\}`, 'm'))?.[0];
      expect(pythonCopies).toBeDefined();
      for (const copy of Object.values(catalog)) {
        expect(pythonCopies).toContain(JSON.stringify(copy));
        expect(copy).toContain('We’re');
      }
      expect(Object.keys(catalog)).toEqual(['vein', 'pain']);
    }
  });

  it('keeps both catalog variants within the approved rendered multipart bounds', () => {
    const bound = (name: string) => Number(pythonCatalog.match(new RegExp(`^${name} = (\\d+)$`, 'm'))?.[1]);
    const nameSize = bound('FIRST_NAME_MAX_CHARS');
    const clinicSize = bound('MAX_CLINIC_NAME_CHARS');
    expect([nameSize, clinicSize, bound('MAX_SMS_PARTS')]).toEqual([20, 80, 6]);
    for (const copy of Object.values(PRECALL_PROFILE_CATALOG)) {
      const rendered = copy.replace('{{FirstName}}', 'A'.repeat(nameSize)).replace('{{ClinicName}}', 'B'.repeat(clinicSize));
      // Curly apostrophe requires Unicode SMS; concatenated parts hold 67 UCS-2 units.
      expect(Math.ceil(rendered.length / 67)).toBeLessThanOrEqual(bound('MAX_SMS_PARTS'));
      expect(rendered.length).toBeGreaterThan(MAX_SMS_CHARS);
    }
    for (const copy of Object.values(PRECALL_PROFILE_CATALOG_WITHOUT_CLINIC)) {
      const rendered = copy.replace('{{FirstName}}', 'A'.repeat(nameSize));
      expect(Math.ceil(rendered.length / 67)).toBeLessThanOrEqual(bound('MAX_SMS_PARTS'));
      expect(rendered).not.toContain('{{');
    }
  });

  it('previews the chosen clinic and example patient without modifying the catalog', () => {
    for (const variant of ['vein', 'pain'] as const) {
      expect(profileSmsPreview(variant, '  Cli\u0301nica Norte  ')).toContain('Hi Alex!');
      expect(profileSmsPreview(variant, '  Cli\u0301nica Norte  ')).toContain('Clínica Norte');
      expect(profileSmsPreview(variant, '  Cli\u0301nica Norte  ')).not.toContain('  ');
      expect(profileSmsPreview(variant, 'Clínica Norte')).not.toContain('{{');
      expect(PRECALL_PROFILE_CATALOG[variant]).toContain('{{ClinicName}}');
    }
  });

  it.each([undefined, '', '   '])('omits the whole clinic phrase for an unselected clinic %j', (clinic) => {
    expect(profileSmsPreview('vein', clinic)).toBe('Hi Alex! We’re about to give you a quick call regarding your vein consultation request. Look out for a call!');
    expect(profileSmsPreview('pain', clinic)).toBe('Hi Alex! We’re calling you in just a moment to discuss your pain management request. Talk soon!');
  });
});

describe('availability — the UI must not offer a config the API rejects', () => {
  it('is unavailable on an SMS-delivery campaign', () => {
    const a = precallSmsAvailability({ deliveryType: 'sms', dependsOn: [] });
    expect(a.available).toBe(false);
    expect(a.reason).toMatch(/sms/i);
  });

  it('is unavailable when the campaign has dependsOn', () => {
    // Mirrors Task 5's server-side rejection: a campaign with dependsOn is
    // never pre-warmed (executor.py:2180, 2571-2573), so it has no segmentArn
    // at activation and the SMS would silently never fire.
    const a = precallSmsAvailability({
      deliveryType: 'campaign',
      dependsOn: ['other'],
    });
    expect(a.available).toBe(false);
    expect(a.reason).toMatch(/depend/i);
  });

  it('is available on a plain voice campaign with no dependencies', () => {
    expect(
      precallSmsAvailability({ deliveryType: 'campaign', dependsOn: [] })
        .available,
    ).toBe(true);
  });

  it('is available on a journey campaign too', () => {
    expect(
      precallSmsAvailability({ deliveryType: 'journey', dependsOn: [] })
        .available,
    ).toBe(true);
  });

  it('is unavailable on a hypothetical future non-voice deliveryType', () => {
    // The forward-looking gap this allowlist closes: a denylist of just
    // 'sms' would silently pass any new deliveryType that isn't 'sms',
    // deferring the rejection to the server. The allowlist rejects anything
    // not explicitly known-voice, matching _VOICE_DELIVERY_TYPES's semantics.
    const a = precallSmsAvailability({ deliveryType: 'email', dependsOn: [] });
    expect(a.available).toBe(false);
    expect(a.reason).toMatch(/email/i);
  });
});

describe('validateBulkSms — bulk-SMS panel parity with pre-call SMS', () => {
  // These mirror _validate_sms_campaign in handlers/plans.py, which shares
  // _screen_sms_template_content with _validate_precall_sms above — so the
  // same fixtures and expectations apply, just against the bulk-SMS field
  // names (smsMessageTemplate/clinicName, not messageTemplate/precall's shape).

  it('rejects a non-allowlisted placeholder', () => {
    const errs = validateBulkSms({
      smsMessageTemplate: 'Hi {{FirstName}}, your {{Diagnosis}} is ready.',
      clinicName: CLINIC,
      smsOriginationNumberArn: 'arn:x',
    });
    expect(errs.some((e) => e.includes('Diagnosis'))).toBe(true);
  });

  it('requires clinicName when the template uses {{ClinicName}}, and accepts it once set', () => {
    const withoutClinic = validateBulkSms({
      smsMessageTemplate: PAIN,
      smsOriginationNumberArn: 'arn:x',
    });
    expect(withoutClinic.some((e) => e.includes('clinicName'))).toBe(true);

    const withClinic = validateBulkSms({
      smsMessageTemplate: PAIN,
      clinicName: CLINIC,
      smsOriginationNumberArn: 'arn:x',
    });
    expect(withClinic.some((e) => e.includes('clinicName'))).toBe(false);
  });

  it('does not require clinicName when the template only uses {{FirstName}}', () => {
    // Conditional, not unconditional — mirrors the identical fix already
    // applied to validatePrecallSms for the same reason: a template that
    // never references {{ClinicName}} has nothing to interpolate.
    const errs = validateBulkSms({
      smsMessageTemplate: 'Hi {{FirstName}}, quick reminder about your visit.',
      smsOriginationNumberArn: 'arn:x',
    });
    expect(errs.some((e) => e.includes('clinicName'))).toBe(false);
  });

  it('rejects a template whose rendered worst-case length exceeds MAX_SMS_CHARS', () => {
    // 158 raw / 168 rendered — under the ceiling raw, over it rendered.
    const errs = validateBulkSms({
      smsMessageTemplate: VEIN_REJECTED_DRAFT,
      clinicName: CLINIC,
      smsOriginationNumberArn: 'arn:x',
    });
    expect(errs.some((e) => e.includes('160'))).toBe(true);
  });

  it('accepts a valid template — allowlisted placeholders, under the ceiling, clinicName set', () => {
    expect(
      validateBulkSms({
        smsMessageTemplate: PAIN,
        clinicName: CLINIC,
        smsOriginationNumberArn: 'arn:x',
      }),
    ).toEqual([]);
  });
});

describe.each(['precall', 'bulk'] as const)('%s placeholder syntax', (channel) => {
  const validate = (template: string, clinicName = 'VIP') => channel === 'precall'
    ? validatePrecallSms({ enabled: true, messageTemplate: template, clinicName, originationNumberArn: 'arn:x' })
    : validateBulkSms({ smsMessageTemplate: template, clinicName });

  it.each([
    '{{First Name}}', '{{First-Name}}', '{{}}', '{{   }}',
    '{{FirstName', 'FirstName}}', '{{FirstName}', '{FirstName}}',
    '{{{FirstName}}}', '{{FirstName}}}', '{{{FirstName}}',
    '{{outer {{FirstName}} }}', '{{FirstName}}{{}}', '{{Unknown}}',
  ])('rejects unresolved expression %s', (template) => {
    expect(validate(template).some((e) => e.includes('placeholder'))).toBe(true);
  });

  it.each(['{{FirstName}}', '{{Unknown}}', '{{First Name}}', '{{', '}}'])(
    'rejects unresolved expression inserted by clinic %s', (clinic) => {
      expect(validate('Hi {{ClinicName}}', clinic).some((e) => e.includes('placeholder'))).toBe(true);
    },
  );

  it('preserves supported whitespace, adjacent tokens and literal single braces', () => {
    expect(validate('{ Hi {{ FirstName }}{{ ClinicName }} }')).toEqual([]);
  });
});
